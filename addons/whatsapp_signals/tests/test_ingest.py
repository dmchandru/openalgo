"""What reaches an order path, and what is turned away before it.

``handle`` is the whole pipeline for one message. These tests replace the
executor so nothing is ordered, and assert on what was recorded and on whether
the executor was reached at all -- which is the thing that matters, because
every gate here exists to stop a message short of it.
"""

from __future__ import annotations

import pytest

from addons.whatsapp_signals import db, executor, ingest


class _Executor:
    """Records that it was called, and answers however the test asks."""

    def __init__(self, status="executed", detail="did the thing"):
        self.calls = []
        self.status = status
        self.detail = detail

    def __call__(self, signal, group, chat_jid):
        self.calls.append((signal, group, chat_jid))
        return executor.Outcome(self.status, self.detail)


@pytest.fixture
def no_orders(monkeypatch):
    stub = _Executor()
    monkeypatch.setattr(executor, "execute", stub)
    monkeypatch.setattr(ingest, "_notify", lambda *a, **k: None)
    return stub


def _message(chat, text, sender="919876543210@s.whatsapp.net"):
    return {"chat_jid": chat, "sender_jid": sender, "text": text, "is_from_me": False}


# ------------------------------------------------------------------- offering


@pytest.mark.parametrize(
    "chat,text,queued",
    [
        ("120363000000000000@g.us", "BUY NIFTY 25000 CE @ 120", True),
        ("919876543210@s.whatsapp.net", "BUY NIFTY 25000 CE @ 120", False),  # 1:1 chat
        ("120363000000000000@g.us", "/positions", False),  # bot command
        ("120363000000000000@g.us", "   ", False),
        ("", "BUY NIFTY 25000 CE", False),
    ],
)
def test_only_group_messages_that_are_not_commands_are_queued(chat, text, queued):
    assert ingest.offer(chat, "s@x", text) is queued
    if queued:
        ingest._queue.get_nowait()


def test_a_flood_is_dropped_rather_than_queued_without_limit(monkeypatch):
    """One Gunicorn worker that never restarts: an unbounded queue is a leak."""
    drained = []
    try:
        for i in range(ingest.QUEUE_MAXSIZE + 20):
            ingest.offer("120363000000000000@g.us", "s@x", f"BUY NIFTY {i} CE @ 1")
        assert ingest._queue.qsize() == ingest.QUEUE_MAXSIZE
    finally:
        while not ingest._queue.empty():
            drained.append(ingest._queue.get_nowait())


# -------------------------------------------------------------------- gating


def test_a_group_nobody_enabled_is_not_read_at_all(no_orders):
    """Not even logged: sighting a group must cost nothing and leak nothing."""
    db.observe_group("120363000000000099@g.us")
    result = ingest.handle(_message("120363000000000099@g.us", "BUY NIFTY 25000 CE @ 120"))
    assert result["status"] == "ignored"
    assert no_orders.calls == []
    assert db.list_events("120363000000000099@g.us") == []


def test_an_unknown_group_is_recorded_as_seen_but_not_read(no_orders):
    ingest.handle(_message("120363000000000098@g.us", "BUY NIFTY 25000 CE @ 120"))
    seen = db.get_group("120363000000000098@g.us")
    assert seen is not None and seen["is_enabled"] is False
    assert no_orders.calls == []


def test_a_sender_off_the_allowlist_never_reaches_the_executor(group, no_orders):
    db.update_group(group["chat_jid"], {"allowed_senders": ["911111111111"]})
    result = ingest.handle(
        _message(group["chat_jid"], "BUY NIFTY 25000 CE @ 120", "919999999999@s.whatsapp.net")
    )
    assert result["status"] == "rejected"
    assert no_orders.calls == []
    assert db.list_events(group["chat_jid"])[0]["status"] == "rejected"


def test_a_sender_on_the_allowlist_gets_through(group, no_orders):
    db.update_group(group["chat_jid"], {"allowed_senders": ["919876543210"]})
    result = ingest.handle(_message(group["chat_jid"], "BUY NIFTY 25000 CE @ 120"))
    assert result["status"] == "executed"
    assert len(no_orders.calls) == 1


# -------------------------------------------------------------------- reading


def test_conversation_is_logged_and_left_alone(group, no_orders):
    result = ingest.handle(_message(group["chat_jid"], "good morning everyone"))
    assert result["status"] == "ignored"
    assert no_orders.calls == []
    logged = db.list_events(group["chat_jid"])[0]
    assert logged["status"] == "ignored"
    assert logged["action"] == "none"


def test_a_status_report_is_logged_and_left_alone(group, no_orders):
    ingest.handle(_message(group["chat_jid"], "Target 150 achieved"))
    assert no_orders.calls == []


def test_a_signal_reaches_the_executor_and_its_outcome_is_recorded(group, no_orders):
    result = ingest.handle(_message(group["chat_jid"], "BUY NIFTY 25000 CE @ 120 SL 100"))
    assert result["status"] == "executed"
    signal, passed_group, chat = no_orders.calls[0]
    assert signal.side == "BUY"
    assert signal.stop_loss == 100
    assert chat == group["chat_jid"]
    assert passed_group["chat_jid"] == group["chat_jid"]

    logged = db.list_events(group["chat_jid"])[0]
    assert logged["status"] == "executed"
    assert logged["detail"] == "did the thing"
    assert logged["action"] == "entry"


def test_the_same_signal_twice_only_trades_once(group, no_orders):
    """A forwarded signal lands twice. The second copy must not open a second
    position."""
    text = "BUY NIFTY 25000 CE @ 120"
    assert ingest.handle(_message(group["chat_jid"], text))["status"] == "executed"
    second = ingest.handle(_message(group["chat_jid"], text))
    assert second["status"] == "duplicate"
    assert len(no_orders.calls) == 1


def test_a_repeated_message_that_never_traded_is_not_a_duplicate(group, no_orders):
    text = "good morning everyone"
    ingest.handle(_message(group["chat_jid"], text))
    assert ingest.handle(_message(group["chat_jid"], text))["status"] == "ignored"


def test_a_failed_signal_is_recorded_as_failed(group, monkeypatch):
    monkeypatch.setattr(executor, "execute", _Executor("failed", "the broker said no"))
    monkeypatch.setattr(ingest, "_notify", lambda *a, **k: None)
    result = ingest.handle(_message(group["chat_jid"], "BUY NIFTY 25000 CE @ 120"))
    assert result["status"] == "failed"
    assert db.list_events(group["chat_jid"])[0]["detail"] == "the broker said no"


# ---------------------------------------------------------------- the model


def test_the_model_is_not_called_for_a_message_the_rules_read(group, no_orders, monkeypatch):
    calls = []
    monkeypatch.setattr(ingest.llm, "is_available", lambda: True)
    monkeypatch.setattr(ingest.llm, "parse", lambda text: calls.append(text))
    ingest.handle(_message(group["chat_jid"], "BUY NIFTY 25000 CE @ 120"))
    assert calls == []


def test_the_model_is_not_called_when_the_group_turned_it_off(group, no_orders, monkeypatch):
    calls = []
    db.update_group(group["chat_jid"], {"llm_fallback": False})
    monkeypatch.setattr(ingest.llm, "is_available", lambda: True)
    monkeypatch.setattr(ingest.llm, "parse", lambda text: calls.append(text))
    ingest.handle(_message(group["chat_jid"], "shift the nifty ce stop up a bit"))
    assert calls == []


def test_the_model_answer_is_used_when_the_rules_could_not_read_it(group, no_orders, monkeypatch):
    from addons.whatsapp_signals.parser import SET_SL, ParsedSignal

    monkeypatch.setattr(ingest.llm, "is_available", lambda: True)
    monkeypatch.setattr(
        ingest.llm,
        "parse",
        lambda text: ParsedSignal(action=SET_SL, tier="llm", stop_loss=130.0, raw=text),
    )
    result = ingest.handle(_message(group["chat_jid"], "shift the nifty ce stop up a bit"))
    assert result["status"] == "executed"
    signal, _group, _chat = no_orders.calls[0]
    assert signal.tier == "llm"
    assert signal.stop_loss == 130.0


def test_a_model_that_finds_nothing_leaves_the_message_alone(group, no_orders, monkeypatch):
    monkeypatch.setattr(ingest.llm, "is_available", lambda: True)
    monkeypatch.setattr(ingest.llm, "parse", lambda text: None)
    result = ingest.handle(_message(group["chat_jid"], "shift the nifty ce stop up a bit"))
    assert result["status"] == "ignored"
    assert no_orders.calls == []


# --------------------------------------------------------------- notifying


def test_the_operator_is_told_what_was_done_in_their_name(group, monkeypatch):
    sent = []
    monkeypatch.setattr(executor, "execute", _Executor("executed", "BUY 75 NIFTY25000CE"))
    monkeypatch.setattr(ingest, "_notify", lambda g, s, o: sent.append(o.detail))
    ingest.handle(_message(group["chat_jid"], "BUY NIFTY 25000 CE @ 120"))
    assert sent == ["BUY 75 NIFTY25000CE"]


def test_no_notification_when_the_group_asked_not_to_be_told(group, monkeypatch):
    sent = []
    db.update_group(group["chat_jid"], {"notify_operator": False})
    monkeypatch.setattr(executor, "execute", _Executor())
    monkeypatch.setattr(ingest, "_notify", lambda g, s, o: sent.append(o.detail))
    ingest.handle(_message(group["chat_jid"], "BUY NIFTY 25000 CE @ 120"))
    assert sent == []
