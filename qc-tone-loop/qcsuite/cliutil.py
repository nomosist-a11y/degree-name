"""What every ``qcsuite`` subcommand shares: the rig, confirmations, output.

A command function receives ``(ctx, args)``. ``ctx.rig()`` opens the unit (or
the fake with ``--mock``) under the device lock; ``ctx.confirm_plan`` shows a
plan and asks before running it; ``ctx.out`` prints JSON or text.

Confirmation rules, the same for every command:

* plans are dry runs unless ``--apply`` is given;
* with ``--apply``, a plan is printed and the player types ``yes``; ``--yes``
  skips the typing (for scripts) but never the ``--apply``;
* anything that reamps prints the monitor-volume warning and waits for Enter
  unless ``--yes``.
"""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field

from qcsuite import plan as plan_mod
from qcsuite.audio.render import MONITOR_WARNING
from qcsuite.device.session import open_rig


@dataclass
class Ctx:
    mock: bool = False
    allow_folders: list = field(default_factory=list)
    yes: bool = False
    json: bool = False
    _shown: int = 0

    @contextmanager
    def rig(self, what: str = "qcsuite"):
        with open_rig(self.mock, allow_folders=self.allow_folders, what=what) as rig:
            try:
                yield rig
            finally:
                self.notices(rig.device)

    def notices(self, device):
        for n in device.notices[self._shown:]:
            mark = {"sound_change": "音", "inert_write": "注意", "retry": "再試行"}.get(n.kind, "情報")
            print(f"[{mark}] {n.message}", file=sys.stderr)
        self._shown = len(device.notices)

    def out(self, data, text: str | None = None):
        if self.json or text is None:
            print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
        else:
            print(text)

    def ask(self, prompt: str) -> bool:
        if self.yes:
            return True
        if not sys.stdin.isatty():
            print(f"{prompt}\n(確認できない環境なので中止しました。--yes で確認を省略できます)",
                  file=sys.stderr)
            return False
        return input(f"{prompt} [yes と入力]: ").strip().lower() in ("yes", "y", "はい")

    def warn_monitor(self, mock: bool):
        if mock:
            print("(モック: 実際の音は出ません)", file=sys.stderr)
            return
        print(f"注意: {MONITOR_WARNING}", file=sys.stderr)
        if not self.yes and sys.stdin.isatty():
            input("準備ができたら Enter を押してください...")

    def run_plan(self, plan: plan_mod.Plan, device, apply: bool, rebuild=None) -> dict:
        """Print the plan; execute it only with ``--apply`` and a confirmation."""
        print(plan.render(), file=sys.stderr)
        if not apply:
            result = plan_mod.execute(plan, device, confirm=False)
            print("\n(dry-run: 何も実行していません。実行するには --apply を付けてください)",
                  file=sys.stderr)
            return result
        if not self.ask("この計画を実行しますか?"):
            return {"dry_run": True, "cancelled": True, "plan": plan.to_dict()}

        def progress(i, n, text):
            print(f"  [{i}/{n}] {text}", file=sys.stderr)
        return plan_mod.execute(plan, device, confirm=True, rebuild=rebuild, on_progress=progress)
