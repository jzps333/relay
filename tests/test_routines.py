"""Tests for relay/routines.py with a fake device and a temp routines file."""
import json

import pytest

from relay import config, routines
from relay.types import Action, Element, Screen


@pytest.fixture(autouse=True)
def tmp_routines(tmp_path, monkeypatch):
    """Point ROUTINES_PATH at a temp file and make replay instant."""
    path = tmp_path / "routines.json"
    monkeypatch.setattr(config, "ROUTINES_PATH", str(path))
    monkeypatch.setattr(routines, "STEP_DELAY", 0)
    return path


def screen(app, *texts, kind="button"):
    return Screen(device="phone", app=app, elements=[
        Element(id=i, text=t, kind=kind, bounds=(0, 0, 10, 10), clickable=True)
        for i, t in enumerate(texts)
    ])


class FakeDevice:
    """Returns screens from a list, one per get_screen(); records act() calls."""

    def __init__(self, screens, name="phone"):
        self.name = name
        self.screens = list(screens)
        self.acted = []

    def get_screen(self, with_screenshot=False):
        # Keep returning the last screen if we run out.
        return self.screens.pop(0) if len(self.screens) > 1 else self.screens[0]

    def act(self, action, screen):
        self.acted.append(action)

    def emergency_call(self):
        pass


def youtube_steps():
    s1 = screen("launcher", "Chrome", "YouTube")
    s2 = screen("com.google.android.youtube", "Home", "Search")
    s3 = screen("com.google.android.youtube", "Search YouTube", kind="input")
    return [
        (s1, Action(kind="tap", element_id=1)),                      # YouTube
        (s2, Action(kind="tap", element_id=1)),                      # Search
        (s3, Action(kind="type", element_id=0, text="lofi music")),
        (s3, Action(kind="ask_user", text="Which one?")),            # skipped
        (s3, Action(kind="done", text="Playing")),                   # skipped
    ]


# ---- save / find ------------------------------------------------------------

def test_save_stores_text_not_id(tmp_routines):
    routines.save("Play lofi music on YouTube", "phone", youtube_steps())
    data = json.loads(tmp_routines.read_text())
    steps = data[0]["steps"]
    assert [s["kind"] for s in steps] == ["tap", "tap", "type"]
    assert [s["target_text"] for s in steps] == ["YouTube", "Search", "Search YouTube"]
    assert steps[2]["text"] == "lofi music"
    assert "element_id" not in steps[0]


def test_find_exact_normalized():
    routines.save("Play lofi music on YouTube", "phone", youtube_steps())
    r = routines.find("  play LOFI music on youtube!! ")
    assert r is not None and r.device == "phone"


def test_find_fuzzy_jaccard():
    routines.save("play lofi music on youtube now", "phone", youtube_steps())
    # 5 of 6 words shared -> 5/6 = 0.83 >= 0.8
    assert routines.find("play lofi music on youtube") is not None
    # too different
    assert routines.find("play jazz on spotify") is None


def test_find_missing_file_returns_none():
    assert routines.find("anything") is None


def test_save_replaces_same_goal():
    routines.save("open youtube", "phone", youtube_steps()[:1])
    routines.save("Open YouTube", "phone", youtube_steps()[:2])
    data = json.loads(open(config.ROUTINES_PATH).read())
    assert len(data) == 1 and len(data[0]["steps"]) == 2


def test_never_saves_password(tmp_routines):
    s = screen("bank", "Password", kind="input")
    routines.save("log in to bank", "phone", [
        (screen("launcher", "Bank"), Action(kind="tap", element_id=0)),
        (s, Action(kind="type", element_id=0, text="hunter2")),
    ])
    assert not tmp_routines.exists()
    assert routines.find("log in to bank") is None


def test_never_saves_card_number(tmp_routines):
    s = screen("shop", "Notes", kind="input")
    routines.save("pay", "phone", [(s, Action(kind="type", element_id=0, text="4111111111111111"))])
    assert not tmp_routines.exists()


# ---- replay -----------------------------------------------------------------

def test_replay_uses_new_ids():
    routines.save("play lofi", "phone", youtube_steps())
    r = routines.find("play lofi")
    # Same buttons, but in a different order -> different ids.
    dev = FakeDevice([
        screen("launcher", "YouTube", "Maps", "Chrome"),
        screen("com.google.android.youtube", "Search", "Home"),
        screen("com.google.android.youtube", "Cancel", "search youtube...", kind="input"),
    ])
    assert routines.replay(r, dev) is True
    assert [a.element_id for a in dev.acted] == [0, 0, 1]   # 3rd via case-insensitive contains
    assert dev.acted[2].text == "lofi music"
    assert all(a.source == "routine" for a in dev.acted)


def test_replay_fails_when_element_missing():
    routines.save("play lofi", "phone", youtube_steps())
    dev = FakeDevice([screen("launcher", "Maps", "Chrome")])
    assert routines.replay(routines.find("play lofi"), dev) is False
    assert dev.acted == []


def test_replay_stops_before_send():
    s_chat = screen("com.instagram.android", "Message", "Send")
    routines.save("dm sam hi", "phone", [
        (s_chat, Action(kind="tap", element_id=0)),
        (s_chat, Action(kind="tap", element_id=1)),   # Send -> needs confirmation
    ])
    dev = FakeDevice([s_chat])
    assert routines.replay(routines.find("dm sam hi"), dev) is False
    assert len(dev.acted) == 1          # tapped Message, did NOT tap Send


def test_replay_non_element_steps():
    s = screen("launcher", "x")
    routines.save("go home", "phone", [
        (s, Action(kind="back")),
        (s, Action(kind="scroll", direction="down")),
        (s, Action(kind="home")),
    ])
    dev = FakeDevice([s])
    assert routines.replay(routines.find("go home"), dev) is True
    assert [a.kind for a in dev.acted] == ["back", "scroll", "home"]
    assert dev.acted[1].direction == "down"


def test_replay_wrong_device():
    routines.save("go home", "phone", [(screen("x"), Action(kind="home"))])
    dev = FakeDevice([screen("Finder")], name="mac")
    assert routines.replay(routines.find("go home"), dev) is False


def test_match_element_prefers_exact_then_shortest():
    s = screen("app", "Send a message to Sam", "send")
    assert routines.match_element(s, "send").id == 1        # exact
    assert routines.match_element(s, "SEND").id == 1        # contains, shortest
    assert routines.match_element(s, "nope") is None
