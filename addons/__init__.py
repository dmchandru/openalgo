"""OpenAlgo add-ons: code this deployment owns, kept apart from upstream.

Everything under this package is local to this fork. Upstream OpenAlgo knows
nothing about it, so a ``git pull`` from upstream can never conflict with it.
The whole overlay attaches through one call:

    from addons import install_addons
    install_addons(app)

``app.py`` carries those two lines and nothing else. Every other integration
point -- the inbound WhatsApp hook, the blueprint, the table creation, the
background worker -- is wired from inside this package, so the list of upstream
files this fork modifies stays at one.

See ``addons/README.md`` for what each add-on touches and why.
"""

from __future__ import annotations

from utils.logging import get_logger

logger = get_logger(__name__)


def install_addons(app) -> None:
    """Attach every add-on to the Flask app.

    Each add-on installs independently and failure is contained: an add-on that
    cannot start logs and is skipped, because a local feature must never stop
    the trading platform it sits on from booting.

    Args:
        app: The Flask application, after upstream blueprints are registered.
    """
    from addons.whatsapp_signals import install as install_whatsapp_signals

    for name, installer in (("whatsapp_signals", install_whatsapp_signals),):
        try:
            installer(app)
            logger.info("Add-on installed: %s", name)
        except Exception:
            logger.exception("Add-on %s failed to install and was skipped", name)
