"""Writes to stored presets go through a plan: show, confirm, back up, run, verify.

Every feature that saves, copies, deletes or moves presets builds a
:class:`Plan` first. Building one sends nothing. :func:`execute` then:

1. checks every step against the safety rules - one refusal stops the whole
   plan before anything is sent;
2. stops there unless ``confirm=True`` (dry run is the default everywhere);
3. backs up: the listings of every folder the plan touches, and - for steps
   that delete or overwrite a preset - that preset itself, read from the unit
   and normalized to JSON (reading RECALLS it, so the sound changes);
4. runs the steps in order;
5. reads back after each step (listings are eventually consistent, so it polls)
   and stops at the first step that does not verify.

Each run leaves ``plan.json``, ``backup/`` and ``report.jsonl`` under
``~/.qctone/plans/<id>/``.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pyquadcortex as pq

from qcsuite import paths, presets
from qcsuite.audio.export import safe_name
from qcsuite.device import guard
from qcsuite.errors import PlanError, VerifyFailed, WriteRefused

ACTIONS = ("copy", "save", "delete", "move", "create_folder", "delete_folder", "rebuild")


@dataclass
class Step:
    action: str
    folder: str                      # target folder key (or name for folders)
    position: int | None = None
    name: str | None = None
    source: dict | None = None       # {"folder": key, "position": int} for copy
    overwrites: str | None = None    # name currently at the target slot
    note: str = ""
    payload: dict | None = None      # a normalized preset, for rebuild
    adjust: dict | None = None       # edits between recall and save, for copy

    @property
    def destructive(self) -> bool:
        return self.action in ("delete", "move", "delete_folder") or bool(self.overwrites)

    def describe(self) -> str:
        where = guard.folder_name(self.folder)
        slot = pq.position_to_slot(self.position) if self.position is not None else ""
        if self.action == "copy":
            src = self.source or {}
            text = (f"コピー: 「{guard.folder_name(src.get('folder', '?'))}」"
                    f"{pq.position_to_slot(src['position']) if 'position' in src else ''} → "
                    f"「{where}」{slot}「{self.name}」")
        elif self.action == "save":
            text = f"保存: 現在のグリッド → 「{where}」{slot}「{self.name}」"
        elif self.action == "rebuild":
            text = f"復元: JSON から組み立て → 「{where}」{slot}「{self.name}」"
        elif self.action == "delete":
            text = f"削除: 「{where}」の「{self.name}」"
        elif self.action == "move":
            text = f"移動: 「{where}」の「{self.name}」→ {slot}"
        elif self.action == "create_folder":
            text = f"フォルダ作成: 「{where}」"
        elif self.action == "delete_folder":
            text = f"フォルダ削除: 「{where}」"
        else:
            text = f"{self.action}: {where}"
        if self.adjust and self.adjust.get("lane_db"):
            levels = ", ".join(f"行{int(r) + 1} {db:+.1f} dB"
                               for r, db in self.adjust["lane_db"].items())
            text += f"(出力レベルを {levels} に設定)"
        if self.adjust and self.adjust.get("gain_block"):
            blocks = ", ".join(f"行{int(r) + 1} 列{g['col'] + 1} {g['db']:+.1f} dB"
                               for r, g in self.adjust["gain_block"].items())
            text += f"(Gain ブロックを追加: {blocks})"
        if self.adjust and self.adjust.get("knobs"):
            text += f"(ノブ {len(self.adjust['knobs'])} 個を変更)"
        if self.adjust and self.adjust.get("patch"):
            text += f"(パッチ {len(self.adjust['patch'].get('ops', []))} 操作を適用)"
        if self.overwrites:
            text += f"(上書き: 「{self.overwrites}」が消えます)"
        if self.note:
            text += f" — {self.note}"
        return text


@dataclass
class Plan:
    title: str
    steps: list[Step] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    id: str = field(default_factory=lambda: time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6])
    created: str = field(default_factory=lambda: time.strftime("%Y-%m-%d %H:%M:%S"))

    @property
    def destructive(self) -> bool:
        return any(s.destructive for s in self.steps)

    @property
    def changes_sound(self) -> bool:
        return any(s.action in ("copy", "rebuild") for s in self.steps) or self.destructive

    def to_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "created": self.created,
                "destructive": self.destructive, "changes_sound": self.changes_sound,
                "warnings": self.warnings, "steps": [asdict(s) for s in self.steps],
                "text": self.render()}

    @classmethod
    def from_dict(cls, d: dict) -> "Plan":
        return cls(title=d["title"], steps=[Step(**s) for s in d["steps"]],
                   warnings=list(d.get("warnings", [])), id=d["id"], created=d["created"])

    def render(self) -> str:
        lines = [f"計画: {self.title}({len(self.steps)} 手順)"]
        for i, s in enumerate(self.steps, 1):
            lines.append(f"  {i:>3}. {'[破壊的] ' if s.destructive else ''}{s.describe()}")
        notes = list(self.warnings)
        if self.changes_sound:
            notes.append("実行中は本体のプリセットが次々に読み込まれ、音が切り替わります。")
        if self.destructive:
            notes.append("破壊的な手順の前に、対象のプリセットを JSON にバックアップします。")
        if notes:
            lines.append("注意:")
            lines.extend(f"  - {n}" for n in notes)
        return "\n".join(lines)


def plans_dir(mock: bool) -> Path:
    return paths.sub("plans", mock)


def save_plan(plan: Plan, mock: bool) -> Path:
    d = paths.ensure(plans_dir(mock) / plan.id)
    (d / "plan.json").write_text(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2) + "\n")
    return d


def load_plan(plan_id: str, mock: bool) -> Plan:
    path = plans_dir(mock) / plan_id / "plan.json"
    if not path.exists():
        raise PlanError(f"計画 {plan_id} が見つかりません。")
    return Plan.from_dict(json.loads(path.read_text()))


def check(plan: Plan, device) -> list[str]:
    """Every step against the safety rules. Returns refusals (empty = all allowed)."""
    problems = []
    existing = {f["key"] for f in device.setlists()}
    for i, s in enumerate(plan.steps, 1):
        if s.action not in ACTIONS:
            problems.append(f"手順 {i}: 未知の操作 {s.action}")
            continue
        try:
            if s.action == "create_folder":
                device.policy.check_create_folder(s.folder, existing)
            elif s.action == "delete_folder":
                device.policy.check_delete_folder(s.folder)
            else:
                device.policy.check(s.folder, s.describe())
        except WriteRefused as exc:
            problems.append(f"手順 {i}: {exc}")
    return problems


def _backup(plan: Plan, device, folder: Path) -> list[str]:
    folder = paths.ensure(folder / "backup")
    written = []
    keys = sorted({s.folder for s in plan.steps if s.action not in ("create_folder",)})
    for key in keys:
        if key not in {f["key"] for f in device.setlists()}:
            continue
        listing = device.presets(key)
        path = folder / f"listing_{safe_name(guard.folder_name(key))}.json"
        path.write_text(json.dumps(listing, ensure_ascii=False, indent=2) + "\n")
        written.append(str(path))
    catalog = device.catalog
    for s in plan.steps:
        target = None
        if s.action in ("delete", "move"):
            target = next((p for p in device.presets(s.folder) if p["name"] == s.name), None)
        elif s.overwrites:
            target = next((p for p in device.presets(s.folder) if p["index"] == s.position), None)
        if target is None:
            continue
        stored = device.read_stored(s.folder, target["index"])
        n = presets.normalize(stored, catalog, folder=s.folder, position=target["index"])
        path = folder / f"{safe_name(guard.folder_name(s.folder))}_{target['slot']}_" \
                        f"{safe_name(target['name'])}.json"
        path.write_text(json.dumps(n, ensure_ascii=False, indent=2) + "\n")
        written.append(str(path))
    return written


def _verify(step: Step, device, stored_name: str | None):
    if step.action in ("copy", "save", "rebuild"):
        entries = device.wait_for_listing(
            step.folder, until=lambda es: any(e.index == step.position and e.name for e in es))
        return next((e.name for e in entries if e.index == step.position), None)
    if step.action == "delete":
        device.wait_for_listing(step.folder,
                                until=lambda es: not any(e.name == step.name for e in es))
        return None
    if step.action == "move":
        device.wait_for_listing(
            step.folder, until=lambda es: any(e.index == step.position and e.name == step.name
                                              for e in es))
        return step.name
    if step.action == "create_folder":
        key = guard.folder_key(step.folder)
        if key not in {f["key"] for f in device.setlists()}:
            raise VerifyFailed(f"フォルダ「{step.folder}」が一覧に現れません。")
    if step.action == "delete_folder":
        key = guard.folder_key(step.folder)
        if key in {f["key"] for f in device.setlists()}:
            raise VerifyFailed(f"フォルダ「{step.folder}」がまだ一覧にあります。")
    return None


def _run_step(step: Step, device, rebuild=None):
    if step.action == "copy":
        return device.copy_preset(step.source["folder"], step.source["position"], step.folder,
                                  to_position=step.position, name=step.name, step=step,
                                  adjust=step.adjust)
    if step.action == "save":
        return device.save_current(step.folder, step.position, step.name, step=step)
    if step.action == "rebuild":
        if rebuild is None:
            from qcsuite.rebuild import rebuild_grid as rebuild
        rebuild(device, step.payload)
        return device.save_current(step.folder, step.position, step.name, step=step)
    if step.action == "delete":
        return device.delete_preset(step.folder, step.name, step=step)
    if step.action == "move":
        return device.move_preset(step.folder, step.name, step.position, step=step)
    if step.action == "create_folder":
        return device.create_folder(guard.folder_name(guard.folder_key(step.folder)), step=step)
    if step.action == "delete_folder":
        return device.delete_folder(step.folder, step=step)
    raise PlanError(f"未知の操作 {step.action}")


def execute(plan: Plan, device, *, confirm: bool = False, rebuild=None,
            on_progress=None) -> dict:
    """Run a plan (see module docstring). Dry run unless ``confirm``."""
    problems = check(plan, device)
    if problems:
        raise WriteRefused("計画に拒否される手順があるため、何も実行していません:\n"
                           + "\n".join(problems))
    folder = save_plan(plan, device.mock)
    if not confirm:
        return {"dry_run": True, "plan": plan.to_dict(), "dir": str(folder)}
    backups = _backup(plan, device, folder) if plan.destructive else []
    report = folder / "report.jsonl"
    results = []
    for i, step in enumerate(plan.steps, 1):
        if on_progress:
            on_progress(i, len(plan.steps), step.describe())
        record = {"step": i, "action": step.action, "describe": step.describe(),
                  "at": time.strftime("%H:%M:%S")}
        try:
            stored = _run_step(step, device, rebuild)
            verified = _verify(step, device, stored)
            record.update(ok=True, stored_name=verified or stored)
        except Exception as exc:
            record.update(ok=False, error=f"{type(exc).__name__}: {exc}")
            with open(report, "a") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            raise PlanError(f"手順 {i}「{step.describe()}」で止まりました: {exc}"
                            f"(それまでの手順は実行済み。記録: {report})") from exc
        with open(report, "a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        results.append(record)
    return {"dry_run": False, "plan": plan.to_dict(), "dir": str(folder),
            "backups": backups, "results": results}
