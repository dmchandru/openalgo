"""Trade the signals posted in a WhatsApp group, and keep their stops current.

A group posts "BUY NIFTY 25000 CE @ 120", then ten minutes later "SL to 110",
then "book half". This add-on reads all three off the group the server is
already linked to, and turns them into an order, a stop change and a partial
exit -- with the platform's own risk monitor watching the position on live
ticks in between.

How the pieces fit:

    upstream WhatsApp bot   a linked device, already receiving group messages
            |               hooks.py wraps its inbound handler
            v
    ingest.py               one worker, off the bot's loop, messages in order
            |
            v
    parser.py               regex tier: deterministic, free, covers most
    llm.py                  model tier: only for what the first cannot read
            |
            v
    resolver.py             does this contract exist? which expiry? what lot?
            |
            v
    executor.py             gates, caps, the order, and the stop row
            |
            v
    scalping_risk_monitor   the platform's tick-driven stop engine takes over

Nothing in here evaluates a stop or watches a price. That belongs to the
monitor the platform already runs, and a second evaluator beside it is the
defect this design exists to avoid.

Installed by ``addons.install_addons(app)``. Everything it touches upstream is
listed in ``addons/README.md``.
"""

from __future__ import annotations

from utils.logging import get_logger

logger = get_logger(__name__)


def install(app) -> None:
    """Create the tables, hook the bot, start the worker, register the page."""
    from addons.whatsapp_signals import db, hooks, ingest, routes

    db.init_db()

    app.register_blueprint(routes.bp)

    # The add-on's session is not one of the ones app.py knows to clean up, so
    # it registers its own. Production is a single Gunicorn worker that never
    # restarts: a session left registered per request holds a SQLite connection
    # open until the process dies.
    @app.teardown_appcontext
    def _remove_whatsapp_signal_session(_exception=None):  # pragma: no cover - plumbing
        db.remove_session()

    hooks.install_inbound_hook()
    ingest.start()
