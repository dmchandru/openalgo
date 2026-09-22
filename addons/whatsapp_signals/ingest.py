"""Where a group message becomes a decision, off the bot's thread.

The upstream WhatsApp bot runs one pump loop that both drains inbound messages
and serves outbound sends. Parsing a signal, calling a model, resolving a
contract and placing an order takes seconds, and doing any of it on that loop
would stall every other message and every alert the platform tries to send
while it happens. So the hook does one cheap thing -- put the message on this
queue -- and everything else happens on the single worker below.

One worker, not a pool, and that is a correctness choice rather than a frugal
one. Messages from a group are ordered and dependent: "BUY NIFTY 25000 CE" then
"SL 110" then "book half" only mean what they mean in that order. Two workers
would race the stop against the entry that has not been recorded yet.

The queue is bounded. A group that floods -- or a forward storm -- drops
messages with a log line rather than growing a list until the worker dies of
memory, because production is one Gunicorn worker that never restarts.

Under eventlet this worker is a green thread, and every resource it touches
(the database, the platform services, the bot's own send queue) is green too.
Nothing here crosses into a real OS thread, which is what keeps it clear of the
hub-poisoning failure the platform documents. The tokio threads that deliver
WhatsApp messages never reach this module: they hand off to the bot's deque,
the bot greenlet calls :func:`offer`, and :func:`offer` only enqueues.
"""

from __future__ import annotations

import atexit
import queue
import threading
import time
from collections import deque
from typing import Any

from addons.whatsapp_signals import ai_manager, ai_parser, db, executor, llm, parser
from utils.logging import get_logger

logger = get_logger(__name__)

#: Deep enough for a burst, shallow enough that a flood is visibly dropped.
QUEUE_MAXSIZE = 200

#: WhatsApp group JIDs end in this. Anything else is a 1:1 chat and belongs to
#: the upstream bot's command handling, not here.
GROUP_SUFFIX = "@g.us"

#: WhatsApp Channels (newsletters) and old-style broadcast lists use these.
#: All three are valid signal sources and are treated identically by the worker.
NEWSLETTER_SUFFIX = "@newsletter"
BROADCAST_SUFFIX = "@broadcast"

#: All accepted source JID suffixes. A message whose chat_jid ends with any of
#: these is offered to the signal worker; anything else (1:1 chats) is skipped.
ALLOWED_SUFFIXES = (GROUP_SUFFIX, NEWSLETTER_SUFFIX, BROADCAST_SUFFIX)

_worker: threading.Thread | None = None
_queue: queue.Queue = queue.Queue(maxsize=QUEUE_MAXSIZE)
_stop = threading.Event()
_lock = threading.Lock()

_msg_buffers: dict[str, deque] = {}
_buf_lock = threading.Lock()


def record_to_buffer(chat_jid: str, text: str, sender_jid: str | None, ts: float | None = None) -> None:
    """Record a message to the group's rolling context buffer."""
    with _buf_lock:
        if chat_jid not in _msg_buffers:
            _msg_buffers[chat_jid] = deque(maxlen=ai_parser.CONTEXT_WINDOW)
        _msg_buffers[chat_jid].append({
            "text": text,
            "sender": sender_jid,
            "ts": ts if ts is not None else time.time(),
        })


def get_context(chat_jid: str) -> list[dict[str, Any]]:
    """Get the recent context messages for a group."""
    with _buf_lock:
        if chat_jid not in _msg_buffers:
            return []
        return list(_msg_buffers[chat_jid])


def clear_buffers() -> None:
    """Clear in-memory context buffers (primarily for tests)."""
    with _buf_lock:
        _msg_buffers.clear()


def offer(chat_jid: str, sender_jid: str | None, text: str, is_from_me: bool = False) -> bool:
    """Hand one inbound group/channel message to the worker. Called on the bot loop.

    Returns True when the message was queued. Does no database work and never
    blocks: anything slower than a dictionary lookup belongs on the worker.

    Accepted JID types:
        @g.us        — WhatsApp group (original source)
        @newsletter  — WhatsApp Channel / Newsletter
        @broadcast   — old-style broadcast list
    """
    if not chat_jid or not any(chat_jid.endswith(s) for s in ALLOWED_SUFFIXES):
        return False
    message = (text or "").strip()
    if not message or message.startswith("/"):
        return False
    try:
        _queue.put_nowait(
            {
                "chat_jid": chat_jid,
                "sender_jid": sender_jid,
                "text": message,
                "is_from_me": bool(is_from_me),
            }
        )
        return True
    except queue.Full:
        logger.warning(
            "WhatsApp signal queue is full; a message from %s was dropped unread", chat_jid
        )
        return False


def start() -> None:
    """Start the worker once. Idempotent."""
    global _worker
    with _lock:
        if _worker is not None and _worker.is_alive():
            return
        _stop.clear()
        _worker = threading.Thread(target=_run, name="wa-signal-worker", daemon=True)
        _worker.start()
        logger.info("WhatsApp signal worker started")


def stop() -> None:
    """Ask the worker to finish. Registered with atexit."""
    _stop.set()


atexit.register(stop)


def is_running() -> bool:
    return _worker is not None and _worker.is_alive()


def queue_depth() -> int:
    return _queue.qsize()


def _run() -> None:
    while not _stop.is_set():
        try:
            message = _queue.get(timeout=0.5)
        except queue.Empty:
            continue
        try:
            handle(message)
        except Exception:
            logger.exception("WhatsApp signal worker failed on a message")
        finally:
            # Belt and braces with the executor's own cleanup: a message that
            # never reached the executor still read the group and wrote an
            # event, and this thread outlives every one of them.
            db.remove_session()
            try:
                from utils.db_sessions import remove_all_scoped_sessions

                remove_all_scoped_sessions()
            except Exception:
                logger.debug("Scoped session cleanup skipped", exc_info=True)
    logger.info("WhatsApp signal worker stopped")


def handle(message: dict[str, Any]) -> dict[str, Any]:
    """Process one message end to end. Returns what was recorded, for tests."""
    chat_jid = message["chat_jid"]
    sender_jid = message.get("sender_jid")
    text = message["text"]

    db.observe_group(chat_jid)
    group = db.get_group(chat_jid)

    # A group nobody enabled is watched and nothing more. Its messages are not
    # even logged as events: sighting a group must cost nothing and leak
    # nothing, and the operator has not asked for any of it to be read.
    if not group or not group.get("is_enabled"):
        return {"status": "ignored", "detail": "group not enabled"}

    if not executor.sender_allowed(group, sender_jid):
        db.record_event(
            chat_jid,
            sender_jid,
            text,
            status="rejected",
            detail="This member is not on the group's allowed-sender list.",
        )
        return {"status": "rejected", "detail": "sender not allowed"}

    record_to_buffer(chat_jid, text, sender_jid)

    signal = None
    if group.get("ai_parser_mode") and llm.is_available():
        context = get_context(chat_jid)
        open_pos = db.open_positions(chat_jid)
        signal = ai_parser.parse(text, context, open_pos)

    if signal is None:
        signal = parser.parse(text)
        if parser.worth_llm_attempt(text, signal) and group.get("llm_fallback") and llm.is_available():
            from_model = llm.parse(text)
            if from_model is not None and from_model.is_actionable:
                signal = from_model

    if not signal.is_actionable:
        # Before recording as ignored, check whether this might be a management
        # instruction aimed at an open position that the regex/LLM signal tier
        # could not read as a new signal (because it isn't one).
        if (group.get("llm_fallback") or group.get("ai_parser_mode")) and llm.is_available():
            open_pos = db.open_positions(chat_jid)
            if open_pos and ai_manager.looks_like_management(text):
                return _handle_ai_management(
                    chat_jid, sender_jid, text, group, open_pos
                )

        db.record_event(
            chat_jid,
            sender_jid,
            text,
            tier=signal.tier if signal.tier != "regex" else "none",
            action=parser.NONE,
            parsed=signal.to_dict(),
            status="ignored",
            detail=signal.note,
        )
        return {"status": "ignored", "detail": signal.note}

    # A forwarded signal often lands twice. The second copy must not open a
    # second position, so an identical message that already traded is dropped.
    if db.recent_duplicate(chat_jid, text):
        db.record_event(
            chat_jid,
            sender_jid,
            text,
            tier=signal.tier,
            action=signal.action,
            parsed=signal.to_dict(),
            status="duplicate",
            detail="The same message was already acted on moments ago.",
        )
        return {"status": "duplicate", "detail": "repeat message"}

    event_id = db.record_event(
        chat_jid,
        sender_jid,
        text,
        tier=signal.tier,
        action=signal.action,
        parsed=signal.to_dict(),
        status="ignored",
        detail="Working on it.",
    )

    outcome = executor.execute(signal, group, chat_jid)
    db.update_event(
        event_id,
        status=outcome.status,
        detail=outcome.detail,
        position_id=outcome.position_id,
        order_id=outcome.order_id,
    )
    logger.info(
        "WhatsApp signal from %s: %s -> %s (%s)",
        chat_jid,
        signal.action,
        outcome.status,
        outcome.detail,
    )

    if group.get("notify_operator"):
        _notify(group, signal, outcome)

    return {"status": outcome.status, "detail": outcome.detail, "event_id": event_id}


#: Outcomes worth interrupting the operator for. An ignored message is not one:
#: the whole point of the filter is that they do not have to read the group.
_NOTIFY_STATUSES = ("executed", "failed", "rejected")


def _notify(group: dict[str, Any], signal, outcome) -> None:
    """Tell the operator, in their own WhatsApp, what was just done in their name."""
    if outcome.status not in _NOTIFY_STATUSES:
        return
    try:
        from services.whatsapp_bot_service import whatsapp_bot_service

        if not whatsapp_bot_service.is_ready():
            return
        heading = {
            "executed": "Signal taken",
            "failed": "Signal failed",
            "rejected": "Signal not taken",
        }[outcome.status]
        mode = "sandbox" if (group.get("execution_mode") == "analyze") else "live"
        lines = [
            f"{heading} ({mode})",
            group.get("label") or group.get("chat_jid", ""),
            "",
            outcome.detail,
            "",
            f"Message: {(signal.raw or '')[:180]}",
        ]
        whatsapp_bot_service.send_sync(None, "\n".join(lines))
    except Exception:
        logger.exception("Could not send the operator a signal notification")


def _handle_ai_management(
    chat_jid: str,
    sender_jid: str | None,
    text: str,
    group: dict[str, Any],
    open_positions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Route a follow-up message through the AI manager.

    If auto_apply_ai is on: execute the actions immediately and log them.
    If off: create a pending suggestion row and notify the operator.
    """
    try:
        actions, reasoning = ai_manager.analyze_message(text, open_positions, group)
    except Exception:
        logger.exception("AI manager raised for %s", chat_jid)
        actions, reasoning = [], "AI manager error — check logs."

    event_id = db.record_event(
        chat_jid,
        sender_jid,
        text,
        tier="llm",
        action="management",
        status="pending_review" if actions else "ignored",
        detail=reasoning,
    )

    if not actions:
        return {"status": "ignored", "detail": reasoning}

    auto_apply = bool(group.get("auto_apply_ai"))

    if auto_apply:
        outcomes = executor.execute_sequence(actions, group, chat_jid)
        any_executed = any(o.status == "executed" for o in outcomes)
        status = "executed" if any_executed else "failed"
        detail = " | ".join(o.detail for o in outcomes)
        db.update_event(event_id, status=status, detail=detail)
        db.create_suggestion(
            chat_jid,
            reasoning,
            actions,
            event_id=event_id,
            status="auto",
        )
        _notify_ai_applied(group, reasoning, outcomes)
        return {"status": status, "detail": detail}

    # Human-in-the-loop — create a pending suggestion.
    db.create_suggestion(
        chat_jid,
        reasoning,
        actions,
        event_id=event_id,
        status="pending",
    )
    _notify_ai_suggestion_pending(group, reasoning, actions)
    return {"status": "pending_review", "detail": reasoning}


def _notify_ai_applied(
    group: dict[str, Any],
    reasoning: str,
    outcomes: list,
) -> None:
    """Send the operator a WhatsApp message when AI actions were auto-applied."""
    if not group.get("notify_operator"):
        return
    try:
        from services.whatsapp_bot_service import whatsapp_bot_service

        if not whatsapp_bot_service.is_ready():
            return
        detail = " | ".join(o.detail for o in outcomes)
        lines = [
            "AI auto-applied management",
            group.get("label") or group.get("chat_jid", ""),
            "",
            reasoning,
            "",
            detail,
        ]
        whatsapp_bot_service.send_sync(None, "\n".join(lines))
    except Exception:
        logger.exception("Could not notify operator of AI auto-apply")


def _notify_ai_suggestion_pending(
    group: dict[str, Any],
    reasoning: str,
    actions: list[dict],
) -> None:
    """Notify the operator that a new AI suggestion is waiting for review."""
    if not group.get("notify_operator"):
        return
    try:
        from services.whatsapp_bot_service import whatsapp_bot_service

        if not whatsapp_bot_service.is_ready():
            return
        action_names = ", ".join(a.get("action", "?") for a in actions)
        lines = [
            "AI suggestion pending your review",
            group.get("label") or group.get("chat_jid", ""),
            "",
            reasoning,
            "",
            f"Actions: {action_names}",
            "Open the WhatsApp Signals page to Apply or Dismiss.",
        ]
        whatsapp_bot_service.send_sync(None, "\n".join(lines))
    except Exception:
        logger.exception("Could not notify operator of pending AI suggestion")
