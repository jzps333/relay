"""Tests for relay/safety.py. No network, no devices."""
import pytest

from relay import safety
from relay.types import Action, Element, Screen


def make_screen(app="com.instagram.android", *texts_and_kinds):
    """Build a fake Screen. Each arg is (text, kind)."""
    elements = [
        Element(id=i, text=text, kind=kind, bounds=(0, i * 10, 100, i * 10 + 10), clickable=True)
        for i, (text, kind) in enumerate(texts_and_kinds)
    ]
    return Screen(device="phone", app=app, elements=elements)


# ---- is_emergency ---------------------------------------------------------

@pytest.mark.parametrize("text", [
    "call 911",
    "911",
    "Nine one one",
    "nine-one-one please",
    "this is an EMERGENCY",
    "call an ambulance",
    "call the police",
    "Call the fire department",
    "I need help, I fell and I'm hurt",
    "help me I'm bleeding",
])
def test_emergency_true(text):
    assert safety.is_emergency(text)


@pytest.mark.parametrize("text", [
    "open youtube",
    "I need help finding my keys",
    "call mom",
    "play song 9110",          # word boundary: not 911
    "",
])
def test_emergency_false(text):
    assert not safety.is_emergency(text)


# ---- is_stop ----------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "stop", "STOP!", "cancel", "never mind", "Nevermind", "quit", "halt",
    "okay, cancel that", "please stop now", "stop it", "stop. I changed my mind",
    "I said stop",
])
def test_stop_true(text):
    assert safety.is_stop(text)


@pytest.mark.parametrize("text", [
    "get directions home and stop at the store on the way",
    "stop at the store on the way",
    "cancel my uber order",
    "open the stopwatch",
    "find the bus stop near me",
    "find bus stop",
    "",
])
def test_stop_false(text):
    assert not safety.is_stop(text)


# ---- needs_confirmation -----------------------------------------------------

def test_tap_send_asks_with_app_name():
    screen = make_screen("com.instagram.android", ("Message", "input"), ("Send", "button"))
    q = safety.needs_confirmation(Action(kind="tap", element_id=1), screen)
    assert q == "About to tap Send in Instagram. Should I do it?"


@pytest.mark.parametrize("label", [
    "Post", "Pay $12.00", "Buy now", "Place order", "Checkout", "Confirm",
    "Delete", "Remove", "Call", "Transfer", "Submit", "Share",
])
def test_risky_taps_ask(label):
    screen = make_screen("Shop", (label, "button"))
    q = safety.needs_confirmation(Action(kind="tap", element_id=0), screen)
    assert q is not None and label in q and not q.startswith("BLOCKED:")


@pytest.mark.parametrize("label", ["Search", "Home", "Sender info", "Settings"])
def test_safe_taps_pass(label):
    screen = make_screen("Shop", (label, "button"))
    assert safety.needs_confirmation(Action(kind="tap", element_id=0), screen) is None


@pytest.mark.parametrize("field", ["Password", "Card number", "CVV", "SSN", "Security code"])
def test_type_into_sensitive_field_blocked(field):
    screen = make_screen("Bank", (field, "input"))
    q = safety.needs_confirmation(Action(kind="type", element_id=0, text="hunter2"), screen)
    assert q.startswith(safety.BLOCKED_PREFIX)


def test_typed_card_number_blocked_even_in_plain_field():
    screen = make_screen("Notes", ("Note body", "input"))
    q = safety.needs_confirmation(Action(kind="type", element_id=0, text="4111 1111 1111 1111"), screen)
    assert q.startswith("BLOCKED:")


def test_type_normal_text_ok():
    screen = make_screen("Messages", ("Text message", "input"))
    assert safety.needs_confirmation(Action(kind="type", element_id=0, text="running late"), screen) is None


def test_non_element_actions_ok():
    screen = make_screen("Messages", ("Send", "button"))
    for kind in ["back", "home", "scroll", "open_app", "done"]:
        assert safety.needs_confirmation(Action(kind=kind, text="x"), screen) is None


def test_missing_element_id_is_safe():
    screen = make_screen("Messages", ("Send", "button"))
    assert safety.needs_confirmation(Action(kind="tap", element_id=99), screen) is None


def test_friendly_app_name():
    assert safety.friendly_app_name("com.instagram.android") == "Instagram"
    assert safety.friendly_app_name("Safari") == "Safari"
    assert safety.friendly_app_name("") == "this app"


# ---- yes / no ---------------------------------------------------------------

@pytest.mark.parametrize("text", ["yes", "Yeah do it", "sure", "go ahead", "OK"])
def test_yes(text):
    assert safety.is_yes(text)
    assert not safety.is_no(text)


@pytest.mark.parametrize("text", ["no", "nope", "don't", "do not send it", "cancel", "wait"])
def test_no(text):
    assert safety.is_no(text)
    assert not safety.is_yes(text)


def test_unclear_reply_is_neither():
    assert not safety.is_yes("what?")
    assert not safety.is_no("what?")


def test_picking_a_recipient_is_not_a_send():
    from relay import safety
    from relay.types import Screen, Element, Action
    scr = Screen(device="phone", app="com.google.android.apps.messaging",
                 elements=[Element(0, "Send to (555) 123-4567", "button", (0, 0, 1, 1), True),
                           Element(1, "Send SMS", "button", (0, 0, 1, 1), True)])
    assert safety.needs_confirmation(Action(kind="tap", element_id=0), scr) is None
    assert safety.needs_confirmation(Action(kind="tap", element_id=1), scr) is not None


def test_follow_and_block_need_confirmation():
    from relay import safety
    from relay.types import Screen, Element, Action
    scr = Screen(device="phone", app="com.instagram.android",
                 elements=[Element(0, "Follow", "button", (0, 0, 1, 1), True),
                           Element(1, "Block", "button", (0, 0, 1, 1), True),
                           Element(2, "Following", "button", (0, 0, 1, 1), True)])
    assert safety.needs_confirmation(Action(kind="tap", element_id=0), scr)
    assert safety.needs_confirmation(Action(kind="tap", element_id=1), scr)


def test_linkedin_connect_needs_confirmation():
    from relay import safety
    from relay.types import Screen, Element, Action
    scr = Screen(device="phone", app="com.linkedin.android",
                 elements=[Element(0, "Connect", "button", (0, 0, 1, 1), True),
                           Element(1, "Send without a note", "button", (0, 0, 1, 1), True)])
    assert safety.needs_confirmation(Action(kind="tap", element_id=0), scr)
    assert safety.needs_confirmation(Action(kind="tap", element_id=1), scr)  # "send"


def test_auto_confirm_skips_send_but_not_pay(monkeypatch):
    from relay import safety
    from relay.types import Action, Element, Screen
    def scr(label):
        return Screen(device="phone", app="com.google.android.apps.messaging", elements=[Element(id=1, text=label, kind="button", bounds=(0, 0, 10, 10), clickable=True)])
    monkeypatch.setenv("AUTO_CONFIRM", "1")
    assert safety.needs_confirmation(Action(kind="tap", element_id=1), scr("Send SMS")) is None
    assert safety.needs_confirmation(Action(kind="tap", element_id=1), scr("Pay now")) is not None
    monkeypatch.setenv("AUTO_CONFIRM", "0")
    assert safety.needs_confirmation(Action(kind="tap", element_id=1), scr("Send SMS")) is not None
