"""What the storage layer guarantees regardless of who calls it."""

from __future__ import annotations

from addons.whatsapp_signals import db


def test_a_newly_sighted_group_is_granted_nothing():
    """Seeing a group and trusting it are different things.

    This is the default that stands between a linked device that is in fifty
    WhatsApp groups and fifty groups that can place orders.
    """
    sighted = db.observe_group("120363000000000001@g.us", "Some Group")
    assert sighted["is_enabled"] is False
    assert sighted["execution_mode"] == "analyze"
    assert sighted["lots"] == 1
    assert sighted["default_sl_pct"] > 0


def test_sighting_the_same_group_twice_counts_rather_than_duplicates():
    db.observe_group("120363000000000002@g.us", "G")
    again = db.observe_group("120363000000000002@g.us")
    assert again["message_count"] == 2
    assert len([g for g in db.list_groups() if g["chat_jid"].endswith("0002@g.us")]) == 1


def test_an_observed_label_is_not_overwritten_by_a_later_blank():
    db.observe_group("120363000000000003@g.us", "Named")
    again = db.observe_group("120363000000000003@g.us", None)
    assert again["label"] == "Named"


def test_an_unknown_trading_mode_is_refused():
    db.observe_group("120363000000000004@g.us")
    saved, error = db.update_group("120363000000000004@g.us", {"execution_mode": "yolo"})
    assert saved is None
    assert "sandbox or live" in error


def test_max_lots_is_never_below_lots():
    """A cap under the size it caps would silently shrink every order."""
    db.observe_group("120363000000000005@g.us")
    saved, _ = db.update_group("120363000000000005@g.us", {"lots": 5, "max_lots": 2})
    assert saved["max_lots"] == 5


def test_unknown_fields_are_ignored_rather_than_written():
    db.observe_group("120363000000000006@g.us")
    saved, error = db.update_group(
        "120363000000000006@g.us", {"lots": 2, "is_admin": True, "chat_jid": "spoofed@g.us"}
    )
    assert error is None
    assert saved["chat_jid"] == "120363000000000006@g.us"
    assert not hasattr(saved, "is_admin")


def test_allowed_senders_round_trip_as_a_list():
    db.observe_group("120363000000000007@g.us")
    saved, _ = db.update_group(
        "120363000000000007@g.us", {"allowed_senders": [" 919876543210 ", "", "91999"]}
    )
    assert saved["allowed_senders"] == ["919876543210", "91999"]


def test_the_event_log_is_bounded(monkeypatch):
    """A busy group posts hundreds of messages a day, and production is one
    worker that never restarts."""
    monkeypatch.setattr(db, "EVENT_RETENTION_ROWS", 10)
    for i in range(25):
        db.record_event("120363000000000008@g.us", None, f"message {i}")
    remaining = db.list_events("120363000000000008@g.us", limit=500)
    assert len(remaining) <= 11
    assert remaining[0]["text"] == "message 24"  # the newest survives


def test_only_a_message_that_traded_counts_as_a_duplicate():
    """An ignored message repeating is just a chatty group. One that placed an
    order repeating is the case this guard exists for."""
    chat = "120363000000000009@g.us"
    db.record_event(chat, None, "BUY NIFTY 25000 CE", status="ignored")
    assert db.recent_duplicate(chat, "BUY NIFTY 25000 CE") is False
    db.record_event(chat, None, "BUY NIFTY 25000 CE", status="executed")
    assert db.recent_duplicate(chat, "BUY NIFTY 25000 CE") is True


def test_a_duplicate_is_scoped_to_its_group():
    db.record_event("a@g.us", None, "BUY NIFTY 25000 CE", status="executed")
    assert db.recent_duplicate("b@g.us", "BUY NIFTY 25000 CE") is False


def test_the_observed_group_table_is_capped(monkeypatch):
    monkeypatch.setattr(db, "MAX_OBSERVED_GROUPS", 3)
    for i in range(6):
        db.observe_group(f"12036300000000001{i}@g.us")
    assert len(db.list_groups()) == 3


def test_closing_a_position_stamps_the_time_once():
    created = db.create_position(
        {
            "chat_jid": "a@g.us",
            "symbol": "NIFTY25000CE",
            "exchange": "NFO",
            "product": "MIS",
            "mode": "analyze",
            "side": "BUY",
            "quantity": 75,
        }
    )
    closed = db.update_position(created["id"], status="closed")
    assert closed["closed_at"] is not None
    again = db.update_position(created["id"], status="closed")
    assert again["closed_at"] == closed["closed_at"]


def test_open_positions_are_filtered_by_group_and_mode():
    for chat, mode in (("a@g.us", "analyze"), ("a@g.us", "live"), ("b@g.us", "analyze")):
        db.create_position(
            {
                "chat_jid": chat,
                "symbol": "NIFTY25000CE",
                "exchange": "NFO",
                "product": "MIS",
                "mode": mode,
                "side": "BUY",
                "quantity": 75,
            }
        )
    assert len(db.open_positions("a@g.us")) == 2
    assert len(db.open_positions("a@g.us", mode="analyze")) == 1
    assert len(db.open_positions("b@g.us", mode="live")) == 0


def test_a_group_can_be_configured_before_it_has_ever_been_seen():
    """The operator may paste a JID rather than wait for the group to post.

    The row does not exist yet, and a new row's column defaults are not applied
    until the flush -- so this once failed on a comparison against None and
    reported "one of those values is not a number" for a perfectly good save.
    """
    saved, error = db.update_group("120363000000000020@g.us", {"is_enabled": True, "lots": 2})
    assert error is None
    assert saved["is_enabled"] is True
    assert saved["lots"] == 2
    assert saved["max_lots"] >= 2
    assert saved["execution_mode"] == "analyze"


def test_init_db_auto_migrates_missing_columns():
    """If a table existed from an older release, init_db ensures added columns exist."""
    from sqlalchemy import inspect
    cols = {col["name"] for col in inspect(db.engine).get_columns("wa_signal_group")}
    for _, col_name, _ in db.ADDED_COLUMNS:
        assert col_name in cols
