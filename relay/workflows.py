"""Workflows: one saying runs several steps.

"Give me my morning outlook" -> macro news from one group of X accounts, then
company news from another. You define them in workflows.json (copy
workflows.example.json to start). The future Relay app will edit this same file.

    {
      "account_groups": {"macro": ["DeItaone", ...], "micro": [...]},
      "workflows": [
        {"name": "morning outlook",
         "phrases": ["give me my morning outlook", "morning outlook"],
         "steps": [{"skill": "news_briefing", "intro": "The big picture.",
                    "params": {"group": "macro", "topic": "..."}}, ...]}
      ]
    }
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field

from relay import config

log = logging.getLogger("relay.workflows")

EXAMPLE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "workflows.example.json")


@dataclass
class Step:
    skill: str
    params: dict = field(default_factory=dict)
    intro: str = ""


@dataclass
class Workflow:
    name: str
    phrases: list[str]
    steps: list[Step]


def _load() -> dict:
    """Read workflows.json, or the example file if you haven't made your own yet."""
    for path in (config.WORKFLOWS_PATH, EXAMPLE_PATH):
        try:
            with open(path) as f:
                return json.load(f)
        except FileNotFoundError:
            continue
        except json.JSONDecodeError as e:
            log.warning("%s has a typo (%s); ignoring it", path, e)
            continue
    return {}


def account_groups() -> dict[str, list[str]]:
    groups = _load().get("account_groups", {})
    return {name: [h.strip().lstrip("@") for h in handles if h.strip()] for name, handles in groups.items()}


def all_workflows() -> list[Workflow]:
    out = []
    for w in _load().get("workflows", []):
        steps = [Step(skill=s["skill"], params=dict(s.get("params", {})), intro=s.get("intro", ""))
                 for s in w.get("steps", []) if s.get("skill")]
        if steps and w.get("phrases"):
            out.append(Workflow(name=w.get("name", w["phrases"][0]), phrases=list(w["phrases"]), steps=steps))
    return out


def _norm(text: str) -> str:
    from relay.skills import _clean  # same filler-stripping as the keyword commands
    return " ".join(_clean(text).lower().replace("’", "'").split())


def match(goal: str) -> Workflow | None:
    """The workflow whose phrase is exactly what was said (ignoring filler like 'hey relay')."""
    said = _norm(goal)
    for w in all_workflows():
        if any(_norm(p) == said for p in w.phrases):
            log.info("workflow match %r", w.name)
            return w
    return None


def resolve_accounts(params: dict) -> list[str]:
    """news_briefing params -> the list of X handles to read."""
    if params.get("accounts"):
        return [a.lstrip("@") for a in params["accounts"]]
    group = params.get("group")
    if group:
        handles = account_groups().get(group)
        if handles:
            return handles
        log.warning("account group %r isn't in workflows.json", group)
    return list(config.NEWS_ACCOUNTS)
