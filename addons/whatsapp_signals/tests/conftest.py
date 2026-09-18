"""Test fixtures for the WhatsApp Signals add-on.

The add-on's database module binds its engine at import time, so the test
database has to be chosen before anything imports it. That is what the
``DATABASE_URL`` assignment below is doing, and why it sits above the imports
rather than in a fixture.

Because that assignment is process-wide, run these as their own invocation:

    uv run pytest addons/whatsapp_signals/tests/ -q

The project's ``testpaths`` is ``test``, so a bare ``uv run pytest`` does not
collect this directory and the two never share a process by accident.
"""

from __future__ import annotations

import os
import tempfile

_TEST_DB = os.path.join(tempfile.mkdtemp(prefix="wa-signals-test-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DB}"

import pytest  # noqa: E402

from addons.whatsapp_signals import db  # noqa: E402


@pytest.fixture(autouse=True)
def clean_tables():
    """Each test starts against empty tables."""
    db.init_db()
    for model in (db.WaSignalEvent, db.WaSignalPosition, db.WaSignalGroup):
        db.db_session.query(model).delete()
    db.db_session.commit()
    yield
    db.db_session.rollback()
    db.remove_session()


@pytest.fixture
def group():
    """An enabled group with ordinary settings."""
    db.observe_group("120363000000000000@g.us", "Test Signals")
    saved, error = db.update_group(
        "120363000000000000@g.us",
        {
            "is_enabled": True,
            "execution_mode": "analyze",
            "product": "MIS",
            "lots": 1,
            "max_lots": 5,
            "max_open_positions": 3,
            "max_signals_per_day": 30,
            "default_sl_pct": 30.0,
        },
    )
    assert error is None
    return saved
