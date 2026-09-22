"""AI-driven position management tier.

Reached only after the regex and signal-LLM tiers have already run and
produced no actionable signal, but the message looks like a management
instruction aimed at an open position.

Two entry points:

    analyze_message(text, positions, group)
        Read a follow-up message ("SL to cost, book 30%") against known open
        positions and return a list of validated action dicts to execute. Used
        by the ingest worker when a group has open positions and the message
        carries management vocabulary.

    describe_positions(positions)
        Produce a compact human-readable summary of open positions to embed in
        the model prompt. Kept separate so the caller can enrich it before the
        call if needed.

This module **never places an order**. It returns structured action dicts; the
caller (ingest.py) either records them as a pending suggestion (default, human
approves in the UI) or passes them to executor.execute_sequence() immediately
when auto_apply_ai is on for the group.

The invariants that apply to every other LLM call in this add-on apply here:

- A reply that is not valid JSON is discarded entirely (returns []).
- An action that omits what its type requires is discarded individually.
- "Return [] if unsure" is in the system prompt and enforced on validation.
- Past-tense reports cannot become instructions here any more than they can
  in parser.py: the prompt explicitly lists them as "none" examples.
"""

from __future__ import annotations

import json
import re
from typing import Any

from utils.logging import get_logger

logger = get_logger(__name__)

MAX_INPUT_CHARS = 800
MAX_OUTPUT_TOKENS = 400
LLM_TIMEOUT_SECONDS = 25.0

_JSON_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)

_SYSTEM_PROMPT = """\
You are a position manager for an Indian F&O trading account.

You receive:
  1. A message from the signal group.
  2. A list of currently open positions held by the account.

Decide whether the message is asking for one or more management actions on
those positions.

Reply with a JSON ARRAY and nothing else — no prose, no code fences.
Each element of the array is one action:

{
  "action": "set_sl" | "set_target" | "partial_exit" | "exit",
  "symbol": exact symbol string from the positions list, or null if it applies
            to the only open position,
  "stop_loss": number or null,
  "target": number or null,
  "fraction": number between 0 and 1 for a partial exit, or null,
  "sl_to_cost": true | false,
  "reasoning": one short sentence
}

Rules, in order of importance:
1. Return [] if the message is a status report ("SL hit", "target achieved",
   "booked at 150"). Past tense = not an instruction.
2. Return [] if you are unsure what the message means.
3. Return [] if the message refers to a position that is not in the list.
4. A "set_sl" action must have either stop_loss or sl_to_cost=true.
5. A "set_target" action must have a target value.
6. A "partial_exit" action must have a fraction.
7. Do not invent a price the message did not state. Leave it null.
8. Multiple actions are allowed and will be executed left to right.
   Stop on first failure — include only actions you are confident about.
"""

_MANAGEMENT_VOCAB_RE = re.compile(
    r"\b(?:SL|STOP|STOPLOSS|TGT|TARGET|BOOK|EXIT|TRAIL|COST|ENTRY|PARTIAL|HALF|"
    r"REDUCE|TRIM|BREAKEVEN|BE|NO\s*LOSS|PROFIT|CLOSE|SQUARE)\b",
    re.IGNORECASE,
)


def looks_like_management(text: str) -> bool:
    """Quick heuristic: does this message look like a position management command?

    Called by ingest.py before paying for a model call. Returns True when the
    message carries management vocabulary and is not obviously a report.
    """
    if not text or len(text.strip()) < 4:
        return False
    norm = text.strip().upper()
    # Past-tense markers — these are reports, not instructions
    if re.search(
        r"\b(?:ACHIEVED|HIT|DONE|REACHED|BOOKED|FILLED|TRIGGERED|MADE|PROFIT\s+BOOKED)\b",
        norm,
    ):
        return False
    return bool(_MANAGEMENT_VOCAB_RE.search(norm))


def describe_positions(positions: list[dict[str, Any]]) -> str:
    """Compact position summary for the model prompt."""
    if not positions:
        return "No open positions."
    lines = []
    for p in positions:
        parts = [
            f"  Symbol: {p.get('symbol')}",
            f"Side: {p.get('side')}",
            f"Qty: {p.get('quantity')}",
        ]
        if p.get("entry_price") is not None:
            parts.append(f"Entry: {p['entry_price']}")
        if p.get("stop_loss") is not None:
            parts.append(f"SL: {p['stop_loss']}")
        if p.get("target") is not None:
            parts.append(f"Target: {p['target']}")
        lines.append("  " + "  ".join(parts))
    return "Open positions:\n" + "\n".join(lines)


def analyze_message(
    text: str,
    positions: list[dict[str, Any]],
    group: dict[str, Any],
) -> tuple[list[dict[str, Any]], str | None]:
    """Ask the model to extract management actions from a follow-up message.

    Returns (actions, reasoning_summary):
        actions             — list of validated action dicts (may be empty)
        reasoning_summary   — single string summarising what the model read,
                              for the suggestion row and the event log
    """
    if not positions:
        return [], "No open positions to manage."

    message = (text or "").strip()[:MAX_INPUT_CHARS]
    if not message:
        return [], "Empty message."

    pos_desc = describe_positions(positions)
    user_content = f"Message: {message}\n\n{pos_desc}"

    raw = _complete(user_content)
    if not raw:
        return [], "Model did not respond."

    actions, reasoning = _parse_reply(raw, positions)
    return actions, reasoning


def _complete(user_content: str) -> str | None:
    """Run the LLM completion. Returns accumulated text or None."""
    api_key = None
    call_kwargs = None
    try:
        from database import agent_db
        from services.agent import builder, providers

        resolved = builder.resolve_model()
        api_key = agent_db.get_api_key_for_model(resolved.id, resolved.provider_kind)
        call_kwargs = providers.litellm_kwargs(resolved, api_key)

        import litellm

        stream = litellm.completion(
            model=call_kwargs["id"],
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            max_tokens=MAX_OUTPUT_TOKENS,
            timeout=LLM_TIMEOUT_SECONDS,
            num_retries=0,
            stream=True,
            api_key=call_kwargs.get("api_key"),
            api_base=call_kwargs.get("api_base"),
        )
        chunks: list[str] = []
        for chunk in stream:
            try:
                delta = chunk.choices[0].delta
                piece = getattr(delta, "content", None) if delta else None
            except (AttributeError, IndexError, KeyError):
                piece = None
            if piece:
                chunks.append(piece)
        return "".join(chunks).strip() or None
    except Exception as exc:
        logger.error("WhatsApp AI manager LLM call failed: %s", exc)
        return None
    finally:
        api_key = None
        call_kwargs = None


def _parse_reply(
    content: str, positions: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], str]:
    """Validate the model's JSON array reply into action dicts."""
    match = _JSON_ARRAY_RE.search(content)
    if not match:
        logger.warning("AI manager reply carried no JSON array")
        return [], "Model reply had no JSON array."

    try:
        raw_list = json.loads(match.group(0))
    except ValueError:
        logger.warning("AI manager reply was not valid JSON")
        return [], "Model reply was not valid JSON."

    if not isinstance(raw_list, list):
        return [], "Model reply was not a JSON array."

    known_symbols = {p["symbol"] for p in positions}
    valid_actions: list[dict[str, Any]] = []
    reasoning_parts: list[str] = []

    for item in raw_list:
        if not isinstance(item, dict):
            continue
        validated, reason = _validate_action(item, known_symbols, len(positions))
        if validated is not None:
            valid_actions.append(validated)
            reasoning_parts.append(item.get("reasoning") or reason)
        else:
            logger.info("AI manager discarded action: %s — %s", item.get("action"), reason)

    summary = "; ".join(reasoning_parts) if reasoning_parts else "No actionable instructions."
    return valid_actions, summary


def _validate_action(
    item: dict[str, Any],
    known_symbols: set[str],
    position_count: int,
) -> tuple[dict[str, Any] | None, str]:
    """Validate one action dict from the model. Returns (action, reason)."""
    action = str(item.get("action") or "").strip().lower()
    if action not in ("set_sl", "set_target", "partial_exit", "exit"):
        return None, f"unknown action '{action}'"

    symbol = item.get("symbol")
    if symbol is not None:
        symbol = str(symbol).strip().upper()
        if symbol not in known_symbols:
            return None, f"symbol '{symbol}' not in open positions"
    elif position_count > 1:
        # Unqualified action with multiple positions is ambiguous.
        return None, "no symbol specified with multiple open positions"

    stop_loss = _as_number(item.get("stop_loss"))
    target = _as_number(item.get("target"))
    sl_to_cost = bool(item.get("sl_to_cost"))
    fraction = _as_number(item.get("fraction"))

    if action == "set_sl" and stop_loss is None and not sl_to_cost:
        return None, "set_sl needs stop_loss or sl_to_cost"
    if action == "set_target" and target is None:
        return None, "set_target needs a target value"
    if action == "partial_exit":
        if fraction is None or not (0 < fraction < 1):
            return None, "partial_exit needs a fraction between 0 and 1"

    return {
        "action": action,
        "symbol": symbol,
        "stop_loss": stop_loss,
        "target": target,
        "sl_to_cost": sl_to_cost,
        "fraction": fraction,
    }, "valid"


def _as_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if n != n or n in (float("inf"), float("-inf")):
        return None
    return n

