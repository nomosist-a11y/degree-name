"""``qcsuite``: everyday Quad Cortex tools. ``qcsuite -h`` lists the commands.

Global options come before the command::

    qcsuite --mock library list           # against the fake unit
    qcsuite --allow-folder Live setlist plan songs.txt --folder Live --apply

Writes are dry runs unless ``--apply``; the player's own folders are never
written (see ``qcsuite/device/guard.py``).
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys

from qcsuite import __version__, jobs, paths
from qcsuite import plan as plan_mod
from qcsuite.cliutil import Ctx
from qcsuite.errors import QcsuiteError
from qcsuite.features import MODULES
from qctone import device as qdevice


def cmd_status(ctx, a):
    with ctx.rig("status") as rig:
        d = rig.device
        info = d.version()
        folders = d.setlists()
        data = {"mode": "mock" if rig.mock else "hardware", **info,
                "policy": d.policy.describe(),
                "folders": [{k: f[k] for k in ("name", "occupied", "slots", "kind", "protected")}
                            for f in folders]}
    lines = [f"モード: {'モック' if ctx.mock else '実機'}  ファームウェア: {info['firmware']}",
             "フォルダ:"]
    for f in data["folders"]:
        mark = "保護" if f["protected"] else ("sandbox" if f["kind"] == "sandbox" else "")
        lines.append(f"  {f['name']:<20} {f['occupied']:>3}/{f['slots']}  {mark}")
    lines.append(f"書き込み可: sandbox と {', '.join(data['policy']['allowed']) or '(追加なし)'}")
    ctx.out(data, "\n".join(lines))


def cmd_grid(ctx, a):
    with ctx.rig("grid") as rig:
        grid = rig.device.grid()
    ctx.out(grid)


def cmd_apply(ctx, a):
    from qctone.cli import load_patch
    patch = load_patch(a.patch)
    with ctx.rig("apply") as rig:
        result = rig.device.apply_patch(patch)
    ctx.out(result, f"{result['applied']} 件の操作を適用しました(保存はしていません)。")


def cmd_jobs(ctx, a):
    if a.id:
        job = jobs.open_job(a.id, ctx.mock)
        ctx.out({"summary": job.summary(), "spec": job.spec})
        return
    rows = jobs.listing(ctx.mock)
    text = "\n".join(f"{j['id']}  {j['status']:<16} {j.get('done') or 0}/{j.get('total') or 0}"
                     f"  {j.get('title') or ''}" for j in rows) or "(ジョブはありません)"
    ctx.out(rows, text)


def cmd_plan(ctx, a):
    plan = plan_mod.load_plan(a.id, ctx.mock)
    if not a.run:
        ctx.out(plan.to_dict(), plan.render())
        return
    with ctx.rig("plan") as rig:
        result = ctx.run_plan(plan, rig.device, apply=a.apply)
    ctx.out(result, json.dumps(result.get("results", []), ensure_ascii=False, indent=1))


def cmd_mock_reset(ctx, a):
    from qcsuite.device.session import state_path
    path = state_path()
    if path.exists():
        path.unlink()
    print(f"モックの状態を初期化しました({path})")


def cmd_serve(ctx, a):
    from qcsuite.web.app import serve
    serve(mock=ctx.mock, allow_folders=ctx.allow_folders, port=a.port,
          open_browser=not a.no_browser)


def cmd_selftest(ctx, a):
    from qcsuite import selftest
    if a.hardware:
        if ctx.mock:
            print("--hardware は実機用です。モックでの通し確認は `qcsuite --mock selftest`。",
                  file=sys.stderr)
        code = selftest.run_hardware(ctx, report_path=a.report, start_at=a.start_at)
    else:
        code = selftest.run_mock(ctx, report_path=a.report)
    raise SystemExit(code)


def build_parser():
    p = argparse.ArgumentParser(prog="qcsuite", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"qcsuite {__version__}")
    p.add_argument("--mock", action="store_true",
                   help="実機の代わりにモック(FakeQuadCortex)を使う")
    p.add_argument("--allow-folder", action="append", default=[], metavar="NAME",
                   help="書き込みを許可するフォルダ(Live / Artist / Song / Workshop など)。複数可")
    p.add_argument("--yes", action="store_true",
                   help="確認の入力を省略する(--apply が無い計画は実行されない)")
    p.add_argument("--json", action="store_true", help="結果を JSON で出す")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="command")

    c = sub.add_parser("status", help="接続、ファームウェア、フォルダと書き込み許可を表示")
    c.set_defaults(fn=cmd_status)
    c = sub.add_parser("grid", help="現在のグリッドを JSON で表示(副作用なし)")
    c.set_defaults(fn=cmd_grid)
    c = sub.add_parser("apply", help="パッチをグリッドに適用(保存しない)")
    c.add_argument("patch", help="パッチ JSON(インラインかファイル)。形式は qctone/device.py")
    c.set_defaults(fn=cmd_apply)
    c = sub.add_parser("jobs", help="ジョブの一覧、または 1 件の詳細")
    c.add_argument("id", nargs="?")
    c.set_defaults(fn=cmd_jobs)
    c = sub.add_parser("plan", help="保存した計画を表示、または --run で実行")
    c.add_argument("id")
    c.add_argument("--run", action="store_true")
    c.add_argument("--apply", action="store_true")
    c.set_defaults(fn=cmd_plan)
    c = sub.add_parser("mock-reset", help="モックの状態を初期化する")
    c.set_defaults(fn=cmd_mock_reset)
    c = sub.add_parser("serve", help="ローカル Web アプリを起動する(127.0.0.1 のみ)")
    c.add_argument("--port", type=int, default=8765)
    c.add_argument("--no-browser", action="store_true")
    c.set_defaults(fn=cmd_serve)
    c = sub.add_parser("selftest", help="通しの自己テスト(--hardware で実機テスト手順)")
    c.add_argument("--hardware", action="store_true")
    c.add_argument("--report", help="レポートの書き出し先(既定: ~/.qctone/selftest/)")
    c.add_argument("--start-at", type=int, default=1, help="この手順番号から再開する")
    c.set_defaults(fn=cmd_selftest)

    for name in MODULES:
        module = importlib.import_module(f"qcsuite.features.{name}")
        module.add_cli(sub)
    return p


def main(argv=None):
    a = build_parser().parse_args(argv)
    ctx = Ctx(mock=a.mock, allow_folders=a.allow_folder, yes=a.yes, json=a.json)
    paths.ensure(paths.home(a.mock))
    try:
        a.fn(ctx, a)
    except QcsuiteError as exc:
        raise SystemExit(f"エラー: {exc}") from None
    except qdevice.PatchError as exc:
        raise SystemExit(f"パッチを拒否しました(何も送っていません): {exc}") from None
    except KeyboardInterrupt:
        raise SystemExit("中断しました。ジョブは --resume で再開できます。") from None


if __name__ == "__main__":
    main()
