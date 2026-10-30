"""Read the Quad Cortex grid as JSON and apply JSON patches to it.

Talks to the unit through the unofficial ``pyquadcortex`` library, over the
same USB link Cortex Control uses - so Cortex Control must be closed while
this runs.

Nothing here saves. Edits live on the grid until :func:`save` is called
explicitly, so a bad patch is undone by recalling the preset on the unit.

Patch format (``ops`` run in order)::

    {"note": "why this change",
     "ops": [
       {"op": "set_param",  "row": 0, "col": 3, "param": "GAIN", "real": 6.5},
       {"op": "set_param",  "row": 0, "col": 3, "param": "BASS", "value": 0.4},
       {"op": "set_option", "row": 0, "col": 5, "param": "MIC", "option": "Dynamic 57"},
       {"op": "set_block",  "row": 0, "col": 2, "model": "Chief DS1"},
       {"op": "remove_block", "row": 0, "col": 6},
       {"op": "bypass",     "row": 0, "col": 1, "bypassed": true},
       {"op": "set_input",  "row": 0, "port": "USB_5"},
       {"op": "set_output", "row": 0, "port": "XLR_1_2"}
     ]}

Rows and columns are zero-based (the unit shows rows 1-4). ``real`` is in the
parameter's own units (dB, Hz, ms, %); ``value`` is the normalized 0..1.
"""

from __future__ import annotations

import math
from contextlib import contextmanager

ROWS, COLUMNS = 4, 8
OPS = {"set_param", "set_option", "set_block", "remove_block", "bypass",
       "set_input", "set_output"}


class PatchError(ValueError):
    pass


def _qc():
    try:
        import pyquadcortex
    except ImportError as exc:  # pragma: no cover - depends on the host
        raise SystemExit("pyquadcortex is missing: pip install -e '.[device]'") from exc
    return pyquadcortex


@contextmanager
def connect():
    """Open the unit under qcsuite's device lock (one tool at a time).

    Fails while Cortex Control holds the USB link, and while another qctone or
    qcsuite process is using the unit.
    """
    from qcsuite import paths
    from qcsuite.device.lock import device_lock
    from qcsuite.errors import DeviceBusy
    try:
        with device_lock(paths.lock_file(False), what="qctone"):
            with _connect() as qc:
                yield qc
    except DeviceBusy as exc:
        raise SystemExit(str(exc)) from None


@contextmanager
def _connect():
    pq = _qc()
    try:
        qc = pq.connect()
    except pq.DeviceNotFoundError as exc:
        raise SystemExit(f"Quad Cortex not reachable over USB: {exc}") from exc
    except TimeoutError as exc:
        raise SystemExit(
            f"the Quad Cortex is connected but did not answer: {exc}. If it just "
            "booted, wait 30 s and retry; otherwise power-cycle it."
        ) from exc
    try:
        yield qc
    finally:
        qc.close()


def _clean(v):
    if isinstance(v, float):
        return None if math.isnan(v) else round(v, 4)
    return v


def snapshot(qc) -> dict:
    """The live grid as JSON: every block, its model, its knobs in real units."""
    pq = _qc()
    p = qc.read_current_preset()
    scene = int(qc.active_scene())
    rows = []
    for i, chain in enumerate(p.chains):
        rows.append({
            "row": i,
            "in_port": _port_name(pq.Input, chain.in_portid),
            "out_port": _port_name(pq.Output, chain.out_portid),
        })
    out_blocks = []
    for b in pq.blocks(p):
        model = qc.catalog.get(b.model_id)
        entry = {
            "row": b.row,
            "col": b.column,
            "model_id": b.model_id,
            "model": model.name if model else f"unknown {b.model_id}",
            "category": model.category if model else None,
            "bypassed": _bypassed(pq, p, b, scene),
            "params": [],
        }
        if model:
            for spec in model.parameters:
                try:
                    state = pq.param_state(p, b.row, b.column, spec.index)
                except IndexError:
                    continue
                raw = state.values[0] if state.values else None
                param = {"name": spec.name, "index": spec.index}
                if isinstance(raw, str):
                    param["text"] = raw
                elif raw is not None and not math.isnan(raw):
                    param["value"] = round(raw, 4)
                    options = pq.param_options(p, b.row, b.column, spec.index)
                    if options:
                        param["option"] = options[min(len(options) - 1,
                                                      round(raw * (len(options) - 1)))]
                        param["options"] = options
                    elif not spec.range_is_placeholder:
                        param["real"] = _clean(spec.to_real(raw))
                        param["units"] = spec.units
                        param["range"] = [spec.minimum, spec.maximum]
                param["scene_mode"] = state.scene_mode
                entry["params"].append(param)
        out_blocks.append(entry)
    return {"active_scene": "ABCDEFGH"[scene], "rows": rows, "blocks": out_blocks}


def _bypassed(pq, p, b, scene):
    try:
        state = pq.bypass_state(p, b.row, b.column)
    except IndexError:
        return None
    return state.scenes[scene] if scene < len(state.scenes) else None


def _port_name(enum, value):
    try:
        return enum(value).name
    except ValueError:
        return str(value)


def _block_at(grid: dict, row: int, col: int):
    return next((b for b in grid["blocks"] if b["row"] == row and b["col"] == col), None)


def resolve_model(catalog, model):
    """A model given by id or by name, looked up in the device catalog."""
    if isinstance(model, int) or (isinstance(model, str) and model.isdigit()):
        found = catalog.get(int(model))
    else:
        try:
            found = catalog.find(model)
        except KeyError:
            found = None
    if found is None:
        raise PatchError(f"no model {model!r} on this unit; run `qctone catalog --search`")
    return found


def validate(patch: dict, grid: dict, catalog) -> list:
    """Check a patch against the live grid before anything is sent.

    Tracks blocks placed or removed earlier in the same patch, so a
    ``set_block`` followed by ``set_param`` on the new block validates.
    Returns the ops with model references resolved to ids.
    """
    if not isinstance(patch.get("ops"), list) or not patch["ops"]:
        raise PatchError("patch needs a non-empty 'ops' list")
    pq = _qc()
    cells = {(b["row"], b["col"]): b["model_id"] for b in grid["blocks"]}
    resolved = []
    for n, op in enumerate(patch["ops"], 1):
        kind = op.get("op")
        where = f"op {n} ({kind})"
        if kind not in OPS:
            raise PatchError(f"{where}: unknown op; use one of {sorted(OPS)}")
        row = op.get("row")
        if not isinstance(row, int) or not 0 <= row < ROWS:
            raise PatchError(f"{where}: row must be 0-{ROWS - 1}")
        op = dict(op)
        if kind in ("set_input", "set_output"):
            enum = pq.Input if kind == "set_input" else pq.Output
            try:
                op["port_id"] = int(enum[op["port"]])
            except KeyError:
                raise PatchError(f"{where}: port must be one of {[e.name for e in enum]}") from None
            resolved.append(op)
            continue
        col = op.get("col")
        if not isinstance(col, int) or not 0 <= col < COLUMNS:
            raise PatchError(f"{where}: col must be 0-{COLUMNS - 1}")
        if kind == "set_block":
            op["model_id"] = resolve_model(catalog, op["model"]).id
            cells[(row, col)] = op["model_id"]
        elif kind == "remove_block":
            if (row, col) not in cells:
                raise PatchError(f"{where}: no block at row {row} col {col}")
            del cells[(row, col)]
        else:
            if (row, col) not in cells:
                raise PatchError(f"{where}: no block at row {row} col {col}")
            model = catalog.get(cells[(row, col)])
            if kind in ("set_param", "set_option"):
                if model is None:
                    raise PatchError(f"{where}: block model {cells[(row, col)]} not in catalog")
                try:
                    spec = model.parameter(op["param"])
                except KeyError as exc:
                    raise PatchError(f"{where}: {exc.args[0]}") from None
                op["model_id"] = model.id
                if kind == "set_param":
                    if ("real" in op) == ("value" in op):
                        raise PatchError(f"{where}: give exactly one of 'real' or 'value'")
                    if "real" in op:
                        if spec.range_is_placeholder:
                            raise PatchError(f"{where}: {spec.name} has no real-unit range; use 'value' (0..1)")
                        if not spec.minimum <= op["real"] <= spec.maximum:
                            raise PatchError(
                                f"{where}: {spec.name}={op['real']} outside "
                                f"{spec.minimum}..{spec.maximum} {spec.units}")
                    elif not 0.0 <= op["value"] <= 1.0:
                        raise PatchError(f"{where}: 'value' must be 0..1")
                elif "option" not in op:
                    raise PatchError(f"{where}: set_option needs 'option' (the option's name)")
            elif kind == "bypass" and not isinstance(op.get("bypassed"), bool):
                raise PatchError(f"{where}: 'bypassed' must be true or false")
        resolved.append(op)
    return resolved


def apply(qc, ops: list):
    """Send validated ops to the unit, in order."""
    for op in ops:
        kind, row = op["op"], op["row"]
        if kind == "set_param":
            model = qc.catalog[op["model_id"]]
            if "real" in op:
                qc.set_param(row, op["col"], param=op["param"], model=model, real=op["real"])
            else:
                qc.set_param(row, op["col"], param=op["param"], model=model, value=op["value"])
        elif kind == "set_option":
            qc.set_param_option(row, op["col"], op["param"], op["option"],
                                source=qc.read_current_preset())
        elif kind == "set_block":
            qc.set_block(row, op["col"], op["model_id"])
        elif kind == "remove_block":
            qc.remove_block(row, op["col"])
        elif kind == "bypass":
            qc.set_bypass(row, op["col"], op["bypassed"])
        elif kind == "set_input":
            qc.set_chain_input(row, op["port_id"])
        elif kind == "set_output":
            qc.set_chain_output(row, op["port_id"])


def restore_ops(saved: dict, current: dict) -> dict:
    """A patch that turns the ``current`` grid back into ``saved``.

    Re-places blocks whose model changed, removes blocks added since, and
    rewrites every numeric parameter and bypass state. Routing is restored
    too. String parameters (capture files, IRs) are not touched.
    """
    ops = []
    for row in saved["rows"]:
        now = next(r for r in current["rows"] if r["row"] == row["row"])
        if now["in_port"] != row["in_port"]:
            ops.append({"op": "set_input", "row": row["row"], "port": row["in_port"]})
        if now["out_port"] != row["out_port"]:
            ops.append({"op": "set_output", "row": row["row"], "port": row["out_port"]})
    for b in current["blocks"]:
        if _block_at(saved, b["row"], b["col"]) is None:
            ops.append({"op": "remove_block", "row": b["row"], "col": b["col"]})
    for b in saved["blocks"]:
        now = _block_at(current, b["row"], b["col"])
        if now is None or now["model_id"] != b["model_id"]:
            ops.append({"op": "set_block", "row": b["row"], "col": b["col"],
                        "model": b["model_id"]})
        for prm in b["params"]:
            if "value" in prm:
                ops.append({"op": "set_param", "row": b["row"], "col": b["col"],
                            "param": prm["name"], "value": prm["value"]})
        if b["bypassed"] is not None:
            ops.append({"op": "bypass", "row": b["row"], "col": b["col"],
                        "bypassed": b["bypassed"]})
    return {"note": "restore", "ops": ops}


def save(qc, slot: str, name: str, folder: str | None = None) -> str:
    """Save the live grid into an EMPTY slot of the sandbox (or an allowed folder).

    qcsuite's safety rules apply here too: the player's own folders (My
    Presets, Favorite, kami, Cory Wong) are never written, an occupied slot is
    never overwritten from this command, and the stored name is read back.
    Overwrites go through ``qcsuite`` plans, which back up first.
    """
    from qcsuite.device import guard
    from qcsuite.device.core import Device
    pq = _qc()
    device = Device(qc, mock=False, policy=guard.WritePolicy(
        allowed={folder} if folder and not guard.is_sandbox(guard.folder_key(folder)) else set()))
    key = device.ensure_sandbox() if not folder else guard.folder_key(folder)
    return device.save_current(key, pq.slot_to_position(slot), name)


def catalog_listing(catalog, search: str | None = None, category: str | None = None):
    out = []
    for m in catalog:
        if m.hidden or m.internal or m.category_hidden or m.superseded:
            continue
        if category and category.lower() not in m.category.lower():
            continue
        if search and search.lower() not in f"{m.name} {m.based_on}".lower():
            continue
        out.append({
            "id": m.id, "name": m.name, "category": m.category,
            "based_on": m.based_on,
            "params": [
                {"name": p.name, "range": [p.minimum, p.maximum], "units": p.units,
                 **({"options": p.option_count} if p.option_count else {})}
                for p in m.parameters
            ],
        })
    return out
