"""Tests for skills, the passphrase, and unlocking. No phone or API keys needed."""
from types import SimpleNamespace

import pytest

from relay import agent, config, skills


class FakePhone:
    name = "phone"

    def __init__(self, locked=False):
        self.calls = []
        self.locked = locked

    def key(self, name):
        self.calls.append(("key", name))

    def start_app(self, app):
        self.calls.append(("start", app))

    def close_app(self, app):
        self.calls.append(("close", app))

    def open_uri(self, uri, package=None):
        self.calls.append(("uri", uri, package))

    def is_locked(self):
        return self.locked

    def unlock(self, pin=""):
        self.calls.append(("unlock", pin))
        self.locked = False
        return True


# ---- skills.run ----

def test_navigate_opens_maps_intent():
    phone = FakePhone()
    said = skills.run("navigate", {"destination": "nearest Starbucks"}, phone)
    assert phone.calls == [("close", "maps"),
                           ("uri", "google.navigation:q=nearest%20Starbucks&mode=d", "com.google.android.apps.maps")]
    assert "Starbucks" in said


def test_media_with_app_opens_it_then_plays(monkeypatch):
    monkeypatch.setattr(skills.time, "sleep", lambda s: None)
    phone = FakePhone()
    said = skills.run("media", {"action": "play", "app": "spotify"}, phone)
    assert phone.calls == [("start", "spotify"), ("key", "play")]
    assert said == "Playing on Spotify."


def test_volume_presses_key_several_times():
    phone = FakePhone()
    skills.run("volume", {"direction": "up", "steps": 2}, phone)
    assert phone.calls == [("key", "volume_up")] * 2


def test_close_app():
    phone = FakePhone()
    assert skills.run("close_app", {"app": "instagram"}, phone) == "Closed Instagram."


# ---- skills.route ----

def _fake_client(block):
    return SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: SimpleNamespace(content=[block])))


def test_route_picks_skill(monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "k")
    block = SimpleNamespace(type="tool_use", name="media", input={"action": "pause"})
    monkeypatch.setattr("relay.brains.claude._client", lambda: _fake_client(block))
    assert skills.route("pause the music", "phone") == ("media", {"action": "pause"})


def test_route_screen_agent_means_none(monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "k")
    block = SimpleNamespace(type="tool_use", name="screen_agent", input={})
    monkeypatch.setattr("relay.brains.claude._client", lambda: _fake_client(block))
    assert skills.route("text my group chat on instagram", "phone") is None


def test_route_skips_mac_and_missing_key(monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    assert skills.route("open spotify and play my workout playlist", "phone") is None  # not a keyword command
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "k")
    assert skills.route("open spotify", "mac") is None


# ---- passphrase + unlock ----

def test_strip_passphrase(monkeypatch):
    monkeypatch.setattr(config, "RELAY_PASSPHRASE", "open sesame")
    assert agent.strip_passphrase("Open sesame, open Spotify") == ("open Spotify", True)
    assert agent.strip_passphrase("open spotify") == ("open spotify", False)


def test_locked_without_passphrase_refuses(monkeypatch):
    monkeypatch.setattr(config, "RELAY_PASSPHRASE", "open sesame")
    task = agent.Task("open spotify", device_name="phone")
    phone = FakePhone(locked=True)
    with pytest.raises(agent.Cancelled):
        task._unlock_if_needed(phone)
    assert ("unlock", "1234") not in phone.calls


def test_locked_with_passphrase_unlocks_with_pin(monkeypatch):
    monkeypatch.setattr(config, "RELAY_PASSPHRASE", "open sesame")
    monkeypatch.setattr(config, "EMULATOR_PIN", "1234")
    task = agent.Task("open sesame open spotify", device_name="phone")
    phone = FakePhone(locked=True)
    task._unlock_if_needed(phone)
    assert phone.calls == [("unlock", "1234")]
    assert "sesame" not in task.goal  # the passphrase never reaches the AI


# ---- popups after a skill ----

def test_find_blocker_spots_onboarding_skip(monkeypatch):
    from relay.types import Screen, Element
    monkeypatch.setattr(skills.time, "sleep", lambda s: None)
    phone = FakePhone()
    phone.get_screen = lambda with_screenshot=False: Screen(device="phone", app="maps", elements=[
        Element(0, "Skip", "button", (0, 0, 1, 1), True),
        Element(1, "Make it your map", "text", (0, 0, 1, 1), False)])
    assert skills.find_blocker(phone) == "Skip"


def test_find_blocker_none_when_clear(monkeypatch):
    from relay.types import Screen, Element
    monkeypatch.setattr(skills.time, "sleep", lambda s: None)
    phone = FakePhone()
    phone.get_screen = lambda with_screenshot=False: Screen(device="phone", app="maps", elements=[
        Element(0, "Exit navigation", "button", (0, 0, 1, 1), True)])
    assert skills.find_blocker(phone) is None


def test_popup_is_cleared_without_redoing_task(monkeypatch):
    """The skill worked; a popup sits on top. Relay taps it away and stops."""
    from relay.types import Screen, Element, Action
    monkeypatch.setattr(skills.time, "sleep", lambda s: None)
    monkeypatch.setattr(agent, "SETTLE_SECONDS", 0)
    popup = Screen(device="phone", app="maps", elements=[Element(0, "No thanks", "button", (0, 0, 1, 1), True)])
    clear = Screen(device="phone", app="maps", elements=[Element(i, f"x{i}", "text", (0, 0, 1, 1), False) for i in range(5)])
    phone = FakePhone()
    screens = [popup, clear]  # 1st read: pick the button; 2nd read: check it is gone
    phone.get_screen = lambda with_screenshot=False: screens.pop(0) if len(screens) > 1 else screens[0]
    phone.act = lambda a, s: phone.calls.append(("tap", a.element_id))
    monkeypatch.setattr("relay.brains.picker.choose_action", lambda *a: Action("tap", element_id=0))
    from relay import safety
    task = agent.Task("navigate to starbucks", device_name="phone")
    assert task._clear_popups(phone, safety, skills) is True
    assert phone.calls == [("tap", 0)]


def test_navigation_running_detects_results_list():
    from relay.types import Screen, Element
    phone = FakePhone()
    phone.get_screen = lambda with_screenshot=False: Screen(device="phone", app="maps", elements=[
        Element(0, "Sponsored", "text", (0, 0, 1, 1), False),
        Element(1, "Directions", "button", (0, 0, 1, 1), True),
        Element(2, "Call", "button", (0, 0, 1, 1), True)])
    assert not skills.navigation_running(phone)
    phone.get_screen = lambda with_screenshot=False: Screen(device="phone", app="maps", elements=[
        Element(0, "Exit navigation", "button", (0, 0, 1, 1), True),
        Element(1, "Re-center", "button", (0, 0, 1, 1), True)])
    assert skills.navigation_running(phone)


# ---- inbox summary ----

def test_email_keywords():
    assert skills.match_keywords("summarize my emails") == ("email_summary", {"timeframe": "recent"})
    assert skills.match_keywords("hey relay, what came in overnight?") == ("email_summary", {"timeframe": "overnight"})


def test_collect_inbox_reads_and_scrolls(monkeypatch):
    from relay.types import Screen, Element
    monkeypatch.setattr(skills.time, "sleep", lambda s: None)
    pages = [["Prof. Lee", "Exam moved to Friday"], ["Prof. Lee", "Chase", "Your statement is ready"]]
    phone = FakePhone()
    phone.get_screen = lambda with_screenshot=False: Screen(device="phone", app="gmail", elements=[
        Element(i, t, "text", (0, 0, 1, 1), False) for i, t in enumerate(pages.pop(0) if pages else [])])
    phone.act = lambda a, s: phone.calls.append(("act", a.kind, a.direction))
    lines = skills.collect_inbox(phone, pages=2)
    assert lines == ["Prof. Lee", "Exam moved to Friday", "Chase", "Your statement is ready"]
    assert phone.calls == [("act", "scroll", "down")]


def test_summarize_inbox_uses_fast_model(monkeypatch):
    seen = {}

    def create(**kw):
        seen.update(kw)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="You have 2 emails that matter.")])

    monkeypatch.setattr("relay.brains.claude._client", lambda: SimpleNamespace(messages=SimpleNamespace(create=create)))
    out = skills.summarize_inbox(["Prof. Lee", "Exam moved"], "overnight")
    assert out == "You have 2 emails that matter."
    assert seen["model"] == config.CLAUDE_FAST_MODEL
    assert "6 PM yesterday" in seen["messages"][0]["content"]
    assert "NEVER read out verification codes" in seen["system"]
