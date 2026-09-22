"""Tests for multi-message sequence signals, multi-targets, and ai_parser."""

import json
from unittest.mock import MagicMock, patch

import pytest

from addons.whatsapp_signals import ai_parser, db, executor, ingest, parser


# --- Regex parser tier tests for real-world signal formats ------------------

def test_parse_buy_above_sets_is_above_price():
    msg = """Nifty   23400CE
Buy    above 68
Stoploss 63"""
    sig = parser.parse(msg)
    assert sig.action == parser.ENTRY
    assert sig.side == "BUY"
    assert sig.base == "NIFTY"
    assert sig.strike == 23400.0
    assert sig.option_type == "CE"
    assert sig.entry_price == 68.0
    assert sig.is_above_price is True
    assert sig.stop_loss == 63.0


def test_parse_active_is_informational_confirmation():
    sig = parser.parse("Active")
    assert sig.action == parser.NONE
    assert sig.informational is True
    assert not sig.is_actionable
    assert "active confirmation" in sig.note


def test_parse_multi_targets():
    sig = parser.parse("Tgt 78/95/115/140")
    assert sig.action == parser.SET_TARGET
    assert sig.target == 78.0
    assert sig.targets == (78.0, 95.0, 115.0, 140.0)


def test_parse_target_hit_with_book_partial():
    msg = """1st Target  78 hit 🎯 
Book partial / full"""
    sig = parser.parse(msg)
    assert sig.action == parser.PARTIAL_EXIT
    assert sig.fraction == 0.5
    assert not sig.informational


# --- Executor tests for above_tick_offset -----------------------------------

def test_executor_adds_above_tick_offset_for_buy_above(monkeypatch):
    """When a signal has is_above_price=True, executor applies group.above_tick_offset."""
    import sys
    import types

    placed_orders = []

    def mock_place_order(order_data, api_key=None, prefetched_quote=None, **kwargs):
        placed_orders.append(order_data)
        return True, {"order_id": "ORD123"}, 200

    fake = types.ModuleType("services.place_order_service")
    fake.place_order = mock_place_order
    monkeypatch.setitem(sys.modules, "services.place_order_service", fake)

    from addons.whatsapp_signals import resolver

    dummy_inst = resolver.ResolvedInstrument(
        symbol="NIFTY26SEP23400CE",
        exchange="NFO",
        product="MIS",
        lotsize=25,
        expiry="26-SEP-26",
        kind="option",
    )
    monkeypatch.setattr(resolver, "resolve", lambda signal, product: (dummy_inst, None))
    monkeypatch.setattr(executor, "_write_stop", lambda *args, **kwargs: True)
    monkeypatch.setattr(executor, "_remove_sessions", lambda: None)

    group = {
        "id": 1,
        "chat_jid": "120363999999@g.us",
        "is_enabled": True,
        "execution_mode": "analyze",
        "lots": 1,
        "order_type": "LIMIT",
        "above_tick_offset": 0.5,
    }

    sig = parser.ParsedSignal(
        action=parser.ENTRY,
        side="BUY",
        base="NIFTY",
        strike=23400.0,
        option_type="CE",
        entry_price=68.0,
        is_above_price=True,
        stop_loss=63.0,
    )

    outcome = executor._enter(sig, group, group["chat_jid"], "analyze", "test_key")
    assert outcome.status == "executed"
    assert len(placed_orders) == 1
    # 68.0 + 0.5 = 68.5
    assert placed_orders[0]["price"] == 68.5
    assert placed_orders[0]["pricetype"] == "LIMIT"


# --- Context-aware ai_parser unit tests -------------------------------------

def test_ai_parser_build_user_block():
    context = [
        {"sender": "Chandru", "text": "Nifty 23400CE Buy above 68 Stoploss 63"},
        {"sender": "Chandru", "text": "Active"},
    ]
    open_pos = [
        {"side": "BUY", "symbol": "NIFTY26SEP23400CE", "quantity": 25, "entry_price": 68.5, "stop_loss": 63.0, "target": 78.0}
    ]
    current = "1st Target 78 hit 🎯 Book partial / full"

    block = ai_parser._build_user_block(current, context, open_pos)
    assert "RECENT MESSAGES" in block
    assert "Nifty 23400CE" in block
    assert "OPEN POSITIONS" in block
    assert "NIFTY26SEP23400CE" in block
    assert "CURRENT MESSAGE:\n1st Target 78 hit" in block


def test_ai_parser_to_signal_multi_targets():
    reply_json = json.dumps({
        "action": "set_target",
        "side": None,
        "base": "NIFTY",
        "strike": 23400,
        "option_type": "CE",
        "target": 78.0,
        "targets": [78.0, 95.0, 115.0, 140.0],
        "reason": "Target levels updated",
    })
    sig = ai_parser._to_signal(reply_json, raw="Tgt 78/95/115/140")
    assert sig is not None
    assert sig.action == parser.SET_TARGET
    assert sig.target == 78.0
    assert sig.targets == (78.0, 95.0, 115.0, 140.0)


def test_ai_parser_to_signal_partial_exit():
    reply_json = json.dumps({
        "action": "partial_exit",
        "side": None,
        "base": "NIFTY",
        "fraction": 0.5,
        "reason": "Book partial at target 1",
    })
    sig = ai_parser._to_signal(reply_json, raw="1st Target 78 hit Book partial")
    assert sig is not None
    assert sig.action == parser.PARTIAL_EXIT
    assert sig.fraction == 0.5


# --- Ingest message buffer tests --------------------------------------------

def test_ingest_message_buffer():
    chat_jid = "test_group@g.us"
    ingest.clear_buffers()
    assert ingest.get_context(chat_jid) == []

    ingest.record_to_buffer(chat_jid, "Msg 1", "sender1", ts=100.0)
    ingest.record_to_buffer(chat_jid, "Msg 2", "sender1", ts=101.0)

    ctx = ingest.get_context(chat_jid)
    assert len(ctx) == 2
    assert ctx[0]["text"] == "Msg 1"
    assert ctx[1]["text"] == "Msg 2"


# --- Regex parser without LLM (offline robustness) -------------------------

@pytest.mark.parametrize("text,expected_targets", [
    ("Tgt 78/95/115/140", (78.0, 95.0, 115.0, 140.0)),
    ("Tgt: 78 / 95 / 115 / 140", (78.0, 95.0, 115.0, 140.0)),
    ("Tgt 78, 95, 115, 140", (78.0, 95.0, 115.0, 140.0)),
    ("Target: 78, 95, 115", (78.0, 95.0, 115.0)),
    ("TARGETS: 78, 95, 115, 140", (78.0, 95.0, 115.0, 140.0)),
    ("T1 78 T2 95 T3 115 T4 140", (78.0, 95.0, 115.0, 140.0)),
    ("T1: 78, T2: 95", (78.0, 95.0)),
    ("TARGET 1 - 78, TARGET 2 - 95", (78.0, 95.0)),
])
def test_regex_multi_target_formats(text, expected_targets):
    sig = parser.parse(text)
    assert sig.action == parser.SET_TARGET
    assert sig.targets == expected_targets
    assert sig.target == expected_targets[0]


@pytest.mark.parametrize("text", [
    "Active",
    "active",
    "Call Active",
    "Trade Active",
    "Order Active",
    "Signal Active",
    "Now Active",
    "Active now",
    "Activated",
])
def test_regex_active_variations_are_informational(text):
    sig = parser.parse(text)
    assert sig.action == parser.NONE
    assert sig.informational is True


def test_regex_target_hit_and_book_full():
    sig = parser.parse("1st Target 78 hit 🎯 Book full")
    assert sig.action == parser.EXIT
    assert not sig.informational


def test_regex_target_hit_and_book_profit():
    sig = parser.parse("Target 1 done, book profit")
    assert sig.action == parser.EXIT
    assert not sig.informational


def test_regex_target_hit_with_compound_sl_to_cost():
    sig = parser.parse("Target 1 hit, book partial and move SL to cost")
    assert sig.action == parser.PARTIAL_EXIT
    assert sig.fraction == 0.5
    assert sig.sl_to_cost is True


def test_regex_target_hit_with_compound_sl_move():
    sig = parser.parse("Target 1 hit, book 50% and trail SL to 65")
    assert sig.action == parser.PARTIAL_EXIT
    assert sig.fraction == 0.5
    assert sig.stop_loss == 65.0


def test_regex_safe_traders_book_profit():
    sig = parser.parse("Safe traders book profit")
    assert sig.action == parser.EXIT
    assert not sig.informational


def test_regex_sl_variations():
    sig1 = parser.parse("Trail SL to cost")
    assert sig1.action == parser.SET_SL
    assert sig1.sl_to_cost is True

    sig2 = parser.parse("Revise SL to 65")
    assert sig2.action == parser.SET_SL
    assert sig2.stop_loss == 65.0

    sig3 = parser.parse("Shift SL to 65")
    assert sig3.action == parser.SET_SL
    assert sig3.stop_loss == 65.0


def test_regex_pure_reports_remain_informational():
    sig1 = parser.parse("1st Target 78 hit 🎯")
    assert sig1.action == parser.NONE
    assert sig1.informational is True

    sig2 = parser.parse("Target hit")
    assert sig2.action == parser.NONE
    assert sig2.informational is True

    sig3 = parser.parse("Booked profit at 150")
    assert sig3.action == parser.NONE
    assert sig3.informational is True


def test_sl_hit_and_cancel_triggers_exit():
    sig1 = parser.parse("SL hit")
    assert sig1.action == parser.EXIT

    sig2 = parser.parse("Stop loss hit")
    assert sig2.action == parser.EXIT

    sig3 = parser.parse("Cancel")
    assert sig3.action == parser.EXIT
