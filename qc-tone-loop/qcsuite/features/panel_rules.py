"""Words to grid edits, by rules: 「暗く」「ゲインを 6 に」「ブースト追加」.

The translator only OPERATES. It turns what the player says into a qctone patch
(``qctone/device.py`` docstring) and a readable summary; it never decides that
something would sound better. A phrase it does not understand is reported back
and the whole instruction is refused, so nothing half-understood is sent.

How a clause is read, in order:

1. the target: a block named outright (「Brit 2203 の」), a position (「列3」
   「行2」), a category (「アンプの」「リバーブを」) with an optional ordinal
   (「2 番目の」), or the default for the action (the amp for tone and gain);
2. the action, first match wins: add a block (「ブースト追加」「ディレイを足して」),
   bypass or re-enable (「リバーブを外して」), choose a switch option (「BRIGHT を
   オンに」), set a number (「GAIN を 6 に」), or a relative move (「暗く」「低音を
   減らして」「中域を出して」);
3. intensity words scale relative moves: 「少しだけ」 half, 「もう少し」 three
   quarters, none or 「もっと」 one step (0.1 of the knob, or 3 dB on EQ gains),
   「かなり」 double.

Facts it follows (measured on the player's unit, CorOS 4.0.1): the gain knob is
called GAIN, DRIVE, VOLUME, INPUT GAIN or GAIN 1/2 depending on the block, so it
is looked up in the block's own parameter list; tone knobs are BASS, MID or
MIDDLE, TREBLE, PRESENCE, DEPTH, RESONANCE; switches (parameters with options)
are never moved by raw value - only by option name, or not at all.

Conventions that are choices, not facts: a boost is an overdrive with its gain
low (0.15) and its level high (0.75); "removing" a block bypasses it, which is
reversible; a block is added into a free cell, because nothing can shift the
blocks already on the grid.
"""

from __future__ import annotations

import re
import unicodedata

from qcsuite.device import inert
from qctone import device as qdevice

AMP = ("Guitar Amplifier", "Bass Amplifier")
DRIVE = ("Guitar Overdrive", "Bass Overdrive")
CAPTURE = ("Neural Capture", "Neural Capture V2")

#: Category words. A clause mentioning one targets a block of that category.
CATEGORIES = [
    ("capture", r"キャプチャー|capture", CAPTURE),
    ("cab", r"キャビネット|キャビ|\bcab\b|cabinet", ("Cabsim",)),
    ("drive", r"オーバードライブ|ドライブペダル|ドライブの|ブースター|ブーストの|歪みペダル|ペダル|"
              r"overdrive|booster|drive pedal", DRIVE),
    ("eq", r"イコライザー|EQ|\beq\b", ("Equalizer",)),
    ("delay", r"ディレイ|エコー|delay", ("Delay",)),
    ("reverb", r"リバーブ|残響|reverb", ("Reverb",)),
    ("comp", r"コンプレッサー|コンプ|compressor|\bcomp\b", ("Compressor",)),
    ("mod", r"コーラス|フランジャー|フェイザー|トレモロ|モジュレーション|chorus|flanger|phaser|"
            r"tremolo", ("Modulation",)),
    ("gate", r"ゲート|\bgate\b", ("Utility",)),
    ("amp", r"アンプ|\bamp\b", AMP),
]
CATEGORY_LABEL = {"capture": "キャプチャー", "cab": "キャビ", "drive": "オーバードライブ",
                  "eq": "EQ", "delay": "ディレイ", "reverb": "リバーブ", "comp": "コンプ",
                  "mod": "モジュレーション", "gate": "ゲート", "amp": "アンプ"}

#: Blocks the add action places, best first; the first one in the catalog wins.
ADD_MODELS = {
    "drive": ["Green 808", "Myth Drive", "Chief SD1", "OD250"],
    "delay": ["Analog Delay M", "Simple Delay M", "Digital Delay M"],
    "reverb": ["Hall", "Room", "Plate"],
    "mod": ["Vintage Chorus", "Dual Chorus"],
    "comp": ["Opto Comp M", "VCA Comp M"],
    "eq": ["Parametric 3"],
}
ADD_CATEGORY = {"drive": DRIVE, "delay": ("Delay",), "reverb": ("Reverb",),
                "mod": ("Modulation",), "comp": ("Compressor",), "eq": ("Equalizer",)}

GAIN_KNOBS = ("GAIN", "DRIVE", "INPUT GAIN", "GAIN 1")
VOLUME_KNOBS = ("MASTER", "VOLUME", "LEVEL")

#: Spoken names of knobs -> the parameter names they may mean, in order.
ALIASES = {
    "gain": r"ゲイン|gain",
    "treble": r"高音|高域|トレブル|ハイ|treble",
    "bass": r"低音|低域|ロー|ベース|bass",
    "mid": r"中域|ミッド|ミドル|mids?|middle",
    "presence": r"プレゼンス|presence",
    "volume": r"音量|ボリューム|volume|マスター|master",
    "mix": r"ミックス|かかり|mix",
    "decay": r"ディケイ|decay",
    "time": r"タイム|time",
    "feedback": r"フィードバック|feedback",
    "level": r"レベル|level",
    "depth": r"デプス|depth",
    "resonance": r"レゾナンス|resonance",
    "threshold": r"スレッショルド|threshold",
}
KNOB_PARAMS = {
    "treble": ("TREBLE",), "bass": ("BASS",), "mid": ("MID", "MIDDLE"),
    "presence": ("PRESENCE",), "mix": ("MIX",), "decay": ("DECAY",), "time": ("TIME",),
    "feedback": ("FEEDBACK",), "level": ("LEVEL",), "depth": ("DEPTH",),
    "resonance": ("RESONANCE",), "threshold": ("THRESHOLD",),
}
#: On an EQ block the same words move the band gains (in dB).
EQ_PARAMS = {"treble": ("HIGH GAIN", "8K HZ", "4K HZ"), "bass": ("LOW GAIN", "125 HZ", "63 HZ"),
             "mid": ("MID GAIN", "1K HZ", "500 HZ"), "presence": ("HIGH GAIN", "4K HZ")}

UP = r"上げ|あげ|増や|増し|足し|出し|出す|強く|強め|大きく|上に|ブースト|boost|up|more|raise|increase"
DOWN = (r"下げ|さげ|減ら|減らし|削|抑え|控えめ|弱く|弱め|小さく|カット|引っ込|絞|cut|down|less|"
        r"lower|reduce|decrease")

#: Relative moves without a number: (pattern, knobs, direction).
INTENTS = [
    (r"暗く|こもら|こもっ|丸く|落ち着|darker|\bdark\b|duller", ("treble", "presence"), -1),
    (r"明るく|きらびやか|抜けを?良く|brighter|\bbright\b", ("treble", "presence"), +1),
    (r"(歪み|ゲイン|gain|distortion).*?(" + UP + r")|もっと歪|歪ませ|more gain|more distortion|"
     r"gain up", ("gain",), +1),
    (r"(歪み|ゲイン|gain|distortion).*?(" + DOWN + r")|クリーンに|less gain|less distortion|"
     r"gain down", ("gain",), -1),
    (r"(" + ALIASES["bass"] + r").*?(" + DOWN + r")", ("bass",), -1),
    (r"(" + ALIASES["bass"] + r").*?(" + UP + r")", ("bass",), +1),
    (r"(" + ALIASES["mid"] + r").*?(" + DOWN + r")|ドンシャリ|scoop", ("mid",), -1),
    (r"(" + ALIASES["mid"] + r").*?(" + UP + r"|前に)", ("mid",), +1),
    (r"(" + ALIASES["treble"] + r").*?(" + DOWN + r")", ("treble",), -1),
    (r"(" + ALIASES["treble"] + r").*?(" + UP + r")", ("treble",), +1),
    (r"(" + ALIASES["presence"] + r").*?(" + DOWN + r")", ("presence",), -1),
    (r"(" + ALIASES["presence"] + r").*?(" + UP + r")", ("presence",), +1),
    (r"(" + ALIASES["volume"] + r").*?(" + DOWN + r")|quieter", ("volume",), -1),
    (r"(" + ALIASES["volume"] + r").*?(" + UP + r")|louder", ("volume",), +1),
    (r"(" + ALIASES["mix"] + r").*?(" + DOWN + r")|浅く|薄く|少なめ", ("mix",), -1),
    (r"(" + ALIASES["mix"] + r").*?(" + UP + r")|深く|多めに|濃く|wetter", ("mix",), +1),
]

ADD_VERBS = r"追加|足し|加え|入れ|置い|\badd\b"
BYPASS_ON = r"外し|外す|切って|切る|オフ|\boff\b|バイパス|bypass|止め|無効|ミュート"
BYPASS_OFF = r"戻し|戻す|オンに|有効|\bon\b|復活|生かし"

SMALL = r"少しだけ|ちょっとだけ|ほんの少し|ちょっと|わずかに|slightly|a little|a bit|a touch"
SOMEWHAT = r"もう少し|もうちょっと|やや"
LARGE = r"かなり|大きく|思い切り|思いっきり|ぐっと|大幅|すごく|much|a lot|way more"

NORMAL_STEP = 0.1      # of a knob's 0..1 travel
DB_STEP = 3.0          # on EQ gains

ON_WORDS = {"オン": "on", "on": "on", "オフ": "off", "off": "off", "入": "on", "切": "off"}


class Unclear(ValueError):
    """A clause the rules cannot turn into edits, with the reason."""


def normalize_text(text: str) -> str:
    return unicodedata.normalize("NFKC", text).strip()


def split_clauses(text: str) -> list[str]:
    """Clauses at punctuation, joining words, and after a て-form followed by more
    words (「暗くしてゲインを上げて」). A wrong split only ever produces a clause
    that is not understood, which refuses the instruction - never a silent edit."""
    parts = re.split(r"[、。,;!?！？\n]+|そして|それから|それと|\band then\b|\band\b|"
                     r"(?<=て)(?=[^\s、。]{2,})", text)
    return [p.strip() for p in parts if p and p.strip()]


def intensity(clause: str) -> float:
    if re.search(LARGE, clause, re.I):
        return 2.0
    if re.search(SOMEWHAT, clause, re.I):
        return 0.75
    if re.search(SMALL + r"|少し", clause, re.I):
        return 0.5
    return 1.0


# -- the grid ---------------------------------------------------------------------

def _blocks(grid: dict) -> list[dict]:
    return sorted(grid["blocks"], key=lambda b: (b["row"], b["col"]))


def _category_of(block: dict) -> str:
    return block.get("category") or ""


def _in_category(block: dict, categories) -> bool:
    cat = _category_of(block)
    return any(cat == c or cat.startswith(c) for c in categories)


def _param(block: dict, name: str) -> dict | None:
    return next((p for p in block["params"] if p["name"].upper() == name.upper()), None)


def _label(block: dict) -> str:
    return f"行{block['row'] + 1} 列{block['col'] + 1} {block['model']}"


def _input_rows(grid: dict) -> list[int]:
    return [r["row"] for r in grid["rows"] if r["in_port"] not in ("EMPTY",)]


def gain_knob(block: dict, clause: str = "") -> dict | None:
    """The block's gain control, whatever this block calls it."""
    if re.search(r"(gain|ゲイン)\s*2", clause, re.I) and _param(block, "GAIN 2"):
        return _param(block, "GAIN 2")
    for name in GAIN_KNOBS:
        p = _param(block, name)
        if p is not None:
            return p
    # A non-master-volume amp: its VOLUME is the gain stage.
    if _param(block, "VOLUME") is not None and _param(block, "MASTER") is None:
        return _param(block, "VOLUME")
    return None


def volume_knob(block: dict) -> dict | None:
    gain = gain_knob(block)
    for name in VOLUME_KNOBS:
        p = _param(block, name)
        if p is not None and p is not gain:
            return p
    return None


def is_switch(param: dict, block: dict, catalog) -> bool:
    if param.get("options") or "option" in param:
        return True
    model = catalog.get(block["model_id"]) if catalog is not None else None
    if model is None:
        return False
    try:
        spec = model.parameter(param["name"])
    except KeyError:
        return False
    return bool(spec.option_count) or spec.type in ("switch", "comboBox", "rotarySwitch")


# -- target selection ------------------------------------------------------------------

def find_category(clause: str) -> str | None:
    for key, pattern, _ in CATEGORIES:
        if re.search(pattern, clause, re.I):
            return key
    # A bare ドライブ or ブースト names the pedal only when it is added or switched;
    # otherwise ドライブ is the DRIVE knob (「ドライブを 6 に」) and ブースト a verb
    # (「低音をブーストして」).
    if re.search(r"ドライブ|ブースト|\bdrive\b|\bboost\b", clause, re.I) and \
            re.search(ADD_VERBS + "|" + BYPASS_ON + "|" + BYPASS_OFF, clause, re.I):
        return "drive"
    return None


def category_names(key: str) -> tuple:
    return next(c for k, _, c in CATEGORIES if k == key)


def select_target(clause: str, grid: dict, default: str | None) -> tuple[dict | None, str, str]:
    """``(block or None, category key, the clause with target words removed)``."""
    blocks = _blocks(grid)
    rest = clause
    col = re.search(r"列\s*(\d)|(\d)\s*列目|\bcol(?:umn)?\s*(\d)", rest, re.I)
    row = re.search(r"行\s*(\d)|(\d)\s*行目|\brow\s*(\d)", rest, re.I)
    nth = re.search(r"(\d+)\s*(?:番目|つ目|個目)", rest)
    for m in (col, row, nth):
        if m:
            rest = rest.replace(m.group(0), " ")
    named = None
    for b in sorted(blocks, key=lambda b: -len(b["model"])):
        if b["model"].lower() in rest.lower():
            named = b
            rest = re.sub(re.escape(b["model"]), " ", rest, flags=re.I)
            break
    category = find_category(rest)
    if named is not None:
        return named, category or "", rest
    candidates = blocks
    if col:
        c = int(next(g for g in col.groups() if g)) - 1
        candidates = [b for b in candidates if b["col"] == c]
    if row:
        r = int(next(g for g in row.groups() if g)) - 1
        candidates = [b for b in candidates if b["row"] == r]
    key = category or (None if (col or row) else default)
    if key:
        candidates = [b for b in candidates if _in_category(b, category_names(key))]
        if not candidates and key == "amp" and default == "amp" and not category:
            # No amp: a capture or a drive stands in for the gain and tone moves.
            candidates = [b for b in blocks if _in_category(b, CAPTURE + DRIVE)]
    if nth:
        n = int(nth.group(1)) - 1
        candidates = candidates[n:n + 1]
    if (col or row or nth) and not candidates:
        where = " ".join(m.group(0) for m in (row, col, nth) if m)
        raise Unclear(f"「{where.strip()}」にブロックがありません。")
    # Prefer the row the guitar comes in on.
    inputs = _input_rows(grid)
    candidates.sort(key=lambda b: (b["row"] not in inputs, b["row"], b["col"]))
    return (candidates[0] if candidates else None), key or "", rest


# -- building ops -------------------------------------------------------------------------

def _display(param: dict, value: float) -> str:
    if "range" in param and param.get("real") is not None:
        lo, hi = param["range"]
        real = lo + value * (hi - lo)
        unit = param.get("units") or ""
        return f"{real:.1f}{(' ' + unit) if unit else ''}"
    return f"{value:.2f}"


def _current_text(param: dict) -> str:
    if "option" in param:
        return str(param["option"])
    if param.get("real") is not None:
        unit = param.get("units") or ""
        return f"{param['real']}{(' ' + unit) if unit else ''}"
    return f"{param.get('value')}"


def relative(block: dict, param: dict, direction: int, factor: float) -> tuple[dict | None, str]:
    """One knob moved one step; ``(op or None, summary)``."""
    where = f"{_label(block)} {param['name']}"
    units = (param.get("units") or "").lower()
    if units == "db" and param.get("real") is not None and "range" in param:
        lo, hi = param["range"]
        new = max(lo, min(hi, param["real"] + direction * DB_STEP * factor))
        if abs(new - param["real"]) < 1e-6:
            return None, f"{where}: すでに{'最大' if direction > 0 else '最小'}です({_current_text(param)})"
        new = round(new, 1)
        return ({"op": "set_param", "row": block["row"], "col": block["col"],
                 "param": param["name"], "real": new},
                f"{where}: {_current_text(param)} → {new} dB")
    value = param.get("value")
    if value is None:
        raise Unclear(f"{where} は数値のパラメーターではありません。")
    new = max(0.0, min(1.0, value + direction * NORMAL_STEP * factor))
    if abs(new - value) < 1e-6:
        return None, f"{where}: すでに{'最大' if direction > 0 else '最小'}です({_current_text(param)})"
    new = round(new, 4)
    return ({"op": "set_param", "row": block["row"], "col": block["col"],
             "param": param["name"], "value": new},
            f"{where}: {_current_text(param)} → {_display(param, new)}")


def knob_params(block: dict, knob: str, clause: str = "") -> list[dict]:
    """The parameters a spoken knob name means on ``block``."""
    if knob == "gain":
        p = gain_knob(block, clause)
        return [p] if p else []
    if knob == "volume":
        p = volume_knob(block)
        return [p] if p else []
    names = EQ_PARAMS.get(knob) if _in_category(block, ("Equalizer",)) else None
    names = names or KNOB_PARAMS.get(knob, ())
    found = []
    for n in names:
        p = _param(block, n)
        if p is not None:
            found.append(p)
            if _in_category(block, ("Equalizer",)) or knob == "mid":
                break
    return found


def _free_cells(grid: dict, row: int) -> list[int]:
    taken = {b["col"] for b in grid["blocks"] if b["row"] == row}
    return [c for c in range(qdevice.COLUMNS) if c not in taken]


def _first_available(catalog, names, categories) -> str | None:
    for name in names:
        try:
            m = catalog.find(name)
        except KeyError:
            continue
        if not (m.hidden or m.internal or m.superseded):
            return m.name
    for m in catalog:
        if m.category in categories and not (m.hidden or m.internal or m.superseded
                                             or inert.is_plugin(m)):
            return m.name
    return None


def add_block(key: str, grid: dict, catalog, clause: str) -> tuple[list[dict], list[str]]:
    categories = ADD_CATEGORY.get(key)
    if categories is None:
        raise Unclear(f"{CATEGORY_LABEL.get(key, key)} の追加には対応していません。")
    existing = [b for b in _blocks(grid) if _in_category(b, categories)]
    if existing:
        b = existing[0]
        if b.get("bypassed"):
            return ([{"op": "bypass", "row": b["row"], "col": b["col"], "bypassed": False}],
                    [f"{_label(b)}: すでにあるのでバイパスを解除します"])
        hint = "深く" if key in ("delay", "reverb", "mod") else "強く"
        return [], [f"{_label(b)} がすでにあります(何もしません。かかりを変えるなら「{CATEGORY_LABEL[key]}を"
                    f"{hint}」などと指示してください)"]
    name = _first_available(catalog, ADD_MODELS.get(key, []), categories)
    if name is None:
        raise Unclear(f"カタログに {CATEGORY_LABEL[key]} のブロックが見つかりません。")
    amps = [b for b in _blocks(grid) if _in_category(b, AMP + CAPTURE)]
    inputs = _input_rows(grid)
    row = amps[0]["row"] if amps else (inputs[0] if inputs else 0)
    free = _free_cells(grid, row)
    if key == "drive":
        if not amps:
            raise Unclear("アンプが見つからないので、ブーストを置く位置を決められません。")
        before = [c for c in free if c < amps[0]["col"]]
        if not before:
            raise Unclear(f"{_label(amps[0])} より前に空きセルがありません。グリッドのブロックは"
                          "ずらせないので、前のブロックを外すか、本体で並べ替えてください。")
        col = max(before)
    else:
        last = max((b["col"] for b in grid["blocks"] if b["row"] == row), default=-1)
        after = [c for c in free if c > last] or [c for c in free if amps and c > amps[0]["col"]]
        if not after:
            raise Unclear(f"行{row + 1} の後ろに空きセルがありません。")
        col = min(after)
    ops = [{"op": "set_block", "row": row, "col": col, "model": name}]
    notes = [f"行{row + 1} 列{col + 1} に {name} を置きます"]
    if key == "drive":
        model = catalog.find(name)
        names = [p.name for p in model.parameters]
        gain = next((n for n in GAIN_KNOBS if n in names), None)
        level = next((n for n in ("LEVEL", "VOLUME", "OUTPUT") if n in names), None)
        for pname, value in ((gain, 0.15), (level, 0.75)):
            if pname:
                ops.append({"op": "set_param", "row": row, "col": col, "param": pname,
                            "value": value})
        notes.append(f"ブーストとして {gain or 'ゲイン'} を低め(0.15)、{level or 'レベル'} を高め(0.75)に"
                     "します(この組み合わせは決めごとです)")
    return ops, notes


def option_choice(param: dict, word: str) -> str | None:
    options = param.get("options") or []
    word_l = word.lower()
    for o in options:
        if o.lower() == word_l:
            return o
    mapped = ON_WORDS.get(word_l) or ON_WORDS.get(word)
    if mapped:
        for o in options:
            if o.lower() == mapped:
                return o
    return None


# -- one clause -------------------------------------------------------------------------------

def _param_by_spoken(block: dict, token: str, clause: str) -> list[dict]:
    token_s = token.strip()
    direct = _param(block, token_s)
    if direct is not None:
        return [direct]
    for knob, pattern in ALIASES.items():
        if re.fullmatch(pattern, token_s, re.I):
            return knob_params(block, knob, clause)
    if re.fullmatch(r"ドライブ|drive", token_s, re.I):
        p = _param(block, "DRIVE") or gain_knob(block)
        return [p] if p else []
    return []


# Spoken names are Latin (parameter names) or katakana/kanji (ゲイン, 高音); hiragana
# is left out so the particle after the name is not swallowed into it.
_NUMBER = re.compile(r"(?P<name>[A-Za-z][A-Za-z0-9]*(?: [A-Za-z0-9]+)*|[ァ-ヶー一-龠]+)\s*"
                     r"(?:を|は|=|:|to)?\s*(?P<num>-?\d+(?:\.\d+)?)\s*(?:dB|db|ms|Hz|hz|%|に|へ|まで)?",
                     re.I)
_OPTION = re.compile(r"(?P<name>[A-Za-z][A-Za-z0-9]*(?: [A-Za-z0-9]+)*|[ァ-ヶー一-龠]+)\s*"
                     r"(?:\s*(?:を|は|=|:|\bto\b)\s*|\s+)(?P<word>[A-Za-z][A-Za-z0-9 ]*?|オン|オフ)\s*(?:に|へ)?"
                     r"(?:して|する|切り替え\S*)?\s*$", re.I)


def clause_ops(clause: str, grid: dict, catalog) -> tuple[list[dict], list[str], list[str]]:
    """``(ops, summary lines, warnings)`` for one clause, or :class:`Unclear`."""
    warnings: list[str] = []
    factor = intensity(clause)

    # 1. Add a block.
    category = find_category(clause)
    if category and re.search(ADD_VERBS, clause, re.I) and not re.search(BYPASS_OFF, clause):
        ops, notes = add_block(category, grid, catalog, clause)
        return ops, notes, warnings

    # 2. Bypass / re-enable.
    off = re.search(BYPASS_ON, clause, re.I)
    on = re.search(BYPASS_OFF, clause, re.I)
    is_option = re.search(r"(を|は)\s*(オン|オフ)\s*に", clause) and not category
    if (off or on) and not is_option and (category or re.search(r"列|行|番目|つ目", clause)):
        block, key, _ = select_target(clause, grid, default=None)
        if block is None:
            raise Unclear(f"{CATEGORY_LABEL.get(key, 'ブロック')} がグリッドにありません。")
        bypassed = bool(off) and not on
        if bool(block.get("bypassed")) == bypassed:
            return [], [f"{_label(block)}: すでに{'バイパス' if bypassed else '有効'}です"], warnings
        return ([{"op": "bypass", "row": block["row"], "col": block["col"], "bypassed": bypassed}],
                [f"{_label(block)}: {'バイパスします(外す=バイパス。元に戻せます)' if bypassed else 'バイパスを解除します'}"],
                warnings)

    block, key, rest = select_target(clause, grid, default="amp")

    # 3. A switch option by name.
    m = _OPTION.search(rest.strip())
    if block is not None and m:
        params = _param_by_spoken(block, m.group("name"), clause)
        if not params and re.fullmatch(r"[A-Z][A-Z0-9 ]*", m.group("name").strip()):
            raise Unclear(f"{_label(block)} に「{m.group('name').strip()}」というパラメーターが"
                          f"見つかりません。")
        if params and is_switch(params[0], block, catalog):
            p = params[0]
            choice = option_choice(p, m.group("word").strip())
            if choice is None:
                raise Unclear(f"{_label(block)} {p['name']} の選択肢に「{m.group('word').strip()}」は"
                              f"ありません(選択肢: {', '.join(p.get('options') or ['不明'])})。")
            return ([{"op": "set_option", "row": block["row"], "col": block["col"],
                      "param": p["name"], "option": choice}],
                    [f"{_label(block)} {p['name']}: {p.get('option')} → {choice}"], warnings)

    # 4. A number.
    m = _NUMBER.search(rest)
    if m:
        if block is None:
            raise Unclear("対象のブロックが見つかりません(アンプがありません)。")
        params = _param_by_spoken(block, m.group("name"), clause)
        if not params:
            raise Unclear(f"{_label(block)} に「{m.group('name')}」というパラメーターが見つかりません"
                          f"(あるもの: {', '.join(p['name'] for p in block['params'] if 'text' not in p)})。")
        p = params[0]
        number = float(m.group("num"))
        if is_switch(p, block, catalog):
            raise Unclear(f"{_label(block)} {p['name']} はスイッチなので、数値では動かしません。"
                          f"選択肢の名前で指定してください(選択肢: {', '.join(p.get('options') or ['不明'])})。")
        if "text" in p:
            raise Unclear(f"{p['name']} は文字列パラメーターです(パソコンから変えても音に反映されません)。")
        if "range" in p and p.get("real") is not None:
            lo, hi = p["range"]
            if not lo <= number <= hi:
                unit = f" {p['units']}" if p.get("units") else ""
                raise Unclear(f"{_label(block)} {p['name']} は {lo}〜{hi}{unit} の範囲です"
                              f"({number:g} は範囲外)。")
            return ([{"op": "set_param", "row": block["row"], "col": block["col"],
                      "param": p["name"], "real": number}],
                    [f"{_label(block)} {p['name']}: {_current_text(p)} → {number:g}"
                     f"{(' ' + p['units']) if p.get('units') else ''}"], warnings)
        if 0.0 <= number <= 1.0:
            return ([{"op": "set_param", "row": block["row"], "col": block["col"],
                      "param": p["name"], "value": number}],
                    [f"{_label(block)} {p['name']}: {_current_text(p)} → {number:g}(0〜1 の値)"],
                    warnings)
        raise Unclear(f"{p['name']} は実際の単位の範囲が分からないため、0〜1 の値で指定してください。")

    # 5. Relative moves.
    ops, notes = [], []
    matched = False
    for pattern, knobs, direction in INTENTS:
        if not re.search(pattern, rest, re.I):
            continue
        matched = True
        target = block
        if knobs[0] == "mix" and (target is None or not _in_category(
                target, ("Delay", "Reverb", "Modulation"))):
            raise Unclear("かかり具合(MIX)はリバーブ・ディレイ・モジュレーションに対して指示してください"
                          "(例:「リバーブを深く」)。")
        if target is None:
            raise Unclear("対象のブロックが見つかりません(アンプがありません)。")
        params = [p for k in knobs for p in knob_params(target, k, clause)]
        if not params and knobs[0] in ("treble", "presence", "bass", "mid") and not key:
            eq = next((b for b in _blocks(grid) if _in_category(b, ("Equalizer",))), None)
            if eq is not None:
                target = eq
                params = [p for k in knobs for p in knob_params(eq, k, clause)]
        if not params:
            raise Unclear(f"{_label(target)} には {'/'.join(knobs)} に当たるつまみがありません。")
        for p in params:
            if is_switch(p, target, catalog):
                raise Unclear(f"{_label(target)} {p['name']} はスイッチなので相対的には動かしません。")
            op, line = relative(target, p, direction, factor)
            notes.append(line)
            if op:
                ops.append(op)
        break
    if not matched:
        raise Unclear(f"「{clause}」の意味が分かりませんでした。")
    return ops, notes, warnings


def _target_warnings(ops: list[dict], grid: dict, catalog) -> list[str]:
    cells = {(b["row"], b["col"]): b for b in grid["blocks"]}
    out = []
    for op in ops:
        b = cells.get((op["row"], op.get("col")))
        if b is None or op["op"] not in ("set_param", "set_option"):
            continue
        if b["model_id"] in inert.CAPTURE_MODELS:
            out.append(f"{_label(b)}: キャプチャーブロックのつまみは、パソコンから書いても音に反映されません"
                       "(本体の画面で操作してください)。")
        elif inert.inert_plugin(catalog.get(b["model_id"]) if catalog is not None else None):
            out.append(f"{_label(b)}: 未所有のプラグインなので音は素通しです(書き込んでも変化しません)。")
    return sorted(set(out))


def translate(instruction: str, grid: dict, catalog) -> dict:
    """Turn an instruction into a patch. Never touches the unit."""
    text = normalize_text(instruction)
    ops, summary, warnings, unclear = [], [], [], []
    for clause in split_clauses(text):
        try:
            o, s, w = clause_ops(clause, grid, catalog)
        except Unclear as exc:
            unclear.append(str(exc))
            continue
        ops.extend(o)
        summary.extend(s)
        warnings.extend(w)
    if not split_clauses(text):
        unclear.append("指示が空です。")
    warnings.extend(_target_warnings(ops, grid, catalog))
    return {"engine": "rules", "instruction": instruction,
            "patch": {"note": f"panel: {instruction}", "ops": ops},
            "summary": summary, "warnings": warnings, "not_understood": unclear}
