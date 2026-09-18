"""Turn a parsed leg into an instrument the broker will accept.

``parser.py`` produces what the message said -- ``NIFTY``, ``25000``, ``CE``.
This module decides whether such a thing exists: on which exchange it is
listed, in which expiry, at what lot size, and under which OpenAlgo symbol. The
master contract is the authority for all of it, so a strike nobody lists and an
underlying that was never a symbol both fail here, before any order path.

That is also the safety net for the one ambiguity the parser cannot resolve.
"PRICE 25000 CE" has the exact shape of an option leg; it fails to resolve
because no underlying named PRICE is listed, and the signal is recorded as
unresolvable rather than traded.

Expiry is read out of the **symbol**, not the expiry column. The OpenAlgo symbol
format is fixed (``[Base][DDMMMYY][Strike][CE/PE]``) while the column's own
format varies by broker feed, and a nearest-expiry choice made from a field that
sometimes parses and sometimes does not is a choice that silently picks the
wrong week.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from database.symbol import SymToken, db_session
from utils.logging import get_logger

logger = get_logger(__name__)

#: Index underlyings and the F&O exchange that lists their options.
_NSE_INDEX_UNDERLYINGS = {
    "NIFTY",
    "BANKNIFTY",
    "FINNIFTY",
    "MIDCPNIFTY",
    "NIFTYNXT50",
}
_BSE_INDEX_UNDERLYINGS = {"SENSEX", "BANKEX", "SENSEX50"}

#: Search order when the base is not a known index. A stock option is on NFO,
#: a commodity on MCX, a currency on CDS; trying them in this order costs one
#: indexed lookup each and settles the common case on the first.
_DERIVATIVE_EXCHANGES = ("NFO", "BFO", "MCX", "CDS")
_EQUITY_EXCHANGES = ("NSE", "BSE")

_DERIVATIVE_PRODUCTS = {"MIS", "NRML"}
_EQUITY_PRODUCTS = {"MIS", "CNC"}

_EXPIRY_IN_SYMBOL = r"(\d{2}[A-Z]{3}\d{2})"


@dataclass(frozen=True)
class ResolvedInstrument:
    """A tradable instrument, as the order path needs it."""

    symbol: str
    exchange: str
    product: str
    lotsize: int
    expiry: str | None
    kind: str  # option | future | equity

    @property
    def is_derivative(self) -> bool:
        return self.kind in ("option", "future")


def _strike_text(strike: float) -> str:
    """Strike as it appears inside an OpenAlgo symbol: 25000, or 292.5."""
    return str(int(strike)) if float(strike) == int(strike) else str(strike)


def _exchange_candidates(base: str) -> tuple[str, ...]:
    if base in _NSE_INDEX_UNDERLYINGS:
        return ("NFO",)
    if base in _BSE_INDEX_UNDERLYINGS:
        return ("BFO",)
    return _DERIVATIVE_EXCHANGES


def _expiry_date(symbol: str, pattern: re.Pattern) -> datetime | None:
    match = pattern.match(symbol or "")
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%d%b%y")
    except ValueError:
        return None


def _product_for(exchange: str, requested: str) -> str:
    """The product to send, corrected to what the instrument class allows.

    A group configured NRML that signals an equity trade would otherwise send a
    product the exchange rejects. Correcting is right here: the operator chose a
    default for options and an equity signal is the exception, not a new
    instruction.
    """
    requested = (requested or "MIS").upper()
    allowed = _EQUITY_PRODUCTS if exchange in _EQUITY_EXCHANGES else _DERIVATIVE_PRODUCTS
    return requested if requested in allowed else "MIS"


def resolve_option(
    base: str,
    strike: float,
    option_type: str,
    expiry: str | None,
    product: str,
) -> tuple[ResolvedInstrument | None, str | None]:
    """Find the listed option contract. Returns (instrument, error)."""
    base = (base or "").upper()
    option_type = (option_type or "").upper()
    if not base or option_type not in ("CE", "PE") or strike is None:
        return None, "The message did not name a complete option contract."

    strike_text = _strike_text(strike)
    wanted_expiry = (expiry or "").upper() or None
    pattern = re.compile(
        rf"^{re.escape(base)}{_EXPIRY_IN_SYMBOL}{re.escape(strike_text)}{option_type}$"
    )
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    for exchange in _exchange_candidates(base):
        try:
            rows = (
                db_session.query(SymToken)
                .filter(
                    SymToken.exchange == exchange,
                    SymToken.symbol.like(f"{base}%{strike_text}{option_type}"),
                )
                .all()
            )
        except Exception:
            logger.exception("Master contract lookup failed for %s on %s", base, exchange)
            db_session.rollback()
            continue

        candidates = []
        for row in rows:
            expiry_on = _expiry_date(row.symbol, pattern)
            if expiry_on is None:
                continue
            if wanted_expiry:
                if pattern.match(row.symbol).group(1) != wanted_expiry:
                    continue
            elif expiry_on < today:
                continue  # an expired contract is never the nearest one
            candidates.append((expiry_on, row))

        if not candidates:
            continue

        candidates.sort(key=lambda pair: pair[0])
        _, row = candidates[0]
        lotsize = int(row.lotsize or 0)
        if lotsize <= 0:
            return None, f"{row.symbol} has no lot size in the master contract."
        return (
            ResolvedInstrument(
                symbol=row.symbol,
                exchange=exchange,
                product=_product_for(exchange, product),
                lotsize=lotsize,
                expiry=row.expiry,
                kind="option",
            ),
            None,
        )

    if wanted_expiry:
        return None, (
            f"{base} {strike_text} {option_type} expiring {wanted_expiry} is not listed. "
            "Check the strike and the expiry."
        )
    return None, (
        f"{base} {strike_text} {option_type} is not listed in any unexpired contract. "
        "Check the strike, or download the master contract if it is stale."
    )


def resolve_future(base: str, product: str) -> tuple[ResolvedInstrument | None, str | None]:
    """Find the near-month future for a base symbol."""
    from services.option_symbol_service import find_near_month_futures

    base = (base or "").upper()
    if not base:
        return None, "The message did not name a futures contract."

    for exchange in _exchange_candidates(base):
        found = find_near_month_futures(base, exchange)
        if not found:
            continue
        row = (
            db_session.query(SymToken)
            .filter(SymToken.symbol == found["symbol"], SymToken.exchange == exchange)
            .first()
        )
        lotsize = int(row.lotsize or 0) if row else 0
        if lotsize <= 0:
            return None, f"{found['symbol']} has no lot size in the master contract."
        return (
            ResolvedInstrument(
                symbol=found["symbol"],
                exchange=exchange,
                product=_product_for(exchange, product),
                lotsize=lotsize,
                expiry=found.get("expiry"),
                kind="future",
            ),
            None,
        )
    return None, f"{base} has no unexpired futures contract listed."


def resolve_equity(base: str, product: str) -> tuple[ResolvedInstrument | None, str | None]:
    """Find a cash-market symbol."""
    base = (base or "").upper()
    if not base:
        return None, "The message did not name a stock."

    for exchange in _EQUITY_EXCHANGES:
        try:
            row = (
                db_session.query(SymToken)
                .filter(SymToken.symbol == base, SymToken.exchange == exchange)
                .first()
            )
        except Exception:
            logger.exception("Master contract lookup failed for %s on %s", base, exchange)
            db_session.rollback()
            continue
        if row:
            return (
                ResolvedInstrument(
                    symbol=row.symbol,
                    exchange=exchange,
                    product=_product_for(exchange, product),
                    lotsize=int(row.lotsize or 1) or 1,
                    expiry=None,
                    kind="equity",
                ),
                None,
            )
    return None, f"{base} is not a listed symbol. Check the spelling."


def resolve(signal, product: str) -> tuple[ResolvedInstrument | None, str | None]:
    """Resolve whatever instrument a parsed signal names."""
    if signal.option_type and signal.strike is not None:
        return resolve_option(
            signal.base, signal.strike, signal.option_type, signal.expiry, product
        )
    if signal.is_futures:
        return resolve_future(signal.base, product)
    if signal.base:
        return resolve_equity(signal.base, product)
    return None, "The message did not name an instrument."


def remove_session() -> None:
    """Release the master-contract session. Background workers have no teardown."""
    try:
        db_session.remove()
    except Exception:
        logger.debug("symbol db_session.remove failed", exc_info=True)
