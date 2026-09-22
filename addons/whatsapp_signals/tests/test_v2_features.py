"""Tests for v2 features: order profiles, LIMIT orders, AI management suggestions, and sequence execution."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from addons.whatsapp_signals import ai_manager, db, executor, routes


def test_order_profile_crud():
    # Create profile
    created, err = db.save_profile(
        {
            "name": "Scalp Profile 1",
            "lots": 4,
            "max_lots": 10,
            "order_type": "LIMIT",
            "limit_price_offset_pct": 0.5,
            "product": "MIS",
            "default_sl_pct": 20.0,
            "default_target_pct": 40.0,
            "trailing_enabled": True,
            "trailing_step": 5.0,
        }
    )
    assert err is None
    assert created is not None
    assert created["name"] == "Scalp Profile 1"
    assert created["order_type"] == "LIMIT"
    assert created["lots"] == 4
    profile_id = created["id"]

    # List profiles
    profiles = db.list_profiles()
    assert any(p["id"] == profile_id for p in profiles)

    # Update profile
    updated, err2 = db.save_profile({"id": profile_id, "lots": 6, "name": "Scalp Profile Renamed"})
    assert err2 is None
    assert updated["lots"] == 6
    assert updated["name"] == "Scalp Profile Renamed"

    # Get profile
    fetched = db.get_profile(profile_id)
    assert fetched["lots"] == 6

    # Delete profile
    deleted = db.delete_profile(profile_id)
    assert deleted is True
    assert db.get_profile(profile_id) is None


def test_group_with_order_profile_overlay():
    prof, _ = db.save_profile(
        {
            "name": "Overlay Profile",
            "lots": 10,
            "max_lots": 20,
            "order_type": "LIMIT",
            "limit_price_offset_pct": 1.0,
        }
    )
    prof_id = prof["id"]

    chat_jid = "120363000000000099@g.us"
    db.observe_group(chat_jid, "Group with Profile")
    saved, err = db.update_group(
        chat_jid,
        {
            "lots": 2,
            "max_lots": 5,
            "order_type": "MARKET",
            "order_profile_id": prof_id,
            "auto_apply_ai": True,
        },
    )
    assert err is None
    assert saved["order_profile_id"] == prof_id
    assert saved["auto_apply_ai"] is True

    # Test executor._effective_group overlays profile values
    effective = executor._effective_group(saved)
    assert effective["lots"] == 10
    assert effective["max_lots"] == 20
    assert effective["order_type"] == "LIMIT"
    assert effective["limit_price_offset_pct"] == 1.0

    # Cleanup
    db.delete_profile(prof_id)


def test_ai_suggestion_crud():
    chat_jid = "120363000000000098@g.us"
    actions = [
        {"action": "set_sl", "symbol": "NIFTY25000CE", "stop_loss": 110.0, "sl_to_cost": False},
        {"action": "partial_exit", "symbol": "NIFTY25000CE", "fraction": 0.5},
    ]

    sug = db.create_suggestion(
        chat_jid=chat_jid,
        reasoning="Move SL to 110 and book 50%",
        actions=actions,
        status="pending",
    )
    assert sug is not None
    assert sug["chat_jid"] == chat_jid
    assert len(sug["suggested_actions"]) == 2
    assert sug["status"] == "pending"

    # List suggestions
    pending_list = db.list_suggestions(chat_jid, status="pending")
    assert any(s["id"] == sug["id"] for s in pending_list)

    # Resolve suggestion
    resolved = db.resolve_suggestion(sug["id"], "applied")
    assert resolved["status"] == "applied"
    assert resolved["resolved_at"] is not None

    # Should no longer be in pending
    pending_list_after = db.list_suggestions(chat_jid, status="pending")
    assert not any(s["id"] == sug["id"] for s in pending_list_after)


def test_ai_manager_looks_like_management():
    # Clear management intents
    assert ai_manager.looks_like_management("Move SL to cost") is True
    assert ai_manager.looks_like_management("Book partial 50% profit here") is True
    assert ai_manager.looks_like_management("Trailing stop to 130") is True
    assert ai_manager.looks_like_management("Exit full position now") is True
    assert ai_manager.looks_like_management("Revise target to 200") is True

    # Past-tense reports (must return False)
    assert ai_manager.looks_like_management("SL hit, booked loss") is False
    assert ai_manager.looks_like_management("Target achieved! Made 40 points") is False
    assert ai_manager.looks_like_management("Target 1 reached and profit booked") is False

    # Irrelevant chat
    assert ai_manager.looks_like_management("Good morning traders") is False
    assert ai_manager.looks_like_management("hi") is False


def test_ai_manager_parse_reply_validation():
    positions = [
        {"symbol": "NIFTY25000CE", "side": "BUY", "quantity": 50, "entry_price": 100.0, "stop_loss": 80.0},
        {"symbol": "BANKNIFTY52000PE", "side": "BUY", "quantity": 30, "entry_price": 250.0, "stop_loss": 200.0},
    ]

    # Valid reply with two actions
    valid_json = json.dumps([
        {"action": "set_sl", "symbol": "NIFTY25000CE", "stop_loss": 100.0, "sl_to_cost": True, "reasoning": "Breakeven"},
        {"action": "partial_exit", "symbol": "NIFTY25000CE", "fraction": 0.5, "reasoning": "Book half"},
    ])
    actions, summary = ai_manager._parse_reply(valid_json, positions)
    assert len(actions) == 2
    assert actions[0]["action"] == "set_sl"
    assert actions[0]["sl_to_cost"] is True
    assert actions[1]["action"] == "partial_exit"
    assert actions[1]["fraction"] == 0.5

    # Discards unknown symbol
    invalid_symbol = json.dumps([
        {"action": "set_sl", "symbol": "SENSEX80000CE", "stop_loss": 50.0}
    ])
    actions_inv, _ = ai_manager._parse_reply(invalid_symbol, positions)
    assert len(actions_inv) == 0

    # Discards invalid action
    unknown_action = json.dumps([
        {"action": "buy_more", "symbol": "NIFTY25000CE"}
    ])
    actions_act, _ = ai_manager._parse_reply(unknown_action, positions)
    assert len(actions_act) == 0

    # Handles prose surrounding JSON array
    prose_json = "Here is the plan:\n" + valid_json + "\nHope that helps!"
    actions_prose, _ = ai_manager._parse_reply(prose_json, positions)
    assert len(actions_prose) == 2


def test_executor_execute_sequence_stops_on_failure():
    group = {
        "is_enabled": True,
        "execution_mode": "analyze",
        "lots": 1,
        "max_lots": 1,
    }
    chat_jid = "120363000000000097@g.us"

    actions = [
        {"action": "invalid_action_type", "symbol": "XYZ"},
        {"action": "set_sl", "symbol": "XYZ", "stop_loss": 100.0},
    ]

    with patch("addons.whatsapp_signals.executor.check_mode", return_value=("analyze", None)), \
         patch("addons.whatsapp_signals.executor._api_key", return_value="fake-api-key"):
        outcomes = executor.execute_sequence(actions, group, chat_jid)
        # Should stop after the first rejection or failure
        assert len(outcomes) == 1
        assert outcomes[0].status == "rejected"

