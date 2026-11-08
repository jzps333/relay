"""Short-term memory of the last summary Relay spoke.

After "summarize my emails" the user often follows up with
"reply to the second one saying I'll be late" or "read me the email from
Professor Lee". The screen agent can't know what "the second one" is unless we
tell it, so the summary handlers call remember(...) and the agent loop calls
add_to_goal(...) to put the summary in front of the next request.

It is one module-level slot (one user per server), guarded by a lock because
tasks run in background threads. It expires after 15 minutes so an old
summary doesn't confuse a request an hour later.
"""
from __future__ import annotations

import threading
import time

MAX_AGE_SECONDS = 15 * 60   # forget the summary after 15 minutes
MAX_LINES = 300             # raw lines kept (what was read off the screen)
MAX_GOAL_CHARS = 1200       # how much of the summary goes into the agent's goal
GOAL_PREFIX = "Context from what you just told the user (they may refer to it): "

_lock = threading.Lock()
_last: dict | None = None   # {"summary", "lines", "source", "time"}


def remember(summary: str, lines: list[str] | None = None, source: str = "") -> None:
    """Save the summary we just spoke (and the raw lines it came from)."""
    global _last
    summary = (summary or "").strip()
    if not summary:
        return
    with _lock:
        _last = {
            "summary": summary,
            "lines": list(lines or [])[:MAX_LINES],
            "source": source,          # "email", "notifications" or "news"
            "time": time.time(),
        }


def get(now: float | None = None) -> dict | None:
    """The saved context if it's still fresh, else None. `now` is for tests."""
    now = time.time() if now is None else now
    with _lock:
        if _last is None or now - _last["time"] > MAX_AGE_SECONDS:
            return None
        return dict(_last)  # a copy, so callers can't change ours


def clear() -> None:
    global _last
    with _lock:
        _last = None


def add_to_goal(goal: str, now: float | None = None) -> str:
    """Put the fresh summary (not the raw lines) in front of the goal.

    No fresh context -> the goal comes back unchanged.
    """
    ctx = get(now)
    if ctx is None:
        return goal
    summary = ctx["summary"]
    if len(summary) > MAX_GOAL_CHARS:
        summary = summary[:MAX_GOAL_CHARS].rstrip() + "..."
    return f"{GOAL_PREFIX}{summary}\n\n{goal}"
