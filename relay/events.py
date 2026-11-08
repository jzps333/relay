"""Event bus for the live dashboard (GET /dashboard).

Anywhere in Relay can call

    events.publish("step", text="Tapping Send")

and every open dashboard shows it a moment later. Events live only in memory:
a "ring buffer" keeps the newest MAX_EVENTS and quietly forgets older ones.

Each event is a small dict:  {"id": 7, "ts": 1731090000.12, "kind": "step", "text": "..."}
  id   counts up from 1, so a dashboard can ask "everything after id 6"
  ts   time.time() when it happened

Two promises:
  * publish() NEVER raises. The dashboard is a nice-to-have; it must never break Relay.
  * Payloads stay small and never hold secrets: keys like "token" or "pin" are dropped,
    raw bytes (screenshots) are dropped, and long strings are cut short.

Threads: tasks run in background threads and the web server reads from others,
so one threading.Condition guards the buffer. wait_for_new() blocks a thread
until something is published (the server calls it through run_in_threadpool so
it never blocks the async event loop).
"""
from __future__ import annotations

import threading
import time
from collections import deque

MAX_EVENTS = 500       # how many events we remember
MAX_TEXT = 300         # longest string we keep in an event (characters)

# Never put these in an event, whatever the caller passes.
_SECRET_KEYS = {"token", "pin", "password", "passphrase", "authorization", "screenshot",
                "screenshot_png", "api_key", "secret"}

_cond = threading.Condition()            # guards _buffer and _next_id, and wakes waiters
_buffer: deque[dict] = deque(maxlen=MAX_EVENTS)
_next_id = 1


def _small(value):
    """Make one value safe and small for JSON (None / bool / number / short string)."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (bytes, bytearray)):
        return None  # screenshots and other binary blobs never go to the dashboard
    text = value if isinstance(value, str) else str(value)
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT - 1] + "…"


def publish(kind: str, /, **data) -> int | None:
    """Record an event and wake every dashboard. Returns its id (None if something failed)."""
    global _next_id
    try:
        event = {}
        for key, value in data.items():
            if key.lower() in _SECRET_KEYS:
                continue
            try:
                small = _small(value)
            except Exception:
                continue  # a weird object whose str() fails: just leave it out
            if small is not None:
                event[key] = small
        with _cond:
            event.update(id=_next_id, ts=round(time.time(), 3), kind=str(kind))
            _next_id += 1
            _buffer.append(event)
            _cond.notify_all()  # wake every waiting SSE stream
        return event["id"]
    except Exception:
        return None  # never let the dashboard break Relay


def newest_id() -> int:
    """Id of the newest event (0 if nothing has happened yet)."""
    with _cond:
        return _buffer[-1]["id"] if _buffer else 0


def since(last_id: int) -> list[dict]:
    """Every remembered event with id > last_id, oldest first.

    If last_id is NEWER than anything we have, the server must have restarted
    (ids start again at 1), so we send everything instead of nothing.
    """
    with _cond:
        if _buffer and last_id > _buffer[-1]["id"]:
            last_id = 0
        return [dict(e) for e in _buffer if e["id"] > last_id]


def recent(limit: int = 100) -> list[dict]:
    """The newest `limit` events, oldest first (for a dashboard's first load)."""
    with _cond:
        return [dict(e) for e in list(_buffer)[-limit:]] if limit > 0 else []


def wait_for_new(last_id: int, timeout: float) -> list[dict]:
    """Block (up to `timeout` seconds) until there is an event after last_id, then return them.

    Returns [] on timeout. This blocks a THREAD, so async code must call it via
    run_in_threadpool.
    """
    with _cond:
        _cond.wait_for(lambda: bool(_buffer) and _buffer[-1]["id"] != last_id, timeout)
    return since(last_id)


def clear() -> None:
    """Forget everything and restart ids at 1 (used by tests)."""
    global _next_id
    with _cond:
        _buffer.clear()
        _next_id = 1
        _cond.notify_all()
