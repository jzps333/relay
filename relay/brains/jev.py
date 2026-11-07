"""Jev brain: fast, typed action picking with calibrated confidence.

Jev (TypeSafe AI) does not write free text. Instead you send it some `state` (context) and a
set of typed `questions`, and it answers each one with probabilities:
  - "choice": pick one label from `criteria` -> {"choice", "probabilities", "confidence"}
  - "noul":   yes/no                          -> {"noul": P(yes)}
We ask everything in ONE request (fast): which action kind, which element, which scroll
direction, and (only if we have candidates) which text to type.

API shape (OpenRouter Decisions API, POST https://openrouter.ai/api/alpha/decisions):
    request  {"model": ..., "state": {...}, "questions": {name: {"type", "instructions", "criteria"}}}
    response {"answers": {name: {"type": "choice", "choice": "...", "probabilities": {...},
              "confidence": 0.75}}, "usage": {...}}
Errors come back as {"error": {"code", "message"}} with a 4xx/5xx status.
"""
from __future__ import annotations

import os
import re

import httpx

from relay import config
from relay.types import Action, Screen

# Default endpoint if JEV_BASE_URL is empty in .env (OpenRouter hosts Jev's Decisions API).
DEFAULT_URL = "https://openrouter.ai/api/alpha/decisions"
# Set JEV_MODEL in .env to change it ("~typesafe/jev-latest" also works).
JEV_MODEL = config.JEV_MODEL
TIMEOUT_S = 8.0

# What each action kind means, written as Jev "criteria" (label -> guidance).
KIND_CRITERIA = {
    "tap": "Tap one clickable element on screen to make progress.",
    "type": "Type text into an input field that is on screen.",
    "scroll": "The needed item is probably off screen; scroll up or down.",
    "back": "Go back; this screen is wrong or a dead end.",
    "home": "Go to the home screen to start over.",
    "open_app": "Open a different app by name.",
    "done": "The goal is already complete on this screen.",
    "ask_user": "Unclear what to do, or a password/card number is needed; ask the user.",
}

# Jev cannot write sentences, so these kinds are handed to Claude (via JevError).
NEEDS_FREE_TEXT = {"open_app", "done", "ask_user"}

NO_FIT = "none_of_these"  # extra option for the text question


class JevError(Exception):
    """Anything that goes wrong with Jev. The picker catches this and asks Claude instead."""


def _call_jev(payload: dict) -> dict:
    """Send one Decisions request and return the parsed JSON. ALL HTTP lives here.

    TODO(verify live): shape follows the OpenRouter Decisions API reference. If you call
    TypeSafe directly (api.typesafe.ai/v1/systemone) the body may differ slightly.
    """
    if not config.JEV_API_KEY:
        raise JevError("JEV_API_KEY is not set")
    url = config.JEV_BASE_URL or DEFAULT_URL
    try:
        r = httpx.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {config.JEV_API_KEY}"},
            timeout=TIMEOUT_S,
        )
    except httpx.HTTPError as e:  # timeout, DNS, connection refused...
        raise JevError(f"network error: {e}") from e
    if r.status_code != 200:
        raise JevError(f"HTTP {r.status_code}: {r.text[:200]}")
    try:
        data = r.json()
    except ValueError as e:
        raise JevError("response was not JSON") from e
    if "error" in data:
        raise JevError(f"API error: {data['error']}")
    if not isinstance(data.get("answers"), dict):
        raise JevError("response has no 'answers'")
    return data


def _text_candidates(goal: str, subgoal: str) -> list[str]:
    """Short strings the user might want typed, pulled from the goal.

    Jev can only choose, not write, so we give it options:
      - anything in quotes:      search "pizza near me"  -> pizza near me
      - text after a trigger:    text mom saying I'm late -> I'm late
    """
    found: list[str] = []
    for source in (subgoal, goal):
        found += re.findall(r"[\"“']([^\"”']{1,80})[\"”']", source)
        m = re.search(r"\b(?:type|search for|search|saying|say|that)\s+(.+)$", source, re.I)
        if m:
            found.append(m.group(1).strip(" .!?\"'“”"))
    # Remove duplicates but keep order.
    unique: list[str] = []
    for f in found:
        if f and f not in unique:
            unique.append(f)
    return unique[:10]


def build_payload(goal: str, subgoal: str, screen: Screen, history: list[Action]) -> tuple[dict, dict, list[str]]:
    """Build the Jev request. Returns (payload, element_labels, text_candidates).

    element_labels maps a Jev label like "e3" back to element id 3.
    """
    recent = [f"{a.kind} {a.element_id if a.element_id is not None else ''} {a.text or ''}".strip()
              for a in history[-5:]]
    state = {
        "goal": goal,
        "current_subgoal": subgoal,
        "screen": screen.as_prompt(),
        "recent_actions": recent or ["(none)"],
    }
    questions: dict = {
        "kind": {
            "type": "choice",
            "instructions": "What is the single best next action to reach the current sub-goal?",
            "criteria": KIND_CRITERIA,
        },
        "direction": {
            "type": "choice",
            "instructions": "If scrolling, which way is the needed item most likely?",
            "criteria": {"down": "Further down the list or page.", "up": "Back up toward the top."},
        },
    }

    # Only clickable elements and inputs are worth choosing (Jev allows up to 255 options).
    targets = [e for e in screen.elements if e.clickable or e.kind == "input"][:250]
    element_labels = {f"e{e.id}": e.id for e in targets}
    if targets:
        questions["element"] = {
            "type": "choice",
            "instructions": "Which element should be tapped or typed into for the next action?",
            "criteria": {f"e{e.id}": f"{e.kind}: {e.text or '(no label)'}" for e in targets},
        }

    candidates = _text_candidates(goal, subgoal)
    if candidates:
        crit = {f"t{i}": c for i, c in enumerate(candidates)}
        crit[NO_FIT] = "None of these is the right text to type."
        questions["text"] = {
            "type": "choice",
            "instructions": "If typing, which exact text should be typed?",
            "criteria": crit,
        }

    payload = {"model": JEV_MODEL, "state": state, "questions": questions}
    return payload, element_labels, candidates


def _prob(answer: dict) -> float:
    """Calibrated probability of the option Jev chose.

    We prefer probabilities[choice] (Jev's calibrated P of that option). If it is missing we
    use the 'confidence' field, and if both are missing we say 0 so the picker falls back.
    """
    probs = answer.get("probabilities") or {}
    choice = answer.get("choice")
    if choice in probs:
        return float(probs[choice])
    if "confidence" in answer:
        return float(answer["confidence"])
    return 0.0


def _answer(answers: dict, name: str) -> dict:
    a = answers.get(name)
    if not isinstance(a, dict) or a.get("type") == "refusal" or "choice" not in a:
        raise JevError(f"no usable answer for '{name}'")
    return a


def parse_answers(data: dict, element_labels: dict, candidates: list[str]) -> Action:
    """Turn Jev's answers into an Action, multiplying the probabilities we relied on.

    confidence = P(kind) * P(element) * P(text)   (only the parts this kind needs)
    Multiplying means "how likely is it that every part is right" - one weak part drags it down.
    """
    answers = data["answers"]
    kind_ans = _answer(answers, "kind")
    kind = kind_ans["choice"]
    if kind not in KIND_CRITERIA:
        raise JevError(f"unknown kind {kind!r}")
    if kind in NEEDS_FREE_TEXT:
        raise JevError(f"'{kind}' needs a written sentence; Jev only chooses")

    confidence = _prob(kind_ans)
    action = Action(kind=kind, source="jev")

    if kind in ("tap", "type"):
        el = _answer(answers, "element")
        if el["choice"] not in element_labels:
            raise JevError(f"unknown element label {el['choice']!r}")
        action.element_id = element_labels[el["choice"]]
        confidence *= _prob(el)

    if kind == "type":
        if not candidates or "text" not in answers:
            raise JevError("no candidate text to type")
        tx = _answer(answers, "text")
        if tx["choice"] == NO_FIT or not tx["choice"].startswith("t"):
            raise JevError("Jev says none of the candidate texts fit")
        action.text = candidates[int(tx["choice"][1:])]
        confidence *= _prob(tx)

    if kind == "scroll":
        d = _answer(answers, "direction")
        action.direction = d["choice"] if d["choice"] in ("up", "down") else "down"
        confidence *= _prob(d)

    action.confidence = round(max(0.0, min(1.0, confidence)), 3)
    action.reason = f"jev picked {kind} (p={action.confidence})"
    return action


def pick_action(goal: str, subgoal: str, screen: Screen, history: list[Action]) -> Action:
    """Ask Jev for the next action. Raises JevError on any problem."""
    payload, element_labels, candidates = build_payload(goal, subgoal, screen, history)
    data = _call_jev(payload)
    try:
        return parse_answers(data, element_labels, candidates)
    except JevError:
        raise
    except (KeyError, ValueError, TypeError, IndexError) as e:  # malformed answer
        raise JevError(f"could not parse Jev answer: {e}") from e
