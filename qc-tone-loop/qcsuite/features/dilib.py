"""DI library (idea G): import, record, trim and list the DIs every reamp uses.

Recording takes Input 1's dry signal over USB 1 (qctone's ``record_dry``) - the
player plays, the machine just captures and files it. The synthetic DI is
always available for trying the mock.
"""

from __future__ import annotations

import sys

from qcsuite.audio import di


def record(rig, name: str, seconds: float, note: str = "") -> dict:
    """Record ``seconds`` of the dry Input 1 into the library."""
    if rig.mock:
        audio = di.synth_di(seconds)
        return di.save(name, audio, di.SR, True, source="mock-record",
                       note=note or "モックの録音(合成 DI)")
    from qctone import audio_io
    from qctone.features import load
    tmp = di.library_dir(False) / f".recording-{name}.wav"
    audio_io.record_dry(tmp, seconds)
    data, sr = load(tmp)
    tmp.unlink()
    return di.save(name, data, sr, False, source="record:Input 1", note=note)


def cmd_list(ctx, a):
    di.ensure_synthetic(ctx.mock)
    rows = di.listing(ctx.mock)
    ctx.out(rows, "\n".join(f"{r['name']:<28} {r['seconds']:>6.1f} s  {r['peak_dbfs']:>6.1f} dBFS"
                            f"  {r['source']}  {r.get('note', '')}" for r in rows))


def cmd_import(ctx, a):
    meta = di.import_file(a.file, a.name, ctx.mock, a.start, a.dur, a.note or "")
    ctx.out(meta, f"取り込みました: {meta['name']}({meta['seconds']} 秒)")


def cmd_record(ctx, a):
    with ctx.rig("di record") as rig:
        if not rig.mock:
            print(f"{a.seconds} 秒、Input 1 のドライ信号を録音します。弾き始めてください。",
                  file=sys.stderr)
        meta = record(rig, a.name, a.seconds, a.note or "")
    ctx.out(meta, f"録音しました: {meta['name']}({meta['seconds']} 秒, ピーク {meta['peak_dbfs']} dBFS)")


def cmd_trim(ctx, a):
    meta = di.trim(a.name, ctx.mock, a.start, a.dur, a.new_name)
    ctx.out(meta, f"切り出しました: {meta['name']}({meta['seconds']} 秒)")


def add_cli(sub):
    p = sub.add_parser("di", help="DI ライブラリ(一覧・取り込み・録音・切り出し)")
    s = p.add_subparsers(dest="di_cmd", required=True)
    c = s.add_parser("list", help="DI の一覧")
    c.set_defaults(fn=cmd_list)
    c = s.add_parser("import", help="音声ファイルを DI として取り込む(48 kHz に変換)")
    c.add_argument("file")
    c.add_argument("--name")
    c.add_argument("--start", type=float, default=0.0)
    c.add_argument("--dur", type=float)
    c.add_argument("--note")
    c.set_defaults(fn=cmd_import)
    c = s.add_parser("record", help="Input 1 のドライ信号を USB で録音する")
    c.add_argument("name")
    c.add_argument("--seconds", type=float, default=12.0)
    c.add_argument("--note")
    c.set_defaults(fn=cmd_record)
    c = s.add_parser("trim", help="DI の一部を切り出して別名で保存")
    c.add_argument("name")
    c.add_argument("new_name")
    c.add_argument("--start", type=float, required=True)
    c.add_argument("--dur", type=float, required=True)
    c.set_defaults(fn=cmd_trim)
