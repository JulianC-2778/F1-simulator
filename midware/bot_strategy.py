"""Prompt construction for POST /api/bot/strategy (Module 4 — the AI racing bot).

Why this file exists
--------------------
The bot's prompt used to be a literal inside runtime.py, which is the shared
FastAPI assembly for commentary / engineer / coach / bot.  Iterating on the bot
prompt therefore meant editing a file three other features also live in.  All
bot-prompt logic now lives here so that work on Module 4 produces a one-line
diff in the shared file instead of a churning block in the middle of it.

The three variants
------------------
``TORCS_BOT_PROMPT`` selects which prompt the endpoint uses:

``legacy`` (default)
    The prompt as it shipped: an explicit threshold table telling the model
    which strategy goes with which fuel/damage range.  Kept as-is so the
    default behaviour of the repo does not change for anyone else, and so it
    can serve as the control arm when comparing the variants below.

``bare``
    States what each strategy *does to the car* and nothing about when to
    pick one.  The model gets the facts a human strategist would have on the
    pit wall and makes the call itself.  This is the variant Module 4 is
    actually pursuing: the point is to demonstrate Granite reasoning, and a
    threshold table in the prompt means the model is only doing lookup.

``reasoning``
    ``bare`` plus a required shape for the answer: list the factors that
    matter, name a strategy being ruled out, then decide.  Field order in the
    schema is deliberate — an LLM generates left to right, so ``considered``
    has to precede ``strategy`` or the "reasoning" is written after the
    conclusion and is rationalisation rather than deliberation.

Which one to keep is an empirical question (does the model actually produce
differentiated, input-sensitive decisions?), which is why all three stay
selectable rather than one being deleted.

Why current_strategy is dropped for bare/reasoning
--------------------------------------------------
A previous attempt at removing the threshold table produced 130/130 NORMAL
over a healthy-car race — the model simply echoed the ``current_strategy``
field back.  That experiment also ran with a 160-token ceiling and raw sensor
floats, so it never isolated "no rules" as the cause.  Echoing the previous
answer is the cheapest possible completion when it is sitting right there in
the input, so the rule-free variants remove the field entirely; the model has
to read the situation to say anything at all.
"""

from __future__ import annotations

import json
import os
from typing import Any


# Selected once at import.  Default flipped legacy -> reasoning on 2026-08-12
# once the real-corpus comparison settled it: over 25 states sampled from an
# 8-lap race, legacy answered ATTACK to every single one and did not react to
# the sensitivity probe either (docs/bot_prompt_comparison_race3.md).  A
# threshold table in the prompt does not make the model cautious, it stops it
# reading the state at all, so shipping it as the default was shipping the
# broken variant.  legacy stays selectable as the control arm.
PROMPT_MODE = os.getenv("TORCS_BOT_PROMPT", "reasoning").strip().lower()

VALID_MODES = ("legacy", "bare", "reasoning", "concise")

# Per-variant model settings.  legacy keeps its measured-and-tuned numbers;
# the rule-free variants need room to actually write reasoning (160 tokens
# cannot hold a factor list, and the old 8-word reason cap existed only
# because the JSON was being truncated mid-string at that ceiling).
#
# The ceilings are headroom, not targets: verbosity is held down by the
# prompt (see _REASONING_TAIL) so that hitting the ceiling means something
# went wrong rather than being the normal case.  That distinction matters
# because a truncated answer and a timed-out one both surface as a 502, and
# they need opposite fixes — measured 2026-08-12, the reasoning variant ran
# 65-70 s against a 75 s ceiling with two of five calls failing, which is
# exactly the ambiguous band this is meant to keep us out of.
MODEL_SETTINGS: dict[str, dict[str, Any]] = {
    "legacy":    {"temperature": 0.1, "max_tokens": 160, "timeout": 30},
    "bare":      {"temperature": 0.2, "max_tokens": 300, "timeout": 60},
    "reasoning": {"temperature": 0.2, "max_tokens": 700, "timeout": 150},
    # Same structure as reasoning, roughly half the words. Generation time
    # on a local model scales with tokens emitted, and reasoning measured a
    # 7.6 s median (docs/bot_prompt_comparison_race3.md) — enough that the
    # dashboard sits still between answers. This trades the third factor and
    # some phrasing room for responsiveness; use reasoning when the quality
    # of the trace matters more than how often it refreshes.
    "concise":   {"temperature": 0.2, "max_tokens": 350, "timeout": 90},
}

# Fallback list, used only when the bot did not tell us what it accepts.
# ai_bot.py gates SAVE_FUEL behind a flag, so the authoritative list travels
# with the request (see allowed_strategies below) rather than being duplicated
# here where it would silently drift.
_DEFAULT_STRATEGIES = ("ATTACK", "NORMAL", "DEFEND", "PIT")

# What each strategy DOES to the car.  Deliberately effects, not selection
# rules: a human strategist needs to know what ATTACK costs before choosing
# it, and telling the model that is describing the game, not handing over the
# answer.  Keep this table free of any "use this when ..." phrasing.
_STRATEGY_EFFECTS = {
    "ATTACK": (
        "Top speed 330 km/h, aggressive cornering. Fastest lap times. "
        "Burns fuel faster, wears the car, small margin for error."
    ),
    "NORMAL": "Top speed 250 km/h, balanced cornering.",
    "DEFEND": (
        "Top speed 180 km/h, cautious cornering. Slower, but very unlikely "
        "to make a mistake or pick up damage."
    ),
    "SAVE_FUEL": (
        "Throttle capped at 65%. Uses noticeably less fuel. "
        "Will lose positions to cars running harder."
    ),
    "PIT": (
        "Slow down for the pit lane. Costs roughly 20 seconds and typically "
        "a couple of positions. Refuels and repairs the car."
    ),
}

_LEGACY_PROMPT = (
    "You are a TORCS race strategist. Return JSON only with strategy and reason. "
    "strategy must be ATTACK, NORMAL, DEFEND, SAVE_FUEL, or PIT. "
    "reason must be a single phrase of at most 8 words.\n"
    "Re-evaluate from scratch every time — do not just repeat current_strategy out of habit.\n"
    "Guide (a downstream safety layer already forces PIT/DEFEND/BLOCK in "
    "emergencies, so pick the best proactive choice for the state below):\n"
    "- ATTACK: damage under 4000 and fuel over 20 — push the pace for a better position.\n"
    "- DEFEND: damage between 4000 and 9000 — protect the car, avoid further risk.\n"
    "- SAVE_FUEL: fuel under 20 but above 5 — conserve for the rest of the race.\n"
    "- NORMAL: none of the above clearly applies — steady pace.\n"
)

_BARE_TAIL = (
    "Decide which strategy the car should run for the next lap.\n\n"
    'Return JSON only: {"strategy": "<one of the above>", '
    '"reason": "<one or two sentences>"}\n'
)

# Same shape as _REASONING_TAIL, deliberately halved. The factor count drops
# to two and every free-text field gets a tighter cap; `rejected` survives the
# cut because naming the option being turned down is the part that shows the
# model weighed alternatives rather than pattern-matched one.
_CONCISE_TAIL = (
    "Decide which strategy the car should run for the next lap.\n\n"
    "Work in this order:\n"
    "1. List the 2 factors that matter most right now, with their values.\n"
    "2. Name one strategy you are ruling out, and why.\n"
    "3. Give your decision.\n\n"
    "Be very brief: at most 5 words per value, per implication and per why;\n"
    "one short sentence for reason. No markdown, no text outside the JSON.\n\n"
    "Return JSON only, with the fields in exactly this order:\n"
    '{"considered": [{"factor": "...", "value": "...", "implication": "..."}], '
    '"rejected": {"option": "...", "why": "..."}, '
    '"strategy": "...", "reason": "..."}\n'
)

_REASONING_TAIL = (
    "Decide which strategy the car should run for the next lap.\n\n"
    "Work in this order:\n"
    "1. List the 2-3 factors that matter most right now, with their values.\n"
    "2. Name one strategy you are ruling out, and why.\n"
    "3. Give your decision.\n\n"
    # Terseness is not cosmetic here.  Left unbounded, granite-4.1-8b fills
    # the whole token budget with prose inside the JSON strings, which costs
    # ~65 s per call (a lap is ~84 s, and bot_strategy holds the highest
    # ModelBroker priority, so that starves engineer/coach/commentary) and
    # risks the object being cut off before its closing brace — at which
    # point extract_json_object returns nothing and the whole request 502s.
    "Be terse: under 8 words per value and per implication, under 12 for\n"
    "why, one sentence for reason. No markdown, no text outside the JSON.\n\n"
    "Return JSON only, with the fields in exactly this order:\n"
    '{"considered": [{"factor": "...", "value": "...", "implication": "..."}], '
    '"rejected": {"option": "...", "why": "..."}, '
    '"strategy": "...", "reason": "..."}\n'
)


DEFAULT_MODE = "reasoning"


def resolve_mode(mode: str | None = None) -> str:
    """Normalise a prompt-mode name, falling back to the default on anything odd.

    The fallback is deliberately *not* legacy: a typo in TORCS_BOT_PROMPT
    would otherwise silently select the one variant measured to mode-collapse,
    and it would do so invisibly — same 200 response, plausible-looking text,
    just no longer reading the state.  Landing on the default instead means a
    misspelling costs nothing.
    """
    candidate = (mode or PROMPT_MODE or DEFAULT_MODE).strip().lower()
    if candidate in VALID_MODES:
        return candidate
    print(f"[bot_strategy] unknown prompt mode {candidate!r}; "
          f"using {DEFAULT_MODE} (valid: {', '.join(VALID_MODES)})")
    return DEFAULT_MODE


def model_settings(mode: str | None = None) -> dict[str, Any]:
    """Temperature / token ceiling / timeout for the given prompt mode."""
    return dict(MODEL_SETTINGS[resolve_mode(mode)])


def _allowed_strategies(sensor_state: dict[str, Any]) -> tuple[str, ...]:
    """Strategies the bot will actually honour, as reported by the bot.

    ai_bot.py rejects any strategy outside its own allow-list, so a prompt
    that offers an option the bot discards wastes the model's choice and
    quietly turns into NORMAL downstream.  The bot therefore ships the list
    with each request; this is only a fallback for older clients.
    """
    raw = sensor_state.get("allowed_strategies")
    if isinstance(raw, (list, tuple)) and raw:
        names = tuple(str(item).strip().upper() for item in raw if str(item).strip())
        known = tuple(name for name in names if name in _STRATEGY_EFFECTS)
        if known:
            return known
    return _DEFAULT_STRATEGIES


def _render_situation(sensor_state: dict[str, Any]) -> str:
    """Render the race situation for the prompt.

    Prefers ``situation`` — a pre-formatted mapping of short human-readable
    lines that ai_bot builds, e.g. ``{"fuel": "8.2 L remaining, burning
    4.5 L/lap, ~9.0 L needed to finish"}``.  Without rules in the prompt the
    model has nothing to compare a bare ``8.2`` against, so the numbers have
    to arrive already carrying their own context.  Falls back to a plain JSON
    dump for clients that have not been updated.
    """
    situation = sensor_state.get("situation")
    if isinstance(situation, dict) and situation:
        width = max(len(str(key)) for key in situation)
        return "\n".join(
            f"  {str(key).replace('_', ' ').ljust(width)}  {value}"
            for key, value in situation.items()
        )
    scrubbed = {key: value for key, value in sensor_state.items() if key != "situation"}
    return json.dumps(scrubbed, ensure_ascii=True)


def build_prompt(request_payload: dict[str, Any], mode: str | None = None) -> str:
    """Build the full prompt string for one strategy request.

    Args:
        request_payload: BotStrategyRequest as a plain dict.
        mode: Override the module-level prompt mode (used by the replay
            harness to compare variants over one recorded state).
    """
    resolved = resolve_mode(mode)
    sensor_state = request_payload.get("sensor_state")
    if not isinstance(sensor_state, dict):
        sensor_state = {}

    if resolved == "legacy":
        # Strip the fields the rule-free variants added so legacy renders
        # exactly the JSON it always did — the point of keeping legacy around
        # is that it is an unchanged control arm, not a slightly different one.
        scrubbed = dict(request_payload)
        if isinstance(scrubbed.get("sensor_state"), dict):
            scrubbed["sensor_state"] = {
                key: value
                for key, value in scrubbed["sensor_state"].items()
                if key not in ("situation", "allowed_strategies")
            }
        return _LEGACY_PROMPT + json.dumps(scrubbed, ensure_ascii=True)

    lines = [
        "You are the race strategist for a TORCS car.",
        "",
        "Your driver can run one of these strategies. This is what each one DOES:",
        "",
    ]
    for name in _allowed_strategies(sensor_state):
        lines.append(f"- {name.ljust(10)} {_STRATEGY_EFFECTS[name]}")
    lines += [
        "",
        "Current race situation:",
        "",
        _render_situation(sensor_state),
        "",
        {"reasoning": _REASONING_TAIL,
         "concise":   _CONCISE_TAIL}.get(resolved, _BARE_TAIL),
    ]
    return "\n".join(lines)


def parse_decision(parsed: dict[str, Any]) -> dict[str, Any]:
    """Pull the decision fields out of the model's JSON, tolerating absence.

    ``considered`` / ``rejected`` only exist in the reasoning variant, and a
    model can omit or malform them in any variant, so both degrade to empty
    rather than failing the request — the strategy itself is what the control
    loop needs, and losing the commentary must never cost it a decision.
    """
    considered = parsed.get("considered")
    if not isinstance(considered, list):
        considered = []
    else:
        considered = [item for item in considered if isinstance(item, dict)]

    rejected = parsed.get("rejected")
    if not isinstance(rejected, dict):
        rejected = {}

    return {
        "strategy": str(parsed.get("strategy", "NORMAL")).upper(),
        "reason": str(parsed.get("reason", "")),
        "considered": considered,
        "rejected": rejected,
    }
