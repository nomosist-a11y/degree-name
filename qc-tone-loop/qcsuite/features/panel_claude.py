"""Words to grid edits through the Claude API (optional; used only when asked).

The player picks this engine in the panel; it needs the ``anthropic`` package
(``pip install -e '.[claude]'``) and ``ANTHROPIC_API_KEY``. Without either it
says so and the panel stays on the rules engine.

Claude gets the live grid (blocks, knobs in real units, ranges, options), a
short list of blocks it may add, and the instruction, and must answer with a
qctone patch in a fixed JSON schema (structured output). The system prompt
keeps it to operating: translate the words into the smallest set of edits,
never judge or suggest tone, never add changes nobody asked for. Whatever comes
back is checked like the rules engine's output - ``qctone.device.validate``
against the live grid, plus the rule that switches are never moved by raw
value - and is only shown, never applied, until the player presses apply.

Model: ``claude-opus-5-5`` (the current default per the claude-api skill),
overridable with ``QCSUITE_CLAUDE_MODEL``. Effort is ``low`` because this is a
short translation. Server-side refusal fallbacks are switched on
(``fallbacks="default"``, beta ``server-side-fallback-2026-07-01``), so a
declined request is retried on a fallback model inside the same call; a
refusal that survives that is reported, not hidden. None of this has been run
against the live API from this environment - the tests use a stand-in client.
"""

from __future__ import annotations

import json
import os

from qcsuite.features import panel_rules
from qctone import device as qdevice

DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"

SYSTEM_PROMPT = """You translate a guitarist's instruction about a Neural DSP Quad Cortex grid \
into a JSON patch of grid edits. You operate controls; you never judge tone.

Rules:
- Only make the edits the instruction asks for, and as few as possible. Do not add \
"improvements", suggestions, or opinions about what would sound better.
- Use only blocks and parameter names that appear in the grid or in the addable-blocks list. \
Parameter names differ per block: the gain control may be GAIN, DRIVE, VOLUME, INPUT GAIN or \
GAIN 1/GAIN 2; tone controls may be BASS, MID or MIDDLE, TREBLE, PRESENCE, DEPTH, RESONANCE.
- Rows and columns are zero-based in the patch (the unit shows them from 1).
- For knobs with a real-unit range, use "real" within that range; otherwise use "value" (0..1). \
Relative words: a normal step is 0.1 of the knob's travel (3 dB on EQ gains); "少し"/"slightly" \
is half of that, "かなり"/"a lot" double.
- Parameters that have options are switches: change them only with op "set_option" and an \
option name from the grid. Never give a switch a numeric value.
- To add a block, use "set_block" with a model name from the addable list into an EMPTY cell; \
blocks cannot be shifted. "Removing" a block means op "bypass" with bypassed true.
- If any part of the instruction is unclear, impossible, or would need a judgment about tone, \
put an explanation in not_understood (in Japanese) and leave it out of ops.
- Write summary lines in Japanese, one per edit, e.g. "行1 列3 Brit 2203 TREBLE: 6.0 → 5.0"."""

#: The answer's shape. Optional op fields are optional in the schema; whatever
#: comes back is validated against the live grid anyway.
PATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "ops": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "op": {"type": "string", "enum": sorted(qdevice.OPS)},
                    "row": {"type": "integer"},
                    "col": {"type": "integer"},
                    "param": {"type": "string"},
                    "real": {"type": "number"},
                    "value": {"type": "number"},
                    "option": {"type": "string"},
                    "model": {"type": "string"},
                    "bypassed": {"type": "boolean"},
                    "port": {"type": "string"},
                },
                "required": ["op", "row"],
                "additionalProperties": False,
            },
        },
        "summary": {"type": "array", "items": {"type": "string"}},
        "not_understood": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["ops", "summary", "not_understood"],
    "additionalProperties": False,
}


class ClaudeUnavailable(RuntimeError):
    """The Claude engine cannot run here (no package or no key)."""


def model_name() -> str:
    return os.environ.get("QCSUITE_CLAUDE_MODEL") or DEFAULT_MODEL


def availability() -> dict:
    """Whether the Claude engine can be used, and why not."""
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return {"available": False, "model": model_name(),
                "reason": "anthropic パッケージがありません(pip install -e '.[claude]')。"}
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return {"available": False, "model": model_name(),
                "reason": "ANTHROPIC_API_KEY が設定されていません。"}
    return {"available": True, "model": model_name(), "reason": ""}


def _compact_grid(grid: dict) -> dict:
    blocks = []
    for b in grid["blocks"]:
        params = []
        for p in b["params"]:
            if "text" in p:
                continue
            entry = {"name": p["name"]}
            if "options" in p:
                entry.update(option=p.get("option"), options=p["options"])
            elif p.get("real") is not None:
                entry.update(real=p["real"], range=p.get("range"), units=p.get("units", ""))
            else:
                entry["value"] = p.get("value")
            params.append(entry)
        blocks.append({"row": b["row"], "col": b["col"], "model": b["model"],
                       "category": b.get("category"), "bypassed": b.get("bypassed"),
                       "params": params})
    return {"rows": grid["rows"], "blocks": blocks}


def _addable(catalog) -> list[dict]:
    out = []
    for key, names in panel_rules.ADD_MODELS.items():
        for name in names:
            try:
                m = catalog.find(name)
            except KeyError:
                continue
            out.append({"model": m.name, "category": m.category,
                        "params": [p.name for p in m.parameters]})
    return out


def request_body(instruction: str, grid: dict, catalog) -> dict:
    content = json.dumps({"grid": _compact_grid(grid), "addable_blocks": _addable(catalog),
                          "instruction": instruction}, ensure_ascii=False, sort_keys=True)
    return {
        "model": model_name(),
        "max_tokens": 16000,
        "betas": [FALLBACK_BETA],
        "fallbacks": "default",
        "system": SYSTEM_PROMPT,
        "output_config": {"effort": "low",
                          "format": {"type": "json_schema", "schema": PATCH_SCHEMA}},
        "messages": [{"role": "user", "content": content}],
    }


def _client():
    import anthropic
    return anthropic.Anthropic()


def _call(client, body: dict):
    """One request; API errors become a message for the player (most specific first)."""
    import anthropic
    try:
        return client.beta.messages.create(**body)
    except anthropic.AuthenticationError as exc:
        raise ClaudeUnavailable(f"Claude API の認証に失敗しました: {exc}") from exc
    except anthropic.RateLimitError as exc:
        raise ClaudeUnavailable(f"Claude API の利用上限に達しました。少し待ってから再試行してください: {exc}") from exc
    except anthropic.BadRequestError as exc:
        raise ClaudeUnavailable(f"Claude API がリクエストを受け付けませんでした: {exc}") from exc
    except anthropic.APIStatusError as exc:
        raise ClaudeUnavailable(f"Claude API のエラー({exc.status_code}): {exc}") from exc
    except anthropic.APIConnectionError as exc:
        raise ClaudeUnavailable(f"Claude API に接続できません: {exc}") from exc


def translate(instruction: str, grid: dict, catalog, client=None) -> dict:
    """Same result shape as :func:`panel_rules.translate`; never touches the unit."""
    if client is None:
        status = availability()
        if not status["available"]:
            raise ClaudeUnavailable(status["reason"])
        client = _client()
    response = _call(client, request_body(instruction, grid, catalog))
    if getattr(response, "stop_reason", None) == "refusal":
        raise ClaudeUnavailable("Claude がこの指示への回答を断りました(フォールバック後も)。")
    if getattr(response, "stop_reason", None) == "max_tokens":
        raise ClaudeUnavailable("Claude の回答が途中で切れました。指示を短くして再試行してください。")
    text = next((b.text for b in response.content if getattr(b, "type", "") == "text"), None)
    if text is None:
        raise ClaudeUnavailable("Claude の回答に JSON がありませんでした。")
    try:
        answer = json.loads(text)
    except ValueError as exc:
        raise ClaudeUnavailable(f"Claude の回答を JSON として読めませんでした: {exc}") from exc
    ops = answer.get("ops") or []
    return {"engine": "claude", "model": model_name(), "instruction": instruction,
            "patch": {"note": f"panel (claude): {instruction}", "ops": ops},
            "summary": list(answer.get("summary") or []),
            "warnings": panel_rules._target_warnings(
                [o for o in ops if isinstance(o, dict) and "row" in o], grid, catalog),
            "not_understood": list(answer.get("not_understood") or [])}
