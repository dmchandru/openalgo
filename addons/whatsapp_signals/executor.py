"""Act on a parsed signal: place the order, and keep the stop up to date.

Nothing here decides *whether* a message is a signal -- that is settled by the
time this module is called. What it decides is whether a signal the group sent
is one this deployment has agreed to act on, and it answers that before it
touches an order path.

**The stop is not managed here.** An entry writes a row into
``scalping_sl_state`` and the platform's existing tick-driven risk monitor owns
it from that moment: it watches the live feed, trails the stop if trailing is
on, and fires a freeze-safe exit sized to the live position when the stop or
target is breached. A later "SL to 110" from the group is one update to that
same row. This add-on therefore contains no price loop, no stop evaluator and
no exit timer, which is deliberate -- the platform treats a second risk
evaluator as a defect, and the four ways one goes wrong are documented where
the first one lives.

Four rules shape everything below.

**The sandbox gate is asymmetric.** A group set to sandbox while the platform
is live is refused outright, because executing it would send a real order that
nobody asked for. A group set to live while the platform is in analyze mode is
executed and lands in the sandbox, because the global toggle is the operator
saying "nothing real right now" and honouring it is the safe direction. Both
outcomes are recorded with the mode they actually ran in.

**Exits are sized to the live position, never to what we think we hold.** The
risk monitor may have flattened the leg a second ago on its own stop. Reading
the position book first means a group "exit" after that sends nothing, instead
of opening a short.

**A refused or failed exit leaves the position open and managed.** The stop row
stays, the position row stays open. Reporting success and clearing state is how
a position ends up with nothing watching it.

**One leg holds one position.** An entry into an instrument this add-on already
holds adds to it and keeps a single stop row, because the stop is keyed by leg
and two rows cannot both own it.
"""

from __future__ import annotations

import math
from typing import Any

from addons.whatsapp_signals import db, resolver
from addons.whatsapp_signals.parser import (
    ENTRY,
    EXIT,
    PARTIAL_EXIT,
    SET_SL,
    SET_TARGET,
    ParsedSignal,
)
from utils.logging import get_logger

logger = get_logger(__name__)

#: Stamped on every order so the order book, the position book and Close-All all
#: show where the trade came from.
STRATEGY_TAG = "WhatsApp Signals"


class Outcome:
    """What happened to one signal, in the vocabulary the event log uses."""

    def __init__(
        self,
        status: str,
        detail: str,
        *,
        position_id: int | None = None,
        order_id: str | None = None,
    ) -> None:
        self.status = status  # executed | rejected | failed | ignored
        self.detail = detail
        self.position_id = position_id
        self.order_id = order_id

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Outcome {self.status}: {self.detail}>"


def _rejected(detail: str) -> Outcome:
    return Outcome("rejected", detail)


def _failed(detail: str) -> Outcome:
    return Outcome("failed", detail)


# ---------------------------------------------------------------------------
# Platform plumbing
# ---------------------------------------------------------------------------


def current_mode() -> str | None:
    """ "analyze" or "live", or None when it cannot be determined."""
    try:
        from database.settings_db import get_analyze_mode

        return "analyze" if get_analyze_mode() else "live"
    except Exception:
        logger.exception("Could not read the platform trading mode")
        return None


def _api_key() -> str | None:
    """The operator's OpenAlgo API key.

    Passed alone, with no auth token or broker: the service layer then routes by
    mode -- sandbox in analyze, the broker otherwise -- which is exactly the
    routing this add-on wants and must not second-guess.
    """
    from database.auth_db import get_api_key_for_tradingview
    from database.user_db import find_user_by_username

    try:
        user = find_user_by_username()
        if not user:
            return None
        return get_api_key_for_tradingview(user.username)
    except Exception:
        logger.exception("Could not resolve the OpenAlgo API key")
        return None


def _last_price(symbol: str, exchange: str, api_key: str) -> float | None:
    from services.quotes_service import get_quotes

    try:
        ok, resp, _code = get_quotes(symbol=symbol, exchange=exchange, api_key=api_key)
    except Exception:
        logger.exception("Quote fetch failed for %s", symbol)
        return None
    if not ok or not isinstance(resp, dict):
        return None
    try:
        ltp = float((resp.get("data") or {}).get("ltp") or 0)
    except (TypeError, ValueError):
        return None
    return ltp if ltp > 0 else None


def _live_net_qty(symbol: str, exchange: str, product: str, api_key: str) -> int:
    """Net quantity the broker (or sandbox) says is held on this leg."""
    from services.positionbook_service import get_positionbook

    try:
        ok, resp, _code = get_positionbook(api_key=api_key)
    except Exception:
        logger.exception("Position book fetch failed")
        return 0
    if not ok or not isinstance(resp, dict):
        return 0
    for row in resp.get("data") or []:
        if (
            row.get("symbol") == symbol
            and (row.get("exchange") or "").upper() == exchange.upper()
            and (row.get("product") or "").upper() == product.upper()
        ):
            try:
                return int(float(row.get("quantity") or 0))
            except (TypeError, ValueError):
                return 0
    return 0


def _notify_risk_monitor() -> None:
    """Tell the tick-driven monitor its watched set changed."""
    try:
        from services.scalping_risk_monitor_service import notify_sl_changed

        notify_sl_changed()
    except Exception:
        logger.debug("Risk monitor notify skipped", exc_info=True)


def _write_stop(
    instrument: resolver.ResolvedInstrument,
    *,
    mode: str,
    side: str,
    quantity: int,
    entry_price: float | None,
    stop_loss: float | None,
    target: float | None,
    trailing_enabled: bool,
    trailing_step: float | None,
) -> bool:
    """Create or update the stop row the risk monitor acts on."""
    from database.scalping_db import track_symbol, upsert_sl_state

    payload: dict[str, Any] = {
        "symbol": instrument.symbol,
        "exchange": instrument.exchange,
        "product": instrument.product,
        "mode": mode,
        "side": side,
        "quantity": quantity,
        "is_active": True,
        "trailing_enabled": bool(trailing_enabled),
    }
    if entry_price is not None:
        payload["entry_price"] = entry_price
    if stop_loss is not None:
        payload["initial_sl"] = stop_loss
        payload["current_sl"] = stop_loss
    if target is not None:
        payload["target"] = target
    if trailing_step is not None:
        payload["trailing_step"] = trailing_step

    saved = upsert_sl_state(payload)
    if saved is None:
        return False
    try:
        track_symbol(instrument.symbol, instrument.exchange, instrument.product, mode=mode)
    except Exception:
        logger.exception("Could not add %s to the tracked list", instrument.symbol)
    _notify_risk_monitor()
    return True


def _clear_stop(symbol: str, exchange: str, product: str, mode: str) -> None:
    from database.scalping_db import delete_sl_state

    try:
        delete_sl_state(symbol, exchange, product, mode=mode)
    except Exception:
        logger.exception("Could not clear the stop for %s", symbol)
    _notify_risk_monitor()


def _remove_sessions() -> None:
    """Release every scoped session this path touched.

    The platform's registry is used rather than a hand-written list, and that
    is the point: one order reaches far more of the database than it looks
    like. ``place_order`` alone touches the auth, settings, analyzer, sandbox,
    api-log, latency and action-center sessions, and the freeze-safe exit adds
    the quantity-freeze and master-contract ones. A list written by hand here
    would be missing several on the day it was written and more after every
    upstream release.

    It matters because this runs on a worker thread that lives as long as the
    process. A thread with no Flask teardown keeps whatever sessions it opened,
    and a session left holding an uncommitted read keeps its SQLite connection
    with it -- in a single Gunicorn worker that never restarts, for good.
    """
    try:
        from utils.db_sessions import remove_all_scoped_sessions

        remove_all_scoped_sessions()
    except Exception:
        logger.debug("Scoped session cleanup skipped", exc_info=True)
    # Not in the registry, because the registry is upstream's and this add-on
    # stays out of upstream files.
    resolver.remove_session()


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


def check_mode(group: dict[str, Any]) -> tuple[str | None, str | None]:
    """Decide which mode this signal may run in. Returns (mode, refusal)."""
    platform = current_mode()
    if platform is None:
        return None, "The platform trading mode could not be read, so nothing was sent."
    wanted = (group.get("execution_mode") or "analyze").lower()
    if wanted == "analyze" and platform == "live":
        return None, (
            "This group is set to sandbox but the platform is trading live, "
            "so the signal was not sent. Switch the group to live, or put the "
            "platform in analyze mode."
        )
    # A live group while the platform is in analyze runs in the sandbox: the
    # global toggle is the operator saying nothing real should go out.
    return platform, None


def check_caps(
    group: dict[str, Any], chat_jid: str, mode: str, *, opens_a_new_leg: bool = True
) -> str | None:
    """Whether the group has any authority left today. Returns a refusal or None.

    ``opens_a_new_leg`` is False when the signal adds to an instrument the group
    already holds. The open-positions cap counts positions, and an add does not
    make one; refusing it would leave a group able to enter a leg and then
    unable to finish building it.
    """
    max_signals = int(group.get("max_signals_per_day") or 0)
    if max_signals and db.count_signals_today(chat_jid) >= max_signals:
        return (
            f"This group has already traded {max_signals} signals today, which is its daily limit."
        )
    max_open = int(group.get("max_open_positions") or 0)
    if opens_a_new_leg and max_open and len(db.open_positions(chat_jid, mode=mode)) >= max_open:
        return (
            f"This group already holds {max_open} open positions, "
            "which is its limit. Close one before it can take another."
        )
    return None


def sender_allowed(group: dict[str, Any], sender_jid: str | None) -> bool:
    """Whether this member may signal. An empty allowlist means every member."""
    allowed = group.get("allowed_senders") or []
    if not allowed:
        return True
    sender = (sender_jid or "").strip()
    digits = "".join(ch for ch in sender if ch.isdigit())
    for entry in allowed:
        entry = str(entry).strip()
        if not entry:
            continue
        if entry == sender:
            return True
        entry_digits = "".join(ch for ch in entry if ch.isdigit())
        if entry_digits and digits and entry_digits == digits:
            return True
    return False


# ---------------------------------------------------------------------------
# Position matching
# ---------------------------------------------------------------------------


def match_position(
    signal: ParsedSignal, chat_jid: str, mode: str
) -> tuple[dict[str, Any] | None, str | None]:
    """Find the position a follow-up message refers to.

    A message that names its leg is matched on the leg. One that does not ("SL
    to 110") is matched only when the group holds exactly one position, because
    a stop moved onto the wrong leg is worse than a stop not moved at all.
    """
    positions = db.open_positions(chat_jid, mode=mode)
    if not positions:
        return None, "There is no open position from this group to apply that to."

    if signal.names_leg:
        instrument, error = resolver.resolve(signal, positions[0].get("product") or "MIS")
        if instrument is None:
            return None, error or "That instrument could not be identified."
        for position in positions:
            if (
                position["symbol"] == instrument.symbol
                and position["exchange"] == instrument.exchange
            ):
                return position, None
        return None, f"This group holds no open position in {instrument.symbol}."

    if len(positions) == 1:
        return positions[0], None
    held = ", ".join(p["symbol"] for p in positions)
    return None, (
        "That message did not say which position it applies to, and this group "
        f"holds more than one: {held}. Nothing was changed."
    )


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


def execute(signal: ParsedSignal, group: dict[str, Any], chat_jid: str) -> Outcome:
    """Run one parsed signal against the account. Never raises."""
    try:
        mode, refusal = check_mode(group)
        if refusal:
            return _rejected(refusal)

        api_key = _api_key()
        if not api_key:
            return _failed(
                "Nobody is logged in to OpenAlgo, so the signal could not be sent. "
                "Log in and the next one will go through."
            )

        if signal.action == ENTRY:
            return _enter(signal, group, chat_jid, mode, api_key)
        if signal.action in (SET_SL, SET_TARGET):
            return _adjust(signal, group, chat_jid, mode)
        if signal.action == EXIT:
            return _exit(signal, group, chat_jid, mode, api_key, fraction=None)
        if signal.action == PARTIAL_EXIT:
            return _exit(signal, group, chat_jid, mode, api_key, fraction=signal.fraction or 0.5)
        return Outcome("ignored", "Nothing to do.")
    except Exception:
        logger.exception("WhatsApp signal execution crashed")
        return _failed("Something went wrong sending that signal. Check the logs.")
    finally:
        _remove_sessions()


def _enter(
    signal: ParsedSignal,
    group: dict[str, Any],
    chat_jid: str,
    mode: str,
    api_key: str,
) -> Outcome:
    instrument, error = resolver.resolve(signal, group.get("product") or "MIS")
    if instrument is None:
        return _rejected(error or "That instrument could not be identified.")

    existing = _find_open(chat_jid, instrument, mode)
    side = (signal.side or "BUY").upper()

    # A signal on the opposite side of a leg the group already holds is not an
    # entry, whatever it says. It would either net that position down or reverse
    # it, and which of those the group meant is not in the message. Refusing is
    # the only reading that cannot be wrong in an expensive direction: an
    # explicit exit closes the leg, and a fresh entry then opens the other side.
    if existing and (existing.get("side") or "").upper() != side:
        return _rejected(
            f"This group is already {existing.get('side')} in {instrument.symbol}. "
            f"A {side} signal on the same contract would reverse or close that position, "
            "so nothing was sent. Send an exit first if that is what was meant."
        )

    cap_refusal = check_caps(group, chat_jid, mode, opens_a_new_leg=existing is None)
    if cap_refusal:
        return _rejected(cap_refusal)

    lots = int(signal.lots or group.get("lots") or 1)
    max_lots = int(group.get("max_lots") or lots)
    if lots > max_lots:
        lots = max_lots
    if lots < 1:
        return _rejected("This group is configured to trade zero lots.")

    quantity = lots * instrument.lotsize if instrument.is_derivative else lots
    if quantity <= 0:
        return _rejected("The order quantity worked out to nothing.")

    ltp = _last_price(instrument.symbol, instrument.exchange, api_key)
    reference_price = ltp or signal.entry_price

    from services.place_order_service import place_order

    order_data = {
        "strategy": STRATEGY_TAG,
        "symbol": instrument.symbol,
        "exchange": instrument.exchange,
        "action": side,
        "pricetype": "MARKET",
        "product": instrument.product,
        "quantity": quantity,
    }
    prefetched = {"ltp": ltp} if ltp else None
    ok, response, _code = place_order(
        order_data=order_data, api_key=api_key, prefetched_quote=prefetched
    )
    if not ok:
        message = response.get("message") if isinstance(response, dict) else str(response)
        return _failed(f"The order was not accepted: {message}")

    order_id = str((response or {}).get("orderid") or "") or None

    # Adding to a leg we already hold keeps one position row and one stop row.
    # The stop is keyed by leg, so a second row would fight the first over it.
    position_entry = (
        _averaged_entry(existing, quantity, reference_price) if existing else reference_price
    )
    stop_loss, target = _levels_for_entry(signal, group, side, position_entry)
    if existing:
        # An add that states no level is adding size, not asking for the level
        # it already has to be recalculated off the new average.
        if signal.stop_loss is None and existing.get("stop_loss") is not None:
            stop_loss = existing["stop_loss"]
        if signal.target is None and existing.get("target") is not None:
            target = existing["target"]

        total_qty = int(existing["quantity"] or 0) + quantity
        db.update_position(
            existing["id"],
            quantity=total_qty,
            lots=int(existing["lots"] or 0) + lots,
            entry_price=position_entry,
            stop_loss=stop_loss,
            target=target,
        )
        position_id = existing["id"]
        stop_quantity = total_qty
    else:
        created = db.create_position(
            {
                "chat_jid": chat_jid,
                "symbol": instrument.symbol,
                "exchange": instrument.exchange,
                "product": instrument.product,
                "mode": mode,
                "side": side,
                "quantity": quantity,
                "lots": lots,
                "entry_price": position_entry,
                "stop_loss": stop_loss,
                "target": target,
                "order_id": order_id,
            }
        )
        position_id = created["id"] if created else None
        stop_quantity = quantity

    stop_written = _write_stop(
        instrument,
        mode=mode,
        side=side,
        quantity=stop_quantity,
        entry_price=position_entry,
        stop_loss=stop_loss,
        target=target,
        trailing_enabled=bool(group.get("trailing_enabled")),
        trailing_step=group.get("trailing_step"),
    )

    detail = (
        f"{side} {quantity} {instrument.symbol} at market"
        f"{f' (around {reference_price})' if reference_price else ''}"
    )
    if existing:
        detail += f", added to a position now {stop_quantity} at an average of {position_entry}"
    if stop_loss is not None:
        detail += f", stop {stop_loss}"
    if target is not None:
        detail += f", target {target}"
    if not stop_written:
        detail += (
            ". The stop could not be saved, so this position is NOT being watched "
            "- set a stop manually."
        )
    return Outcome("executed", detail, position_id=position_id, order_id=order_id)


def _levels_for_entry(
    signal: ParsedSignal,
    group: dict[str, Any],
    side: str,
    reference_price: float | None,
) -> tuple[float | None, float | None]:
    """The stop and target to hand the risk monitor.

    A signal that states its own levels keeps them. One that does not falls back
    to the group's percentage defaults, which is the only sane default shape: an
    option premium of 8 and one of 800 both occur, and a fixed points default is
    a stop that never triggers on one and triggers instantly on the other.
    """
    stop_loss = signal.stop_loss
    target = signal.target

    if reference_price and reference_price > 0:
        if stop_loss is None:
            pct = float(group.get("default_sl_pct") or 0)
            if pct > 0:
                stop_loss = (
                    reference_price * (1 - pct / 100.0)
                    if side == "BUY"
                    else reference_price * (1 + pct / 100.0)
                )
                stop_loss = round(stop_loss, 2)
        if target is None:
            pct = float(group.get("default_target_pct") or 0)
            if pct > 0:
                target = (
                    reference_price * (1 + pct / 100.0)
                    if side == "BUY"
                    else reference_price * (1 - pct / 100.0)
                )
                target = round(target, 2)

    if stop_loss is not None and (stop_loss <= 0 or not math.isfinite(stop_loss)):
        stop_loss = None
    if target is not None and (target <= 0 or not math.isfinite(target)):
        target = None
    return stop_loss, target


def _averaged_entry(
    existing: dict[str, Any], added_quantity: int, added_price: float | None
) -> float | None:
    """The cost of a leg after adding to it, weighted by size.

    It has to be one number, because two things read it and they must agree: a
    later "SL to cost" prices the stop off the position row, and the risk
    monitor trails from the stop row's entry price. Keeping the first entry in
    one and the latest in the other gives a single leg two different costs.
    """
    held = int(existing.get("quantity") or 0)
    held_price = existing.get("entry_price")
    if added_price is None:
        return held_price
    if not held or held_price is None:
        return added_price
    total = held + added_quantity
    if total <= 0:
        return added_price
    return round((held * float(held_price) + added_quantity * float(added_price)) / total, 2)


def _find_open(
    chat_jid: str, instrument: resolver.ResolvedInstrument, mode: str
) -> dict[str, Any] | None:
    for position in db.open_positions(chat_jid, mode=mode):
        if (
            position["symbol"] == instrument.symbol
            and position["exchange"] == instrument.exchange
            and position["product"] == instrument.product
        ):
            return position
    return None


def _adjust(signal: ParsedSignal, group: dict[str, Any], chat_jid: str, mode: str) -> Outcome:
    """Move the stop or the target on a position the group already holds."""
    position, error = match_position(signal, chat_jid, mode)
    if position is None:
        return _rejected(error or "That position could not be identified.")

    stop_loss = position.get("stop_loss")
    target = position.get("target")

    if signal.action == SET_SL:
        if signal.sl_to_cost:
            entry = position.get("entry_price")
            if not entry:
                return _rejected(
                    "The entry price for that position is not on record, "
                    "so the stop could not be moved to cost."
                )
            stop_loss = round(float(entry), 2)
        elif signal.stop_loss is not None:
            stop_loss = signal.stop_loss
        else:
            return _rejected("That message did not name a stop price.")
        if signal.target is not None:
            target = signal.target
    else:
        if signal.target is None:
            return _rejected("That message did not name a target price.")
        target = signal.target

    instrument = resolver.ResolvedInstrument(
        symbol=position["symbol"],
        exchange=position["exchange"],
        product=position["product"],
        lotsize=0,
        expiry=None,
        kind="option",
    )
    written = _write_stop(
        instrument,
        mode=mode,
        side=position["side"],
        quantity=int(position["quantity"] or 0),
        entry_price=position.get("entry_price"),
        stop_loss=stop_loss,
        target=target,
        trailing_enabled=bool(signal.trail or group.get("trailing_enabled")),
        trailing_step=group.get("trailing_step"),
    )
    if not written:
        return _failed(
            f"The stop for {position['symbol']} could not be saved. "
            "It is unchanged, and the position is still being watched on the old one."
        )

    db.update_position(position["id"], stop_loss=stop_loss, target=target)
    if signal.action == SET_SL:
        detail = f"Stop on {position['symbol']} moved to {stop_loss}"
        if signal.sl_to_cost:
            detail += " (cost)"
    else:
        detail = f"Target on {position['symbol']} set to {target}"
    return Outcome("executed", detail, position_id=position["id"])


def _exit(
    signal: ParsedSignal,
    group: dict[str, Any],
    chat_jid: str,
    mode: str,
    api_key: str,
    *,
    fraction: float | None,
) -> Outcome:
    """Close a position, or part of one.

    "Exit all" with no instrument named closes every position this group holds,
    which is the one case where an unqualified follow-up is unambiguous.
    """
    positions = db.open_positions(chat_jid, mode=mode)
    if not positions:
        return _rejected("There is no open position from this group to close.")

    if fraction is None and not signal.names_leg and len(positions) > 1:
        results = [_exit_one(p, api_key, mode, None) for p in positions]
        closed = [r for r in results if r.status == "executed"]
        failed = [r for r in results if r.status != "executed"]
        if failed and not closed:
            return _failed("; ".join(r.detail for r in failed))
        detail = "; ".join(r.detail for r in closed)
        if failed:
            detail += ". Still open: " + "; ".join(r.detail for r in failed)
            return Outcome("failed", detail)
        return Outcome("executed", detail)

    position, error = match_position(signal, chat_jid, mode)
    if position is None:
        return _rejected(error or "That position could not be identified.")
    return _exit_one(position, api_key, mode, fraction)


def _exit_one(position: dict[str, Any], api_key: str, mode: str, fraction: float | None) -> Outcome:
    """Send one closing order, sized to what is actually held."""
    symbol = position["symbol"]
    exchange = position["exchange"]
    product = position["product"]

    net_qty = _live_net_qty(symbol, exchange, product, api_key)
    if net_qty == 0:
        # Already flat -- most likely the risk monitor's own stop got there
        # first. Reconcile rather than send an order that would open a new
        # position in the opposite direction.
        _clear_stop(symbol, exchange, product, mode)
        db.update_position(position["id"], status="closed")
        return Outcome("executed", f"{symbol} was already flat; its stop was cleared.")

    action = "SELL" if net_qty > 0 else "BUY"
    quantity = abs(net_qty)

    if fraction is not None and 0 < fraction < 1:
        quantity = _partial_quantity(symbol, exchange, abs(net_qty), fraction)
        if quantity <= 0:
            return _rejected(
                f"A {int(fraction * 100)}% exit of {symbol} works out to less than one lot, "
                "so nothing was sent."
            )

    from blueprints.scalping import _reducing_exit

    ok, response, _code = _reducing_exit(
        symbol, exchange, product, action, quantity, None, None, api_key
    )
    if not ok:
        message = response.get("message") if isinstance(response, dict) else str(response)
        # The position is still there, so its stop stays and its row stays open.
        return _failed(
            f"{symbol} could not be closed and is STILL OPEN: {message}. "
            "Its stop is still in place."
        )

    remaining = abs(net_qty) - quantity
    if remaining > 0:
        db.update_position(position["id"], quantity=remaining)
        from database.scalping_db import upsert_sl_state

        upsert_sl_state(
            {
                "symbol": symbol,
                "exchange": exchange,
                "product": product,
                "mode": mode,
                "quantity": remaining,
            }
        )
        _notify_risk_monitor()
        return Outcome(
            "executed",
            f"Closed {quantity} of {symbol}, {remaining} still open on the same stop.",
            position_id=position["id"],
        )

    _clear_stop(symbol, exchange, product, mode)
    db.update_position(position["id"], status="closed", quantity=0)
    return Outcome("executed", f"Closed {quantity} {symbol}.", position_id=position["id"])


def _partial_quantity(symbol: str, exchange: str, held: int, fraction: float) -> int:
    """A partial exit rounded down to whole lots.

    Derivatives cannot be exited in part-lots, and an order that is not a lot
    multiple is rejected by the exchange. Rounding down also means a "book half"
    on a single lot sends nothing rather than closing the whole position, which
    is the safer reading of an instruction to keep some on.
    """
    from database.symbol import SymToken
    from database.symbol import db_session as symbol_session

    try:
        row = (
            symbol_session.query(SymToken)
            .filter(SymToken.symbol == symbol, SymToken.exchange == exchange)
            .first()
        )
        lotsize = int(row.lotsize or 0) if row else 0
    except Exception:
        logger.exception("Lot size lookup failed for %s", symbol)
        symbol_session.rollback()
        lotsize = 0

    wanted = int(held * fraction)
    if lotsize > 1:
        wanted = (wanted // lotsize) * lotsize
    return max(0, min(wanted, held))
