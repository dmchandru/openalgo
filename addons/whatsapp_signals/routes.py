"""The operator's surface: one page and the endpoints behind it.

Served by this add-on rather than by the React frontend, so nothing under
``frontend/`` changes and a ``git pull`` from upstream cannot conflict with it.
The page is plain HTML, CSS and JavaScript from this package's ``web``
directory -- no build step, no bundler, nothing to rebuild after an upgrade.

Every route is session-authenticated exactly like the rest of the platform's
web surface, and POSTs carry the platform's CSRF token. Nothing here is exposed
on ``/api/v1/``: an API key must not be able to point this deployment at a new
WhatsApp group.
"""

from __future__ import annotations

import os

from flask import Blueprint, jsonify, request, send_from_directory

from addons.whatsapp_signals import db, executor, ingest, llm, parser, resolver
from utils.logging import get_logger
from utils.session import check_session_validity

logger = get_logger(__name__)

_WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

bp = Blueprint(
    "whatsapp_signals",
    __name__,
    url_prefix="/whatsapp-signals",
    static_folder=_WEB_DIR,
    static_url_path="/assets",
)


def _ok(data=None, **extra):
    payload = {"status": "success"}
    if data is not None:
        payload["data"] = data
    payload.update(extra)
    return jsonify(payload)


def _error(message: str, code: int = 400):
    return jsonify({"status": "error", "message": message}), code


@bp.route("", strict_slashes=False)
@check_session_validity
def page():
    """The add-on's page. Registered as a real route so a refresh on it is a
    known path rather than a 404 counted against the visitor's IP."""
    return send_from_directory(_WEB_DIR, "index.html")


@bp.route("/api/state")
@check_session_validity
def state():
    """Everything the page draws, in one call."""
    from services.whatsapp_bot_service import whatsapp_bot_service

    chat_jid = (request.args.get("chat_jid") or "").strip() or None
    return _ok(
        {
            "bot": {
                "is_paired": bool(whatsapp_bot_service.is_paired),
                "is_running": bool(whatsapp_bot_service.is_running),
                "is_ready": bool(whatsapp_bot_service.is_ready()),
            },
            "worker": {
                "is_running": ingest.is_running(),
                "queue_depth": ingest.queue_depth(),
            },
            "platform_mode": executor.current_mode(),
            "llm_available": llm.is_available(),
            "groups": db.list_groups(),
            "positions": db.list_positions(chat_jid, limit=50),
            "events": db.list_events(chat_jid, limit=100),
            "profiles": db.list_profiles(),
            "pending_suggestions": db.list_suggestions(chat_jid, status="pending"),
        }
    )


@bp.route("/api/events")
@check_session_validity
def events():
    chat_jid = (request.args.get("chat_jid") or "").strip() or None
    try:
        limit = int(request.args.get("limit") or 100)
    except (TypeError, ValueError):
        limit = 100
    return _ok(db.list_events(chat_jid, limit=limit))


@bp.route("/api/group", methods=["POST"])
@check_session_validity
def save_group():
    payload = request.get_json(silent=True) or {}
    chat_jid = (payload.get("chat_jid") or "").strip()
    if not chat_jid:
        return _error("Pick a group first.")

    # Accept @g.us groups, @newsletter channels, and @broadcast lists.
    # If no @ at all treat as bare digits and auto-suffix as a group JID.
    if not any(chat_jid.endswith(s) for s in ingest.ALLOWED_SUFFIXES):
        if "@" not in chat_jid:
            chat_jid = f"{chat_jid}{ingest.GROUP_SUFFIX}"
            payload["chat_jid"] = chat_jid
        else:
            return _error(
                "That JID type is not supported. Use a @g.us group, "
                "@newsletter channel, or @broadcast list."
            )

    group, error = db.update_group(chat_jid, payload)
    if error:
        return _error(error)
    logger.info(
        "WhatsApp signal group %s saved: enabled=%s mode=%s lots=%s",
        chat_jid,
        group.get("is_enabled"),
        group.get("execution_mode"),
        group.get("lots"),
    )
    return _ok(group)


@bp.route("/api/group/delete", methods=["POST"])
@check_session_validity
def remove_group():
    payload = request.get_json(silent=True) or {}
    chat_jid = (payload.get("chat_jid") or "").strip()
    if not chat_jid:
        return _error("Pick a group first.")
    return _ok({"deleted": db.delete_group(chat_jid)})


@bp.route("/api/profiles")
@check_session_validity
def profiles():
    return _ok(db.list_profiles())


@bp.route("/api/profile", methods=["POST"])
@check_session_validity
def save_profile():
    payload = request.get_json(silent=True) or {}
    profile, error = db.save_profile(payload)
    if error:
        return _error(error)
    return _ok(profile)


@bp.route("/api/profile/delete", methods=["POST"])
@check_session_validity
def remove_profile():
    payload = request.get_json(silent=True) or {}
    profile_id = payload.get("id")
    if not profile_id:
        return _error("Profile ID required.")
    return _ok({"deleted": db.delete_profile(int(profile_id))})


@bp.route("/api/suggestions")
@check_session_validity
def suggestions():
    chat_jid = (request.args.get("chat_jid") or "").strip() or None
    status_filter = (request.args.get("status") or "pending").strip() or None
    return _ok(db.list_suggestions(chat_jid, status=status_filter))


@bp.route("/api/suggestion/apply", methods=["POST"])
@check_session_validity
def apply_suggestion():
    payload = request.get_json(silent=True) or {}
    suggestion_id = payload.get("id")
    if not suggestion_id:
        return _error("Suggestion ID required.")

    row = db.db_session.query(db.WaSignalAiSuggestion).filter_by(id=int(suggestion_id)).first()
    if not row:
        return _error("No suggestion with that ID.")
    if row.status != "pending":
        return _error(f"Suggestion is already {row.status}.")

    sug = db.suggestion_to_dict(row)
    group = db.get_group(sug["chat_jid"]) or {}
    outcomes = executor.execute_sequence(sug["suggested_actions"], group, sug["chat_jid"])

    any_executed = any(o.status == "executed" for o in outcomes)
    final_status = "executed" if any_executed else "failed"
    detail = " | ".join(o.detail for o in outcomes)

    if sug.get("event_id"):
        db.update_event(sug["event_id"], status=final_status, detail=detail)

    resolved = db.resolve_suggestion(int(suggestion_id), "applied")
    return _ok({"suggestion": resolved, "outcomes": [o.detail for o in outcomes], "status": final_status})


@bp.route("/api/suggestion/dismiss", methods=["POST"])
@check_session_validity
def dismiss_suggestion():
    payload = request.get_json(silent=True) or {}
    suggestion_id = payload.get("id")
    if not suggestion_id:
        return _error("Suggestion ID required.")

    row = db.db_session.query(db.WaSignalAiSuggestion).filter_by(id=int(suggestion_id)).first()
    if not row:
        return _error("No suggestion with that ID.")
    if row.status != "pending":
        return _error(f"Suggestion is already {row.status}.")

    if row.event_id:
        db.update_event(row.event_id, status="ignored", detail="Suggestion dismissed by operator.")

    resolved = db.resolve_suggestion(int(suggestion_id), "dismissed")
    return _ok(resolved)


@bp.route("/api/parse", methods=["POST"])
@check_session_validity
def dry_run():
    """Read a message the way the worker would, and change nothing.

    This is how an operator finds out what their group's house style parses to
    before trusting it with money, and how they check a message that was
    ignored. It resolves the instrument too, because "NIFTY 25000 CE" reading
    correctly and that contract existing are different questions.
    """
    payload = request.get_json(silent=True) or {}
    text = (payload.get("text") or "").strip()
    if not text:
        return _error("Paste a message to test.")

    signal = parser.parse(text)
    used_llm = False
    if payload.get("use_llm") and parser.worth_llm_attempt(text, signal) and llm.is_available():
        from_model = llm.parse(text)
        if from_model is not None:
            signal = from_model
            used_llm = True

    resolved = None
    resolve_error = None
    if signal.base:
        try:
            instrument, resolve_error = resolver.resolve(signal, (payload.get("product") or "MIS"))
            if instrument:
                resolved = {
                    "symbol": instrument.symbol,
                    "exchange": instrument.exchange,
                    "product": instrument.product,
                    "lotsize": instrument.lotsize,
                    "expiry": instrument.expiry,
                    "kind": instrument.kind,
                }
        finally:
            resolver.remove_session()

    return _ok(
        {
            "signal": signal.to_dict(),
            "used_llm": used_llm,
            "would_trade": signal.is_actionable,
            "resolved": resolved,
            "resolve_error": resolve_error,
        }
    )
