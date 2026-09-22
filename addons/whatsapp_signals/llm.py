"""The second parsing tier: ask the configured model what a message means.

Reached only for messages ``parser.py`` could not read and that
``parser.worth_llm_attempt`` judged worth the call. A group is mostly
conversation, and paying a model to be told "not a signal" four hundred times a
day is the failure mode this gate exists to prevent.

The model used is whichever one the operator already configured for the Agent
under Settings; nothing separate is set up here, and a deployment with no model
configured simply runs on the regex tier alone.

Two properties matter more than coverage:

**It answers in one shape or not at all.** The model is given a closed schema
and its reply is validated field by field against the same
:class:`~addons.whatsapp_signals.parser.ParsedSignal` the regex tier produces,
so the executor downstream cannot tell which tier answered. A reply that is not
valid JSON, names an action outside the set, or omits what its action needs
resolves to ``none``.

**It is never asked to be helpful.** The prompt tells it to return ``none``
whenever the message is commentary, a status report, or ambiguous about which
position it refers to. A model that guesses is worse than no model here: the
regex tier already caught everything unambiguous, so every message reaching
this one is a message where guessing is the risk.

The call is streamed. That is not a preference: LiteLLM's non-streaming reader
raises on a perfectly good reply from the ChatGPT subscription provider, which
an operator may well have configured, and streaming is the path the rest of the
platform takes for the same reason.
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

#: A signal message is short. Anything longer is a forwarded article.
MAX_INPUT_CHARS = 600
#: Enough for the JSON object and no more.
MAX_OUTPUT_TOKENS = 300
LLM_TIMEOUT_SECONDS = 20.0

_SYSTEM_PROMPT = """You read one message from an Indian stock-market WhatsApp \
group and decide whether it is an instruction to trade.

Reply with ONE JSON object and nothing else. No prose, no code fence.

{
  "action": "entry" | "exit" | "partial_exit" | "set_sl" | "set_target" | "none",
  "side": "BUY" | "SELL" | null,
  "base": underlying symbol in capitals, e.g. "NIFTY", "BANKNIFTY", "RELIANCE", or null,
  "strike": number or null,
  "option_type": "CE" | "PE" | null,
  "expiry": "DDMMMYY" as written in the message, or null,
  "is_futures": true | false,
  "entry_price": number or null,
  "is_above_price": true | false,
  "stop_loss": number or null,
  "target": number or null,
  "targets": [number, ...] or null,
  "sl_to_cost": true | false,
  "trail": true | false,
  "lots": integer or null,
  "fraction": number between 0 and 1 for a partial exit, else null,
  "reason": one short sentence, plain English, no jargon
}

Rules, in order of importance:

1. Return "none" unless the message tells someone to do something RIGHT NOW.
   A report of what already happened is not an instruction. "Target achieved",
   "SL hit", "booked at 150", "we made 30 points" are all "none".
2. Return "none" if you are unsure. A missed signal costs an opportunity; an
   invented one costs money.
3. Return "none" for questions, opinions, greetings, analysis, and anything
   about a market view rather than a position.
4. "entry" needs a direction and an instrument. Without both, it is "none".
5. Levels stated inside an entry message belong to that entry: put them in
   stop_loss and target, and keep action "entry".
6. Do not invent a price, a strike or an expiry the message does not state.
   Leave it null.
"""

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)

_NUMERIC_FIELDS = ("strike", "entry_price", "stop_loss", "target", "fraction")


def is_available() -> bool:
    """Whether a model is configured for the LLM tier to use."""
    try:
        from database import agent_db

        return bool(agent_db.is_configured())
    except Exception:
        logger.debug("LLM availability check failed", exc_info=True)
        return False


def parse(text: str) -> ParsedSignal | None:
    """Ask the model to read one message.

    Returns:
        A :class:`ParsedSignal` tagged ``tier="llm"``, or None when no model is
        configured, the provider refused, or the reply was not usable. None
        means "the regex tier's answer stands", which is ``none``.
    """
    message = (text or "").strip()[:MAX_INPUT_CHARS]
    if not message:
        return None

    content = _complete(message)
    if not content:
        return None
    return _to_signal(content, raw=text or "")


def _complete(message: str) -> str | None:
    """Run the completion and return the accumulated text.

    This frame holds a decrypted provider key in its locals, so its failure path
    logs ``str(exc)`` via ``logger.error`` rather than ``logger.exception``:
    ``exc_info`` captures locals, and that would write the operator's key into
    ``log/errors.jsonl`` in plaintext.
    """
    api_key = None
    call_kwargs = None
    try:
        from database import agent_db
        from services.agent import builder, providers

        resolved = builder.resolve_model()
        api_key = agent_db.get_api_key_for_model(resolved.id, resolved.provider_kind)
        call_kwargs = providers.litellm_kwargs(resolved, api_key)

        # Imported here, not at module scope: importing litellm costs seconds of
        # start-up and the regex tier must not pay for a tier it never uses.
        import litellm

        stream = litellm.completion(
            model=call_kwargs["id"],
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": message},
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
        logger.error("WhatsApp signal LLM tier could not read a message: %s", exc)
        return None
    finally:
        api_key = None
        call_kwargs = None


def _to_signal(content: str, raw: str) -> ParsedSignal | None:
    """Validate the model's reply into a ParsedSignal, or discard it."""
    match = _JSON_RE.search(content)
    if not match:
        logger.warning("LLM tier reply carried no JSON object")
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        logger.warning("LLM tier reply was not valid JSON")
        return None
    if not isinstance(data, dict):
        return None

    action = str(data.get("action") or NONE).strip().lower()
    if action not in ACTIONS:
        logger.warning("LLM tier returned an unknown action: %r", action)
        return None

    values: dict[str, Any] = {}
    for field_name in _NUMERIC_FIELDS:
        values[field_name] = _as_number(data.get(field_name))

    side = str(data.get("side") or "").strip().upper() or None
    if side not in ("BUY", "SELL", None):
        side = None
    option_type = str(data.get("option_type") or "").strip().upper() or None
    if option_type not in ("CE", "PE", None):
        option_type = None
    base = str(data.get("base") or "").strip().upper() or None
    lots = _as_number(data.get("lots"))
    fraction = values["fraction"]
    if fraction is not None and not (0 < fraction < 1):
        fraction = None

    raw_targets = data.get("targets")
    targets: tuple[float, ...] = ()
    if isinstance(raw_targets, list):
        parsed_targets = []
        for v in raw_targets:
            n = _as_number(v)
            if n is not None and n > 0:
                parsed_targets.append(n)
        targets = tuple(parsed_targets)

    target = values["target"]
    if target is None and targets:
        target = targets[0]

    signal = ParsedSignal(
        action=action,
        tier="llm",
        side=side,
        base=base,
        strike=values["strike"],
        option_type=option_type,
        expiry=(str(data.get("expiry")).strip().upper() if data.get("expiry") else None),
        is_futures=bool(data.get("is_futures")),
        entry_price=values["entry_price"],
        is_above_price=bool(data.get("is_above_price")),
        stop_loss=values["stop_loss"],
        target=target,
        targets=targets,
        sl_to_cost=bool(data.get("sl_to_cost")),
        trail=bool(data.get("trail")),
        lots=int(lots) if lots and lots > 0 else None,
        fraction=fraction,
        note=(str(data.get("reason")).strip()[:200] if data.get("reason") else "read by the model"),
        raw=raw,
    )
    return _reject_incoherent(signal)


def _reject_incoherent(signal: ParsedSignal) -> ParsedSignal:
    """Downgrade a reply that does not carry what its own action needs.

    The model can return a well-formed object that says nothing usable -- an
    entry with no instrument, a stop change with no stop. Executing those would
    mean filling in the missing half by guessing, which is the one thing this
    tier must not do.
    """
    if signal.action == ENTRY:
        if not signal.side or not signal.base:
            return _downgrade(signal, "the model read an entry but named no instrument or side")
        if not (signal.option_type or signal.is_futures) and signal.strike is not None:
            return _downgrade(signal, "the model read a strike but no option type")
    elif signal.action == SET_SL:
        if signal.stop_loss is None and not signal.sl_to_cost:
            return _downgrade(signal, "the model read a stop change but no stop")
    elif signal.action == SET_TARGET:
        if signal.target is None:
            return _downgrade(signal, "the model read a target change but no target")
    elif signal.action == PARTIAL_EXIT:
        if signal.fraction is None:
            return _downgrade(signal, "the model read a partial exit but no size")
    elif signal.action not in (EXIT, NONE):
        return _downgrade(signal, "the model returned an action this add-on cannot execute")
    return signal


def _downgrade(signal: ParsedSignal, why: str) -> ParsedSignal:
    logger.info("LLM tier answer discarded: %s", why)
    return ParsedSignal(action=NONE, tier="llm", note=why, raw=signal.raw)


def _as_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):  # NaN / inf
        return None
    return number
