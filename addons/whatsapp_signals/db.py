"""Storage for WhatsApp-sourced signals.

Three tables, all prefixed ``wa_signal_`` and all living in the main
``openalgo.db`` alongside the rest of the platform. They are created by this
add-on's own ``init_db()`` and by ``upgrade/migrate_whatsapp_signals.py``; no
upstream schema is touched, so an upstream migration can never collide.

    wa_signal_group     one row per WhatsApp group the server has seen. A group
                        appears here the first time a message arrives from it,
                        disabled and with no permission to trade anything. The
                        operator enables it, which is the only way a group's
                        messages ever reach an order path.

    wa_signal_event     every message that was considered, what it parsed to,
                        and what happened. This is the audit trail: a trader
                        looking at a position needs to see the message that
                        opened it and the message that moved its stop.

    wa_signal_position  what this add-on believes it is holding, and which group
                        and message opened it. The stop itself lives in
                        ``scalping_sl_state`` where the tick-driven risk monitor
                        can act on it; this table is the link back to the group,
                        which is what lets "SL to 110" ten minutes later find
                        the right leg.

Retention is bounded on purpose. ``wa_signal_event`` records a row per message
considered, including chat that parsed to nothing, so an untended install would
otherwise grow forever on a busy group. Inserts prune beyond
``EVENT_RETENTION_ROWS``.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import declarative_base, scoped_session, sessionmaker

from database.engine_factory import create_db_engine
from utils.logging import get_logger

logger = get_logger(__name__)

#: Oldest events are pruned beyond this count. A busy group posts a few hundred
#: messages a day; this holds roughly a month of them.
EVENT_RETENTION_ROWS = 5000

#: How many groups may be auto-recorded on sighting. A linked device can be in
#: hundreds of groups, and an unbounded table of every one of them is noise the
#: operator has to scroll past to find the one that matters.
MAX_OBSERVED_GROUPS = 200


def utcnow() -> datetime:
    """Naive UTC, which is what these columns hold.

    ``utcnow()`` is deprecated, and a timezone-aware value cannot be
    substituted directly: the columns are plain ``DateTime`` and their
    server-side defaults are naive, so storing an aware value alongside them
    makes every comparison between the two raise.
    """
    return datetime.now(UTC).replace(tzinfo=None)


DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///db/openalgo.db")

engine = create_db_engine(DATABASE_URL)
db_session = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=engine))
Base = declarative_base()
Base.query = db_session.query_property()


class WaSignalGroup(Base):
    """A WhatsApp group, and the authority it has been granted.

    Defaults are deliberately inert: a newly sighted group is disabled, trades
    one lot, in sandbox, with a stop. Sighting a group must never be the same
    thing as trusting it.
    """

    __tablename__ = "wa_signal_group"

    id = Column(Integer, primary_key=True)
    chat_jid = Column(String(120), nullable=False, unique=True, index=True)
    label = Column(String(160), nullable=True)

    is_enabled = Column(Boolean, nullable=False, default=False)
    #: "analyze" routes to the sandbox engine, "live" to the broker. A group set
    #: to analyze is REFUSED while the platform is live -- see executor.py.
    execution_mode = Column(String(10), nullable=False, default="analyze")

    product = Column(String(10), nullable=False, default="MIS")
    lots = Column(Integer, nullable=False, default=1)
    max_lots = Column(Integer, nullable=False, default=1)
    max_open_positions = Column(Integer, nullable=False, default=3)
    max_signals_per_day = Column(Integer, nullable=False, default=30)

    #: JSON list of sender JIDs or bare phone digits. Empty means every member
    #: of the group may signal, which is what a broadcast-style channel wants.
    allowed_senders = Column(Text, nullable=True)

    #: Applied when a signal names no stop of its own. A percentage of the entry
    #: price, because option premiums differ by two orders of magnitude between
    #: a far OTM weekly and an ITM monthly, and a points default fits neither.
    default_sl_pct = Column(Float, nullable=False, default=30.0)
    default_target_pct = Column(Float, nullable=True)
    trailing_enabled = Column(Boolean, nullable=False, default=False)
    trailing_step = Column(Float, nullable=True)

    llm_fallback = Column(Boolean, nullable=False, default=True)
    notify_operator = Column(Boolean, nullable=False, default=True)

    message_count = Column(Integer, nullable=False, default=0)
    last_seen_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class WaSignalEvent(Base):
    """One considered message and its outcome."""

    __tablename__ = "wa_signal_event"

    id = Column(Integer, primary_key=True)
    chat_jid = Column(String(120), nullable=False, index=True)
    sender_jid = Column(String(120), nullable=True)
    text = Column(Text, nullable=True)
    received_at = Column(DateTime, nullable=False, default=utcnow, index=True)

    #: "regex" | "llm" | "none"
    tier = Column(String(10), nullable=True)
    action = Column(String(24), nullable=True)
    parsed = Column(Text, nullable=True)

    #: ignored | rejected | executed | failed | duplicate
    status = Column(String(16), nullable=False, default="ignored")
    detail = Column(Text, nullable=True)

    position_id = Column(Integer, nullable=True)
    order_id = Column(String(60), nullable=True)


class WaSignalPosition(Base):
    """A leg this add-on opened, and the group that asked for it."""

    __tablename__ = "wa_signal_position"

    id = Column(Integer, primary_key=True)
    chat_jid = Column(String(120), nullable=False, index=True)

    symbol = Column(String(60), nullable=False)
    exchange = Column(String(10), nullable=False)
    product = Column(String(10), nullable=False)
    mode = Column(String(10), nullable=False, default="analyze")

    side = Column(String(4), nullable=False, default="BUY")
    quantity = Column(Integer, nullable=False, default=0)
    lots = Column(Integer, nullable=False, default=0)
    entry_price = Column(Float, nullable=True)

    stop_loss = Column(Float, nullable=True)
    target = Column(Float, nullable=True)

    #: "open" | "closed". Deliberately NOT part of a unique key: the same
    #: instrument is traded repeatedly across a session, so each entry earns its
    #: own row. At most one row per leg is open at a time, which the executor
    #: enforces by looking for an open row before it opens another -- the stop
    #: in scalping_sl_state is keyed by leg, so two open rows would fight over
    #: one stop.
    status = Column(String(8), nullable=False, default="open", index=True)

    entry_event_id = Column(Integer, nullable=True)
    order_id = Column(String(60), nullable=True)
    opened_at = Column(DateTime, nullable=False, default=utcnow)
    closed_at = Column(DateTime, nullable=True)


def init_db() -> None:
    """Create the add-on's tables if they are absent. Idempotent."""
    Base.metadata.create_all(bind=engine)
    logger.info("WhatsApp signals tables ready")


# ----------------------------------------------------------------------------
# Serialisation
# ----------------------------------------------------------------------------


def _json_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [str(v) for v in value] if isinstance(value, list) else []


def group_to_dict(row: WaSignalGroup) -> dict[str, Any]:
    return {
        "id": row.id,
        "chat_jid": row.chat_jid,
        "label": row.label or row.chat_jid.split("@", 1)[0],
        "is_enabled": bool(row.is_enabled),
        "execution_mode": row.execution_mode,
        "product": row.product,
        "lots": row.lots,
        "max_lots": row.max_lots,
        "max_open_positions": row.max_open_positions,
        "max_signals_per_day": row.max_signals_per_day,
        "allowed_senders": _json_list(row.allowed_senders),
        "default_sl_pct": row.default_sl_pct,
        "default_target_pct": row.default_target_pct,
        "trailing_enabled": bool(row.trailing_enabled),
        "trailing_step": row.trailing_step,
        "llm_fallback": bool(row.llm_fallback),
        "notify_operator": bool(row.notify_operator),
        "message_count": row.message_count,
        "last_seen_at": row.last_seen_at.isoformat() if row.last_seen_at else None,
    }


def event_to_dict(row: WaSignalEvent) -> dict[str, Any]:
    parsed = None
    if row.parsed:
        try:
            parsed = json.loads(row.parsed)
        except (TypeError, ValueError):
            parsed = None
    return {
        "id": row.id,
        "chat_jid": row.chat_jid,
        "sender_jid": row.sender_jid,
        "text": row.text,
        "received_at": row.received_at.isoformat() if row.received_at else None,
        "tier": row.tier,
        "action": row.action,
        "parsed": parsed,
        "status": row.status,
        "detail": row.detail,
        "position_id": row.position_id,
        "order_id": row.order_id,
    }


def position_to_dict(row: WaSignalPosition) -> dict[str, Any]:
    return {
        "id": row.id,
        "chat_jid": row.chat_jid,
        "symbol": row.symbol,
        "exchange": row.exchange,
        "product": row.product,
        "mode": row.mode,
        "side": row.side,
        "quantity": row.quantity,
        "lots": row.lots,
        "entry_price": row.entry_price,
        "stop_loss": row.stop_loss,
        "target": row.target,
        "status": row.status,
        "order_id": row.order_id,
        "opened_at": row.opened_at.isoformat() if row.opened_at else None,
        "closed_at": row.closed_at.isoformat() if row.closed_at else None,
    }


# ----------------------------------------------------------------------------
# Groups
# ----------------------------------------------------------------------------


def observe_group(chat_jid: str, label: str | None = None) -> dict[str, Any] | None:
    """Record that a message arrived from this group, creating it if new.

    A new group is created **disabled**. This exists so the operator can pick
    the group out of a list instead of copying a 20-digit JID off their phone,
    and it grants nothing.
    """
    if not chat_jid:
        return None
    try:
        row = db_session.query(WaSignalGroup).filter_by(chat_jid=chat_jid).first()
        if row is None:
            count = db_session.query(func.count(WaSignalGroup.id)).scalar() or 0
            if count >= MAX_OBSERVED_GROUPS:
                # Drop the sighting rather than the cap. An operator with 200
                # groups on record has what they need to find theirs.
                return None
            row = WaSignalGroup(chat_jid=chat_jid, label=label, is_enabled=False)
            db_session.add(row)
        if label and not row.label:
            row.label = label
        row.message_count = (row.message_count or 0) + 1
        row.last_seen_at = utcnow()
        db_session.commit()
        return group_to_dict(row)
    except Exception:
        logger.exception("observe_group failed for %s", chat_jid)
        db_session.rollback()
        return None


def get_group(chat_jid: str) -> dict[str, Any] | None:
    try:
        row = db_session.query(WaSignalGroup).filter_by(chat_jid=chat_jid).first()
        return group_to_dict(row) if row else None
    except Exception:
        logger.exception("get_group failed for %s", chat_jid)
        db_session.rollback()
        return None


def list_groups() -> list[dict[str, Any]]:
    try:
        rows = (
            db_session.query(WaSignalGroup)
            .order_by(WaSignalGroup.is_enabled.desc(), WaSignalGroup.last_seen_at.desc())
            .all()
        )
        return [group_to_dict(r) for r in rows]
    except Exception:
        logger.exception("list_groups failed")
        db_session.rollback()
        return []


#: Columns a caller may set, and the coercion each takes. Anything else in the
#: payload is ignored rather than written, so a typo cannot create a column.
_GROUP_FIELDS: dict[str, str] = {
    "label": "str",
    "is_enabled": "bool",
    "execution_mode": "mode",
    "product": "upper",
    "lots": "int",
    "max_lots": "int",
    "max_open_positions": "int",
    "max_signals_per_day": "int",
    "allowed_senders": "list",
    "default_sl_pct": "float",
    "default_target_pct": "float",
    "trailing_enabled": "bool",
    "trailing_step": "float",
    "llm_fallback": "bool",
    "notify_operator": "bool",
}


def update_group(
    chat_jid: str, changes: dict[str, Any]
) -> tuple[dict[str, Any] | None, str | None]:
    """Apply an operator's edits to a group. Returns (group, error)."""
    try:
        row = db_session.query(WaSignalGroup).filter_by(chat_jid=chat_jid).first()
        if row is None:
            row = WaSignalGroup(chat_jid=chat_jid)
            db_session.add(row)
            # Column defaults are applied by the flush, not by the constructor.
            # Without this the new row's numeric columns are still None while
            # the checks below read them, and saving settings for a group the
            # device has not seen yet fails on a comparison against None.
            db_session.flush()

        for key, kind in _GROUP_FIELDS.items():
            if key not in changes:
                continue
            value = changes[key]
            if value is None:
                if kind in ("float",):
                    setattr(row, key, None)
                continue
            if kind == "bool":
                setattr(row, key, bool(value))
            elif kind == "int":
                setattr(row, key, max(0, int(value)))
            elif kind == "float":
                setattr(row, key, float(value))
            elif kind == "upper":
                setattr(row, key, str(value).strip().upper())
            elif kind == "mode":
                mode = str(value).strip().lower()
                if mode not in ("analyze", "live"):
                    return None, "Trading mode must be either sandbox or live."
                setattr(row, key, mode)
            elif kind == "list":
                items = value if isinstance(value, list) else []
                setattr(row, key, json.dumps([str(v).strip() for v in items if str(v).strip()]))
            else:
                setattr(row, key, str(value).strip() or None)

        # Belt and braces around the same class of None: a caller may clear a
        # column, and a cap below the size it caps would silently shrink every
        # order the group sends.
        lots = row.lots if row.lots is not None else 1
        max_lots = row.max_lots if row.max_lots is not None else lots
        row.lots = lots
        row.max_lots = max(lots, max_lots)
        db_session.commit()
        return group_to_dict(row), None
    except (TypeError, ValueError):
        db_session.rollback()
        return None, "One of those values is not a number."
    except Exception:
        logger.exception("update_group failed for %s", chat_jid)
        db_session.rollback()
        return None, "The group settings could not be saved."


def delete_group(chat_jid: str) -> bool:
    try:
        deleted = db_session.query(WaSignalGroup).filter_by(chat_jid=chat_jid).delete()
        db_session.commit()
        return bool(deleted)
    except Exception:
        logger.exception("delete_group failed for %s", chat_jid)
        db_session.rollback()
        return False


# ----------------------------------------------------------------------------
# Events
# ----------------------------------------------------------------------------


def record_event(
    chat_jid: str,
    sender_jid: str | None,
    text: str | None,
    *,
    tier: str | None = None,
    action: str | None = None,
    parsed: dict[str, Any] | None = None,
    status: str = "ignored",
    detail: str | None = None,
    position_id: int | None = None,
    order_id: str | None = None,
) -> int | None:
    """Append one event and prune the tail. Returns the new event id."""
    try:
        row = WaSignalEvent(
            chat_jid=chat_jid,
            sender_jid=sender_jid,
            text=(text or "")[:4000],
            tier=tier,
            action=action,
            parsed=json.dumps(parsed) if parsed else None,
            status=status,
            detail=detail,
            position_id=position_id,
            order_id=order_id,
        )
        db_session.add(row)
        db_session.commit()
        _prune_events()
        return row.id
    except Exception:
        logger.exception("record_event failed for %s", chat_jid)
        db_session.rollback()
        return None


def update_event(event_id: int, **changes: Any) -> None:
    """Fill in an event's outcome once the order path has answered."""
    if not event_id:
        return
    try:
        row = db_session.query(WaSignalEvent).filter_by(id=event_id).first()
        if row is None:
            return
        for key in ("status", "detail", "position_id", "order_id", "action", "tier"):
            if key in changes and changes[key] is not None:
                setattr(row, key, changes[key])
        if "parsed" in changes and changes["parsed"]:
            row.parsed = json.dumps(changes["parsed"])
        db_session.commit()
    except Exception:
        logger.exception("update_event failed for %s", event_id)
        db_session.rollback()


def _prune_events() -> None:
    """Keep the event log bounded. Cheap: only counts, and only trims when over."""
    try:
        total = db_session.query(func.count(WaSignalEvent.id)).scalar() or 0
        if total <= EVENT_RETENTION_ROWS:
            return
        cutoff_id = (
            db_session.query(WaSignalEvent.id)
            .order_by(WaSignalEvent.id.desc())
            .offset(EVENT_RETENTION_ROWS)
            .limit(1)
            .scalar()
        )
        if cutoff_id:
            db_session.query(WaSignalEvent).filter(WaSignalEvent.id <= cutoff_id).delete()
            db_session.commit()
    except Exception:
        logger.exception("Event pruning failed")
        db_session.rollback()


def list_events(chat_jid: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    try:
        q = db_session.query(WaSignalEvent)
        if chat_jid:
            q = q.filter_by(chat_jid=chat_jid)
        rows = q.order_by(WaSignalEvent.id.desc()).limit(max(1, min(limit, 500))).all()
        return [event_to_dict(r) for r in rows]
    except Exception:
        logger.exception("list_events failed")
        db_session.rollback()
        return []


def count_signals_today(chat_jid: str) -> int:
    """Executed signals for this group since midnight UTC.

    Counts executions rather than messages: the daily cap exists to bound how
    much a group can trade, and a chatty group that parses to nothing has not
    used any of it.
    """
    try:
        since = utcnow() - timedelta(hours=24)
        return (
            db_session.query(func.count(WaSignalEvent.id))
            .filter(
                WaSignalEvent.chat_jid == chat_jid,
                WaSignalEvent.status == "executed",
                WaSignalEvent.received_at >= since,
            )
            .scalar()
            or 0
        )
    except Exception:
        logger.exception("count_signals_today failed")
        db_session.rollback()
        return 0


def recent_duplicate(chat_jid: str, text: str, within_seconds: int = 90) -> bool:
    """Whether this exact message was already handled moments ago.

    A forwarded signal often lands twice, and the second copy must not open a
    second position.
    """
    try:
        since = utcnow() - timedelta(seconds=within_seconds)
        return bool(
            db_session.query(WaSignalEvent.id)
            .filter(
                WaSignalEvent.chat_jid == chat_jid,
                WaSignalEvent.text == (text or "")[:4000],
                WaSignalEvent.received_at >= since,
                WaSignalEvent.status.in_(("executed", "failed")),
            )
            .first()
        )
    except Exception:
        logger.exception("recent_duplicate check failed")
        db_session.rollback()
        return False


# ----------------------------------------------------------------------------
# Positions
# ----------------------------------------------------------------------------


def create_position(data: dict[str, Any]) -> dict[str, Any] | None:
    try:
        row = WaSignalPosition(
            chat_jid=data["chat_jid"],
            symbol=data["symbol"],
            exchange=data["exchange"],
            product=data["product"],
            mode=data.get("mode") or "analyze",
            side=data.get("side") or "BUY",
            quantity=int(data.get("quantity") or 0),
            lots=int(data.get("lots") or 0),
            entry_price=data.get("entry_price"),
            stop_loss=data.get("stop_loss"),
            target=data.get("target"),
            entry_event_id=data.get("entry_event_id"),
            order_id=data.get("order_id"),
            status="open",
        )
        db_session.add(row)
        db_session.commit()
        return position_to_dict(row)
    except Exception:
        logger.exception("create_position failed")
        db_session.rollback()
        return None


def update_position(position_id: int, **changes: Any) -> dict[str, Any] | None:
    try:
        row = db_session.query(WaSignalPosition).filter_by(id=position_id).first()
        if row is None:
            return None
        for key in ("quantity", "lots", "stop_loss", "target", "entry_price", "status", "side"):
            if key in changes and changes[key] is not None:
                setattr(row, key, changes[key])
        if changes.get("status") == "closed" and row.closed_at is None:
            row.closed_at = utcnow()
        db_session.commit()
        return position_to_dict(row)
    except Exception:
        logger.exception("update_position failed for %s", position_id)
        db_session.rollback()
        return None


def open_positions(chat_jid: str | None = None, mode: str | None = None) -> list[dict[str, Any]]:
    try:
        q = db_session.query(WaSignalPosition).filter_by(status="open")
        if chat_jid:
            q = q.filter_by(chat_jid=chat_jid)
        if mode:
            q = q.filter_by(mode=mode)
        rows = q.order_by(WaSignalPosition.id.desc()).all()
        return [position_to_dict(r) for r in rows]
    except Exception:
        logger.exception("open_positions failed")
        db_session.rollback()
        return []


def list_positions(chat_jid: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    try:
        q = db_session.query(WaSignalPosition)
        if chat_jid:
            q = q.filter_by(chat_jid=chat_jid)
        rows = q.order_by(WaSignalPosition.id.desc()).limit(max(1, min(limit, 500))).all()
        return [position_to_dict(r) for r in rows]
    except Exception:
        logger.exception("list_positions failed")
        db_session.rollback()
        return []


def remove_session() -> None:
    """Release this module's scoped session.

    Background workers have no Flask teardown, so every worker that touches
    these helpers calls this in its ``finally``. A session left registered on a
    long-lived thread holds its SQLite connection open, and production is one
    Gunicorn worker that never restarts.
    """
    try:
        db_session.remove()
    except Exception:
        logger.debug("wa_signal db_session.remove failed", exc_info=True)
