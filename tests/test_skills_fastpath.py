"""Tests for the keyword fast path in skills (no LLM, no phone, no API key)."""
import logging

import pytest

from relay import config, skills

match = skills.match_keywords


# ---- media ----

@pytest.mark.parametrize("said,action", [
    ("pause", "pause"),
    ("pause the music", "pause"),
    ("stop the music", "pause"),
    ("play", "play"),
    ("resume", "play"),
    ("resume the music", "play"),
    ("next", "next"),
    ("next song", "next"),
    ("skip", "next"),
    ("skip this song", "next"),
    ("previous", "previous"),
    ("previous song", "previous"),
    ("go back a song", "previous"),
])
def test_media_commands(said, action):
    assert match(said) == ("media", {"action": action})


@pytest.mark.parametrize("said", [
    "PAUSE THE MUSIC",
    "Pause the music.",
    "hey relay, pause the music",
    "Hey Relay pause the music please",
    "please pause the music",
    "can you pause the music?",
    "  could you   pause the music  ",
    "okay relay, can you please pause the music",
])
def test_filler_and_case_are_ignored(said):
    assert match(said) == ("media", {"action": "pause"})


# ---- volume ----

@pytest.mark.parametrize("said,direction", [
    ("volume up", "up"),
    ("volume down", "down"),
    ("turn it up", "up"),
    ("turn it down", "down"),
    ("turn the volume up", "up"),
    ("turn the volume down", "down"),
    ("louder", "up"),
    ("quieter", "down"),
    ("mute", "mute"),
    ("Hey relay, turn it up!", "up"),
])
def test_volume_commands(said, direction):
    assert match(said) == ("volume", {"direction": direction})


# ---- open / close apps ----

@pytest.mark.parametrize("said,app", [
    ("open spotify", "spotify"),
    ("Open Spotify", "spotify"),
    ("launch instagram", "instagram"),
    ("open google maps", "google maps"),
    ("open the google maps app", "google maps"),
    ("open youtube music", "youtube music"),
    ("can you open settings please", "settings"),
    ("hey relay, open Spotify.", "spotify"),
])
def test_open_app(said, app):
    assert match(said) == ("open_app", {"app": app})


@pytest.mark.parametrize("said,app", [
    ("close instagram", "instagram"),
    ("quit spotify", "spotify"),
    ("exit youtube", "youtube"),
    ("close the instagram app", "instagram"),
    ("please close google maps", "google maps"),
])
def test_close_app(said, app):
    assert match(said) == ("close_app", {"app": app})


# ---- navigate ----

@pytest.mark.parametrize("said,dest", [
    ("navigate to Starbucks", "Starbucks"),
    ("take me to the nearest Starbucks", "the nearest Starbucks"),
    ("directions to 123 Main St.", "123 Main St"),
    ("drive to Target!", "Target"),
    ("hey relay, can you navigate to Whole Foods?", "Whole Foods"),
    ("NAVIGATE TO the airport", "the airport"),
])
def test_navigate(said, dest):
    assert match(said) == ("navigate", {"destination": dest})


# ---- things that must NOT match (the LLM router / screen agent handles them) ----

@pytest.mark.parametrize("said", [
    # multi-part requests
    "open spotify and play my workout playlist",
    "open instagram and message mom",
    "open instagram then check my dms",
    "close instagram and open spotify",
    "navigate to work and text mom I'm on my way",
    "take me to starbucks then call dad",
    "directions to the gym, and play music",
    # "play" with a specific thing is not the play button
    "play my liked songs on spotify",
    "play taylor swift",
    "play some music",
    "pause the music and turn it down",
    "skip two songs",
    "next episode of my podcast",
    # stop / cancel belong to the STOP handler, not to skills
    "stop",
    "cancel",
    "stop it",
    "cancel that",
    "never mind",
    # open/close with something that isn't just an app name
    "open my email",
    "open the chat with mom",
    "open spotify playlist workout mix",  # more than 3 words
    "open",
    "close",
    "exit navigation",
    "close all apps",
    # things that only sound similar
    "turn it up to eleven",
    "what's on my screen",
    "text mom that I'm running late",
    "",
    "   ",
    "hey relay",
])
def test_not_obvious_returns_none(said):
    assert match(said) is None


def test_returned_params_are_a_copy():
    """Changing a result must not change the next result."""
    first = match("pause")
    first[1]["action"] = "play"
    assert match("pause") == ("media", {"action": "pause"})


def test_logs_keyword_match(caplog):
    with caplog.at_level(logging.INFO, logger="relay.skills"):
        match("open spotify")
    assert "keyword match open_app {'app': 'spotify'}" in caplog.text


# ---- route() uses the fast path ----

def _boom():
    raise AssertionError("the LLM router should not be called")


def test_route_uses_keywords_without_calling_llm(monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr("relay.brains.claude._client", _boom)
    assert skills.route("hey relay, skip this song", "phone") == ("media", {"action": "next"})


def test_route_keywords_work_without_api_key(monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    assert skills.route("navigate to Starbucks", "phone") == ("navigate", {"destination": "Starbucks"})


def test_route_keywords_only_on_phone(monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    assert skills.route("pause the music", "mac") is None


def test_route_no_match_and_no_key_is_none(monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    assert skills.route("open spotify and play my workout playlist", "phone") is None


def test_route_falls_back_to_llm_when_no_match(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "k")
    calls = []
    block = SimpleNamespace(type="tool_use", name="media", input={"action": "play", "app": "spotify"})

    def create(**kw):
        calls.append(kw)
        return SimpleNamespace(content=[block])

    monkeypatch.setattr("relay.brains.claude._client",
                        lambda: SimpleNamespace(messages=SimpleNamespace(create=create)))
    assert skills.route("play my liked songs on spotify", "phone") == ("media", {"action": "play", "app": "spotify"})
    assert len(calls) == 1
