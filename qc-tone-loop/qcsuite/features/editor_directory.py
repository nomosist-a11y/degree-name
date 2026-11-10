"""エディターの DIRECTORY: preset and setlist management, like Cortex Control's
Directory view, inside qcsuite's safety rules.

What it offers, and where:

* Per preset - 名前を変更 / スロットを移動 / 削除 - only in folders the write
  policy lets through (the sandbox ``_qcsuite_sandbox``, or ``Live`` /
  ``Artist`` / ``Song`` / ``Workshop`` when the server was started with
  ``--allow-folder`` for it). Every other folder is read-only here; the
  answer says why (:func:`writable`).
* Setlists - 新しいセットリスト (a sandbox name, or an opt-in folder that is
  allowed and does not exist yet) and 削除 (sandbox setlists only).
* Every one of those writes is a :mod:`qcsuite.plan` plan. Building one sends
  nothing; the page shows its steps and backup folder and runs it only after
  the player types the confirm word (``/api/plans/{id}/execute``). Destructive
  steps back the preset up as JSON first (reading it RECALLS it).

Rename: the unit has no rename command (pyquadcortex has none; files are
addressed as ``<setlist>/<name>.pb``). It is built from steps the plan system
already verifies: copy the preset into a free slot of the same folder under
the new name, delete the old one (backed up first), move the copy into the old
slot. Its limits are stated in the plan: the copy recalls the preset (the
sound changes; unsaved edits on the unit are lost), tags are not carried, and
bypassed capture blocks come back switched on (unit behaviour measured
2026-11-10). Names that would be ambiguous (the same name twice in the
folder, or the new name already present) are refused, since delete and move
address presets by name.

Reads: the capture library (:func:`captures_listing`) and the live preset's
MIDI Out per footswitch / expression pedal (:func:`midi_listing`) - both on
request only, never polled.
"""

from __future__ import annotations

import pyquadcortex as pq

from qcsuite import plan as plan_mod
from qcsuite.device import guard
from qcsuite.features import panel
from qcsuite.errors import QcsuiteError, WriteRefused

#: How long a name the unit is assumed to keep as given (it cuts de-duplicated
#: names to 20 characters, so longer ones might not come back unchanged).
NAME_LIMIT = 20

READ_ONLY_NOTE = "読むだけのフォルダです"


class DirectoryError(QcsuiteError):
    """A directory request that cannot be planned (nothing was sent)."""


# -- which folders may be written ---------------------------------------------------------

def writable(device, key: str) -> tuple[bool, str]:
    """Whether presets in ``key`` may be renamed / moved / deleted, and if not, why."""
    try:
        device.policy.check(key, "書き込み")
    except WriteRefused as exc:
        return False, f"{READ_ONLY_NOTE}({exc})"
    return True, ""


def _deletable(device, key: str) -> bool:
    try:
        device.policy.check_delete_folder(key)
    except WriteRefused:
        return False
    return True


def _creatable_names(device, existing: set[str]) -> list[str]:
    """Opt-in folders the player allowed that do not exist yet."""
    return [n for n in guard.OPT_IN
            if guard.folder_key(n) in device.policy.allowed and guard.folder_key(n) not in existing]


def directory(device, folder: str | None = None) -> dict:
    """Setlists (cached listing) with what may be done in each, and one folder's presets."""
    listing = device.setlists_cached()
    existing = {f["key"] for f in listing}
    setlists = []
    for f in listing:
        ok, why = writable(device, f["key"])
        setlists.append({"key": f["key"], "name": f["name"], "kind": f["kind"],
                         "writable": ok, "reason": why,
                         "deletable": _deletable(device, f["key"])})
    here = device.current_slot()
    key = folder or (here["folder"] if here else None) or \
        next((f["key"] for f in setlists if f["kind"] == "user"), None)
    presets, free = [], []
    ok, why = (False, "")
    if key:
        ok, why = writable(device, key)
        entries = [p for p in device.presets(key) if p["index"] >= 0]
        presets = [{"position": p["index"], "slot": p["slot"], "name": p["name"]} for p in entries]
        if ok:
            taken = {p["index"] for p in entries}
            free = [pq.position_to_slot(i) for i in range(256) if i not in taken]
    user = [f for f in listing if f["kind"] in ("user", "sandbox")]
    limit = guard.setlist_limit()
    return {"setlists": setlists, "folder": key, "writable": ok, "reason": why,
            "presets": presets, "free_slots": free, "current": here,
            "room": {"user_folders": len(user), "limit": limit, "free": limit - len(user)},
            "create": {"prefix": guard.SANDBOX_PREFIX, "sandbox": guard.SANDBOX_NAME,
                       "allowed": _creatable_names(device, existing),
                       "readonly": device.policy.readonly}}


# -- plans -----------------------------------------------------------------------------------

def _require_writable(device, key: str):
    ok, why = writable(device, key)
    if not ok:
        raise DirectoryError(why)


def _entry(device, key: str, position) -> tuple[dict, list[dict]]:
    if not isinstance(position, int) or not 0 <= position < 256:
        raise DirectoryError("position(0〜255)を指定してください。")
    entries = [p for p in device.presets(key) if p["index"] >= 0]
    hit = next((p for p in entries if p["index"] == position), None)
    if hit is None or not hit["name"]:
        raise DirectoryError(f"{pq.position_to_slot(position)} は空きスロットです。")
    same = [p for p in entries if p["name"] == hit["name"]]
    if len(same) > 1:
        raise DirectoryError(
            f"「{hit['name']}」という名前が {len(same)} つあります。本体は名前でプリセットを"
            "指定するため、どれに効くか分からないので計画を作りません。本体で名前を変えてください。")
    return hit, entries


def _folder(req: dict) -> str:
    folder = req.get("folder")
    if not isinstance(folder, str) or not folder.strip():
        raise DirectoryError("セットリスト(folder)を指定してください。")
    return guard.folder_key(folder)


def rename_plan(device, req: dict) -> plan_mod.Plan:
    """Rename = copy under the new name into a free slot, delete the old, move into its slot."""
    key = _folder(req)
    _require_writable(device, key)
    new = req.get("name")
    if not isinstance(new, str):
        raise DirectoryError("新しい名前を入れてください。")
    new = new.strip()
    guard.check_preset_name(new)
    if len(new) > NAME_LIMIT:
        raise DirectoryError(f"名前は {NAME_LIMIT} 文字までにしてください(本体が名前を短くすると、"
                             "あとの手順で見つけられなくなるため)。")
    hit, entries = _entry(device, key, req.get("position"))
    if new == hit["name"]:
        raise DirectoryError("いまと同じ名前です。")
    clash = next((p for p in entries if p["name"].casefold() == new.casefold()), None)
    if clash is not None:
        raise DirectoryError(f"「{clash['name']}」({clash['slot']})がすでにあります。別の名前にしてください"
                             "(大文字・小文字だけの違いも避けます)。")
    taken = {p["index"] for p in entries}
    spare = next((i for i in range(256) if i not in taken), None)
    if spare is None:
        raise DirectoryError(f"「{guard.folder_name(key)}」に空きスロットが無いため、名前を変えられません"
                             "(途中で一時的に 1 つ使います)。")
    old_slot = hit["slot"]
    steps = [
        plan_mod.Step("copy", key, position=spare, name=new,
                      source={"folder": key, "position": hit["index"]},
                      note=f"新しい名前で {pq.position_to_slot(spare)} に一時的に置きます"),
        plan_mod.Step("delete", key, name=hit["name"], note=f"元の {old_slot}(先にバックアップ)"),
        plan_mod.Step("move", key, position=hit["index"], name=new, note=f"元の {old_slot} に戻します"),
    ]
    warnings = [
        "本体には名前を変える命令が無いため、コピー → 元を削除 → 元のスロットへ移動、の 3 手順で行います。",
        "コピーでプリセットが本体に読み込まれます(音が切り替わり、保存していない編集は消えます)。",
        "タグは引き継がれません。",
        "バイパスしたキャプチャーは、オンになって保存されます(本体の仕様)。終わったら本体で確かめてください。",
        "途中で止まったときは、それまでの手順だけが済んでいます(記録とバックアップが残ります)。",
    ]
    if device.preset_dirty():
        warnings.insert(0, "いま本体に保存していない編集があります。実行するとその編集は消えます。")
    return plan_mod.Plan(f"名前を変更: 「{hit['name']}」→「{new}」({guard.folder_name(key)} {old_slot})",
                         steps=steps, warnings=warnings)


def move_plan(device, req: dict) -> plan_mod.Plan:
    """Move one preset to an empty slot of the same setlist."""
    key = _folder(req)
    _require_writable(device, key)
    hit, entries = _entry(device, key, req.get("position"))
    to = req.get("to")
    try:
        target = pq.slot_to_position(str(to).strip().upper()) if isinstance(to, str) else int(to)
    except (TypeError, ValueError, KeyError):
        raise DirectoryError(f"移動先のスロットが分かりません: {to!r}(例: 3C)") from None
    if not 0 <= target < 256:
        raise DirectoryError(f"移動先のスロットが範囲外です: {to!r}")
    if target == hit["index"]:
        raise DirectoryError("いまと同じスロットです。")
    occupant = next((p for p in entries if p["index"] == target), None)
    if occupant is not None:
        raise DirectoryError(f"{occupant['slot']} には「{occupant['name']}」があります。空きスロットだけに"
                             "移動できます(上書きはしません)。")
    return plan_mod.Plan(
        f"移動: 「{hit['name']}」{hit['slot']} → {pq.position_to_slot(target)}({guard.folder_name(key)})",
        steps=[plan_mod.Step("move", key, position=target, name=hit["name"],
                             note=f"元: {hit['slot']}")],
        warnings=["移動の前に、このプリセットを JSON にバックアップします(読み込むため音が切り替わります)。"])


def delete_plan(device, req: dict) -> plan_mod.Plan:
    """Delete one preset (backed up as JSON first)."""
    key = _folder(req)
    _require_writable(device, key)
    hit, _ = _entry(device, key, req.get("position"))
    return plan_mod.Plan(
        f"削除: 「{hit['name']}」({guard.folder_name(key)} {hit['slot']})",
        steps=[plan_mod.Step("delete", key, name=hit["name"], note=f"{hit['slot']}")],
        warnings=["削除の前に、このプリセットを JSON にバックアップします(読み込むため音が切り替わります)。"])


def create_setlist_plan(device, req: dict) -> plan_mod.Plan:
    """A new setlist: a sandbox name (``_qcsuite...``) or an allowed opt-in folder."""
    name = req.get("name")
    if not isinstance(name, str) or not name.strip():
        raise DirectoryError("セットリストの名前を入れてください。")
    name = name.strip()
    key = guard.folder_key(name)
    if not guard.is_user_folder(key):
        raise DirectoryError(f"「{name}」はセットリストの名前に使えません。")
    listing = device.setlists()                    # fresh: the plan must see the unit as it is
    existing = {f["key"] for f in listing}
    try:
        device.policy.check_create_folder(name, existing)
    except WriteRefused as exc:
        raise DirectoryError(f"{exc} 新しく作れるのは「{guard.SANDBOX_PREFIX}」で始まる名前か、"
                             "許可したフォルダ(Live / Artist / Song / Workshop)だけです。") from None
    user = [f for f in listing if f["kind"] in ("user", "sandbox")]
    limit = guard.setlist_limit()
    if len(user) >= limit:
        raise DirectoryError(f"ユーザーのセットリストが {len(user)} 個あり、上限({limit} 個と想定)に"
                             "余裕がないため作りません。")
    return plan_mod.Plan(f"セットリストを作る: 「{name}」",
                         steps=[plan_mod.Step("create_folder", key, note="空のセットリスト")],
                         warnings=[f"作ったあとのユーザーのセットリストは {len(user) + 1} / {limit} 個(想定の上限)です。"])


def delete_setlist_plan(device, req: dict) -> plan_mod.Plan:
    """Delete a sandbox setlist; every preset in it is backed up as JSON first."""
    key = _folder(req)
    try:
        device.policy.check_delete_folder(key)
    except WriteRefused as exc:
        raise DirectoryError(str(exc)) from None
    existing = {f["key"] for f in device.setlists()}
    if key not in existing:
        raise DirectoryError(f"「{guard.folder_name(key)}」は本体にありません。")
    count = len([p for p in device.presets(key) if p["index"] >= 0])
    warnings = [f"中のプリセット {count} 件も消えます。"] if count else ["中は空です。"]
    if count:
        warnings.append(f"削除の前に {count} 件すべてを JSON にバックアップします。1 件ずつ本体に読み込むため、"
                        "音が切り替わり、保存していない編集は消えます。")
    return plan_mod.Plan(f"セットリストを削除: 「{guard.folder_name(key)}」",
                         steps=[plan_mod.Step("delete_folder", key, note=f"{count} 件入り")],
                         warnings=warnings)


PLANS = {"rename": rename_plan, "move": move_plan, "delete": delete_plan,
         "create_setlist": create_setlist_plan, "delete_setlist": delete_setlist_plan}


def build_plan(device, req: dict) -> plan_mod.Plan:
    action = req.get("action")
    builder = PLANS.get(action) if isinstance(action, str) else None
    if builder is None:
        raise DirectoryError(f"未知の操作です: {action!r}")
    if device.policy.readonly:
        raise DirectoryError("QCSUITE_READONLY が設定されているため、計画を作りません。")
    return builder(device, req)


def plan_finished(device, plan_id: str) -> dict:
    """A directory plan ran: presets were loaded on the unit (copies, backups), so the
    editor's undo history stops here, as it does after a recall."""
    plan = plan_mod.load_plan(str(plan_id), device.mock)
    if plan.changes_sound:
        panel.mark_preset_change(device.mock, f"計画 {plan.id}: {plan.title}")
    return {"barrier": plan.changes_sound}


def backup_dir(plan: plan_mod.Plan, mock: bool) -> str | None:
    """Where :func:`qcsuite.plan.execute` will put the backups of a destructive plan."""
    if not plan.destructive:
        return None
    return str(plan_mod.plans_dir(mock) / plan.id / "backup")


# -- reads -------------------------------------------------------------------------------

CAPTURE_NOTE = ("パソコンから選んだキャプチャーは鳴りません(本体の仕様)。使うときは本体の画面で"
                "キャプチャーブロックに読み込んでください。ここは一覧を見るだけです。")


def captures_listing(device) -> dict:
    """The unit's capture library, names only (read-only)."""
    return {"captures": device.captures(), "note": CAPTURE_NOTE}


MIDI_SOURCES = ["フットスイッチ A", "フットスイッチ B", "フットスイッチ C", "フットスイッチ D",
                "フットスイッチ E", "フットスイッチ F", "フットスイッチ G", "フットスイッチ H",
                "エクスプレッション 1", "エクスプレッション 2"]


def _midi_row(source: str, m, expression: bool) -> dict:
    t = int(m.type)
    if t == int(pq.MidiOutType.PC):
        kind, detail = "PC", f"プログラム {m.param3}(バンク MSB {m.param1} / LSB {m.param2})"
    elif t == int(pq.MidiOutType.CC_TOGGLE):
        kind, detail = "CC トグル", f"CC {m.param1}: {m.param2} ⇄ {m.param3}"
    elif t == int(pq.MidiOutType.CC):
        kind = "CC"
        detail = f"CC {m.param1}: {m.param2}〜{m.param3}" if expression else f"CC {m.param1} = {m.param2}"
    else:
        kind, detail = f"種類 {t}", f"{m.param1} / {m.param2} / {m.param3}"
    return {"source": source, "type": kind, "channel": int(m.channel), "detail": detail}


def midi_listing(device) -> dict:
    """MIDI Out of the live preset, per footswitch / expression pedal (read-only).

    Read from the live grid (``read_current_preset``: no recall, no side
    effects). Messages the player entered on the unit and has not saved are
    included only if the unit already holds them in the live preset.
    """
    p = device.current_preset()
    rows = []
    for source, messages in sorted(pq.midi_out(p).items()):
        label = MIDI_SOURCES[source] if 0 <= source < len(MIDI_SOURCES) else f"ソース {source}"
        rows.extend(_midi_row(label, m, source >= 8) for m in messages)
    rows.extend(_midi_row("プリセットを読み込んだとき", m, False) for m in pq.preset_load_midi_out(p))
    return {"rows": rows,
            "note": "見るだけです。変更は本体か Cortex Control で。チャンネルは本体に保存された値のまま"
                    "表示します。"}


def add_cli(sub):
    """No CLI commands: the CLI has ``qcsuite library`` for the same plans."""
