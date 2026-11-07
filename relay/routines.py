"""Saved routines: replay a task that already worked once, with no AI calls.

When the agent finishes a task successfully it calls save(). Next time the
user says the same thing, find() returns the routine and replay() taps the
same buttons again.

Key idea: element ids change every time the screen is read (they are just
list positions), so we store the TEXT of each element we touched
("Send", "Search", "Mom") and look it up again on the new screen.

File format (config.ROUTINES_PATH), a JSON list:
[
  {
    "goal": "text mom I'm running late",
    "device": "phone",
    "steps": [
      {"app": "com.google.android.apps.messaging", "kind": "tap",
       "text": null, "direction": null, "target_text": "Mom"},
      ...
    ]
  }
]
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from relay import config, safety
from relay.types import Action, Element, Screen

# Seconds to wait after each replayed action so the screen can update.
# Tests set this to 0.
STEP_DELAY = 0.8
# How many times to re-read the screen when the target isn't there yet
# (apps can be slow to load).
MATCH_TRIES = 3

# These actions are not "real" device steps, so they are never saved.
_SKIP_KINDS = {"ask_user", "done"}
# These actions need an element on screen; the rest (back, home, scroll,
# open_app) do not.
_NEEDS_ELEMENT = {"tap", "type"}
# Jaccard similarity needed for a "close enough" goal match.
MIN_SIMILARITY = 0.8


@dataclass
class Routine:
    goal: str
    device: str
    steps: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------

def _path() -> str:
    # Read config at call time so tests can point it at a temp file.
    return config.ROUTINES_PATH


def _load_all() -> list[Routine]:
    """Read every routine from disk. Missing or broken file -> empty list."""
    try:
        with open(_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    routines = []
    for item in data if isinstance(data, list) else []:
        try:
            routines.append(Routine(goal=item["goal"], device=item["device"],
                                    steps=list(item.get("steps", []))))
        except (KeyError, TypeError):
            continue  # skip a malformed entry instead of crashing
    return routines


def _write_all(routines: list[Routine]) -> None:
    """Write to a temp file, then rename, so a crash never leaves half a file."""
    path = _path()
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump([asdict(r) for r in routines], f, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# find()
# ---------------------------------------------------------------------------

def normalize(goal: str) -> str:
    """"Open  YouTube!" -> "open youtube" (lowercase, no punctuation, single spaces)."""
    cleaned = re.sub(r"[^a-z0-9' ]+", " ", (goal or "").lower())
    return " ".join(cleaned.split())


def _jaccard(a: str, b: str) -> float:
    """Share of words two goals have in common: |A and B| / |A or B|."""
    sa, sb = set(a.split()), set(b.split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


# Words that don't change what a request means.
FILLER_WORDS = {"please", "the", "a", "an", "my", "can", "could", "you", "for", "me",
                "hey", "relay", "okay", "ok", "now", "just", "go", "ahead"}


def find(goal: str) -> Routine | None:
    """Return the saved routine for this goal, or None.

    1. Exact match after normalize().
    2. Otherwise a goal whose ONLY different words are filler ("please", "the",
       "can you"...). Word overlap alone is dangerous: "turn ON dark mode" and
       "turn OFF dark mode" share almost every word but mean the opposite.
    """
    wanted = normalize(goal)
    if not wanted:
        return None
    routines = _load_all()

    for r in routines:
        if normalize(r.goal) == wanted:
            return r

    wanted_words = set(wanted.split())
    for r in routines:
        different = wanted_words ^ set(normalize(r.goal).split())
        if different and different <= FILLER_WORDS:
            return r
    return None


# ---------------------------------------------------------------------------
# save()
# ---------------------------------------------------------------------------

def _element(screen: Screen, element_id: int | None) -> Element | None:
    return safety.find_element(screen, element_id)


def save(goal: str, device: str, steps: list[tuple[Screen, Action]]) -> None:
    """Store a task that worked. Each step is (screen it was taken on, action).

    The routine is NOT saved (silently) if:
      - something was typed into a password / card / SSN field, or a card
        number was typed anywhere (we never write those to disk), or
      - a tap/type step's element has no text (we could not find it again), or
      - there are no real steps left after skipping ask_user/done.
    """
    saved_steps: list[dict[str, Any]] = []
    for screen, action in steps:
        if action.kind in _SKIP_KINDS:
            continue

        target_text = None
        if action.kind in _NEEDS_ELEMENT:
            el = _element(screen, action.element_id)
            if el is None or not el.text.strip():
                return  # can't be replayed reliably -> don't save
            target_text = el.text

        if action.kind == "type":
            # Re-use the safety check: anything it blocks must never be stored.
            check = safety.needs_confirmation(action, screen)
            if check and check.startswith(safety.BLOCKED_PREFIX):
                return

        saved_steps.append({
            "app": screen.app,
            "kind": action.kind,
            "text": action.text,
            "direction": action.direction,
            "target_text": target_text,
        })

    if not saved_steps:
        return

    # Replace an older routine with the same goal on the same device.
    wanted = normalize(goal)
    routines = [r for r in _load_all()
                if not (normalize(r.goal) == wanted and r.device == device)]
    routines.append(Routine(goal=goal, device=device, steps=saved_steps))
    _write_all(routines)


# ---------------------------------------------------------------------------
# replay()
# ---------------------------------------------------------------------------

def match_element(screen: Screen, target_text: str) -> Element | None:
    """Find the element whose text matches target_text.

    1. Exact text match (prefer clickable elements).
    2. Case-insensitive "contains" match (prefer clickable, then shortest text,
       so "Send" picks the "Send" button over a long paragraph mentioning send).
    """
    if not target_text:
        return None
    exact = [e for e in screen.elements if e.text == target_text]
    if exact:
        exact.sort(key=lambda e: not e.clickable)  # clickable first
        return exact[0]

    needle = target_text.lower()
    contains = [e for e in screen.elements if needle in (e.text or "").lower()]
    if contains:
        contains.sort(key=lambda e: (not e.clickable, len(e.text)))
        return contains[0]
    return None


def replay(routine: Routine, device) -> bool:
    """Run a saved routine on a device. True = every step ran.

    Returns False (and stops) as soon as:
      - the routine was saved for a different device,
      - a step's element can't be found on the new screen,
      - a step needs the user's confirmation (send / pay / delete ...) or is
        blocked; the agent loop then takes over and asks the user live.
    """
    if routine.device != getattr(device, "name", routine.device):
        return False

    for step in routine.steps:
        kind = step.get("kind")
        target_text = step.get("target_text")

        screen = device.get_screen()
        element_id = None

        if kind in _NEEDS_ELEMENT:
            el = match_element(screen, target_text)
            tries = 1
            while el is None and tries < MATCH_TRIES:
                time.sleep(STEP_DELAY)       # maybe the app is still loading
                screen = device.get_screen()
                el = match_element(screen, target_text)
                tries += 1
            if el is None:
                return False
            element_id = el.id

        action = Action(
            kind=kind,
            element_id=element_id,
            text=step.get("text"),
            direction=step.get("direction"),
            confidence=1.0,
            source="routine",
            reason=f"replaying routine: {routine.goal}",
        )

        # Safety still applies to replays: never auto-send / auto-pay.
        if safety.needs_confirmation(action, screen) is not None:
            return False

        device.act(action, screen)
        time.sleep(STEP_DELAY)

    return True
