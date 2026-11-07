"""Picker: Jev first (fast, cheap, calibrated), Claude when Jev is unsure or cannot answer."""
from __future__ import annotations

import logging
import time

from relay import config
from relay.brains import claude, jev
from relay.types import Action, Screen

log = logging.getLogger("relay.picker")

# Typing more than this many characters means real writing (a message), which Claude does better.
MAX_JEV_TEXT = 20


def _ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


def _jev_ok(action: Action) -> tuple[bool, str]:
    """Return (use_it, reason_if_not)."""
    if action.confidence < config.JEV_MIN_CONFIDENCE:
        return False, f"confidence {action.confidence:.2f} < {config.JEV_MIN_CONFIDENCE}"
    if action.kind == "type" and len(action.text or "") > MAX_JEV_TEXT:
        return False, "free text too long for Jev"
    return True, ""


def choose_action(goal: str, subgoal: str, screen: Screen, history: list[Action]) -> Action:
    # 1) Try Jev, unless there is no key at all.
    if config.JEV_API_KEY:
        start = time.perf_counter()
        try:
            action = jev.pick_action(goal, subgoal, screen, history)
            ok, why = _jev_ok(action)
            if ok:
                log.info("brain=jev kind=%s conf=%.2f ms=%d", action.kind, action.confidence, _ms(start))
                return action
            log.info("jev rejected (%s) ms=%d -> claude", why, _ms(start))
        except jev.JevError as e:
            log.info("jev error (%s) ms=%d -> claude", e, _ms(start))
    else:
        log.info("no JEV_API_KEY -> claude")

    # 2) Claude fallback.
    start = time.perf_counter()
    action = claude.pick_action(goal, subgoal, screen, history)
    log.info("brain=claude kind=%s ms=%d", action.kind, _ms(start))
    return action
