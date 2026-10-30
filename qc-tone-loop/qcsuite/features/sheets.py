"""Preset sheets (feature 7), scene maps (idea C) and share packs (idea N).

A sheet is what a player tapes inside the gig bag or sends a bandmate: the
chain as a diagram, every knob in real units, the scene names, and a scene map
- which block is on in which scene, and which knobs change per scene. Output:

* **HTML** - one printable document (A4, a page per preset), opened in a
  browser and printed or saved as PDF from there;
* **PDF** - drawn with matplotlib, one page per preset (plus continuation
  pages for long knob lists), with a Japanese font when one is installed;
* **share pack** - a zip of the normalized JSON, the sheet, renders if any,
  and a Japanese README. It cannot be loaded into a Quad Cortex: the unit has
  no import for it, so a bandmate rebuilds the preset by hand from the sheet,
  or with ``qcsuite backup restore`` into a sandbox - and in both cases loads
  captures, cab mics and IRs on the unit's screen, because those do not change
  the sound when written from a computer (measured on the player's unit).

Everything works from normalized presets (:mod:`qcsuite.presets`), which come
from the backup, the live grid, or a recall (:mod:`.sheets_sources`).
"""

from __future__ import annotations

import html
import json
import sys
import tempfile
import time
import zipfile
from pathlib import Path

from qcsuite import paths, presets
from qcsuite.audio import export
from qcsuite.device import guard
from qcsuite.features import sheets_sources as src

SCENES = "ABCDEFGH"
CATEGORY_COLORS = {
    "Guitar Amplifier": "#f3c9a7", "Bass Amplifier": "#f3c9a7",
    "Guitar Overdrive": "#f6e3a1", "Bass Overdrive": "#f6e3a1",
    "Neural Capture": "#e8b9d9", "Neural Capture V2": "#e8b9d9",
    "Reverb": "#b9d7f0", "Delay": "#c7e3d4", "Modulation": "#d7cdf2",
    "Compressor": "#e3e3e3", "Equalizer": "#e3e3e3", "Utility": "#eeeeee",
}
REACHES_OUT = {"XLR_1_2", "XLR_1", "XLR_2", "MULTIPLE"}


def _e(v) -> str:
    return html.escape("" if v is None else str(v))


def _color(b: dict) -> str:
    cat = b.get("category") or ""
    if cat.startswith("Cabsim") or cat == "IRLoaders":
        return "#d9cfc4"
    return CATEGORY_COLORS.get(cat, "#f0f0f0")


def block_notes(b: dict) -> list[str]:
    """What a reader must know about a block: what will not follow a host edit."""
    notes = []
    if b.get("inert_plugin"):
        notes.append(f"未所有のプラグイン({b['inert_plugin']}): 本体で鳴らすにはプラグインが必要")
    if b.get("capture"):
        notes.append(f"キャプチャー「{b['capture']}」: パソコンから変えても音は変わらない。本体で読み込む")
    if b.get("mic"):
        notes.append(f"マイク: {b['mic']}(パソコンからの変更は音に反映されない)")
    if b.get("ir"):
        notes.append(f"IR: {b['ir']}")
    return notes


def _all_bypassed(b: dict) -> bool:
    bp = b.get("bypass") or {}
    scenes = bp.get("scenes") or []
    return bool(scenes) and all(scenes)


def _used_rows(n: dict) -> list[dict]:
    rows = []
    for r in n["rows"]:
        blocks = [b for b in n["blocks"] if b["row"] == r["row"]]
        if blocks or r["in_port"] != "EMPTY" or r["out_port"] != "EMPTY":
            rows.append(r)
    return rows


# -- chain diagram (SVG) ----------------------------------------------------------

def chain_svg(n: dict) -> str:
    """The grid as an inline SVG: one lane per used row, blocks at their columns."""
    rows = _used_rows(n)
    if not rows:
        return '<p class="muted">(ブロックがありません)</p>'
    cell_w, cell_h, box_w, box_h = 104, 64, 94, 38
    left, right = 86, 92
    width = left + 8 * cell_w + right
    height = 16 + len(rows) * cell_h
    y_of = {r["row"]: 16 + i * cell_h + box_h / 2 for i, r in enumerate(rows)}
    parts = [f'<svg class="chain" viewBox="0 0 {width} {height}" width="100%" '
             f'role="img" aria-label="{_e(n.get("name"))} のチェーン" '
             'xmlns="http://www.w3.org/2000/svg">',
             '<defs><marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" '
             'markerHeight="6" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="#777"/></marker></defs>']
    for r in rows:
        y = y_of[r["row"]]
        blocks = sorted((b for b in n["blocks"] if b["row"] == r["row"]), key=lambda b: b["col"])
        parts.append(f'<text x="4" y="{y + 4}" font-size="11" fill="#333">行{r["row"] + 1} '
                     f'{_e(r["in_port"])}</text>')
        xs = [left + b["col"] * cell_w for b in blocks]
        prev = left - 6
        for b, x in zip(blocks, xs):
            parts.append(f'<line x1="{prev}" y1="{y}" x2="{x}" y2="{y}" stroke="#777" '
                         'stroke-width="1.5" marker-end="url(#arr)"/>')
            inert = bool(b.get("inert_plugin") or b.get("capture"))
            style = 'stroke-dasharray="4 3" opacity="0.55"' if _all_bypassed(b) else ""
            stroke = "#c2392b" if inert else "#555"
            label = b["model"] if len(b["model"]) <= 15 else b["model"][:14] + "…"
            title = "\n".join([b["model"]] + [f"{k}: {v}" for k, v in presets.main_knobs(b, 30)])
            parts.append(
                f'<g><title>{_e(title)}</title><rect x="{x}" y="{y - box_h / 2}" width="{box_w}" '
                f'height="{box_h}" rx="6" fill="{_color(b)}" stroke="{stroke}" '
                f'stroke-width="{2 if inert else 1}" {style}/>'
                f'<text x="{x + box_w / 2}" y="{y - 2}" font-size="10.5" text-anchor="middle" '
                f'fill="#111">{_e(label)}</text>'
                f'<text x="{x + box_w / 2}" y="{y + 12}" font-size="9" text-anchor="middle" '
                f'fill="#555">列{b["col"] + 1}{" バイパス" if _all_bypassed(b) else ""}</text></g>')
            prev = x + box_w
        out_x = left + 8 * cell_w
        lane = r.get("lane", {})
        db = (lane.get("VOLUME") or {}).get("db")
        muted = (lane.get("MUTE") or {}).get("value") or 0
        parts.append(f'<line x1="{prev}" y1="{y}" x2="{out_x}" y2="{y}" stroke="#777" '
                     'stroke-width="1.5" marker-end="url(#arr)"/>')
        out_label = r["out_port"] + (f" {db:+.1f}dB" if isinstance(db, (int, float)) else "")
        if muted >= 0.5:
            out_label += " ミュート"
        parts.append(f'<text x="{out_x + 4}" y="{y + 4}" font-size="11" fill="#333">'
                     f'{_e(out_label)}</text>')
    for s in n.get("splits", []):
        lane_row = s["row"] + 1
        if s["row"] in y_of and lane_row in y_of:
            x = left + s["split_column"] * cell_w - 4
            parts.append(f'<line x1="{x}" y1="{y_of[s["row"]]}" x2="{x}" y2="{y_of[lane_row]}" '
                         'stroke="#2f6fdf" stroke-width="1.5" marker-end="url(#arr)"/>')
            if s["mix_column"] >= 0:
                mx = left + s["mix_column"] * cell_w - 4
                parts.append(f'<line x1="{mx}" y1="{y_of[lane_row]}" x2="{mx}" y2="{y_of[s["row"]]}"'
                             ' stroke="#2f6fdf" stroke-width="1.5" marker-end="url(#arr)"/>')
    parts.append("</svg>")
    return "".join(parts)


# -- scene map (idea C) -------------------------------------------------------------

def scene_map(n: dict) -> dict:
    """Blocks x scenes: on/bypassed per scene, and knobs whose value differs per scene."""
    labels = [(n.get("scene_labels") or [""] * 8)[i] or "" for i in range(8)]
    rows = []
    for b in n["blocks"]:
        bp = b.get("bypass") or {"scene_mode": False, "scenes": [False] * 8}
        scenes = list(bp.get("scenes") or [False] * 8)[:8]
        rows.append({"kind": "bypass", "block": presets.block_label(b),
                     "scene_mode": bool(bp.get("scene_mode")),
                     "cells": ["—" if s else "●" for s in scenes]})
        for name, entry in b["params"].items():
            values = entry.get("scenes")
            if not values or len({v for v in values if v is not None}) < 2:
                continue
            rows.append({"kind": "param", "block": presets.block_label(b), "param": name,
                         "cells": ["" if v is None else f"{v:.2f}" if isinstance(v, float) else str(v)
                                   for v in values[:8]]})
    used = any(r["kind"] == "param" for r in rows) or any(
        r["scene_mode"] and len(set(r["cells"])) > 1 for r in rows if r["kind"] == "bypass")
    return {"labels": labels, "rows": rows, "scenes_used": used,
            "default_scene": n.get("default_scene")}


def _scene_map_html(n: dict) -> str:
    m = scene_map(n)
    if not m["rows"]:
        return ""
    head = "".join(f"<th>{s}<br><small>{_e(l)}</small></th>" for s, l in zip(SCENES, m["labels"]))
    body = []
    for r in m["rows"]:
        if r["kind"] == "bypass":
            name = _e(r["block"]) + ("" if r["scene_mode"] else ' <small class="muted">(全シーン共通)</small>')
        else:
            name = f'<span class="muted">└ {_e(r["param"])}</span>'
        body.append(f"<tr><td>{name}</td>" + "".join(f"<td>{_e(c)}</td>" for c in r["cells"]) + "</tr>")
    note = ("<p class='small muted'>● = 有効、— = バイパス。数値は 0〜1 の正規化値(シーンごとに違うものだけ)。"
            f"既定のシーン: {_e(m['default_scene'])}</p>")
    return (f"<h3>シーン早見表</h3><table class='scene'><thead><tr><th>ブロック</th>{head}</tr></thead>"
            f"<tbody>{''.join(body)}</tbody></table>{note}")


# -- HTML ----------------------------------------------------------------------------

_CSS = """
body { font-family: -apple-system, "Hiragino Sans", "Noto Sans JP", "IPAGothic", sans-serif;
  color: #111; margin: 16px; font-size: 13px; }
h1 { font-size: 20px; margin: 0 0 4px; } h2 { font-size: 17px; margin: 0 0 4px; }
h3 { font-size: 14px; margin: 14px 0 6px; }
.muted { color: #666; } small { font-size: 11px; }
.meta { color: #444; margin-bottom: 8px; }
table { border-collapse: collapse; width: 100%; margin: 4px 0 8px; }
th, td { border: 1px solid #ccc; padding: 3px 6px; text-align: left; vertical-align: top; }
th { background: #f3f3f3; }
table.scene td { text-align: center; } table.scene td:first-child { text-align: left; }
.blocks { display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); gap: 8px; }
.block { border: 1px solid #ccc; border-radius: 6px; padding: 6px 8px; break-inside: avoid; }
.block h4 { margin: 0 0 4px; font-size: 13px; }
.block table td, .block table th { border: none; border-bottom: 1px solid #eee; padding: 1px 4px; }
.note { color: #a3271b; font-size: 11px; }
section.preset { page-break-after: always; break-after: page; }
section.preset:last-child { page-break-after: auto; break-after: auto; }
.cover { margin-bottom: 12px; }
.chainwrap { overflow-x: auto; } .chainwrap svg { min-width: 720px; }
@page { size: A4; margin: 12mm; }
@media print { body { margin: 0; } .noprint { display: none; } .chainwrap svg { min-width: 0; } }
"""


def _block_html(b: dict) -> str:
    knobs = presets.main_knobs(b, limit=24)
    rows = "".join(f"<tr><td>{_e(k)}</td><td>{_e(v)}</td></tr>" for k, v in knobs)
    notes = "".join(f"<div class='note'>{_e(t)}</div>" for t in block_notes(b))
    state = " (バイパス)" if _all_bypassed(b) else ""
    return (f"<div class='block'><h4>{_e(presets.block_label(b))}{state}</h4>"
            f"<div class='muted small'>{_e(b.get('category'))}</div>"
            f"<table>{rows}</table>{notes}</div>")


def _rows_html(n: dict) -> str:
    out = []
    for r in _used_rows(n):
        lane = r.get("lane", {})
        db = (lane.get("VOLUME") or {}).get("db")
        mute = (lane.get("MUTE") or {}).get("value") or 0
        out.append(f"<tr><td>行{r['row'] + 1}</td><td>{_e(r['in_port'])}</td><td>{_e(r['out_port'])}</td>"
                   f"<td>{'—' if db is None else f'{db:+.1f} dB'}</td>"
                   f"<td>{'ミュート' if mute >= 0.5 else ''}</td></tr>")
    return ("<table><thead><tr><th>行</th><th>入力</th><th>出力</th><th>出力レベル</th><th></th>"
            f"</tr></thead><tbody>{''.join(out)}</tbody></table>")


def preset_section(n: dict) -> str:
    where = src.ref_text(n.get("folder"), n.get("position"))
    labels = [l for l in n.get("scene_labels", []) if l]
    meta = [f"場所: {_e(where)}", f"既定のシーン: {_e(n.get('default_scene'))}"]
    if n.get("tags"):
        meta.append("タグ: " + _e(", ".join(n["tags"])))
    scene_line = ("シーン名: " + " / ".join(f"{s}={_e(l)}" for s, l in zip(SCENES, n.get("scene_labels", []))
                                          if l)) if labels else "シーン名: (なし)"
    return (f"<section class='preset'><h2>{_e(n.get('name') or '(名前なし)')}</h2>"
            f"<div class='meta'>{' ・ '.join(meta)}<br>{scene_line}</div>"
            f"<div class='chainwrap'>{chain_svg(n)}</div><h3>行の入出力</h3>{_rows_html(n)}"
            f"<h3>ブロックとノブ</h3><div class='blocks'>"
            f"{''.join(_block_html(b) for b in n['blocks']) or '<p class=muted>(ブロックなし)</p>'}</div>"
            f"{_scene_map_html(n)}</section>")


def sheet_html(items: list[dict], title: str = "プリセット資料") -> str:
    """A complete, printable HTML document for one or more normalized presets."""
    stamp = time.strftime("%Y-%m-%d %H:%M")
    toc = ""
    if len(items) > 1:
        toc = "<ol class='small'>" + "".join(
            f"<li>{_e(n.get('name'))} — {_e(src.ref_text(n.get('folder'), n.get('position')))}</li>"
            for n in items) + "</ol>"
    return (f"<!doctype html><html lang='ja'><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width, initial-scale=1'>"
            f"<title>{_e(title)}</title><style>{_CSS}</style></head><body>"
            f"<div class='cover'><h1>{_e(title)}</h1><div class='muted small'>qcsuite で作成 {stamp}。"
            "値は本体から読んだもの。キャプチャー・マイク・IR は名前だけで、音は本体で確かめてください。"
            f"</div>{toc}<p class='noprint small muted'>印刷するとプリセットごとに改ページされます(A4)。</p>"
            f"</div>{''.join(preset_section(n) for n in items)}</body></html>")


# -- PDF ------------------------------------------------------------------------------

JP_FONTS = ["Hiragino Sans", "Hiragino Kaku Gothic ProN", "Hiragino Kaku Gothic Pro",
            "IPAGothic", "IPAexGothic", "IPAPGothic", "Noto Sans CJK JP", "Noto Sans JP",
            "Yu Gothic", "TakaoGothic"]


def japanese_font() -> str | None:
    from matplotlib import font_manager
    names = {f.name for f in font_manager.fontManager.ttflist}
    return next((f for f in JP_FONTS if f in names), None)


def _pdf_lines(n: dict) -> list[str]:
    lines = []
    for r in _used_rows(n):
        lane = r.get("lane", {})
        db = (lane.get("VOLUME") or {}).get("db")
        lines.append(f"行{r['row'] + 1}: {r['in_port']} → {r['out_port']}"
                     + (f"  出力 {db:+.1f} dB" if isinstance(db, (int, float)) else ""))
    lines.append("")
    for b in n["blocks"]:
        lines.append(f"■ {presets.block_label(b)}" + (" (バイパス)" if _all_bypassed(b) else ""))
        for k, v in presets.main_knobs(b, limit=24):
            lines.append(f"    {k}: {v}")
        for note in block_notes(b):
            lines.append(f"    ※ {note}")
    m = scene_map(n)
    if m["scenes_used"]:
        lines.append("")
        lines.append("シーン早見表 (●有効 —バイパス): " + " ".join(
            f"{s}{'=' + l if l else ''}" for s, l in zip(SCENES, m["labels"])))
        for r in m["rows"]:
            name = r["block"] if r["kind"] == "bypass" else f"  {r['param']}"
            lines.append(f"  {name[:30]:<30} " + " ".join(f"{c:>4}" for c in r["cells"]))
    return lines


def _draw_chain(ax, n: dict):
    from matplotlib.patches import FancyBboxPatch
    rows = _used_rows(n)
    ax.set_xlim(0, 11)
    ax.set_ylim(-len(rows) - 0.2, 0.6)
    ax.axis("off")
    for i, r in enumerate(rows):
        y = -i - 0.2
        ax.text(0.0, y, f"行{r['row'] + 1}\n{r['in_port']}", fontsize=7, va="center")
        blocks = sorted((b for b in n["blocks"] if b["row"] == r["row"]), key=lambda b: b["col"])
        prev = 0.95
        for b in blocks:
            x = 1.1 + b["col"] * 1.15
            ax.annotate("", xy=(x, y), xytext=(prev, y),
                        arrowprops={"arrowstyle": "->", "color": "#777", "lw": 0.8})
            inert = bool(b.get("inert_plugin") or b.get("capture"))
            ax.add_patch(FancyBboxPatch((x, y - 0.28), 1.0, 0.56, boxstyle="round,pad=0.02",
                                        fc=_color(b), ec="#c2392b" if inert else "#555",
                                        lw=1.4 if inert else 0.6,
                                        ls="--" if _all_bypassed(b) else "-"))
            label = b["model"] if len(b["model"]) <= 13 else b["model"][:12] + "…"
            ax.text(x + 0.5, y, label, fontsize=6, ha="center", va="center")
            prev = x + 1.0
        ax.annotate("", xy=(10.35, y), xytext=(prev, y),
                    arrowprops={"arrowstyle": "->", "color": "#777", "lw": 0.8})
        ax.text(10.4, y, r["out_port"], fontsize=7, va="center")


def sheet_pdf(items: list[dict], path: Path, title: str = "プリセット資料") -> dict:
    """One A4 page per preset (plus continuation pages). Returns path, font, pages."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages

    font = japanese_font()
    family = [font, "DejaVu Sans"] if font else ["DejaVu Sans"]
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pages = 0
    per_page = 64
    with plt.rc_context({"font.family": family, "pdf.fonttype": 42}), PdfPages(path) as pdf:
        for n in items:
            lines = _pdf_lines(n)
            chunks = [lines[i:i + per_page] for i in range(0, max(len(lines), 1), per_page)] or [[]]
            for k, chunk in enumerate(chunks):
                fig = plt.figure(figsize=(8.27, 11.69))
                header = f"{n.get('name') or '(名前なし)'} — {src.ref_text(n.get('folder'), n.get('position'))}"
                fig.text(0.06, 0.965, header + ("" if k == 0 else f"(続き {k + 1})"), fontsize=13)
                fig.text(0.06, 0.945, f"{title} / qcsuite {time.strftime('%Y-%m-%d')}"
                         + ("" if font else " / 日本語フォントが見つからないため一部文字が欠けることがあります"),
                         fontsize=7, color="#666")
                top = 0.93
                if k == 0:
                    used = max(1, len(_used_rows(n)))
                    height = min(0.3, 0.07 * used + 0.03)
                    _draw_chain(fig.add_axes([0.04, top - height, 0.92, height]), n)
                    top -= height + 0.02
                fig.text(0.06, top, "\n".join(chunk), fontsize=7.2, va="top", linespacing=1.35)
                pdf.savefig(fig)
                plt.close(fig)
                pages += 1
    return {"file": str(path), "font": font or "DejaVu Sans", "pages": pages,
            "note": "" if font else "日本語フォントが見つかりませんでした(DejaVu Sans で代用)。"}


# -- share pack (idea N) -----------------------------------------------------------------

README = """qcsuite 共有パック
==================

作成: {stamp}
内容: {count} 個のプリセット

このパックに入っているもの
- presets/*.json  : プリセットの中身(qcsuite.preset/1 形式。ブロック、ノブの値、ルーティング、シーン)
- sheet.html      : 資料(ブラウザで開いて印刷できます)
{pdf_line}{audio_line}
大事な注意
- このパックは Quad Cortex に直接読み込めません。本体にも Cortex Control にも、この形式の読み込み機能はありません。
- 再現するには、次のどちらかで作り直してください。
  1. 手で作る: sheet.html のチェーン図とノブの表を見ながら、本体のグリッドで同じブロックを置き、値を合わせる。
  2. qcsuite を使う: presets/*.json を自分の qcsuite のバックアップフォルダに置き、
     `qcsuite backup restore` で sandbox(_qcsuite_sandbox)に復元する。元のフォルダには書き込みません。
- キャプチャー、キャビのマイク、IR は、名前だけが記録されています。パソコンから書き込んでも音に反映されない
  (作成者の本体で確認済み)ので、必ず本体の画面で同じものを読み込んでください。
- プラグイン(Plini、Gojira などのアーティストモデル)のブロックは、そのプラグインを持っていないと音が素通しになります。
- タグ(Clean、Crunch など)は引き継がれません。
- 音量は環境で変わります。最後は自分の耳で合わせてください。

作成した qcsuite は非公式のツールで、Neural DSP とは関係ありません。
"""


def share_pack(items: list[dict], out_zip: Path, *, pdf: bool = False,
               audio_files: list[str] = (), renders: list[dict] = (),
               title: str = "共有パック") -> dict:
    """Write the zip. ``renders`` are ``{"file", "name", "excluded", "reasons"}`` dicts."""
    out_zip = Path(out_zip)
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    manifest = {"created": time.strftime("%Y-%m-%d %H:%M:%S"), "title": title,
                "presets": [], "audio": []}
    used_names: set[str] = set()

    def unique(name: str) -> str:
        base, n = name, 1
        while name in used_names:
            n += 1
            name = f"{Path(base).stem}_{n}{Path(base).suffix}"
        used_names.add(name)
        return name

    pdf_info = None
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as z:
        for n in items:
            slot = n.get("slot") or "current"
            arc = unique(f"presets/{export.safe_name(slot)}_{export.safe_name(n.get('name') or 'preset')}.json")
            clean = {k: v for k, v in n.items() if not k.startswith("_")}
            z.writestr(arc, json.dumps(clean, ensure_ascii=False, indent=2) + "\n")
            manifest["presets"].append({"name": n.get("name"), "where": src.ref_text(
                n.get("folder"), n.get("position")), "file": arc, "fingerprint": n.get("fingerprint")})
        z.writestr("sheet.html", sheet_html(items, title))
        if pdf:
            with tempfile.TemporaryDirectory() as tmp:
                pdf_info = sheet_pdf(items, Path(tmp) / "sheet.pdf", title)
                z.write(pdf_info["file"], "sheet.pdf")
            pdf_info["file"] = "sheet.pdf (zip 内)"
        for f in list(audio_files):
            p = Path(f)
            if not p.is_file():
                raise FileNotFoundError(f"音声ファイルがありません: {f}")
            arc = unique(f"audio/{p.name}")
            z.write(p, arc)
            manifest["audio"].append({"file": arc, "source": str(p)})
        for r in renders:
            p = Path(r["file"])
            arc = unique(f"audio/{p.name}")
            z.write(p, arc)
            manifest["audio"].append({"file": arc, "preset": r.get("name"),
                                      "excluded": r.get("excluded", False),
                                      "reasons": r.get("reasons", [])})
        audio_line = ""
        if manifest["audio"]:
            audio_line = ("- audio/*        : 同じ DI を通して録った音(リアンプ)。素通しの疑いがあるものは "
                          "manifest.json の excluded が true\n")
        z.writestr("README.txt", README.format(
            stamp=manifest["created"], count=len(items),
            pdf_line="- sheet.pdf       : 資料の PDF 版\n" if pdf else "", audio_line=audio_line))
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return {"file": str(out_zip), "presets": len(items), "audio": len(manifest["audio"]),
            "pdf": pdf_info}


def render_for_pack(rig, refs: list[tuple[str, int]], di_name: str, mock: bool,
                    out_dir: Path) -> list[dict]:
    """Reamp each preset through one DI for the pack (recalls each one; the unit makes sound)."""
    from qcsuite.audio import di as di_mod
    from qcsuite.audio import reamp
    from qctone import features
    audio, sr = di_mod.load(di_name, mock)
    dry = features.analyze_array(audio[:, None], sr)
    out = []
    for i, (key, position) in enumerate(refs, 1):
        info = reamp.render_preset(rig, key, position, audio, sr, dry,
                                   out_path=out_dir / f"{i:02d}_{export.safe_name(di_name)}__"
                                                      f"{export.safe_name(guard.folder_name(key))}"
                                                      f"_{position}.flac")
        out.append({"file": info["file"], "name": info["preset"]["name"],
                    "excluded": info["excluded"]["excluded"],
                    "reasons": info["excluded"]["reasons"]})
    return out


# -- CLI ------------------------------------------------------------------------------------

def _selection(ctx, a, device) -> list[dict]:
    refs = [src.parse_ref(r) for r in (a.preset or [])]
    if not (refs or a.folder or a.current or a.from_backup):
        raise SystemExit("--preset フォルダ:スロット、--folder、--current、--from-backup のどれかを指定してください。")
    if not a.from_backup:
        targets = list(refs) + (src.folder_refs(device, a.folder) if a.folder else [])
        recalls = src.needs_recall(targets, ctx.mock)
        if recalls:
            print(f"{recalls} 個のプリセットを本体に読み込みます(音が切り替わり、本体で保存していない"
                  "編集は消えます)。バックアップがあれば --from-backup で読み込みなしに作れます。",
                  file=sys.stderr)
            if not ctx.ask("続けますか?"):
                raise SystemExit("中止しました。")
    items = src.collect(device, mock=ctx.mock, refs=refs, folder=a.folder,
                        current_grid=a.current, from_backup=a.from_backup)
    if not items:
        raise SystemExit("対象のプリセットがありません(--from-backup ならバックアップがあるか確認)。")
    return items


def _out_dir(ctx, a) -> Path:
    return paths.ensure(Path(a.out)) if a.out else paths.sub("sheets", ctx.mock)


def _stem(items: list[dict]) -> str:
    if len(items) == 1:
        return export.safe_name(items[0].get("name") or "preset")
    return f"sheets-{time.strftime('%Y%m%d-%H%M%S')}"


def cmd_html(ctx, a):
    with ctx.rig("sheets html") as rig:
        items = _selection(ctx, a, rig.device)
    path = _out_dir(ctx, a) / f"{_stem(items)}.html"
    path.write_text(sheet_html(items, a.title or "プリセット資料"))
    ctx.out({"file": str(path), "presets": len(items)},
            f"資料を書き出しました: {path}({len(items)} 件)。ブラウザで開いて印刷できます。")


def cmd_pdf(ctx, a):
    with ctx.rig("sheets pdf") as rig:
        items = _selection(ctx, a, rig.device)
    info = sheet_pdf(items, _out_dir(ctx, a) / f"{_stem(items)}.pdf", a.title or "プリセット資料")
    ctx.out(info, f"PDF を書き出しました: {info['file']}({info['pages']} ページ、フォント {info['font']})"
            + (f"\n{info['note']}" if info["note"] else ""))


def cmd_pack(ctx, a):
    renders = []
    out_dir = _out_dir(ctx, a)
    with ctx.rig("sheets pack") as rig:
        items = _selection(ctx, a, rig.device)
        if a.render_di:
            refs = [(n["folder"], n["position"]) for n in items
                    if n.get("folder") is not None and n.get("position") is not None]
            ctx.warn_monitor(rig.mock)
            renders = render_for_pack(rig, refs, a.render_di, ctx.mock,
                                      paths.ensure(out_dir / f"renders-{time.strftime('%Y%m%d-%H%M%S')}"))
    info = share_pack(items, out_dir / f"{_stem(items)}-pack.zip", pdf=a.pdf,
                      audio_files=a.audio or [], renders=renders, title=a.title or "共有パック")
    ctx.out(info, f"共有パックを作りました: {info['file']}(プリセット {info['presets']}、音声 {info['audio']})")


def _common(c):
    c.add_argument("--preset", action="append", metavar="FOLDER:SLOT",
                   help="対象プリセット(例 kami:1E)。複数可")
    c.add_argument("--folder", help="フォルダ全体")
    c.add_argument("--current", action="store_true", help="本体の現在のグリッド(副作用なし)")
    c.add_argument("--from-backup", action="store_true",
                   help="最新のバックアップから作る(本体の読み込みなし)")
    c.add_argument("--out", help="書き出し先フォルダ(既定: ~/.qctone/sheets)")
    c.add_argument("--title")


def add_cli(sub):
    p = sub.add_parser("sheets", help="プリセットの資料(HTML / PDF)と共有パック")
    s = p.add_subparsers(dest="sheets_cmd", required=True)
    c = s.add_parser("html", help="印刷できる HTML 資料(チェーン図、ノブ、シーン早見表)")
    _common(c)
    c.set_defaults(fn=cmd_html)
    c = s.add_parser("pdf", help="PDF 資料(1 プリセット 1 ページ)")
    _common(c)
    c.set_defaults(fn=cmd_pdf)
    c = s.add_parser("pack", help="共有パック(JSON + 資料 + 音 + README の zip)")
    _common(c)
    c.add_argument("--pdf", action="store_true", help="PDF も入れる")
    c.add_argument("--audio", action="append", help="入れる音声ファイル(複数可)")
    c.add_argument("--render-di", help="この DI でいまリアンプして音を入れる(本体から音が出ます)")
    c.set_defaults(fn=cmd_pack)
