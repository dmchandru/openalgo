"""Context-aware AI signal parser for WhatsApp trading groups.

This module is the AI-first parsing tier, distinct from:
  - ``parser.py``   — the fast regex tier (run for all groups)
  - ``llm.py``      — single-message LLM fallback (for ambiguous messages)
  - ``ai_manager.py`` — position management suggestions for open positions

Activated per-group via ``ai_parser_mode=True``. When enabled, every inbound
message is sent to the LLM with:
  1. The last 10 messages from the group (context window)
  2. Any currently open positions for the group

This allows the model to resolve multi-message signal sequences such as:

  Msg 1: "Nifty 23400CE  Buy above 68  Stoploss 63"  → ENTRY
  Msg 2: "Active"                                     → NONE (already entered)
  Msg 3: "Tgt 78/95/115/140"                         → SET_TARGET targets=[78,95,115,140]
  Msg 4: "1st Target 78 hit  Book partial / full"    → PARTIAL_EXIT fraction=0.5

The module returns the same :class:`~addons.whatsapp_signals.parser.ParsedSignal`
that the regex tier produces, so the executor downstream is unchanged.
"""

from __future__ import annotations

import json
import re
from typing import Any

from addons.whatsapp_signals.parser import (
    ACTIONS,
    ENTRY,
    EXIT,
    NONE,
    PARTIAL_EXIT,
    SET_SL,
    SET_TARGET,
    ParsedSignal,
)
from utils.logging import get_logger

logger = get_logger(__name__)

MAX_OUTPUT_TOKENS = 400
LLM_TIMEOUT_SECONDS = 25.0
#: Context messages passed to the prompt (newest last).
CONTEXT_WINDOW = 10

_SYSTEM_PROMPT = """\
You read messages from an Indian stock-market WhatsApp signal group and decide
what SINGLE trading action to take for the CURRENT message.

You receive three inputs:
1. CURRENT MESSAGE — the message to act on right now.
2. RECENT MESSAGES — the last few messages (oldest first) for context only.
3. OPEN POSITIONS — positions currently held (may be empty).

Signal format used by this group (multiple messages per trade):
  Msg 1: "Nifty 23400CE  Buy above 68  Stoploss 63"
         → entry: BUY NIFTY 23400 CE, entry_price 68 (trigger "above"), sl 63
  Msg 2: "Active"
         → confirms the entry is live. Action: none (already entered in msg 1).
  Msg 3: "Tgt 78/95/115/140"
         → set_target: primary target 78, full list [78,95,115,140]
  Msg 4: "1st Target 78 hit 🎯  Book partial / full"
         → partial_exit: fraction 0.5 (even though it mentions "hit", the
           imperative is "book partial/full", so this IS an instruction)

Rules (in priority order):
1. "Active" alone (after an entry message) → action "none".
2. "Tgt X/Y/Z/W" → action "set_target", target = X (first), targets = [X,Y,Z,W].
3. "Target N hit → Book partial/full" → action "partial_exit", fraction 0.5.
4. "Target N hit → Book full" or "Exit" → action "exit".
5. "SL to cost" / "SL to entry" → action "set_sl", sl_to_cost true.
6. "SL to X" → action "set_sl", stop_loss X.
7. Pure commentary, questions, greetings, status reports → action "none".
8. Return "none" if ambiguous. A missed signal costs opportunity; an invented
   one costs money.
9. Do NOT invent a symbol, strike, or price not in the messages.

Reply with ONE JSON object and nothing else. No prose, no code fence.

{
  "action": "entry" | "exit" | "partial_exit" | "set_sl" | "set_target" | "none",
  "side": "BUY" | "SELL" | null,
  "base": "NIFTY" | "BANKNIFTY" | ... | null,
  "strike": number | null,
  "option_type": "CE" | "PE" | null,
  "expiry": "DDMMMYY" | null,
  "is_futures": true | false,
  "entry_price": number | null,
  "is_above_price": true | false,
  "stop_loss": number | null,
  "sl_to_cost": true | false,
  "target": number | null,
  "targets": [number, ...] | null,
  "trail": false,
  "lots": integer | null,
  "fraction": number 0 to 1 | null,
  "reason": "one short sentence"
}
"""

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
_NUMERIC_FIELDS = ("strike", "entry_price", "stop_loss", "target", "fraction")


def parse(
    text: str,
    context: list[dict[str, Any]],
    open_positions: list[dict[str, Any]],
) -> ParsedSignal | None:
    """Ask the model to read ``text`` given the rolling context and positions.

    Returns:
        A :class:`ParsedSignal` tagged ``tier="ai_parser"``, or ``None`` when
        the model is unavailable or its reply is unusable (the regex tier's
        ``none`` answer stands in that case).
    """
    message = (text or "").strip()
    if not message:
        return None

    user_block = _build_user_block(message, context, open_positions)
    content = _complete(user_block)
    if not content:
        return None
    return _to_signal(content, raw=text or "")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_user_block(
    text: str,
    context: list[dict[str, Any]],
    open_positions: list[dict[str, Any]],
) -> str:
    """Format the three-section user message for the model."""
    parts: list[str] = []

    # Context window (exclude the current message if it was already appended)
    prior = [m for m in context if m.get("text", "").strip() != text.strip()][-CONTEXT_WINDOW:]
    if prior:
        parts.append("RECENT MESSAGES (oldest first):")
        for m in prior:
            sender = m.get("sender", "?")
            parts.append(f"  [{sender}] {m.get('text', '').strip()}")
        parts.append("")

    if open_positions:
        parts.append("OPEN POSITIONS:")
        for p in open_positions:
            parts.append(
                f"  {p.get('side')} {p.get('symbol')} qty={p.get('quantity')} "
                f"entry={p.get('entry_price')} sl={p.get('stop_loss')} tgt={p.get('target')}"
            )
        parts.append("")

    parts.append(f"CURRENT MESSAGE:\n{text}")
    return "\n".join(parts)


def _complete(user_block: str) -> str | None:
    """Call the LLM and return accumulated text. Key never touches logs."""
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
                {"role": "user", "content": user_block},
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
        logger.error("ai_parser LLM call failed: %s", exc)
        return None
    finally:
        api_key = None
        call_kwargs = None


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


def _to_signal(content: str, raw: str) -> ParsedSignal | None:
    """Validate model reply into a ParsedSignal, or return None."""
    match = _JSON_RE.search(content)
    if not match:
        logger.warning("ai_parser: no JSON in reply")
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        logger.warning("ai_parser: invalid JSON in reply")
        return None
    if not isinstance(data, dict):
        return None

    action = str(data.get("action") or NONE).strip().lower()
    if action not in ACTIONS:
        logger.warning("ai_parser: unknown action %r", action)
        return None

    values: dict[str, Any] = {f: _as_number(data.get(f)) for f in _NUMERIC_FIELDS}

    side = str(data.get("side") or "").strip().upper() or None
    if side not in ("BUY", "SELL", None):
        side = None

    option_type = str(data.get("option_type") or "").strip().upper() or None
    if option_type not in ("CE", "PE", None):
        option_type = None

    base = str(data.get("base") or "").strip().upper() or None

    fraction = values["fraction"]
    if fraction is not None and not (0 < fraction <= 1):
        fraction = 1.0 if fraction > 1 else None

    # targets list from model
    raw_targets = data.get("targets")
    targets: tuple[float, ...] = ()
    if isinstance(raw_targets, list):
        parsed_targets = []
        for v in raw_targets:
            n = _as_number(v)
            if n is not None and n > 0:
                parsed_targets.append(n)
        targets = tuple(parsed_targets)

    # primary target: model's "target" field, or first of targets list
    target = values["target"]
    if target is None and targets:
        target = targets[0]

    lots_raw = _as_number(data.get("lots"))
    lots = int(lots_raw) if lots_raw and lots_raw > 0 else None

    signal = ParsedSignal(
        action=action,
        tier="ai_parser",
        side=side,
        base=base,
        strike=values["strike"],
        option_type=option_type,
        expiry=(str(data.get("expiry")).strip().upper() if data.get("expiry") else None),
        is_futures=bool(data.get("is_futures")),
        entry_price=values["entry_price"],
        is_above_price=bool(data.get("is_above_price")),
        stop_loss=values["stop_loss"],
        sl_to_cost=bool(data.get("sl_to_cost")),
        target=target,
        targets=targets,
        trail=bool(data.get("trail")),
        lots=lots,
        fraction=fraction,
        note=str(data.get("reason") or "ai_parser").strip()[:200],
        raw=raw,
    )
    return _reject_incoherent(signal)


def _reject_incoherent(signal: ParsedSignal) -> ParsedSignal:
    """Downgrade a reply that does not carry what its own action needs."""
    if signal.action == ENTRY:
        if not signal.side or not signal.base:
            return _downgrade(signal, "entry with no instrument or side")
        if not (signal.option_type or signal.is_futures) and signal.strike is not None:
            return _downgrade(signal, "strike with no option type")
    elif signal.action == SET_SL:
        if signal.stop_loss is None and not signal.sl_to_cost:
            return _downgrade(signal, "set_sl with no stop value")
    elif signal.action == SET_TARGET:
        if signal.target is None:
            return _downgrade(signal, "set_target with no target value")
    elif signal.action == PARTIAL_EXIT:
        if signal.fraction is None:
            return _downgrade(signal, "partial_exit with no fraction")
    elif signal.action not in (EXIT, NONE):
        return _downgrade(signal, f"unknown action: {signal.action}")
    return signal


def _downgrade(signal: ParsedSignal, why: str) -> ParsedSignal:
    logger.info("ai_parser discarded reply: %s", why)
    return ParsedSignal(action=NONE, tier="ai_parser", note=why, raw=signal.raw)

