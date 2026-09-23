"""What the regex tier must and must not read.

Each row is a message a real group posts. The ones that assert ``none`` matter
most: they are the messages that look like instructions and are not, and
reading one as an instruction moves a stop or opens a position nobody asked
for.
"""

from __future__ import annotations

import pytest

from addons.whatsapp_signals import parser
from addons.whatsapp_signals.parser import (
    CANCEL,
    ENTRY,
    EXIT,
    NONE,
    PARTIAL_EXIT,
    SET_SL,
    SET_TARGET,
)


@pytest.mark.parametrize(
    "message,action",
    [
        # Entries, in the shapes groups actually write them.
        ("BUY NIFTY 25000 CE @ 120", ENTRY),
        ("*BUY BANKNIFTY 52000 PE ABOVE 200 SL 170 TGT 260*", ENTRY),
        ("NIFTY 25000 CE BUY\nCMP 120\nSL 100\nTGT 150", ENTRY),
        ("Buy NIFTY 25OCT25 25000 CE around 118", ENTRY),
        ("BUY BANKNIFTY 52000PE @ 200", ENTRY),
        ("NIFTY CE 25000 buy @ 120", ENTRY),
        ("SELL RELIANCE 2500 CE @ 30", ENTRY),
        ("BUY RELIANCE @ 1450 SL 1420 TGT 1500", ENTRY),
        ("BUY NIFTY FUT @ 25010 SL 24950", ENTRY),
        # Level changes.
        ("SL to 110", SET_SL),
        ("Revise SL 115 now", SET_SL),
        ("SL at cost", SET_SL),
        ("Move SL to cost friends", SET_SL),
        ("Trail SL to 130", SET_SL),
        ("NIFTY 25000 CE SL 110", SET_SL),
        ("TGT 150", SET_TARGET),
        ("New target 160", SET_TARGET),
        # Exits.
        ("Exit NIFTY 25000 CE", EXIT),
        ("EXIT ALL", EXIT),
        ("Square off everything", EXIT),
        ("Book full profit", EXIT),
        ("Book 100%", EXIT),
        ("SL hit", EXIT),
        # Cancels (untriggered / ignore call).
        ("Cancel", CANCEL),
        ("Cancel call", CANCEL),
        ("Ignore", CANCEL),
        ("Not triggered", CANCEL),
        ("Avoid", CANCEL),
        # Partials.
        ("Book half", PARTIAL_EXIT),
        ("Book 50%", PARTIAL_EXIT),
        ("Book 50 percent", PARTIAL_EXIT),
        ("exit half", PARTIAL_EXIT),
        ("reduce 25%", PARTIAL_EXIT),
        ("Book partial", PARTIAL_EXIT),
        ("get out of half the position", PARTIAL_EXIT),
        ("exit 25% here", PARTIAL_EXIT),
        # Not instructions. Every one of these has been read as an order by a
        # naive matcher at some point; that is why they are here.
        ("Target 150 achieved", NONE),
        ("Booked half at 150", NONE),
        ("TGT 1 done", NONE),
        ("good morning all", NONE),
        ("what is the view on banknifty?", NONE),
        ("Sir thank you, made 30 points", NONE),
        ("/positions", NONE),
        ("", NONE),
    ],
)
def test_action(message, action):
    assert parser.parse(message).action == action


def test_entry_carries_every_level_it_states():
    signal = parser.parse("BUY BANKNIFTY 52000 PE ABOVE 200 SL 170 TGT 260")
    assert signal.side == "BUY"
    assert signal.base == "BANKNIFTY"
    assert signal.strike == 52000
    assert signal.option_type == "PE"
    assert signal.entry_price == 200
    assert signal.stop_loss == 170
    assert signal.target == 260


def test_sell_entry_is_a_short_not_an_exit():
    signal = parser.parse("SELL NIFTY 25000 CE @ 120")
    assert signal.action == ENTRY
    assert signal.side == "SELL"


def test_expiry_is_kept_when_stated_and_absent_when_not():
    assert parser.parse("Buy NIFTY 25OCT25 25000 CE @ 118").expiry == "25OCT25"
    assert parser.parse("Buy NIFTY 25000 CE @ 118").expiry is None


def test_lots_are_read_from_the_message():
    assert parser.parse("2 lots BUY NIFTY 25000 CE @ 120").lots == 2


def test_stop_to_cost_is_flagged_rather_than_priced():
    signal = parser.parse("SL to cost")
    assert signal.action == SET_SL
    assert signal.sl_to_cost is True
    assert signal.stop_loss is None


def test_a_report_with_a_number_in_it_is_still_a_report():
    """The guard this module exists for: a number does not make an instruction."""
    signal = parser.parse("Target 150 achieved, well done")
    assert signal.action == NONE
    assert signal.informational is True
    assert signal.target is None


def test_a_word_ending_in_ce_is_not_an_option_leg():
    """ "reduce 25%" once parsed as base REDU, type CE, strike 25.

    A phantom leg is not harmless: a later "SL to 110" matches itself against
    the legs a group holds, and a fake one is a wrong match waiting to happen.
    """
    signal = parser.parse("reduce 25%")
    assert signal.action == PARTIAL_EXIT
    assert signal.base is None
    assert signal.option_type is None


def test_partial_fractions():
    assert parser.parse("Book half").fraction == 0.5
    assert parser.parse("Book 50%").fraction == 0.5
    assert parser.parse("reduce 25%").fraction == 0.25
    assert parser.parse("Book partial").fraction == 0.5


def test_names_leg_distinguishes_qualified_follow_ups():
    assert parser.parse("NIFTY 25000 CE SL 110").names_leg is True
    assert parser.parse("SL 110").names_leg is False


def test_slash_commands_belong_to_the_upstream_bot():
    signal = parser.parse("/closeall")
    assert signal.action == NONE
    assert parser.worth_llm_attempt("/closeall", signal) is False


@pytest.mark.parametrize(
    "message,worth",
    [
        ("shift the nifty ce stop up a bit", True),
        ("take some off the table on nifty ce", True),
        ("good morning all", False),  # no trading vocabulary
        ("Target 150 achieved", False),  # a report: nothing to find
        ("BUY NIFTY 25000 CE @ 120", False),  # the rules already read it
    ],
)
def test_the_model_is_only_asked_when_it_could_help(message, worth):
    assert parser.worth_llm_attempt(message, parser.parse(message)) is worth


def test_normalize_flattens_decoration_and_newlines():
    assert parser.normalize("*BUY*\n_NIFTY_  25000 CE") == "BUY NIFTY 25000 CE"


def test_msl_enables_trailing_stop():
    sig = parser.parse("MSL 107")
    assert sig.action == SET_SL
    assert sig.stop_loss == 107.0
    assert sig.trail is True


def test_targets_with_leading_slash():
    sig = parser.parse("Tgt /125/150/170")
    assert sig.action == SET_TARGET
    assert sig.target == 125.0
    assert sig.targets == (125.0, 150.0, 170.0)
