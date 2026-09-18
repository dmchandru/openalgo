"""Turn a WhatsApp group message into a trading instruction, or into nothing.

This module performs no I/O. It takes a string and returns a
:class:`ParsedSignal`, which is what makes the whole feature testable without a
broker, a database or a phone: every shape a real group posts can be pinned as
a table row in ``tests/test_parser.py``.

Two tiers parse a message. This is the first: a set of ordered regex rules for
the shapes an Indian options group actually posts. It is deterministic, costs
nothing, and is the only tier most messages ever reach. Anything it cannot read
falls through to ``llm.py``, which is slower, costs money and can be wrong --
so the regex tier is written to cover as much as it honestly can.

**The distinction this module exists to make** is between an instruction and a
report. A group posts both, in the same voice, seconds apart:

    SL to 110              move my stop        -> set_sl
    SL hit                 telling me it blew  -> nothing
    TGT 150                aim for this        -> set_target
    Target 150 achieved    already happened    -> nothing
    Book half              do it now           -> partial_exit
    Booked half at 150     they already did    -> nothing

Reading the second column as the first is how a position gets a stop it never
should have had, so a message carrying a past-tense marker and nothing
imperative resolves to ``none`` and is recorded rather than acted on. Ambiguity
resolves to doing nothing, every time. A missed signal costs an opportunity; an
invented one costs money.

Nothing here resolves a symbol. ``NIFTY 25000 CE`` leaves this module as the
three fields it looks like; whether that instrument exists, on which exchange,
in which expiry and at what lot size is ``resolver.py``'s job against the
master contract.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

#: Every action the rest of the add-on knows how to execute.
ENTRY = "entry"
EXIT = "exit"
PARTIAL_EXIT = "partial_exit"
SET_SL = "set_sl"
SET_TARGET = "set_target"
NONE = "none"

ACTIONS = (ENTRY, EXIT, PARTIAL_EXIT, SET_SL, SET_TARGET, NONE)

_NUM = r"(\d+(?:\.\d+)?)"

# Decorations a group message is full of and that carry no meaning: currency
# marks, WhatsApp emphasis, bullets, arrows, emoji. Stripped before matching so
# "*BUY NIFTY 25000 CE*" and "BUY NIFTY 25000 CE" are one shape.
_DECORATION_RE = re.compile(r"[*_~`₹•→➡️#]|[^\S\n]{2,}")
_EMOJI_RE = re.compile(
    "[\U0001f000-\U0001faff\U00002600-\U000027bf\U0001f1e6-\U0001f1ff]+", flags=re.UNICODE
)

# --- instrument ------------------------------------------------------------

_OPTION_TYPE = r"(?:CE|PE|CALL|PUT)"
# NIFTY 25000 CE  |  NIFTY 25OCT25 25000CE  |  BANKNIFTY 52000 PE
_OPTION_LEG_RE = re.compile(
    r"\b(?P<base>[A-Z][A-Z&\-]{1,19}?)\s*"
    r"(?P<expiry>\d{1,2}[A-Z]{3}\d{2})?\s*"
    r"(?P<strike>\d{2,6}(?:\.\d+)?)\s*"
    r"(?P<opt>" + _OPTION_TYPE + r")\b"
)
# NIFTY CE 25000 — the same leg written the other way round. The space between
# base and type is REQUIRED here, unlike the pattern above: with \s* the engine
# splits ordinary words that happen to end in CE or PE, so "reduce 25%" parsed
# as base REDU, type CE, strike 25. A phantom leg is not harmless -- it is what
# a later "SL to 110" would try to match itself against.
_OPTION_LEG_ALT_RE = re.compile(
    r"\b(?P<base>[A-Z][A-Z&\-]{1,19})\s+"
    r"(?P<opt>" + _OPTION_TYPE + r")\s*"
    r"(?P<strike>\d{2,6}(?:\.\d+)?)\b"
)
_FUTURES_RE = re.compile(r"\b(?P<base>[A-Z][A-Z&\-]{1,19}?)\s*(?:FUT|FUTURES|FUT\.)\b")

# --- verbs -----------------------------------------------------------------

_BUY_RE = re.compile(r"\b(?:BUY|BUYING|LONG|GO\s+LONG|ADD)\b")
_SELL_RE = re.compile(r"\b(?:SELL|SELLING|SHORT|GO\s+SHORT|WRITE)\b")

_EXIT_RE = re.compile(
    r"\b(?:EXIT|SQUARE\s*-?\s*OFF|SQUAREOFF|SQ\s*OFF|CLOSE\s+(?:ALL|POSITION|IT)|"
    r"BOOK\s+(?:FULL|ALL|OUT|COMPLETE)|GET\s+OUT|CUT\s+(?:IT|ALL))\b"
)
# No trailing \b after the percent branch: "%" is not a word character, so a
# boundary there would demand a letter after it and "book 50%" -- which is how
# the instruction is actually written -- would not match.
_PARTIAL_RE = re.compile(
    r"\b(?:BOOK|EXIT|SELL|REDUCE|TRIM)\s+(?:PARTIAL\b|HALF\b|"
    + _NUM
    + r"\s*(?:%|PERCENT\b|PCT\b))|"
    r"\bPARTIAL\s+(?:BOOK|EXIT)\b"
)
_PERCENT_RE = re.compile(r"\b" + _NUM + r"\s*(?:%|PERCENT\b|PCT\b)")
_HALF_RE = re.compile(r"\bHALF\b")
_BOOK_PROFIT_RE = re.compile(r"\bBOOK\s+(?:PROFIT|PROFITS|IT|NOW)\b")

_TRAIL_RE = re.compile(r"\bTRAIL(?:ING)?\b")

# --- levels ----------------------------------------------------------------

_SL_RE = re.compile(
    r"\b(?:SL|S\s*/\s*L|STOP\s*-?\s*LOSS|STOPLOSS|STOP)\s*"
    r"(?:IS|TO|AT|@|:|=|REVISED\s+TO|MOVED\s+TO)?\s*" + _NUM
)
_SL_TO_COST_RE = re.compile(
    r"\b(?:SL|S\s*/\s*L|STOP\s*-?\s*LOSS|STOPLOSS)\s*"
    r"(?:IS|TO|AT|@|:|=)?\s*(?:COST|ENTRY|BREAK\s*-?\s*EVEN|BE|NO\s*LOSS)\b"
)
_TARGET_RE = re.compile(r"\b(?:TGT|TARGET|TP|T1)\s*(?:IS|TO|AT|@|:|=)?\s*" + _NUM)
_ENTRY_PRICE_RE = re.compile(
    r"(?:@|\bAT\b|\bABOVE\b|\bAROUND\b|\bNEAR\b|\bCMP\b|\bPRICE\b|\bENTRY\b|\bRS\.?)\s*" + _NUM
)
_LOTS_RE = re.compile(r"\b(\d{1,3})\s*LOTS?\b")

# --- report, not instruction ----------------------------------------------

# Past-tense and outcome markers. \b keeps BOOKED from matching BOOK.
_REPORT_RE = re.compile(
    r"\b(?:ACHIEVED|ACHIVED|DONE|REACHED|BOOKED|COMPLETED|MET|HIT|TRIGGERED|"
    r"FILLED|GONE|MADE|PROFIT\s+BOOKED|CAME)\b"
)
# Questions and commentary are never instructions.
_CHATTER_RE = re.compile(r"[?]\s*$|\b(?:WHAT|WHY|HOW|WHEN|ANYONE|SIR|THANKS|THANK\s+YOU)\b")


def normalize(text: str) -> str:
    """Upper-case, strip decoration, and collapse whitespace.

    Newlines survive as spaces: a four-line signal and a one-line signal carry
    the same instruction and must parse identically.
    """
    if not text:
        return ""
    cleaned = _EMOJI_RE.sub(" ", text)
    cleaned = _DECORATION_RE.sub(" ", cleaned)
    cleaned = cleaned.replace("\n", " ").replace("\r", " ")
    return re.sub(r"\s+", " ", cleaned).strip().upper()


@dataclass(frozen=True)
class ParsedSignal:
    """What a message asks for, in the add-on's own vocabulary."""

    action: str = NONE
    tier: str = "regex"

    #: BUY or SELL. Set on an entry; ``None`` on a level change, which applies
    #: to whatever side the position is already on.
    side: str | None = None

    base: str | None = None
    strike: float | None = None
    option_type: str | None = None  # CE | PE
    expiry: str | None = None  # DDMMMYY as written, or None for nearest
    is_futures: bool = False

    entry_price: float | None = None
    stop_loss: float | None = None
    target: float | None = None
    sl_to_cost: bool = False
    trail: bool = False

    lots: int | None = None
    #: Fraction of the position a partial exit should close, 0 < f < 1.
    fraction: float | None = None

    #: True when the message reads as a report of something that already
    #: happened. The LLM tier is skipped for these: there is nothing to act on
    #: and asking a model to find an action in a status update is how one gets
    #: invented.
    informational: bool = False

    #: Why the parse landed where it did, for the event log.
    note: str | None = None
    raw: str = ""
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def names_leg(self) -> bool:
        """Whether the message identifies an instrument of its own.

        A follow-up that names its leg ("NIFTY 25000 CE SL 110") can be matched
        to a position with certainty. One that does not ("SL 110") has to be
        matched by context, and is refused when the context is ambiguous.
        """
        return bool(self.base and (self.option_type or self.is_futures))

    @property
    def is_actionable(self) -> bool:
        return self.action != NONE

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["warnings"] = list(self.warnings)
        return data


def _first_float(match: re.Match | None, group: int = 1) -> float | None:
    if not match:
        return None
    try:
        return float(match.group(group))
    except (TypeError, ValueError, IndexError):
        return None


def _normalize_option_type(raw: str | None) -> str | None:
    if not raw:
        return None
    raw = raw.upper()
    if raw in ("CE", "CALL"):
        return "CE"
    if raw in ("PE", "PUT"):
        return "PE"
    return None


def _extract_leg(text: str) -> dict[str, Any]:
    """Pull an instrument out of the message, if it names one.

    Shape alone cannot settle every case: "PRICE 25000 CE" has the exact form of
    a leg and is not one. This module does not try to tell them apart, because
    it has no way to -- ``resolver.py`` looks the base up in the master contract
    and an underlying that does not exist there resolves to nothing, so a
    phantom leg dies before it reaches an order path.
    """
    match = _OPTION_LEG_RE.search(text) or _OPTION_LEG_ALT_RE.search(text)
    if match:
        groups = match.groupdict()
        base = (groups.get("base") or "").strip("-& ")
        # A verb glued to the base ("BUYNIFTY" never happens, but "BUY NIFTY"
        # can capture "BUY" when the base is missing) is not an instrument.
        if base in ("BUY", "SELL", "SL", "TGT", "TARGET", "EXIT", "BOOK"):
            base = ""
        try:
            strike = float(groups["strike"])
        except (TypeError, ValueError):
            strike = None
        return {
            "base": base or None,
            "strike": strike,
            "option_type": _normalize_option_type(groups.get("opt")),
            "expiry": groups.get("expiry"),
            "is_futures": False,
        }

    fut = _FUTURES_RE.search(text)
    if fut:
        return {
            "base": fut.group("base"),
            "strike": None,
            "option_type": None,
            "expiry": None,
            "is_futures": True,
        }
    return {"base": None, "strike": None, "option_type": None, "expiry": None, "is_futures": False}


_EQUITY_ENTRY_RE = re.compile(
    r"\b(?:BUY|SELL)\s+(?P<base>[A-Z][A-Z&\-]{1,19})\b(?!\s*\d{2,6}\s*(?:CE|PE|CALL|PUT))"
)


def _partial_hint(text: str) -> float | None:
    """A size stated alongside an exit verb, or None when none was.

    Distinct from :func:`_extract_fraction`, which answers "how much" for a
    message already known to be a partial and defaults to half. This one only
    reports a size the message actually states, so a plain "exit" is not
    quietly turned into a half exit.
    """
    pct = _PERCENT_RE.search(text)
    if pct:
        try:
            value = float(pct.group(1)) / 100.0
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None
    if _HALF_RE.search(text):
        return 0.5
    return None


def _extract_fraction(text: str) -> float | None:
    """How much of a position a partial instruction means to close."""
    pct = _PERCENT_RE.search(text)
    if pct:
        try:
            value = float(pct.group(1)) / 100.0
        except (TypeError, ValueError):
            return None
        if 0 < value < 1:
            return value
        # "book 100%" is a full exit, which the caller handles as one.
        return 1.0 if value >= 1 else None
    if _HALF_RE.search(text):
        return 0.5
    return 0.5  # "book partial" with no number: half is the group convention


def parse(text: str) -> ParsedSignal:
    """Read one message. Never raises; an unreadable message parses to ``none``."""
    raw = text or ""
    norm = normalize(raw)
    if not norm:
        return ParsedSignal(raw=raw, note="empty message")

    # A slash-command belongs to the upstream WhatsApp bot, not to us.
    if norm.startswith("/"):
        return ParsedSignal(raw=raw, note="bot command, not a signal")

    leg = _extract_leg(norm)
    has_buy = bool(_BUY_RE.search(norm))
    has_sell = bool(_SELL_RE.search(norm))
    is_report = bool(_REPORT_RE.search(norm))
    is_chatter = bool(_CHATTER_RE.search(norm))

    sl_value = _first_float(_SL_RE.search(norm))
    sl_to_cost = bool(_SL_TO_COST_RE.search(norm))
    target_value = _first_float(_TARGET_RE.search(norm))
    entry_price = _first_float(_ENTRY_PRICE_RE.search(norm))
    trail = bool(_TRAIL_RE.search(norm))
    lots_match = _LOTS_RE.search(norm)
    lots = int(lots_match.group(1)) if lots_match else None

    common = {
        "raw": raw,
        "base": leg["base"],
        "strike": leg["strike"],
        "option_type": leg["option_type"],
        "expiry": leg["expiry"],
        "is_futures": leg["is_futures"],
    }

    # 1. Entry. Needs a direction AND an instrument: a bare "buy" is not a
    #    trade, and a bare instrument is a mention.
    if (has_buy or has_sell) and (leg["option_type"] or leg["is_futures"]):
        side = "BUY" if has_buy else "SELL"
        # An entry price that is really the strike ("BUY NIFTY 25000 CE" with no
        # @) must not become the entry. _ENTRY_PRICE_RE requires a price marker,
        # so this only guards the case where the marker precedes the strike.
        if entry_price is not None and leg["strike"] is not None and entry_price == leg["strike"]:
            entry_price = None
        return ParsedSignal(
            action=ENTRY,
            side=side,
            entry_price=entry_price,
            stop_loss=sl_value,
            target=target_value,
            trail=trail,
            lots=lots,
            note="option entry",
            **common,
        )

    equity = _EQUITY_ENTRY_RE.search(norm)
    if (has_buy or has_sell) and equity and not leg["option_type"]:
        return ParsedSignal(
            action=ENTRY,
            side="BUY" if has_buy else "SELL",
            base=equity.group("base"),
            strike=None,
            option_type=None,
            expiry=None,
            is_futures=False,
            entry_price=entry_price,
            stop_loss=sl_value,
            target=target_value,
            trail=trail,
            lots=lots,
            note="equity entry",
            raw=raw,
        )

    # 2. Partial exit before full exit: "book half" contains neither an exit
    #    verb nor a full-exit intent, and "book 50%" must not read as "book".
    if _PARTIAL_RE.search(norm) and not is_report:
        fraction = _extract_fraction(norm)
        if fraction is not None and fraction >= 1.0:
            return ParsedSignal(action=EXIT, note="book 100% reads as a full exit", **common)
        return ParsedSignal(action=PARTIAL_EXIT, fraction=fraction, note="partial exit", **common)

    # 3. An exit verb that names a size is a partial, whatever verb it used.
    #    "get out of half the position" carries a full-exit verb and a partial
    #    size, and reading only the verb closes the whole position when the
    #    instruction was to keep some on.
    exit_verb = bool(_EXIT_RE.search(norm) or _BOOK_PROFIT_RE.search(norm))
    if exit_verb and not is_report:
        sized = _partial_hint(norm)
        if sized is not None and sized < 1.0:
            return ParsedSignal(
                action=PARTIAL_EXIT,
                fraction=sized,
                note="exit of part of the position",
                **common,
            )
        return ParsedSignal(action=EXIT, note="exit", **common)

    # 4. Stop to cost, which is a level change with no number of its own.
    if sl_to_cost:
        return ParsedSignal(
            action=SET_SL, sl_to_cost=True, trail=trail, note="stop to cost", **common
        )

    # 5. A report that happens to carry a number is still a report. This is the
    #    guard that keeps "target 150 achieved" from setting a target.
    if is_report and (sl_value is not None or target_value is not None):
        return ParsedSignal(
            informational=True,
            note="reads as a status update, not an instruction",
            **common,
        )

    # 6. A level change on its own.
    if sl_value is not None:
        return ParsedSignal(
            action=SET_SL,
            stop_loss=sl_value,
            target=target_value,
            trail=trail,
            note="stop loss update",
            **common,
        )
    if target_value is not None:
        return ParsedSignal(action=SET_TARGET, target=target_value, note="target update", **common)

    if is_report:
        return ParsedSignal(informational=True, note="status update", **common)
    if is_chatter:
        return ParsedSignal(informational=True, note="conversation", **common)

    return ParsedSignal(note="no instruction found", **common)


#: Tokens that make a message worth the cost of an LLM call. A group is mostly
#: conversation; sending every line of it to a model is money spent to be told
#: "not a signal" several hundred times a day.
_LLM_WORTH_TRYING_RE = re.compile(
    r"\b(?:BUY|SELL|LONG|SHORT|CE|PE|CALL|PUT|FUT|SL|STOP|TGT|TARGET|EXIT|BOOK|"
    r"TRAIL|SQUARE|ENTRY|CMP|LOT|LOTS|CLOSE|POSITION|HOLD|ADD|TRIM|HEDGE)\b"
)


def worth_llm_attempt(text: str, parsed: ParsedSignal) -> bool:
    """Whether a message the regex tier could not read should go to the model.

    Three things disqualify it: the regex tier already read it, it reads as a
    report (there is no instruction in it to find), or it carries none of the
    vocabulary a signal is made of.
    """
    if parsed.is_actionable or parsed.informational:
        return False
    norm = normalize(text)
    if len(norm) < 4 or norm.startswith("/"):
        return False
    return bool(_LLM_WORTH_TRYING_RE.search(norm))
