"""Shared test setup."""
import pytest

from relay import context, events, server


@pytest.fixture(autouse=True)
def _fresh_memory(monkeypatch):
    """Relay keeps a little module-level memory (last summary, last answer).
    Start every test with it empty so tests can't affect each other."""
    context.clear()
    events.clear()  # the live-dashboard event bus
    monkeypatch.setattr(server, "_last_answer", None)
    yield
    context.clear()
    events.clear()
