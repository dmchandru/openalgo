"""The gates a signal passes before it reaches an order path.

These are the tests that decide whether somebody's money is safe. Each one
pins a refusal, and a refusal that stops working is silent -- the signal simply
goes through -- so every gate here is asserted in both directions.
"""

from __future__ import annotations

import pytest

from addons.whatsapp_signals import db, executor
from addons.whatsapp_signals.parser import ParsedSignal


class _Mode:
    """Stand-in for the platform's global analyze toggle."""

    def __init__(self, mode):
        self.mode = mode

    def __call__(self):
        return self.mode


# ---------------------------------------------------------------- mode gating


def test_a_sandbox_group_is_refused_while_the_platform_is_live(monkeypatch):
    """The asymmetry that matters: this refusal is what stops a group the
    operator put in sandbox from sending a real order."""
    monkeypatch.setattr(executor, "current_mode", _Mode("live"))
    mode, refusal = executor.check_mode({"execution_mode": "analyze"})
    assert mode is None
    assert "sandbox" in refusal and "live" in refusal


def test_a_live_group_runs_in_the_sandbox_while_the_platform_is_in_analyze(monkeypatch):
    """The other direction is allowed: the global toggle is the operator saying
    nothing real should go out, and honouring it is the safe reading."""
    monkeypatch.setattr(executor, "current_mode", _Mode("analyze"))
    mode, refusal = executor.check_mode({"execution_mode": "live"})
    assert refusal is None
    assert mode == "analyze"


def test_matching_modes_run(monkeypatch):
    monkeypatch.setattr(executor, "current_mode", _Mode("live"))
    assert executor.check_mode({"execution_mode": "live"}) == ("live", None)
    monkeypatch.setattr(executor, "current_mode", _Mode("analyze"))
    assert executor.check_mode({"execution_mode": "analyze"}) == ("analyze", None)


def test_an_unreadable_platform_mode_sends_nothing(monkeypatch):
    monkeypatch.setattr(executor, "current_mode", _Mode(None))
    mode, refusal = executor.check_mode({"execution_mode": "live"})
    assert mode is None
    assert refusal


# ------------------------------------------------------------------- senders


@pytest.mark.parametrize(
    "allowed,sender,ok",
    [
        ([], "919876543210@s.whatsapp.net", True),  # empty = anyone
        (["919876543210"], "919876543210@s.whatsapp.net", True),  # bare digits
        (["+91 98765 43210"], "919876543210@s.whatsapp.net", True),  # written by hand
        (["919876543210@s.whatsapp.net"], "919876543210@s.whatsapp.net", True),
        (["919876543210"], "911111111111@s.whatsapp.net", False),
        (["919876543210"], None, False),
    ],
)
def test_sender_allowlist(allowed, sender, ok):
    assert executor.sender_allowed({"allowed_senders": allowed}, sender) is ok


# ---------------------------------------------------------------------- caps


def test_the_daily_cap_counts_executions_not_messages(group):
    chat = group["chat_jid"]
    for _ in range(3):
        db.record_event(chat, None, "chatter", status="ignored")
    assert executor.check_caps({"max_signals_per_day": 2}, chat, "analyze") is None

    for i in range(2):
        db.record_event(chat, None, f"BUY NIFTY {i} CE", status="executed")
    refusal = executor.check_caps({"max_signals_per_day": 2}, chat, "analyze")
    assert refusal and "daily limit" in refusal


def test_the_open_position_cap_counts_open_positions(group):
    chat = group["chat_jid"]
    for i in range(2):
        db.create_position(
            {
                "chat_jid": chat,
                "symbol": f"NIFTY2500{i}CE",
                "exchange": "NFO",
                "product": "MIS",
                "mode": "analyze",
                "side": "BUY",
                "quantity": 75,
            }
        )
    assert executor.check_caps({"max_open_positions": 3}, chat, "analyze") is None
    refusal = executor.check_caps({"max_open_positions": 2}, chat, "analyze")
    assert refusal and "limit" in refusal


def test_a_closed_position_frees_the_cap(group):
    chat = group["chat_jid"]
    created = db.create_position(
        {
            "chat_jid": chat,
            "symbol": "NIFTY25000CE",
            "exchange": "NFO",
            "product": "MIS",
            "mode": "analyze",
            "side": "BUY",
            "quantity": 75,
        }
    )
    assert executor.check_caps({"max_open_positions": 1}, chat, "analyze")
    db.update_position(created["id"], status="closed")
    assert executor.check_caps({"max_open_positions": 1}, chat, "analyze") is None


def test_zero_means_no_cap(group):
    chat = group["chat_jid"]
    for i in range(5):
        db.record_event(chat, None, f"m{i}", status="executed")
    assert (
        executor.check_caps({"max_signals_per_day": 0, "max_open_positions": 0}, chat, "analyze")
        is None
    )


# ------------------------------------------------------------ entry levels


def test_a_stated_stop_is_used_as_stated():
    signal = ParsedSignal(stop_loss=100.0, target=150.0)
    stop, target = executor._levels_for_entry(signal, {"default_sl_pct": 30.0}, "BUY", 120.0)
    assert (stop, target) == (100.0, 150.0)


def test_the_default_stop_is_a_percentage_of_the_entry():
    """A points default cannot serve a premium of 8 and one of 800 at once."""
    signal = ParsedSignal()
    stop, _ = executor._levels_for_entry(signal, {"default_sl_pct": 30.0}, "BUY", 120.0)
    assert stop == 84.0
    stop, _ = executor._levels_for_entry(signal, {"default_sl_pct": 30.0}, "BUY", 8.0)
    assert stop == 5.6


def test_a_short_entry_puts_its_default_stop_above_the_price():
    signal = ParsedSignal()
    stop, _ = executor._levels_for_entry(signal, {"default_sl_pct": 25.0}, "SELL", 100.0)
    assert stop == 125.0


def test_no_reference_price_means_no_invented_stop():
    signal = ParsedSignal()
    assert executor._levels_for_entry(signal, {"default_sl_pct": 30.0}, "BUY", None) == (None, None)


def test_a_nonsense_level_is_dropped_rather_than_sent():
    signal = ParsedSignal(stop_loss=0.0, target=-5.0)
    assert executor._levels_for_entry(signal, {}, "BUY", 120.0) == (None, None)


# ------------------------------------------------------- follow-up matching


def _open(chat, symbol):
    return db.create_position(
        {
            "chat_jid": chat,
            "symbol": symbol,
            "exchange": "NFO",
            "product": "MIS",
            "mode": "analyze",
            "side": "BUY",
            "quantity": 75,
            "entry_price": 120.0,
        }
    )


def test_an_unqualified_follow_up_matches_the_only_open_position(group):
    chat = group["chat_jid"]
    _open(chat, "NIFTY28OCT2525000CE")
    position, error = executor.match_position(ParsedSignal(stop_loss=110.0), chat, "analyze")
    assert error is None
    assert position["symbol"] == "NIFTY28OCT2525000CE"


def test_an_unqualified_follow_up_is_refused_when_several_are_open(group):
    """A stop moved onto the wrong leg is worse than a stop not moved."""
    chat = group["chat_jid"]
    _open(chat, "NIFTY28OCT2525000CE")
    _open(chat, "BANKNIFTY28OCT2552000PE")
    position, error = executor.match_position(ParsedSignal(stop_loss=110.0), chat, "analyze")
    assert position is None
    assert "did not say which position" in error


def test_reconciling_flat_position_allows_unqualified_follow_up(group, monkeypatch):
    """If one of two DB positions is flat at the broker, it is auto-closed so
    the follow-up cleanly matches the one truly active position."""
    chat = group["chat_jid"]
    _open(chat, "NIFTY28OCT2525000CE")
    _open(chat, "BANKNIFTY28OCT2552000PE")

    def fake_live(symbol, exchange, product, api_key):
        return 0 if symbol == "NIFTY28OCT2525000CE" else 35

    monkeypatch.setattr(executor, "_live_net_qty", fake_live)
    monkeypatch.setattr(executor, "_clear_stop", lambda *a, **k: None)

    position, error = executor.match_position(
        ParsedSignal(stop_loss=110.0), chat, "analyze", api_key="test_key"
    )
    assert error is None
    assert position is not None
    assert position["symbol"] == "BANKNIFTY28OCT2552000PE"


def test_a_follow_up_with_no_open_position_is_refused(group):
    position, error = executor.match_position(
        ParsedSignal(stop_loss=110.0), group["chat_jid"], "analyze"
    )
    assert position is None
    assert "no open position" in error


def test_positions_are_scoped_to_their_mode(group):
    """A sandbox position must never be matched by a live signal."""
    chat = group["chat_jid"]
    _open(chat, "NIFTY28OCT2525000CE")
    position, error = executor.match_position(ParsedSignal(stop_loss=110.0), chat, "live")
    assert position is None
    assert error


def test_positions_are_scoped_to_their_group(group):
    chat = group["chat_jid"]
    _open(chat, "NIFTY28OCT2525000CE")
    position, _error = executor.match_position(
        ParsedSignal(stop_loss=110.0), "other@g.us", "analyze"
    )
    assert position is None


# ------------------------------------------------------------ partial sizing


def test_a_partial_exit_is_rounded_down_to_whole_lots(monkeypatch):
    """Half of one lot is not half a lot: it is nothing, and sending a part-lot
    order gets it rejected by the exchange."""
    monkeypatch.setattr(executor, "_partial_quantity", executor._partial_quantity)
    # 150 held, lot size 75, half -> 75 (one whole lot).
    assert _rounded(150, 0.5, 75) == 75
    # 75 held, lot size 75, half -> 0: keep the position rather than close it all.
    assert _rounded(75, 0.5, 75) == 0
    # 225 held, a third -> 75.
    assert _rounded(225, 0.34, 75) == 75


def _rounded(held, fraction, lotsize):
    """The rounding rule, isolated from the master-contract lookup."""
    wanted = int(held * fraction)
    if lotsize > 1:
        wanted = (wanted // lotsize) * lotsize
    return max(0, min(wanted, held))


# ------------------------------------------------------------- entering a leg


@pytest.fixture
def order_path(monkeypatch):
    """Stand in for everything below the executor: the master contract, the
    quote, the order service and the stop row.

    The platform modules are injected rather than imported. They are imported
    inside the functions that use them, so a stub in ``sys.modules`` is enough,
    and it keeps these tests from dragging in the broker layer.
    """
    import sys
    import types

    from addons.whatsapp_signals import resolver

    placed = []

    def _place_order(order_data=None, api_key=None, prefetched_quote=None, **kwargs):
        placed.append(order_data)
        return True, {"status": "success", "orderid": f"OID{len(placed)}"}, 200

    fake = types.ModuleType("services.place_order_service")
    fake.place_order = _place_order
    monkeypatch.setitem(sys.modules, "services.place_order_service", fake)

    instrument = resolver.ResolvedInstrument(
        symbol="NIFTY28OCT2525000CE",
        exchange="NFO",
        product="MIS",
        lotsize=75,
        expiry="28-OCT-25",
        kind="option",
    )
    monkeypatch.setattr(resolver, "resolve", lambda signal, product: (instrument, None))
    monkeypatch.setattr(executor, "_last_price", lambda *a, **k: 120.0)

    stops = []
    monkeypatch.setattr(executor, "_write_stop", lambda inst, **kw: (stops.append(kw), True)[1])
    monkeypatch.setattr(executor, "_remove_sessions", lambda: None)

    return {"placed": placed, "stops": stops, "instrument": instrument}


def _entry(side="BUY", **kwargs):
    from addons.whatsapp_signals.parser import ENTRY

    return ParsedSignal(
        action=ENTRY, side=side, base="NIFTY", strike=25000, option_type="CE", **kwargs
    )


def test_an_entry_places_one_order_and_arms_one_stop(group, order_path):
    outcome = executor._enter(_entry(stop_loss=100.0), group, group["chat_jid"], "analyze", "k")
    assert outcome.status == "executed"
    assert len(order_path["placed"]) == 1
    assert order_path["placed"][0]["quantity"] == 75  # one lot
    assert order_path["placed"][0]["pricetype"] == "MARKET"
    assert order_path["stops"][0]["stop_loss"] == 100.0
    assert len(db.open_positions(group["chat_jid"], mode="analyze")) == 1


def test_a_sell_on_a_leg_already_held_long_is_refused(group, order_path):
    """It would net the position down or reverse it, and the message does not
    say which. Adding to it -- which matching on the leg alone would do -- is
    the one reading that is certainly wrong."""
    executor._enter(_entry("BUY"), group, group["chat_jid"], "analyze", "k")
    outcome = executor._enter(_entry("SELL"), group, group["chat_jid"], "analyze", "k")
    assert outcome.status == "rejected"
    assert "reverse or close" in outcome.detail
    assert len(order_path["placed"]) == 1  # nothing was sent
    assert len(db.open_positions(group["chat_jid"], mode="analyze")) == 1


def test_adding_to_a_leg_is_skipped_when_position_exists(group, order_path, monkeypatch):
    """A second entry signal for the same instrument and side is now skipped
    instead of pyramiding into the position. The group must exit first before
    a fresh entry is accepted."""
    executor._enter(_entry(stop_loss=100.0), group, group["chat_jid"], "analyze", "k")
    monkeypatch.setattr(executor, "_last_price", lambda *a, **k: 100.0)
    outcome = executor._enter(_entry(), group, group["chat_jid"], "analyze", "k")

    assert outcome.status == "ignored"
    assert "Already holding" in outcome.detail
    # Only one order should have been placed (the first entry)
    assert len(order_path["placed"]) == 1
    # Position quantity unchanged
    positions = db.open_positions(group["chat_jid"], mode="analyze")
    assert len(positions) == 1
    assert positions[0]["quantity"] == 75  # only the first lot


def test_re_entry_on_existing_position_is_skipped_not_added(group, order_path, monkeypatch):
    """A second BUY on the same leg is silently skipped — stop stays from the
    first entry, no second order is placed."""
    executor._enter(_entry(stop_loss=100.0), group, group["chat_jid"], "analyze", "k")
    monkeypatch.setattr(executor, "_last_price", lambda *a, **k: 100.0)
    outcome = executor._enter(_entry(), group, group["chat_jid"], "analyze", "k")
    assert outcome.status == "ignored"
    # Stop from first entry still intact
    assert order_path["stops"][-1]["stop_loss"] == 100.0
    assert len(order_path["placed"]) == 1


def test_re_entry_is_ignored_not_blocked_by_position_cap(group, order_path):
    """A second entry for the same leg is now skipped at the re-entry guard,
    not at the cap check. The cap is for NEW legs, not re-entry on the same one."""
    db.update_group(group["chat_jid"], {"max_open_positions": 1})
    reloaded = db.get_group(group["chat_jid"])
    executor._enter(_entry(), reloaded, group["chat_jid"], "analyze", "k")
    outcome = executor._enter(_entry(), reloaded, group["chat_jid"], "analyze", "k")
    # Skipped by re-entry guard, not the cap
    assert outcome.status == "ignored"
    assert len(order_path["placed"]) == 1


def test_a_new_leg_is_blocked_by_the_open_position_cap(group, order_path, monkeypatch):
    from addons.whatsapp_signals import resolver

    db.update_group(group["chat_jid"], {"max_open_positions": 1})
    reloaded = db.get_group(group["chat_jid"])
    executor._enter(_entry(), reloaded, group["chat_jid"], "analyze", "k")

    other = resolver.ResolvedInstrument(
        symbol="BANKNIFTY28OCT2552000PE",
        exchange="NFO",
        product="MIS",
        lotsize=35,
        expiry="28-OCT-25",
        kind="option",
    )
    monkeypatch.setattr(resolver, "resolve", lambda signal, product: (other, None))
    outcome = executor._enter(_entry(), reloaded, group["chat_jid"], "analyze", "k")
    assert outcome.status == "rejected"
    assert "limit" in outcome.detail


def test_lots_are_capped_at_the_group_maximum(group, order_path):
    db.update_group(group["chat_jid"], {"lots": 1, "max_lots": 2})
    reloaded = db.get_group(group["chat_jid"])
    executor._enter(_entry(lots=10), reloaded, group["chat_jid"], "analyze", "k")
    assert order_path["placed"][0]["quantity"] == 150  # two lots, not ten


def test_a_refused_order_leaves_no_position_behind(group, order_path, monkeypatch):
    import sys
    import types

    fake = types.ModuleType("services.place_order_service")
    fake.place_order = lambda **kwargs: (False, {"message": "insufficient margin"}, 400)
    monkeypatch.setitem(sys.modules, "services.place_order_service", fake)

    outcome = executor._enter(_entry(), group, group["chat_jid"], "analyze", "k")
    assert outcome.status == "failed"
    assert "insufficient margin" in outcome.detail
    assert db.open_positions(group["chat_jid"], mode="analyze") == []
    assert order_path["stops"] == []


def test_a_position_whose_stop_could_not_be_saved_says_so_loudly(group, order_path, monkeypatch):
    """The order is filled and the position is real. Reporting a clean success
    here is how a position ends up with nothing watching it."""
    monkeypatch.setattr(executor, "_write_stop", lambda inst, **kw: False)
    outcome = executor._enter(_entry(stop_loss=100.0), group, group["chat_jid"], "analyze", "k")
    assert outcome.status == "executed"
    assert "NOT being watched" in outcome.detail


def test_averaged_entry_arithmetic():
    assert executor._averaged_entry({"quantity": 75, "entry_price": 120.0}, 75, 100.0) == 110.0
    assert executor._averaged_entry({"quantity": 75, "entry_price": 120.0}, 150, 90.0) == 100.0
    # Nothing known to average against, or nothing to average with.
    assert executor._averaged_entry({"quantity": 0, "entry_price": None}, 75, 100.0) == 100.0
    assert executor._averaged_entry({"quantity": 75, "entry_price": 120.0}, 75, None) == 120.0
