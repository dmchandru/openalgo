"""The one place this add-on reaches into upstream code.

The upstream WhatsApp bot already receives every group message the linked
device sees, and drops the ones that are not slash-commands from the operator's
own phone. This wraps its inbound handler so those messages are offered to the
signal worker on the way past, and then calls the original, unchanged, so every
upstream behaviour is exactly what it was.

**Why a wrapper and not an edit.** Editing
``services/whatsapp_bot_service.py`` would put a conflict in that file on every
upstream pull, forever, for the sake of three lines. The wrapper keeps the
whole add-on in files upstream does not have, at the cost of being less
obvious -- which is what this docstring and ``addons/README.md`` are for.

**It refuses to install against a handler it does not recognise.** If upstream
changes that method's signature, the wrapper does not go on: an add-on that
silently stops seeing messages is worse than one that logs that it could not
attach, because the first looks like a quiet group and the second is a line in
the log saying what to fix.
"""

from __future__ import annotations

import inspect

from utils.logging import get_logger

logger = get_logger(__name__)

#: The upstream handler's parameters, as of the version this was written
#: against. Checked at install time.
_EXPECTED_PARAMS = ["self", "wa", "evt"]

#: The event tuple upstream marshals from the wars tokio callbacks:
#: ("message", is_from_me, sender, chat, text).
_MESSAGE_EVENT_LENGTH = 5

_HOOK_FLAG = "_wa_signals_hooked"


def install_inbound_hook() -> bool:
    """Attach the signal worker to the bot's inbound path. Idempotent."""
    try:
        from services.whatsapp_bot_service import WhatsAppBotService
    except Exception:
        logger.exception("WhatsApp bot service unavailable; signals will not be read")
        return False

    original = getattr(WhatsAppBotService, "_handle_inbound", None)
    if original is None:
        logger.error(
            "The WhatsApp bot has no inbound handler to attach to. Group signals will not be read."
        )
        return False
    if getattr(original, _HOOK_FLAG, False):
        return True

    try:
        params = list(inspect.signature(original).parameters)
    except (TypeError, ValueError):
        params = []
    if params != _EXPECTED_PARAMS:
        logger.error(
            "The WhatsApp bot's inbound handler has changed shape (%s). "
            "Group signals will not be read until addons/whatsapp_signals/hooks.py "
            "is updated.",
            params,
        )
        return False

    def _handle_inbound(self, wa, evt):
        # Runs on the bot's own loop. Everything here must stay cheap: the same
        # loop serves outbound sends, so work done here delays every message the
        # platform is trying to deliver. offer() only enqueues.
        try:
            if isinstance(evt, tuple) and len(evt) == _MESSAGE_EVENT_LENGTH and evt[0] == "message":
                from addons.whatsapp_signals import ingest

                _, is_from_me, sender, chat, text = evt
                ingest.offer(chat, sender, text, is_from_me)
        except Exception:
            # A failure reading signals must never stop the bot delivering
            # alerts or answering commands.
            logger.exception("WhatsApp signal hook failed on an inbound message")
        return original(self, wa, evt)

    setattr(_handle_inbound, _HOOK_FLAG, True)
    _handle_inbound.__name__ = getattr(original, "__name__", "_handle_inbound")
    _handle_inbound.__doc__ = getattr(original, "__doc__", None)
    WhatsAppBotService._handle_inbound = _handle_inbound
    logger.info("WhatsApp signal inbound hook attached")
    return True
