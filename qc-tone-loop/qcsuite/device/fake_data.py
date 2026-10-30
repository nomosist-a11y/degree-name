"""The fake unit's starting content: catalog, folders, presets, captures, IRs.

Grounded where facts exist, synthetic where they do not - and labelled:

* **Folder layout and counts are the player's real unit** (2026-10, CorOS 4.0.1):
  My Presets 51, Favorite 21, kami 20, Cory Wong 15, Live / Artist / Song /
  Workshop empty, Factory Library 256. The example names come from the player's
  list; the remaining names are invented to reach the counts. As on the unit,
  20 names appear in more than one folder and 10 user presets share a name with
  a factory preset. The capture library holds 2,078 entries.
* **Model ids and categories** come from :mod:`pyquadcortex.models` (the factory
  blocks every unit has). Display names are rebuilt from the constant names
  (``BRIT_2203`` -> "Brit 2203"), so their casing is a guess.
* **Parameter lists are synthetic.** The real ones come from the unit's
  ModelRepo, which this environment never saw. They follow the facts gathered on
  hardware: gain is called GAIN, DRIVE, VOLUME, INPUT GAIN or GAIN 1/2 depending
  on the block; tone knobs are BASS, MID or MIDDLE, TREBLE, PRESENCE, DEPTH,
  RESONANCE; some knobs are switches with options; a capture block carries
  VOLUME at index 4 and ``file_name`` at index 5 (pyquadcortex ``set_capture``);
  an IR loader carries IR PATH at 2/10 and IR NAME at 22/23 (``set_ir``).
* **Plugin models are synthetic** apart from their owners: blocks from the Plini,
  Gojira, Nolly, Petrucci, John Mayer, Rabea, Misha, Tim Henson, Fortin and
  Soldano SLO-100 X plugins are unowned on the player's unit (they pass audio
  through untouched); Cory Wong's The Amp Snob and Dumbbell ODS sounded.
* **Preset contents (chains and knob values) are invented**, deterministically
  from each preset's name, and steered so the health checks have something to
  find (a preset left on USB 5, one with an unowned plugin, capture blocks).
"""

from __future__ import annotations

import hashlib
import inspect
import random
import re

from pyquadcortex import models as factory_models
from pyquadcortex.catalog import Model, ModelCatalog, Parameter

USER_ROOT = "/media/p4/Presets"
FACTORY_KEY = "/opt/neuraldsp/Factory Library"
CAPTURES_KEY = "local_nc_root"
IR_LIBRARY = "local_ir_root"
USER_IRS = "2_q"

LANE_OUTPUT = 23000
INPUT_GATE = 28000
CAPTURE_V1 = 14000
CAPTURE_V2 = 14001
CAPTURE_MODELS = (CAPTURE_V1, CAPTURE_V2)
IR_LOADERS = range(29001, 29009)
FIRMWARE = "d14e"

#: Category ids. The real unit numbers its categories itself; only the capture
#: categories (14 and 20) are known, from pyquadcortex's catalog module.
_CATEGORY_IDS = {
    "Guitar Amplifier": 1, "Guitar Overdrive": 2, "Bass Amplifier": 3,
    "Bass Overdrive": 4, "Cabsim Guitar (M)": 5, "Cabsim Guitar (ST)": 6,
    "Cabsim Bass (M)": 7, "Cabsim Bass (ST)": 8, "Compressor": 9, "Delay": 10,
    "Equalizer": 11, "FX Loop": 12, "Filter": 13, "Neural Capture": 14,
    "IRLoaders": 15, "Loopers": 16, "Modulation": 17, "Morph": 18, "Pitch": 19,
    "Neural Capture V2": 20, "Reverb": 21, "Synth": 22, "Utility": 23, "Wah": 24,
    "Lane Output": 25, "Input Gate": 26,
}

#: Unowned on the player's unit: placeable and readable, but audio passes through.
UNOWNED_PLUGINS = ("Plini", "Gojira", "Nolly", "Petrucci", "John Mayer", "Rabea",
                   "Misha", "Tim Henson", "Fortin", "Soldano SLO-100 X")
OWNED_PLUGINS = ("Cory Wong",)

# Synthetic plugin model names: three amps per plugin, ids from 40000 up.
_PLUGIN_AMPS = {
    "Plini": ["Plini Clean", "Plini Rhythm", "Plini Lead"],
    "Gojira": ["Gojira Clean", "Gojira Rhythm", "Gojira Lead"],
    "Nolly": ["Nolly Clean", "Nolly Crunch", "Nolly Rhythm"],
    "Petrucci": ["Petrucci Clean", "Petrucci Rhythm", "Petrucci Lead"],
    "John Mayer": ["Mayer Clean", "Mayer Crunch", "Mayer Lead"],
    "Rabea": ["Rabea Clean", "Rabea Rhythm", "Rabea Lead"],
    "Misha": ["Misha Clean", "Misha Rhythm", "Misha Lead"],
    "Tim Henson": ["Henson Clean", "Henson Crunch", "Henson Lead"],
    "Fortin": ["Fortin Nameless", "Fortin Cali", "Fortin Meshuggah"],
    "Soldano SLO-100 X": ["SLO-100 Normal", "SLO-100 Crunch", "SLO-100 Overdrive"],
    "Cory Wong": ["The Amp Snob", "Cory Clean", "Cory Crunch"],
}

_ACRONYMS = {"UK", "US", "CA", "DS1", "OD1", "SD1", "BD2", "MT", "TM45", "TWN",
             "DLX", "PV", "EV101IIIS", "BOD", "D30", "C30", "C15", "OD250", "UBL",
             "ODS", "HP", "MX", "CE2W", "DC2W", "CS3", "OC2", "VMT", "B3K", "BDDI",
             "M", "ST", "S/C", "OS", "V30", "UKV30", "GB", "H4", "2C", "CH1", "CH2",
             "CH3", "CH4", "CH120", "EL34", "6L6", "PPC", "G12", "IR", "FX", "SPR"}


def pretty(constant: str) -> str:
    """Turn a generated constant back into a plausible display name."""
    name = constant
    if re.match(r"^N\d", name):
        name = name[1:]
    words = []
    for word in name.split("_"):
        if word in _ACRONYMS or any(c.isdigit() for c in word):
            words.append(word)
        else:
            words.append(word.capitalize())
    text = " ".join(words)
    return text.replace(" S C", " S/C")


def _p(index, name, lo=0.0, hi=10.0, default=5.0, units="", type_="slider", steps=None):
    return Parameter(index, name, lo, hi, default, units=units, type=type_, steps=steps)


def _knobs(names, start=0):
    return [_p(start + i, n) for i, n in enumerate(names)]


def amp_parameters(model_id: int) -> tuple:
    """Four layouts, picked by id, so one config mixes GAIN/DRIVE/GAIN 1/INPUT GAIN."""
    layout = model_id % 4
    if layout == 0:
        params = _knobs(["GAIN", "BASS", "MIDDLE", "TREBLE", "PRESENCE", "MASTER"])
        params.append(_p(6, "BRIGHT", 0, 1, 0, type_="switch", steps=2))
    elif layout == 1:
        params = _knobs(["DRIVE", "BASS", "MID", "TREBLE", "PRESENCE", "DEPTH", "VOLUME"])
    elif layout == 2:
        params = _knobs(["GAIN 1", "GAIN 2", "BASS", "MIDDLE", "TREBLE", "PRESENCE",
                         "RESONANCE", "MASTER"])
        params.append(_p(8, "CHANNEL", 0, 1, 0, type_="comboBox", steps=2))
    else:
        params = _knobs(["INPUT GAIN", "BASS", "MID", "TREBLE", "PRESENCE", "VOLUME"])
        params.append(_p(6, "BRIGHT", 0, 1, 0, type_="switch", steps=2))
    params.append(_p(len(params), "LEVEL", 0.0, 1.0, 0.76923077, units="dB"))
    return tuple(params)


#: Option names for list parameters; in a real preset they sit in dynamic_steps.
OPTION_NAMES = {
    "BRIGHT": ["Off", "On"],
    "CHANNEL": ["Clean", "Drive"],
    "MODE": ["Mono", "Stereo"],
    "BYPASS": ["Off", "On"],
}


def _parameters_for(category: str, model_id: int) -> tuple:
    if category in ("Guitar Amplifier", "Bass Amplifier"):
        return amp_parameters(model_id)
    if category in ("Guitar Overdrive", "Bass Overdrive"):
        gain = "DRIVE" if model_id % 2 else "GAIN"
        return tuple(_knobs([gain, "TONE", "LEVEL"]))
    if category.startswith("Cabsim"):
        return (
            _p(0, "ir selector", 0, 1, 0, type_="text"),
            _p(1, "ir selector 2", 0, 1, 0, type_="text"),
            _p(2, "DISTANCE", 1.0, 12.0, 3.0, units="in"),
            _p(3, "LOW CUT", 20.0, 500.0, 80.0, units="Hz"),
            _p(4, "HIGH CUT", 2000.0, 20000.0, 9000.0, units="Hz"),
            _p(5, "LEVEL", -12.0, 12.0, 0.0, units="dB"),
        )
    if category == "Equalizer":
        if model_id == factory_models.Equalizer.LOW_HIGH_CUT:
            return (_p(0, "LOW CUT", 20.0, 1000.0, 20.0, units="Hz"),
                    _p(1, "HIGH CUT", 1000.0, 20000.0, 20000.0, units="Hz"))
        if model_id in (factory_models.Equalizer.GRAPHIC_9,
                        factory_models.Equalizer.GRAPHIC_9_LEGACY,
                        factory_models.Equalizer.PLUGIN_GRAPHIC_9):
            bands = ["63", "125", "250", "500", "1K", "2K", "4K", "8K", "16K"]
            return tuple(_p(i, f"{b} HZ", -12.0, 12.0, 0.0, units="dB")
                         for i, b in enumerate(bands)) + (
                _p(9, "LEVEL", -12.0, 12.0, 0.0, units="dB"),)
        return (
            _p(0, "LOW FREQ", 40.0, 500.0, 100.0, units="Hz"),
            _p(1, "LOW GAIN", -12.0, 12.0, 0.0, units="dB"),
            _p(2, "MID FREQ", 200.0, 5000.0, 800.0, units="Hz"),
            _p(3, "MID GAIN", -12.0, 12.0, 0.0, units="dB"),
            _p(4, "MID Q", 0.3, 5.0, 1.0),
            _p(5, "HIGH FREQ", 1000.0, 12000.0, 4000.0, units="Hz"),
            _p(6, "HIGH GAIN", -12.0, 12.0, 0.0, units="dB"),
            _p(7, "LEVEL", -12.0, 12.0, 0.0, units="dB"),
        )
    if category == "Delay":
        return (_p(0, "TIME", 1.0, 2000.0, 350.0, units="ms"),
                _p(1, "FEEDBACK", 0.0, 100.0, 30.0, units="%"),
                _p(2, "MIX", 0.0, 100.0, 25.0, units="%"),
                _p(3, "MODE", 0, 1, 0, type_="switch", steps=2))
    if category == "Reverb":
        return (_p(0, "DECAY", 0.1, 20.0, 2.0, units="s"),
                _p(1, "PREDELAY", 0.0, 500.0, 20.0, units="ms"),
                _p(2, "MIX", 0.0, 100.0, 20.0, units="%"),
                _p(3, "LOW CUT", 20.0, 1000.0, 100.0, units="Hz"),
                _p(4, "HIGH CUT", 1000.0, 20000.0, 8000.0, units="Hz"))
    if category == "Compressor":
        return (_p(0, "THRESHOLD", -60.0, 12.0, -20.0, units="dB"),
                _p(1, "RATIO", 1.0, 20.0, 4.0),
                _p(2, "ATTACK", 0.1, 100.0, 10.0, units="ms"),
                _p(3, "RELEASE", 10.0, 1000.0, 100.0, units="ms"),
                _p(4, "LEVEL", -12.0, 12.0, 0.0, units="dB"),
                _p(5, "MIX", 0.0, 100.0, 100.0, units="%"))
    if category == "Modulation":
        return (_p(0, "RATE", 0.1, 10.0, 1.0, units="Hz"),
                _p(1, "DEPTH", 0.0, 100.0, 50.0, units="%"),
                _p(2, "MIX", 0.0, 100.0, 50.0, units="%"))
    if category == "Utility":
        if model_id == factory_models.Utility.GAIN:
            return (_p(0, "GAIN", -24.0, 24.0, 0.0, units="dB"),)
        if model_id == factory_models.Utility.VOLUME:
            return (_p(0, "VOLUME", 0.0, 100.0, 100.0, units="%"),)
        return (_p(0, "THRESHOLD", -96.0, 0.0, -60.0, units="dB"),
                _p(1, "RELEASE", 10.0, 1000.0, 100.0, units="ms"))
    if category in ("Neural Capture", "Neural Capture V2"):
        return (_p(0, "GAIN"), _p(1, "BASS"), _p(2, "MID"), _p(3, "TREBLE"),
                _p(4, "VOLUME", 0.0, 1.0, 0.5, units="dB"),
                _p(5, "file_name", 0, 1, 0, type_="text"))
    if category == "IRLoaders":
        names = {0: "MIX", 1: "LEVEL", 2: "IR PATH", 3: "LOW CUT", 4: "HIGH CUT",
                 10: "IR PATH 2", 22: "IR NAME", 23: "IR NAME 2"}
        out = []
        for i in range(24):
            name = names.get(i, f"RESERVED {i}")
            text = i in (2, 10, 22, 23)
            out.append(_p(i, name, 0, 1, 0, type_="text") if text else _p(i, name))
        return tuple(out)
    return (_p(0, "MIX", 0.0, 100.0, 50.0, units="%"),
            _p(1, "LEVEL", -12.0, 12.0, 0.0, units="dB"))


#: Synthetic DSP cost of each category, in percent of one preset's budget.
#: The real unit's budget is per core and not public; this only exists so the
#: fake refuses an overfull preset the way pyquadcortex says the unit does.
DSP_COST = {
    "Guitar Amplifier": 22, "Bass Amplifier": 18, "Guitar Overdrive": 4,
    "Bass Overdrive": 4, "Cabsim Guitar (M)": 8, "Cabsim Guitar (ST)": 12,
    "Cabsim Bass (M)": 8, "Cabsim Bass (ST)": 12, "Neural Capture": 28,
    "Neural Capture V2": 34, "Reverb": 14, "Delay": 7, "Modulation": 6,
    "Compressor": 4, "Equalizer": 3, "IRLoaders": 8, "Pitch": 16, "Synth": 20,
    "Morph": 10, "Filter": 5, "Wah": 4, "Utility": 2, "FX Loop": 2, "Loopers": 10,
}
DSP_BUDGET = 100


def build_catalog() -> ModelCatalog:
    catalog = ModelCatalog()
    for _, cls in inspect.getmembers(factory_models, inspect.isclass):
        category = (cls.__doc__ or cls.__name__).strip().rstrip(".")
        constants = [(k, v) for k, v in vars(cls).items() if k.isupper()]
        for constant, model_id in constants:
            superseded = constant.endswith("_LEGACY")
            catalog.models[model_id] = Model(
                id=model_id, name=pretty(constant).replace(" Legacy", " (Legacy)"),
                category=category, category_id=_CATEGORY_IDS.get(category, 99),
                based_on="", parameters=_parameters_for(category, model_id),
                superseded=superseded)
    catalog.models[CAPTURE_V1] = Model(
        id=CAPTURE_V1, name="Eltron 30", category="Neural Capture", category_id=14,
        parameters=_parameters_for("Neural Capture", CAPTURE_V1))
    catalog.models[CAPTURE_V2] = Model(
        id=CAPTURE_V2, name="Capture 2", category="Neural Capture V2", category_id=20,
        parameters=_parameters_for("Neural Capture V2", CAPTURE_V2))
    catalog.models[LANE_OUTPUT] = Model(
        id=LANE_OUTPUT, name="Lane Output", category="Lane Output", category_id=25,
        internal=True, parameters=(
            _p(0, "VOLUME", 0.0, 1.0, 0.76923077, units="dB"),
            _p(1, "PAN", 0.0, 1.0, 0.5, units="%"),
            _p(2, "MUTE", 0, 1, 0, type_="switch", steps=2),
            _p(3, "SOLO", 0, 1, 0, type_="switch", steps=2)))
    catalog.models[INPUT_GATE] = Model(
        id=INPUT_GATE, name="Input Gate", category="Input Gate", category_id=26,
        internal=True, parameters=(
            _p(0, "NOISE REDUCTION", 0.0, 100.0, 0.0, units="%"),
            _p(1, "BYPASS", 0, 1, 1, type_="switch", steps=2),
            _p(2, "GAIN REDUCTION", -40.0, 0.0, -40.0, units="dB", type_="grMeter"),
            _p(3, "INPUT GAIN", -24.0, 24.0, 0.0, units="dB"),
            _p(4, "DUMMY", 0, 1, 0, type_="empty")))
    next_id = 40000
    for plugin, names in _PLUGIN_AMPS.items():
        for name in names:
            catalog.models[next_id] = Model(
                id=next_id, name=name, category="Guitar Amplifier", category_id=1,
                based_on=f"{plugin} plugin", parameters=amp_parameters(next_id),
                sku=f"PLUGIN-{plugin.upper().replace(' ', '-')}",
                plugin_id=plugin)
            next_id += 1
    return catalog


def plugin_owner(model) -> str | None:
    return getattr(model, "plugin_id", None) or None


def is_unowned_plugin(model) -> bool:
    return plugin_owner(model) in UNOWNED_PLUGINS


# -- folders and preset names -------------------------------------------------

MY_PRESETS = [
    "Friedman BE50 X v", "Two Rock Mayer", "Cory Wong Funk Tour", "VSA - Stevie Ray Vee",
    "YNGWIE MALMSTEEN", "Henriksen", "Test2", "ZUTOMAYO_M&M", "ZUTOMAYO_M&M_1",
    "Super Reverb", "Ambient Crunch", "Wide Brit Shred", "Guthrie Govan", "EVH 4Live",
    "aaaa", "Texas Blues", "Matteo", "The Clean", "Laney test", "Fuzz This",
    "Strat Ambience", "D-Cell",
    # Invented to reach the real count of 51.
    "Clean Funk", "Edge of Breakup", "Plexi Crunch", "Modern Rhythm", "Djent Tight",
    "Blues Lead", "Jazz Box", "Shoegaze Wall", "Country Twang", "Worship Swell",
    "Metal Rhythm 2", "Lo-Fi Chime", "Acoustic Sim", "Bass DI Test", "Fusion Lead",
    "Surf Spring", "Doom Fuzz", "AC Chime", "Recto Chug", "5150 Lead", "Bluesbreaker",
    "Tweed Grit", "Glassy Clean", "Octave Fuzz", "Slide Tone", "Rhythm Practice",
    "Session 0412", "Neo Soul Clean", "Ambient Pad",
]
FAVORITE = [
    "ZUTOMAYO_M&M", "Dream Lead High Ga_1", "Matteo", "Lead", "The Clean",
    "Friedman BE50 X v_1", "Friedman BE50 X v_2", "Vna Hlane 4 Live",
    "Vna Hlane 4 Live_1", "Two Rock Mayer", "Guthrie Govan", "AT Lead Tone",
    "Ambient Crunch", "Texas Blues", "OneStar Clean Tweed", "US TWN Vibrato",
    "Fav Crunch", "Solo Boost", "Clean Comp", "Chorus Clean", "Big Lead",
]
KAMI = [
    "Henriksen", "Vna Hlane 4 Live", "AT Lead Tone", "Wide Brit Shred_kai", "Brit 2203",
    "EV101III Red", "Laney test", "Laney test_1", "Laney test_2", "aaaa", "PV505 Lead",
    "Lead", "Dream Lead High Ga_1", "Wide Brit Shred", "EVH 4Live", "YNGWIE MALMSTEEN",
    "Friedman BE50 X v", "kami Crunch", "Riff Master", "Sustain Lead",
]
CORY_WONG = [
    "Cory Wong Funk Tour", "Super Reverb", "CW Clean 1", "CW Clean 2", "Amp Snob Clean",
    "Amp Snob Crunch", "Dumbbell Lead", "Dumbbell Clean", "Vulfpeck Funk", "Wah Funk",
    "Comp Funk", "Octave Funk", "Phase Funk", "Chorus Funk", "Solo Funk",
]
USER_FOLDERS = {
    "My Presets": MY_PRESETS, "Favorite": FAVORITE, "kami": KAMI,
    "Cory Wong": CORY_WONG, "Live": [], "Artist": [], "Song": [], "Workshop": [],
}

#: Factory names that also appear in user folders (10, as on the real unit).
FACTORY_SHARED = ["Super Reverb", "Texas Blues", "Fuzz This", "Strat Ambience", "D-Cell",
                  "Brit 2203", "EV101III Red", "PV505 Lead", "OneStar Clean Tweed",
                  "US TWN Vibrato"]
# Factory names quoted in pyquadcortex's docstrings; the rest are invented.
_FACTORY_KNOWN = ["Cali Basswalk", "Darkglass AO900 1", "Darkglass AO900 2",
                  "Darkglass VMT", "OneStar Clean Tweed", "US TWN Vibrato",
                  "Strat Ambience", "Brit 2203", "Fuzz This", "D-Cell"]
_STYLES = ["Clean", "Crunch", "Lead", "Rhythm", "Ambient", "Edge", "Drive", "Blues",
           "Heavy", "Chime", "Funk", "Solo"]

#: Same content as another preset: (folder, name) -> (folder, name) it copies.
IDENTICAL = {
    ("Favorite", "ZUTOMAYO_M&M"): ("My Presets", "ZUTOMAYO_M&M"),
    ("My Presets", "ZUTOMAYO_M&M_1"): ("My Presets", "ZUTOMAYO_M&M"),
    ("kami", "Henriksen"): ("My Presets", "Henriksen"),
    ("kami", "Vna Hlane 4 Live"): ("Favorite", "Vna Hlane 4 Live"),
    ("Favorite", "Vna Hlane 4 Live_1"): ("Favorite", "Vna Hlane 4 Live"),
    ("kami", "Laney test_1"): ("kami", "Laney test"),
    ("kami", "Laney test_2"): ("kami", "Laney test"),
    ("Favorite", "Friedman BE50 X v_1"): ("My Presets", "Friedman BE50 X v"),
    ("Favorite", "Friedman BE50 X v_2"): ("My Presets", "Friedman BE50 X v"),
}


def factory_names(catalog: ModelCatalog) -> list[str]:
    taken = {n for names in USER_FOLDERS.values() for n in names} - set(FACTORY_SHARED)
    names = list(dict.fromkeys(_FACTORY_KNOWN + FACTORY_SHARED))
    amps = [m.name for m in catalog.factory_models()
            if m.category == "Guitar Amplifier" and not m.superseded]
    i = 0
    while len(names) < 256:
        amp = amps[i % len(amps)]
        style = _STYLES[(i // len(amps)) % len(_STYLES)]
        candidate = f"{amp} {style}"
        if candidate not in taken and candidate not in names:
            names.append(candidate)
        i += 1
    return names


# -- captures and IRs ---------------------------------------------------------

def _hash64(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


CAPTURE_COUNT = 2078


def capture_entries(catalog: ModelCatalog) -> list[tuple[str, str, str]]:
    """``(folder_key, key, name)`` for 2,078 synthetic library captures.

    Grouped like the real library: per-amp folders keyed ``NNN_f``, each with a
    handful of numbered variants ("Corn Vixen 1".."4", "Darkglass VMT 1".."3").
    """
    groups = [("Corn Vixen", 4), ("Darkglass VMT", 3)]
    amps = sorted({m.name.split(" (")[0] for m in catalog.factory_models()
                   if m.category in ("Guitar Amplifier", "Bass Amplifier", "Guitar Overdrive")})
    adjectives = ["Vintage", "Modern", "Boutique", "Studio", "Live", "Hot", "Cold",
                  "Dark", "Bright", "Custom"]
    for i, amp in enumerate(amps):
        for adjective in adjectives:
            groups.append((f"{adjective} {amp}", 2 + (i + len(adjective)) % 9))
    out = []
    for g, (base, variants) in enumerate(groups):
        folder = f"{100 + g}_f"
        for v in range(1, variants + 1):
            name = f"{base} {v}"
            out.append((folder, _hash64(name), name))
            if len(out) == CAPTURE_COUNT:
                return out
    raise AssertionError("not enough synthetic capture groups")


def ir_entries() -> list[tuple[str, str, str]]:
    """``(folder_key, key, name)`` for a small synthetic IR library."""
    out = []
    for i, base in enumerate(["Rex 57 on axis", "Rex 57 off axis", "Brit 4x12 121",
                              "US 2x12 Ribbon", "Tweed 1x12 Condenser", "Recto 4x12 Mix"]):
        out.append((IR_LIBRARY, f"CIR_{_hash64(base)[:31]}", base))
    for base in ["My Room IR", "Studio 412 Blend", "Oversized 212"]:
        out.append((USER_IRS, f"CIR_{_hash64(base)[:31]}", base))
    return out


# -- preset recipes -----------------------------------------------------------

_HIGH_GAIN = re.compile(r"lead|shred|metal|djent|5150|recto|evh|yngwie|riff|heavy|"
                        r"sustain|chug|high|henriksen|doom|govan", re.I)
_CLEAN = re.compile(r"clean|funk|jazz|chime|twang|swell|pad|ambient|surf|neo|"
                    r"acoustic|reverb|tweed|vibrato|cw |snob|comp", re.I)

_AMPS_CLEAN = ["US_TWN_NORMAL", "US_DLX_64_VINTAGE", "UK_C30_TOPBOOST", "ROLS_JAZZ_CH120",
               "CA_1STAR_CLEAN_50W_TWEED", "US_SPR_VIBRATO", "US_PRINCE"]
_AMPS_CRUNCH = ["BRIT_PLEXI_50_BRIGHT", "BRIT_2203", "BRIT_TM45_NORMAL", "MATCHMORE_D30_CH2",
                "CA_TREMO_ORANGE", "SOLO_100_CRUNCH_NORMAL", "US_HP_TWEED_TWN_BRIGHT"]
_AMPS_HIGH = ["PV_505_LEAD", "CA_DUO_CH3_MODERN", "EV101IIIS_RED_EL34_100W",
              "FREEMAN_100_LEAD", "D_CELL_H4_CH3", "SOLO_100_LEAD", "BRIT_900_LEAD"]
_CABS = ["N412_CA_STAND_OS_S_V30_90S_M", "N412_BRIT_60B_GB_71_M", "N212_US_TWN_C12Q_00S_M",
         "N112_US_DLX_BLACK_C12K_00S_M", "N212_UK_C30_65_M", "N412_EV_STRAIGHT_G12_00S_M",
         "N212_TOO_ROCK_G65_M"]


def _const(cls, name):
    return getattr(cls, name)


def recipe(folder: str, name: str, catalog: ModelCatalog, captures) -> dict:
    """A deterministic chain for one preset, steered by its name.

    Returns ``{"blocks": [(row, col, model_id, {param: normalized})], "in": ...,
    "out": ..., "lane_db": ..., "scene_labels": [...], "scene_bypass": {...},
    "capture": (row, col, entry) or None}``.
    """
    rng = random.Random(f"{folder}/{name}")
    GA, GO, CAB = factory_models.GuitarAmplifier, factory_models.GuitarOverdrive, \
        factory_models.CabsimGuitarM
    if _HIGH_GAIN.search(name):
        amp_pool, gain_range = _AMPS_HIGH, (0.55, 0.85)
    elif _CLEAN.search(name):
        amp_pool, gain_range = _AMPS_CLEAN, (0.15, 0.35)
    else:
        amp_pool, gain_range = _AMPS_CRUNCH, (0.35, 0.6)
    amp = _const(GA, rng.choice(amp_pool))
    if name.startswith("PV505"):
        amp = GA.PV_505_LEAD
    elif name.startswith("EV101III"):
        amp = GA.EV101IIIS_RED_EL34_100W
    elif name.startswith("Brit 2203"):
        amp = GA.BRIT_2203
    elif name.startswith("D-Cell"):
        amp = GA.D_CELL_H4_CH2_NORMAL
    elif folder == "Cory Wong":
        amp = rng.choice([GA.DUMBBELL_ODS, catalog.find("The Amp Snob").id])
    cab = _const(CAB, rng.choice(_CABS))
    blocks = []
    col = 0
    if rng.random() < 0.35 or "Comp" in name or folder == "Cory Wong":
        blocks.append((0, col, factory_models.Compressor.OPTO_COMP_M,
                       {"THRESHOLD": rng.uniform(0.4, 0.7)}))
        col += 1
    if rng.random() < 0.5 or "Fuzz" in name or "Boost" in name:
        drive = (GO.FUZZ_PI if "Fuzz" in name else
                 _const(GO, rng.choice(["GREEN_808", "CHIEF_SD1", "MYTH_DRIVE", "RAGE_BOOSTER"])))
        blocks.append((0, col, drive, {}))
        col += 1
    use_capture = (captures and rng.random() < 0.2 and folder in ("My Presets", "kami")
                   and "Clean" not in name)
    capture = None
    if use_capture:
        entry = captures[rng.randrange(len(captures))]
        blocks.append((0, col, CAPTURE_V1 if rng.random() < 0.6 else CAPTURE_V2,
                       {"GAIN": rng.uniform(*gain_range)}))
        capture = (0, col, entry)
        col += 1
    else:
        model = catalog[amp]
        gain_names = [p.name for p in model.parameters
                      if p.name in ("GAIN", "DRIVE", "INPUT GAIN", "GAIN 1")]
        knobs = {g: rng.uniform(*gain_range) for g in gain_names}
        for tone in ("BASS", "MID", "MIDDLE", "TREBLE", "PRESENCE"):
            knobs[tone] = rng.uniform(0.3, 0.75)
        blocks.append((0, col, amp, knobs))
        col += 1
    if name == "aaaa":
        # A leftover from a host-side experiment: an unowned plugin block that
        # reads back fine and plays nothing.
        blocks.append((0, col, catalog.find("Plini Lead").id, {"GAIN": 0.6}))
        col += 1
    blocks.append((0, col, cab, {}))
    col += 1
    if rng.random() < 0.4:
        blocks.append((0, col, factory_models.Equalizer.PARAMETRIC_3,
                       {"MID GAIN": rng.uniform(0.4, 0.6)}))
        col += 1
    if rng.random() < 0.5 or "Ambient" in name or "Live" in name:
        blocks.append((0, col, factory_models.Delay.ANALOG_DELAY_M,
                       {"MIX": rng.uniform(0.1, 0.35)}))
        col += 1
    if col < 8 and (rng.random() < 0.7 or "Reverb" in name or "Ambient" in name):
        blocks.append((0, col, rng.choice([factory_models.Reverb.HALL,
                                           factory_models.Reverb.PLATE,
                                           factory_models.Reverb.SPRING_M]),
                       {"MIX": rng.uniform(0.1, 0.4)}))
        col += 1
    labels = [" "] * 8
    scene_bypass = {}
    if rng.random() < 0.5 or "Live" in name:
        labels[:4] = ["Rhythm", "Lead", "Clean", "Solo"][: 4]
        drive_cells = [(r, c) for r, c, m, _ in blocks
                       if catalog[m].category == "Guitar Overdrive"]
        for cell in drive_cells:
            scene_bypass[cell] = [True, False, True, False, True, True, True, True]
    return {
        "blocks": blocks,
        "in": "USB_5" if name == "Test2" else "INPUT_1",
        "out": "XLR_1_2",
        "lane_db": round(rng.uniform(-6.0, 4.0), 1),
        "scene_labels": labels,
        "scene_bypass": scene_bypass,
        "capture": capture,
        "instrument": 1,
    }
