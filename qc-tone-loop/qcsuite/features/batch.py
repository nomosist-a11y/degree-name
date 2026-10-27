"""Bulk reamping into stems (feature 9), lined up to the DI (idea I), and the
sound map (idea K).

Every DI x preset combination is rendered the same way - recall the preset,
point its guitar rows at USB 5, play the DI, record Out 1/2 - and written as a
stem for Logic Pro:

    ~/.qctone/batch/<job>/<DI>/00_<DI>__DI.wav           the DI itself, dual mono
    ~/.qctone/batch/<job>/<DI>/01_<DI>__<preset>.wav      one per preset, in order
    ~/.qctone/batch/<job>/stems.csv, README.md            order, latency, loudness, exclusions

48 kHz, 24-bit, stereo, every file starting at the DI's sample 0 and the same
length (DI + 2 s tail), so a folder dropped into Logic at bar 1 lines up.

Alignment (idea I): the ~312 ms round trip of a reamp (measured on the
player's unit) is measured per stem from the FIRST ARRIVAL - the first sample
of the recording within 35 dB of its peak, against the same point of what was
played - because the DI starts after silence and the direct sound always
arrives before any echo. The shared envelope cross-correlation in
:func:`qcsuite.audio.render.measure_latency` is also computed, for comparison
only: on the fake it drifted by up to ~80 ms for high-gain presets with a
delay block (the echoes and the flattened envelope pull its peak), which would
cut the attack off a stem. When the two disagree by more than a few ms the CSV
says so, so the stem can be checked by ear. ``QCSUITE_LATENCY_MS`` (a measured
calibration) overrides both. The latency of every stem goes into the CSV, and a
spread of more than a few ms within one DI is reported.

A render that comes back as the DI (passthrough), or a preset holding a block
from an unowned plugin, is still written - never dropped silently - but its
file name ends in ``__EXCLUDED`` and the CSV says why; such renders stay out of
the sound map.

The sound map compares renders of the SAME DI by measured features: the
third-octave shape between 100 Hz and 5 kHz, 1-5 kHz flatness and crest factor,
combined as in :mod:`qcsuite.audio.score`. It answers "which presets measure
alike", not "which sound good" - that stays with the player's ears.

Playing takes (recording what the player plays, dry and wet together) are not
here: they belong to the edit watcher, which ties them to the preset in use.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import numpy as np
import pyquadcortex as pq

from qcsuite import jobs, paths, presets
from qcsuite.audio import di as di_mod
from qcsuite.audio import export, reamp, render, score
from qcsuite.device import guard
from qcsuite.errors import QcsuiteError
from qctone import features

LATENCY_SPREAD_WARN_MS = 3.0
EXCLUDED_SUFFIX = "__EXCLUDED"
FORMATS = ("wav", "flac")


def parse_preset_ref(ref: str) -> tuple[str, int]:
    """``kami:1E``, ``kami:4`` or ``/media/p4/Presets/kami:1E`` -> (key, position)."""
    if ":" not in ref:
        raise QcsuiteError(f"プリセットは フォルダ:スロット で指定します(例 kami:1E): {ref}")
    folder, slot = ref.rsplit(":", 1)
    key = guard.folder_key(folder)
    slot = slot.strip()
    position = int(slot) if slot.isdigit() else pq.slot_to_position(slot)
    return key, position


def build_items(device, dis: list[str], presets: list[str] | None = None,
                folder: str | None = None) -> list[dict]:
    """The work list, preset-major so consecutive renders share one recall."""
    if not dis:
        raise QcsuiteError("DI を 1 つ以上指定してください")
    chosen: list[tuple[str, int]] = []
    if folder:
        key = guard.folder_key(folder)
        chosen += [(key, p["index"]) for p in device.presets(key)]
    for ref in presets or []:
        chosen.append(parse_preset_ref(ref))
    if not chosen:
        raise QcsuiteError("プリセットを指定してください(--preset か --folder)")
    names: dict[str, dict[int, str]] = {}
    items = []
    for n, (key, position) in enumerate(dict.fromkeys(chosen), 1):
        if key not in names:
            names[key] = {p["index"]: p["name"] for p in device.presets(key)}
        name = names[key].get(position)
        if name is None:
            raise QcsuiteError(f"「{guard.folder_name(key)}」{pq.position_to_slot(position)} は空です")
        for di_name in dis:
            items.append({"key": f"{di_name}|{key}|{position}", "di": di_name, "folder": key,
                          "position": position, "slot": pq.position_to_slot(position),
                          "name": name, "n": n})
    return items


def create(rig, dis: list[str], presets: list[str] | None = None, folder: str | None = None,
           fmt: str = "wav") -> jobs.Job:
    if fmt not in FORMATS:
        raise QcsuiteError(f"形式は {', '.join(FORMATS)} のどちらかです")
    for name in dis:
        di_mod.load(name, rig.mock)          # fail now, not halfway through the night
    items = build_items(rig.device, dis, presets, folder)
    presets_n = len({(i["folder"], i["position"]) for i in items})
    return jobs.create("batch", {"dis": dis, "format": fmt, "presets": presets_n}, items,
                       rig.mock, title=f"一括リアンプ {len(dis)} DI × {presets_n} プリセット")


#: Within this much of a signal's peak counts as its first arrival.
ONSET_THRESHOLD_DB = -35.0


def first_arrival_ms(played, recorded, sr: int) -> float | None:
    """Round trip from the first sample of each signal within 35 dB of its peak."""
    from qctone.audio_io import onset
    rec = recorded.mean(axis=1) if recorded.ndim == 2 else recorded
    a = onset(np.asarray(played), ONSET_THRESHOLD_DB)
    b = onset(np.asarray(rec), ONSET_THRESHOLD_DB)
    if a is None or b is None or b < a:
        return None
    return (b - a) / sr * 1000


def reamp_aligned(rig, di, sr: int) -> tuple[render.RenderResult, dict]:
    """Reamp the live grid and align by first arrival (see module docstring).

    Same recording path as :func:`qcsuite.audio.render.render` - pre-roll,
    margin, tail, silence check - with only the alignment rule changed.
    """
    mono = np.asarray(di, dtype=np.float32)
    padded = np.concatenate([mono, np.zeros(int(render.LATENCY_MARGIN_S * sr), dtype=np.float32)])
    if not rig.mock and not rig.warned_monitor:
        rig.device.notice("sound_change", render.MONITOR_WARNING)
        rig.warned_monitor = True
    with rig.device._lock:
        rec = rig.renderer.playrec(padded, sr)
    if rec.ndim == 1:
        rec = np.stack([rec, rec], axis=1)
    if rec.shape[1] == 1:
        rec = np.repeat(rec, 2, axis=1)
    peak = float(np.max(np.abs(rec))) if rec.size else 0.0
    if peak < 1e-4:
        raise RuntimeError(
            "リアンプの戻りが無音でした。グリッドの行の入力が USB 5 になっているか、"
            "出力が Out 1/2 に届いているかを確認してください(qctone doctor --reamp-test)。")
    pre = int(render.PREROLL_S * sr)
    played = np.concatenate([np.zeros(pre), padded, np.zeros(int(render.TAIL_S * sr))])
    fixed = render.fixed_latency_ms()
    first = first_arrival_ms(played, rec, sr)
    corr = render.measure_latency(played, rec, sr)
    if fixed is not None:
        latency, method = fixed, "QCSUITE_LATENCY_MS"
    elif first is not None:
        latency, method = first, "first_arrival"
    else:
        latency = corr if corr is not None else render.MEASURED_LATENCY_MS
        method = "correlation" if corr is not None else "default"
    start = pre + int(round(latency / 1000 * sr))
    aligned = rec[start: start + len(mono) + int(render.TAIL_S * sr)].astype(np.float32)
    result = render.RenderResult(audio=aligned, sr=sr, latency_ms=latency,
                                 peak_dbfs=float(20 * np.log10(peak)), clipped=peak >= 0.999,
                                 mock=rig.renderer.mock)
    diag = {"method": method, "first_arrival_ms": None if first is None else round(first, 2),
            "correlation_ms": None if corr is None else round(float(corr), 2)}
    if first is not None and corr is not None and abs(first - corr) > LATENCY_SPREAD_WARN_MS:
        diag["disagreement"] = (f"相互相関では {corr:.1f} ms(先頭の到着からは {first:.1f} ms)。"
                                "先頭の到着でそろえました。DI と並べて聴いて確認してください。")
    return result, diag


def _render_stem(rig, item: dict, di, sr: int, di_feat: dict, cache: dict) -> dict:
    """Recall (unless the previous stem used the same preset), route, reamp, analyze."""
    device = rig.device
    here = (item["folder"], item["position"])
    if cache.get("last") is None or cache["last"][0] != here:
        stored = device.read_stored(item["folder"], item["position"])
        if pq.field_present(stored, "name") and stored.name != item["name"]:
            # The slot no longer holds what the listing said when the job was made;
            # rendering on would label one preset's sound with another's name.
            cache["last"] = None
            raise RuntimeError(f"{item['slot']} を読み込むと「{stored.name}」でした(一覧では"
                               f"「{item['name']}」)。プリセットが動いた可能性があるので飛ばしました。")
        normalized = presets.normalize(stored, device.catalog, folder=item["folder"],
                                       position=item["position"])
        if not device.prepare_reamp(stored):
            raise RuntimeError(f"「{normalized['name']}」には Input 1/2 から始まる行がないため、"
                               "USB 5 からリアンプできません。")
        cache["last"] = (here, normalized, reamp.static_issues(normalized))
    _, normalized, static = cache["last"]
    result, diag = reamp_aligned(rig, di, sr)
    info = {"render": result.summary(), "alignment": diag,
            **reamp.analyze(result.audio, sr, di_feat), "_audio": result.audio,
            "preset": normalized}
    reasons = list(static)
    if info["passthrough"]["passthrough"]:
        reasons.append(info["passthrough"]["reason"])
    info["excluded"] = {"excluded": bool(reasons), "reasons": reasons}
    return info


def batch_dir(mock: bool) -> Path:
    return paths.sub("batch", mock)


def _rel(path, mock: bool) -> str:
    return str(Path(path).resolve().relative_to(paths.home(mock).resolve()))


def _stem_dir(job: jobs.Job, di_name: str, mock: bool) -> Path:
    return paths.ensure(batch_dir(mock) / job.id / export.safe_name(di_name))


def _write_di_stem(job, di_name, audio, sr, fmt, mock) -> Path:
    stereo = np.stack([audio, audio], axis=1)
    stereo = np.vstack([stereo, np.zeros((int(render.TAIL_S * sr), 2), dtype=np.float32)])
    safe = export.safe_name(di_name)
    return export.write(_stem_dir(job, di_name, mock) / f"00_{safe}__DI", stereo, sr, fmt)


def make_worker(rig, job: jobs.Job):
    spec = job.spec
    fmt = spec.get("format", "wav")
    cache: dict = {"dis": {}, "last": None}

    def di_for(name):
        if name not in cache["dis"]:
            audio, sr = di_mod.load(name, rig.mock)
            cache["dis"][name] = (audio, sr, features.analyze_array(audio[:, None], sr))
            _write_di_stem(job, name, audio, sr, fmt, rig.mock)
        return cache["dis"][name]

    def work(item: dict) -> dict:
        audio, sr, di_feat = di_for(item["di"])
        info = _render_stem(rig, item, audio, sr, di_feat, cache)
        normalized = info["preset"]
        excluded = info["excluded"]["excluded"]
        stem = (f"{item['n']:02d}_{export.safe_name(item['di'])}__"
                f"{export.safe_name(item['name'])}{EXCLUDED_SUFFIX if excluded else ''}")
        path = export.write(_stem_dir(job, item["di"], rig.mock) / stem, info["_audio"], sr, fmt)
        return {"key": item["key"], "di": item["di"], "folder": item["folder"],
                "folder_name": guard.folder_name(item["folder"]), "slot": item["slot"],
                "position": item["position"], "name": item["name"], "n": item["n"],
                "file": _rel(path, rig.mock), "latency_ms": info["render"]["latency_ms"],
                "alignment": info["alignment"],
                "lufs": info["loudness"]["lufs"], "true_peak_dbtp": info["loudness"]["true_peak_dbtp"],
                "clipped": info["render"]["clipped"], "excluded": excluded,
                "reasons": info["excluded"]["reasons"], "features": info["features"],
                "fingerprint": normalized.get("fingerprint")}

    return work


def run(rig, job: jobs.Job, *, cancel=None, on_progress=None) -> dict:
    summary = jobs.run(job, make_worker(rig, job), cancel=cancel, on_progress=on_progress)
    return {**summary, **finalize(job, rig.mock)}


def stems(job: jobs.Job) -> list[dict]:
    return [r["result"] for r in job.results() if r.get("ok")]


def latency_report(rows: list[dict]) -> dict:
    """Latency range overall, and the spread within each DI.

    The spread is judged per DI: the measurement correlates against the DI's
    own waveform, so two different DIs can read a few ms apart through the very
    same chain, while stems of one DI should agree with each other.
    """
    values = [r["latency_ms"] for r in rows if r.get("latency_ms") is not None]
    if not values:
        return {"min_ms": None, "max_ms": None, "spread_ms": None, "warning": None}
    per_di: dict[str, list[float]] = {}
    for r in rows:
        if r.get("latency_ms") is not None:
            per_di.setdefault(r["di"], []).append(r["latency_ms"])
    spreads = {d: max(v) - min(v) for d, v in per_di.items()}
    worst = max(spreads, key=spreads.get)
    spread = spreads[worst]
    warning = None
    if spread > LATENCY_SPREAD_WARN_MS:
        warning = (f"DI「{worst}」のステムで往復遅延が {spread:.1f} ms ばらついています"
                   f"({min(per_di[worst]):.1f}〜{max(per_di[worst]):.1f} ms)。整列が信用できない"
                   "可能性があるので、該当のステムを DI と聴き比べてください。")
    return {"min_ms": round(min(values), 1), "max_ms": round(max(values), 1),
            "spread_ms": round(spread, 1),
            "per_di_spread_ms": {d: round(s, 1) for d, s in spreads.items()}, "warning": warning}


def finalize(job: jobs.Job, mock: bool) -> dict:
    """Write stems.csv and README.md for the finished (or partial) job."""
    rows = sorted(stems(job), key=lambda r: (r["di"], r["n"]))
    folder = paths.ensure(batch_dir(mock) / job.id)
    latency = latency_report(rows)
    fmt = job.spec.get("format", "wav")
    with open(folder / "stems.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["order", "di", "file", "preset", "folder", "slot", "latency_ms",
                    "align_method", "lufs", "true_peak_dbtp", "excluded", "reasons", "notes"])
        for di_name in dict.fromkeys(r["di"] for r in rows):
            safe = export.safe_name(di_name)
            w.writerow([0, di_name, f"{safe}/00_{safe}__DI.{'flac' if fmt == 'flac' else 'wav'}",
                        "(DI)", "", "", "", "", "", "", "", "", ""])
        for r in rows:
            align = r.get("alignment") or {}
            w.writerow([r["n"], r["di"], str(Path(r["file"]).relative_to(Path("batch") / job.id)),
                        r["name"], r["folder_name"], r["slot"], r["latency_ms"],
                        align.get("method", ""), r["lufs"], r["true_peak_dbtp"],
                        "yes" if r["excluded"] else "", "; ".join(r["reasons"]),
                        align.get("disagreement", "")])
    failed = [r for r in job.results() if not r.get("ok")]
    lines = [f"# {job.spec.get('title', job.id)}", "",
             "Logic Pro への読み込み: DI ごとのフォルダをトラック領域にドラッグし、"
             "「1 小節目から」配置してください。すべて 48 kHz / 24 bit / ステレオで、DI の 0 サンプル目に"
             "そろえてあります(長さも同じ)。", ""]
    if mock:
        lines += ["**モックで作ったステムです(実機の音ではありません)。**", ""]
    lines += [f"- ステム数: {len(rows)}(失敗 {len(failed)})",
              f"- 往復遅延: {latency['min_ms']}〜{latency['max_ms']} ms(ばらつき {latency['spread_ms']} ms)"]
    if latency["warning"]:
        lines.append(f"- 注意: {latency['warning']}")
    doubtful = [r for r in rows if (r.get("alignment") or {}).get("disagreement")]
    if doubtful:
        lines += ["", "## 整列を耳で確認したいステム", ""]
        lines += [f"- {r['file']}: {r['alignment']['disagreement']}" for r in doubtful]
    excluded = [r for r in rows if r["excluded"]]
    if excluded:
        lines += ["", f"## 除外の印が付いたステム(ファイル名の末尾 {EXCLUDED_SUFFIX})", ""]
        lines += [f"- {r['name']}({r['folder_name']} {r['slot']}, DI {r['di']}): "
                  + "; ".join(r["reasons"]) for r in excluded]
    if failed:
        lines += ["", "## 失敗", ""] + [f"- {r['key']}: {r.get('error')}" for r in failed]
    (folder / "README.md").write_text("\n".join(lines) + "\n")
    return {"stems_dir": _rel(folder, mock), "csv": _rel(folder / "stems.csv", mock),
            "readme": _rel(folder / "README.md", mock), "latency": latency,
            "excluded": len(excluded)}


def show(job: jobs.Job, mock: bool) -> dict:
    rows = sorted(stems(job), key=lambda r: (r["di"], r["n"]))
    slim = [{k: v for k, v in r.items() if k != "features"} for r in rows]
    return {"job": job.id, "title": job.spec.get("title"), "summary": job.summary(),
            "dis": job.spec.get("dis", []), "format": job.spec.get("format"),
            "stems": slim, "latency": latency_report(rows),
            "di_stems": [f"batch/{job.id}/{export.safe_name(d)}/00_{export.safe_name(d)}__DI."
                         f"{'flac' if job.spec.get('format') == 'flac' else 'wav'}"
                         for d in job.spec.get("dis", [])]}


# -- idea K: the sound map ------------------------------------------------------------

def _distance_matrix(feats: list[dict]) -> np.ndarray:
    n = len(feats)
    d = np.zeros((n, n))
    for i in range(n):
        for j in range(i + 1, n):
            d[i, j] = d[j, i] = score.distance(feats[i], feats[j])["distance"]
    return d


def _embed(d: np.ndarray) -> np.ndarray:
    """2-D coordinates from a distance matrix (classical MDS: PCA of the distances)."""
    n = len(d)
    if n < 2:
        return np.zeros((n, 2))
    j = np.eye(n) - np.ones((n, n)) / n
    b = -0.5 * j @ (d ** 2) @ j
    values, vectors = np.linalg.eigh(b)
    order = np.argsort(values)[::-1][:2]
    coords = vectors[:, order] * np.sqrt(np.maximum(values[order], 0.0))
    if coords.shape[1] < 2:
        coords = np.hstack([coords, np.zeros((n, 2 - coords.shape[1]))])
    return coords


def sound_map(job: jobs.Job, di_name: str | None = None, k: int = 5) -> dict:
    """Which presets measure alike, for renders of one DI (excluded ones left out)."""
    rows = stems(job)
    dis = list(dict.fromkeys(r["di"] for r in rows))
    if not dis:
        raise QcsuiteError("このジョブにはまだ結果がありません")
    di_name = di_name or dis[0]
    if di_name not in dis:
        raise QcsuiteError(f"DI「{di_name}」の結果がありません(あるのは {', '.join(dis)})")
    usable = [r for r in rows if r["di"] == di_name and not r["excluded"]]
    left_out = [r["name"] for r in rows if r["di"] == di_name and r["excluded"]]
    d = _distance_matrix([r["features"] for r in usable])
    coords = _embed(d)
    points, neighbours = [], {}
    for i, r in enumerate(usable):
        points.append({"key": r["key"], "name": r["name"], "folder": r["folder_name"],
                       "slot": r["slot"], "file": r["file"],
                       "x": round(float(coords[i, 0]), 3), "y": round(float(coords[i, 1]), 3)})
        order = [j for j in np.argsort(d[i]) if j != i][:k]
        neighbours[r["key"]] = [{"key": usable[j]["key"], "name": usable[j]["name"],
                                 "folder": usable[j]["folder_name"], "slot": usable[j]["slot"],
                                 "distance": round(float(d[i, j]), 2)} for j in order]
    return {"job": job.id, "di": di_name, "dis": dis, "points": points, "neighbours": neighbours,
            "matrix": [[round(float(x), 2) for x in row] for row in d],
            "keys": [r["key"] for r in usable], "excluded": left_out,
            "note": "測定した特徴量(100 Hz〜5 kHz の形、flatness、クレスト)の近さです。"
                    "音の良し悪しではありません。"}


def nearest(job: jobs.Job, name: str, di_name: str | None = None, k: int = 5) -> dict:
    data = sound_map(job, di_name, k)
    match = [p for p in data["points"] if p["key"] == name or p["name"] == name
             or f"{p['folder']}:{p['slot']}" == name]
    if not match:
        raise QcsuiteError(f"「{name}」は地図にありません(除外されたか、名前が違います)")
    return {"preset": match[0], "neighbours": data["neighbours"][match[0]["key"]],
            "note": data["note"]}


def map_png(data: dict, path: Path) -> Path:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    for family in ("Hiragino Sans", "IPAGothic", "Noto Sans CJK JP"):
        if any(family in f.name for f in font_manager.fontManager.ttflist):
            plt.rcParams["font.family"] = family
            break
    fig, ax = plt.subplots(figsize=(8, 6), dpi=100)
    for p in data["points"]:
        ax.plot(p["x"], p["y"], "o", color="#2f6fdf")
        ax.annotate(p["name"], (p["x"], p["y"]), fontsize=7, xytext=(3, 3),
                    textcoords="offset points")
    ax.set_title(f"音の地図(DI: {data['di']})  近い = 測定値が似ている", fontsize=10)
    ax.set_xticks([])
    ax.set_yticks([])
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return path


# -- CLI -------------------------------------------------------------------------------

def _progress(done, total, key):
    print(f"  [{done + 1}/{total}] {key}", file=sys.stderr)


def cmd_run(ctx, a):
    with ctx.rig("batch") as rig:
        if a.resume:
            job = jobs.open_job(a.resume, ctx.mock)
            print(f"ジョブ {job.id} を再開します({len(job.done_keys())}/{len(job.items)} 済み)",
                  file=sys.stderr)
        else:
            job = create(rig, a.di or [di_mod.SYNTH_NAME], a.preset, a.folder, a.format)
            print(f"ジョブ {job.id}: {len(job.items)} 本のステムを作ります。プリセットを順に"
                  "本体に読み込むので、音が切り替わります。", file=sys.stderr)
        ctx.warn_monitor(rig.mock)
        summary = run(rig, job, on_progress=_progress)
    text = [f"完了: {summary['ok']} 本(失敗 {summary.get('failed') or 0}、除外の印 {summary['excluded']})",
            f"ステム: {paths.home(ctx.mock) / summary['stems_dir']}"]
    if summary["latency"]["warning"]:
        text.append(f"注意: {summary['latency']['warning']}")
    ctx.out(summary, "\n".join(text))


def cmd_list(ctx, a):
    rows = [j for j in jobs.listing(ctx.mock) if j.get("kind") == "batch"]
    ctx.out(rows, "\n".join(f"{j['id']}  {j['status']:<16} {j.get('done') or 0}/{j.get('total') or 0}"
                            f"  {j.get('title')}" for j in rows) or "(一括リアンプのジョブはありません)")


def cmd_show(ctx, a):
    data = show(jobs.open_job(a.job, ctx.mock), ctx.mock)
    lines = [f"{data['title']}  遅延 {data['latency']['min_ms']}〜{data['latency']['max_ms']} ms"]
    for r in data["stems"]:
        mark = " [除外] " + "; ".join(r["reasons"]) if r["excluded"] else ""
        lines.append(f"  {r['file']}  {r['latency_ms']} ms  {r['lufs']} LUFS{mark}")
    if data["latency"]["warning"]:
        lines.append(f"注意: {data['latency']['warning']}")
    ctx.out(data, "\n".join(lines))


def cmd_map(ctx, a):
    job = jobs.open_job(a.job, ctx.mock)
    if a.near:
        data = nearest(job, a.near, a.di)
        lines = [f"「{data['preset']['name']}」に測定値が近いプリセット({data['note']})"]
        lines += [f"  {n['distance']:>6}  {n['name']}({n['folder']} {n['slot']})" for n in data["neighbours"]]
        ctx.out(data, "\n".join(lines))
        return
    data = sound_map(job, a.di)
    png = Path(a.png) if a.png else batch_dir(ctx.mock) / job.id / "sound_map.png"
    map_png(data, png)
    lines = [f"音の地図: {len(data['points'])} 件(除外 {len(data['excluded'])})  グラフ: {png}", data["note"]]
    for p in data["points"]:
        near = data["neighbours"][p["key"]][:1]
        lines.append(f"  {p['name']:<24} 最も近い: "
                     + (f"{near[0]['name']}({near[0]['distance']})" if near else "—"))
    ctx.out(data, "\n".join(lines))


def add_cli(sub):
    p = sub.add_parser("batch", help="一括リアンプ(Logic 用ステム)と音の地図")
    s = p.add_subparsers(dest="batch_cmd", required=True)
    c = s.add_parser("run", help="DI × プリセットをリアンプしてステムを書き出す(--resume で再開)")
    c.add_argument("--di", action="append", help="DI の名前(複数可。省略時は合成 DI)")
    c.add_argument("--preset", action="append", help="フォルダ:スロット(例 kami:1E)。複数可")
    c.add_argument("--folder", help="このフォルダの全プリセット")
    c.add_argument("--format", default="wav", choices=FORMATS)
    c.add_argument("--resume", metavar="JOB_ID")
    c.set_defaults(fn=cmd_run)
    c = s.add_parser("list", help="一括リアンプのジョブ一覧")
    c.set_defaults(fn=cmd_list)
    c = s.add_parser("show", help="ステムの一覧(遅延・音量・除外)")
    c.add_argument("job")
    c.set_defaults(fn=cmd_show)
    c = s.add_parser("map", help="音の地図(測定値が似ているプリセット)")
    c.add_argument("job")
    c.add_argument("--di", help="どの DI の結果で比べるか(省略時は最初の DI)")
    c.add_argument("--near", help="このプリセットに近いものを表示(名前か フォルダ:スロット)")
    c.add_argument("--png", help="地図の画像の書き出し先")
    c.set_defaults(fn=cmd_map)
