"""Safety checks for Relay.

Pure Python, no network, no AI calls. These run on EVERY command and EVERY
action, so they have to be fast and predictable.

What lives here:
    is_emergency(utterance)          -> True if the user needs 911 right now
    is_stop(utterance)               -> True if the user wants to cancel the task
    needs_confirmation(action, screen) -> a spoken question, a "BLOCKED: ..." refusal, or None
    is_yes(reply) / is_no(reply)     -> read a spoken answer to a confirmation question
"""
from __future__ import annotations

import re

from relay.types import Action, Element, Screen

# Every refusal starts with this, so the agent loop can check
# `question.startswith(BLOCKED_PREFIX)` and stop the task.
BLOCKED_PREFIX = "BLOCKED:"


# ---------------------------------------------------------------------------
# Small text helpers
# ---------------------------------------------------------------------------

def _words(text: str) -> list[str]:
    """Lowercase words and punctuation marks, e.g. "Stop, now!" -> ["stop", ",", "now", "!"].

    We keep . , ! ? as their own tokens because "stop." or "stop," means the
    user really said "stop" as a command and then paused.
    """
    return re.findall(r"[a-z0-9']+|[.,!?;]", (text or "").lower())


def _has(pattern: str, text: str) -> bool:
    """Case-insensitive regex search. Patterns use \\b for word boundaries."""
    return re.search(pattern, text or "", flags=re.IGNORECASE) is not None


# ---------------------------------------------------------------------------
# Emergencies
# ---------------------------------------------------------------------------

# Each pattern alone is enough to call it an emergency.
_EMERGENCY_PATTERNS = [
    r"\b911\b",
    r"\b9[\s-]1[\s-]1\b",                    # "9 1 1" from speech-to-text
    r"\bnine[\s-]+one[\s-]+one\b",
    r"\bemergency\b",
    r"\bcall\s+(?:an?\s+|the\s+)?ambulance\b",
    r"\bcall\s+(?:the\s+)?(?:police|cops)\b",
    r"\bcall\s+(?:the\s+)?fire\s+department\b",
]

# "I need help" only counts when it comes with a word about being hurt,
# so "I need help finding my keys" does NOT dial 911.
_HELP_PATTERN = r"\b(?:i\s+need\s+help|help\s+me)\b"
_HURT_PATTERN = (
    r"\b(?:hurt|hurting|injured|injury|bleeding|blood|fell|fallen|fall|pain|"
    r"chest|heart\s+attack|stroke|breathe|breathing|choking|dying|"
    r"unconscious|seizure|overdose|broke|broken)\b"
)


def is_emergency(utterance: str) -> bool:
    """True if the user is asking for emergency help (dial 911, no AI)."""
    text = utterance or ""
    if any(_has(p, text) for p in _EMERGENCY_PATTERNS):
        return True
    return _has(_HELP_PATTERN, text) and _has(_HURT_PATTERN, text)


# ---------------------------------------------------------------------------
# Stop / cancel
# ---------------------------------------------------------------------------

_STOP_WORDS = {"stop", "cancel", "quit", "halt", "nevermind", "abort"}

# Words people say BEFORE the command: "okay, please just stop".
_LEADING_FILLER = {"ok", "okay", "hey", "relay", "please", "just", "no", "wait",
                   "um", "uh", "oh", "actually", ",", ".", "!"}

# Words that can come AFTER the stop word and still mean "stop the task":
# "stop it", "cancel that", "stop now please". Anything else ("stop at the
# store", "cancel my order") means it is part of a real goal.
_TRAILING_OK = {"it", "that", "this", "now", "please", "everything", "all",
                "right", "relay", "the", "task", "immediately", "already",
                "thanks", "thank", "you", "mind", ".", ",", "!", "?", ";"}


def _starts_with_stop(words: list[str]) -> int:
    """How many words the stop phrase at the start uses (0 = no stop phrase)."""
    if not words:
        return 0
    if words[0] in _STOP_WORDS:
        return 1
    if words[:2] == ["never", "mind"]:
        return 2
    return 0


def is_stop(utterance: str) -> bool:
    """True if the MAIN intent of the utterance is "stop the current task".

    Examples:
        "stop"                      -> True
        "okay, cancel that"         -> True
        "never mind"                -> True
        "stop. I changed my mind"   -> True   (punctuation right after stop)
        "stop at the store"         -> False  (it's a goal, not a command)
        "cancel my uber order"      -> False  (a goal that uses the word cancel)
    """
    words = _words(utterance)

    # Drop "okay", "please", "just" ... from the front.
    while words and words[0] in _LEADING_FILLER:
        words = words[1:]

    used = _starts_with_stop(words)
    if used:
        rest = words[used:]
        if not rest:
            return True                       # just "stop"
        if rest[0] in {".", ",", "!", "?", ";"}:
            return True                       # "stop. ..." / "stop, ..."
        if all(w in _TRAILING_OK for w in rest):
            return True                       # "stop it now please"
        return False

    # Short exclamations that END with the stop word: "I said stop",
    # "oh god cancel". Only if every earlier word is a filler word, so
    # "find bus stop" is still a goal.
    real_words = [w for w in words if w[0].isalnum()]
    exclaim = {"i", "said", "oh", "god", "ugh", "come", "on", "can", "you", "please"}
    if 0 < len(real_words) <= 4 and real_words[-1] in _STOP_WORDS:
        return all(w in exclaim for w in real_words[:-1])
    return False


# ---------------------------------------------------------------------------
# Confirmation before risky actions
# ---------------------------------------------------------------------------

# Tapping a button with one of these words does something you can't undo.
_RISKY_TAP = (
    r"\b(?:send|post|pay|buy|place\s+order|order\s+now|checkout|check\s+out|"
    r"confirm|delete|remove|call|transfer|submit|share|purchase|book\s+now)\b"
)

# Fields we must NEVER type into for the user.
_SENSITIVE_FIELD = (
    r"\b(?:password|passcode|passwd|pin|card\s*number|credit\s*card|debit\s*card|"
    r"cvv|cvc|cvv2|security\s*code|ssn|social\s*security|routing\s*number|"
    r"account\s*number)\b"
)

# Typed text that looks like a card number (13-19 digits, spaces/dashes allowed)
# or a US SSN (123-45-6789). Blocks it even if the field label is unhelpful.
_CARD_NUMBER = r"^\s*(?:\d[\s-]?){12,18}\d\s*$"
_SSN_NUMBER = r"^\s*\d{3}-\d{2}-\d{4}\s*$"


def friendly_app_name(app: str) -> str:
    """Turn a package like "com.instagram.android" into "Instagram"."""
    if not app:
        return "this app"
    if "." not in app:
        return app
    boring = {"com", "org", "net", "android", "app", "apps", "google", "mobile", "ios"}
    parts = [p for p in app.split(".") if p.lower() not in boring]
    return parts[-1].capitalize() if parts else app


def find_element(screen: Screen, element_id: int | None) -> Element | None:
    """Look up an element by its id in this screen snapshot."""
    if element_id is None:
        return None
    for e in screen.elements:
        if e.id == element_id:
            return e
    return None


def is_sensitive_field(element: Element | None) -> bool:
    """True if this element looks like a password / card / SSN field."""
    return element is not None and _has(_SENSITIVE_FIELD, element.text)


def needs_confirmation(action: Action, screen: Screen) -> str | None:
    """Check an action BEFORE the device runs it.

    Returns:
        None                     -> safe, go ahead
        "About to tap ... ?"     -> ask the user this question first
        "BLOCKED: ..."           -> never do this; the agent must stop the task
    """
    element = find_element(screen, action.element_id)
    app = friendly_app_name(screen.app)

    if action.kind == "type":
        typed = action.text or ""
        if is_sensitive_field(element) or _has(_CARD_NUMBER, typed) or _has(_SSN_NUMBER, typed):
            return (f"{BLOCKED_PREFIX} That looks like a password, card number or other "
                    f"private field in {app}. I won't type it for you. "
                    "Please enter it yourself, then tell me to continue.")
        return None

    # "Send to (555) 123-4567" in a contact list just PICKS the recipient; nothing is sent.
    if action.kind == "tap" and element is not None and re.match(r"\s*send\s+to\b", element.text, re.I):
        return None
    if action.kind == "tap" and element is not None and _has(_RISKY_TAP, element.text):
        label = element.text.strip()
        return f"About to tap {label} in {app}. Should I do it?"

    return None


# ---------------------------------------------------------------------------
# Yes / no answers to the confirmation question
# ---------------------------------------------------------------------------

_YES = {"yes", "yeah", "yep", "yup", "ya", "sure", "ok", "okay", "confirm",
        "correct", "affirmative", "absolutely", "definitely", "proceed"}
_YES_PHRASES = ["do it", "go ahead", "go for it", "please do", "that's right", "send it"]

_NO = {"no", "nope", "nah", "don't", "dont", "cancel", "stop", "wait", "negative", "abort"}
_NO_PHRASES = ["do not", "never mind", "not now", "hold on"]


def is_no(reply: str) -> bool:
    """True if the spoken reply means no. Checked before yes, because
    "okay, don't" should be a no."""
    words = _words(reply)
    joined = " ".join(words)
    return any(w in _NO for w in words) or any(p in joined for p in _NO_PHRASES)


def is_yes(reply: str) -> bool:
    """True if the spoken reply clearly means yes (and has no "no" in it)."""
    if is_no(reply):
        return False
    words = _words(reply)
    joined = " ".join(words)
    return any(w in _YES for w in words) or any(p in joined for p in _YES_PHRASES)
