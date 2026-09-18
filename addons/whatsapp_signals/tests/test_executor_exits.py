"""Closing a position, and the three ways that goes wrong.

Every test here is about a position that is still open at the broker. Getting
an exit wrong does not just fail to close something -- it can open the opposite
position, or leave a real one with nothing watching its stop.
"""

from __future__ import annotations

import sys
import types

import pytest

from addons.whatsapp_signals import db, executor
from addons.whatsapp_signals.parser import EXIT, PARTIAL_EXIT, ParsedSignal


@pytest.fixture
def exit_path(monkeypatch):
    """Stub the order path and record what it was asked to do."""
    sent = []

    def _reducing_exit(symbol, exchange, product, action, quantity, auth_token, broker, api_key):
        sent.append({"symbol": symbol, "action": action, "quantity": quantity, "product": product})
        return True, {"status": "success", "orderid": "X1"}, 200

    scalping = types.ModuleType("blueprints.scalping")
    scalping._reducing_exit = _reducing_exit
    monkeypatch.setitem(sys.modules, "blueprints.scalping", scalping)

    stop_writes = []
    scalping_db = types.ModuleType("database.scalping_db")
    scalping_db.upsert_sl_state = lambda payload: stop_writes.append(payload)
    scalping_db.delete_sl_state = lambda *a, **k: True
    scalping_db.track_symbol = lambda *a, **k: True
    monkeypatch.setitem(sys.modules, "database.scalping_db", scalping_db)

    cleared = []
    monkeypatch.setattr(executor, "_clear_stop", lambda s, e, p, m: cleared.append((s, e, p, m)))
    monkeypatch.setattr(executor, "_remove_sessions", lambda: None)
    monkeypatch.setattr(executor, "_notify_risk_monitor", lambda: None)

    return {"sent": sent, "cleared": cleared, "stop_writes": stop_writes}


def _position(chat, quantity=150, symbol="NIFTY28OCT2525000CE"):
    return db.create_position(
        {
            "chat_jid": chat,
            "symbol": symbol,
            "exchange": "NFO",
            "product": "MIS",
            "mode": "analyze",
            "side": "BUY",
            "quantity": quantity,
            "lots": 2,
            "entry_price": 120.0,
            "stop_loss": 100.0,
        }
    )


def _held(quantity):
    return lambda *a, **k: quantity


# ------------------------------------------------------------------ full exit


def test_a_full_exit_sells_what_is_actually_held(group, exit_path, monkeypatch):
    chat = group["chat_jid"]
    position = _position(chat)
    # The broker says 75, not the 150 on record: the monitor took a lot out.
    monkeypatch.setattr(executor, "_live_net_qty", _held(75))

    outcome = executor._exit_one(position, "k", "analyze", None)
    assert outcome.status == "executed"
    assert exit_path["sent"] == [
        {"symbol": "NIFTY28OCT2525000CE", "action": "SELL", "quantity": 75, "product": "MIS"}
    ]
    assert db.list_positions(chat)[0]["status"] == "closed"
    assert exit_path["cleared"]


def test_a_short_position_is_closed_by_buying(group, exit_path, monkeypatch):
    position = _position(group["chat_jid"])
    monkeypatch.setattr(executor, "_live_net_qty", _held(-75))
    executor._exit_one(position, "k", "analyze", None)
    assert exit_path["sent"][0]["action"] == "BUY"
    assert exit_path["sent"][0]["quantity"] == 75


def test_a_leg_the_monitor_already_closed_sends_no_order(group, exit_path, monkeypatch):
    """Sizing an exit from our own record instead of the broker's would send a
    SELL against nothing, which does not close a position -- it opens a short."""
    chat = group["chat_jid"]
    position = _position(chat)
    monkeypatch.setattr(executor, "_live_net_qty", _held(0))

    outcome = executor._exit_one(position, "k", "analyze", None)
    assert outcome.status == "executed"
    assert exit_path["sent"] == []
    assert "already flat" in outcome.detail
    assert db.list_positions(chat)[0]["status"] == "closed"
    assert exit_path["cleared"]


def test_a_refused_exit_leaves_the_position_open_and_watched(group, exit_path, monkeypatch):
    """The position is still there. Clearing its stop and marking it closed is
    how a real position ends up with nothing watching it."""
    chat = group["chat_jid"]
    position = _position(chat)
    monkeypatch.setattr(executor, "_live_net_qty", _held(150))

    scalping = sys.modules["blueprints.scalping"]
    scalping._reducing_exit = lambda *a, **k: (False, {"message": "market closed"}, 400)

    outcome = executor._exit_one(position, "k", "analyze", None)
    assert outcome.status == "failed"
    assert "STILL OPEN" in outcome.detail
    assert "market closed" in outcome.detail
    assert db.list_positions(chat)[0]["status"] == "open"
    assert exit_path["cleared"] == []


# --------------------------------------------------------------- partial exit


def test_a_partial_exit_closes_whole_lots_and_keeps_the_rest_watched(group, exit_path, monkeypatch):
    chat = group["chat_jid"]
    position = _position(chat, quantity=150)
    monkeypatch.setattr(executor, "_live_net_qty", _held(150))
    monkeypatch.setattr(executor, "_partial_quantity", lambda s, e, held, f: 75)

    outcome = executor._exit_one(position, "k", "analyze", 0.5)
    assert outcome.status == "executed"
    assert exit_path["sent"][0]["quantity"] == 75
    assert "75 still open" in outcome.detail

    remaining = db.list_positions(chat)[0]
    assert remaining["status"] == "open"
    assert remaining["quantity"] == 75
    # The stop stays on the leg, resized. It is not cleared and not recreated.
    assert exit_path["cleared"] == []
    assert exit_path["stop_writes"][-1]["quantity"] == 75
    assert "initial_sl" not in exit_path["stop_writes"][-1]


def test_a_partial_too_small_to_be_a_lot_sends_nothing(group, exit_path, monkeypatch):
    """Half of one lot is not half a lot. Rounding up would close the whole
    position when the instruction was to keep some on."""
    chat = group["chat_jid"]
    position = _position(chat, quantity=75)
    monkeypatch.setattr(executor, "_live_net_qty", _held(75))
    monkeypatch.setattr(executor, "_partial_quantity", lambda s, e, held, f: 0)

    outcome = executor._exit_one(position, "k", "analyze", 0.5)
    assert outcome.status == "rejected"
    assert exit_path["sent"] == []
    assert db.list_positions(chat)[0]["status"] == "open"


# ------------------------------------------------------------- routing an exit


def test_an_unqualified_exit_closes_every_position_the_group_holds(group, exit_path, monkeypatch):
    chat = group["chat_jid"]
    _position(chat, symbol="NIFTY28OCT2525000CE")
    _position(chat, symbol="BANKNIFTY28OCT2552000PE")
    monkeypatch.setattr(executor, "_live_net_qty", _held(150))

    outcome = executor._exit(ParsedSignal(action=EXIT), group, chat, "analyze", "k", fraction=None)
    assert outcome.status == "executed"
    assert len(exit_path["sent"]) == 2
    assert all(p["status"] == "closed" for p in db.list_positions(chat))


def test_an_unqualified_partial_with_several_open_is_refused(group, exit_path, monkeypatch):
    """ "book half" cannot mean "half of each of three positions"."""
    chat = group["chat_jid"]
    _position(chat, symbol="NIFTY28OCT2525000CE")
    _position(chat, symbol="BANKNIFTY28OCT2552000PE")
    monkeypatch.setattr(executor, "_live_net_qty", _held(150))

    outcome = executor._exit(
        ParsedSignal(action=PARTIAL_EXIT, fraction=0.5),
        group,
        chat,
        "analyze",
        "k",
        fraction=0.5,
    )
    assert outcome.status == "rejected"
    assert exit_path["sent"] == []


def test_an_exit_naming_its_leg_closes_only_that_leg(group, exit_path, monkeypatch):
    chat = group["chat_jid"]
    _position(chat, symbol="NIFTY28OCT2525000CE")
    _position(chat, symbol="BANKNIFTY28OCT2552000PE")
    monkeypatch.setattr(executor, "_live_net_qty", _held(150))

    from addons.whatsapp_signals import resolver

    target = resolver.ResolvedInstrument(
        symbol="NIFTY28OCT2525000CE",
        exchange="NFO",
        product="MIS",
        lotsize=75,
        expiry=None,
        kind="option",
    )
    monkeypatch.setattr(resolver, "resolve", lambda signal, product: (target, None))

    signal = ParsedSignal(action=EXIT, base="NIFTY", strike=25000, option_type="CE")
    outcome = executor._exit(signal, group, chat, "analyze", "k", fraction=None)
    assert outcome.status == "executed"
    assert [s["symbol"] for s in exit_path["sent"]] == ["NIFTY28OCT2525000CE"]

    still_open = [p["symbol"] for p in db.open_positions(chat, mode="analyze")]
    assert still_open == ["BANKNIFTY28OCT2552000PE"]


def test_an_exit_with_nothing_open_is_refused(group, exit_path):
    outcome = executor._exit(
        ParsedSignal(action=EXIT), group, group["chat_jid"], "analyze", "k", fraction=None
    )
    assert outcome.status == "rejected"
    assert "no open position" in outcome.detail
