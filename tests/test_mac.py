"""Tests for relay/devices/mac.py. Run on Linux: no pyobjc, no real mouse."""
import sys
import types

import pytest

from relay.devices import mac
from relay.types import Action, Element, Screen


# --- fake accessibility tree: each node is a dict of AX attributes ---------

def node(role, title=None, desc=None, value=None, pos=(0, 0), size=(10, 10), children=()):
    return {
        "AXRole": role, "AXTitle": title, "AXDescription": desc, "AXValue": value,
        "AXPosition": pos, "AXSize": size, "AXChildren": list(children),
    }


def fake_get(n, name):
    return n.get(name)


def test_role_to_kind():
    assert mac.role_to_kind("AXButton") == "button"
    assert mac.role_to_kind("AXTextField") == "input"
    assert mac.role_to_kind("AXStaticText") == "text"
    assert mac.role_to_kind("AXImage") == "image"
    assert mac.role_to_kind("AXGroup") == "other"
    assert mac.role_to_kind(None) == "other"


def test_node_text_priority():
    assert mac.node_text("Save", "desc", "v") == "Save"
    assert mac.node_text("", "Close window", None) == "Close window"
    assert mac.node_text(None, "  ", 42) == "42"
    assert mac.node_text(None, None, None) == ""


def test_flatten_tree_keeps_useful_nodes():
    root = node("AXApplication", title="Safari", size=None, children=[
        node("AXWindow", children=[
            node("AXGroup"),  # no text, not clickable -> dropped
            node("AXButton", desc="Back", pos=(10, 20), size=(30, 40)),
            node("AXTextField", value="hello", pos=(100, 0), size=(200, 20)),
            node("AXButton", title="Hidden", size=(0, 0)),  # zero size -> dropped
        ]),
    ])
    els = mac.flatten_tree(root, fake_get)
    assert [e.text for e in els] == ["Back", "hello"]
    back = els[0]
    assert back.id == 0 and back.kind == "button" and back.clickable
    assert back.bounds == (10, 20, 40, 60)
    assert els[1].id == 1 and els[1].kind == "input"


def test_flatten_tree_hides_password_value():
    root = node("AXSecureTextField", value="hunter2")
    els = mac.flatten_tree(root, fake_get)
    assert len(els) == 1 and els[0].text == "" and els[0].clickable


def test_flatten_tree_limits():
    # A chain 50 deep: depth limit should cut it off.
    deep = node("AXStaticText", title="leaf")
    for i in range(50):
        deep = node("AXStaticText", title=f"n{i}", children=[deep])
    assert len(mac.flatten_tree(deep, fake_get, max_depth=5)) == 6  # depths 0..5

    # 1000 siblings: node limit should cut it off (root counts as one visit).
    wide = node("AXWindow", children=[node("AXButton", title=str(i)) for i in range(1000)])
    assert len(mac.flatten_tree(wide, fake_get, max_nodes=50)) == 49


# --- act() with fake pyautogui and subprocess -------------------------------

class FakePyAutoGUI(types.ModuleType):
    def __init__(self):
        super().__init__("pyautogui")
        self.calls = []

    def click(self, *a, **k): self.calls.append(("click", a, k))
    def write(self, *a, **k): self.calls.append(("write", a, k))
    def scroll(self, *a, **k): self.calls.append(("scroll", a, k))
    def hotkey(self, *a, **k): self.calls.append(("hotkey", a, k))


@pytest.fixture
def gui(monkeypatch):
    fake = FakePyAutoGUI()
    monkeypatch.setitem(sys.modules, "pyautogui", fake)
    return fake


@pytest.fixture
def runs(monkeypatch):
    calls = []
    monkeypatch.setattr(mac.subprocess, "run", lambda cmd, **k: calls.append(cmd))
    return calls


SCREEN = Screen(device="mac", app="Safari", elements=[
    Element(id=0, text="Back", kind="button", bounds=(10, 20, 30, 40), clickable=True),
    Element(id=1, text="", kind="input", bounds=(100, 0, 300, 20), clickable=True),
])


def test_tap_clicks_center(gui):
    mac.Mac().act(Action(kind="tap", element_id=0), SCREEN)
    assert gui.calls == [("click", (20, 30), {})]


def test_tap_bad_id(gui):
    with pytest.raises(ValueError):
        mac.Mac().act(Action(kind="tap", element_id=99), SCREEN)


def test_type_clicks_then_writes(gui):
    mac.Mac().act(Action(kind="type", element_id=1, text="hi"), SCREEN)
    assert gui.calls[0] == ("click", (200, 10), {})
    assert gui.calls[1][0] == "write" and gui.calls[1][1] == ("hi",)


def test_scroll_directions(gui):
    mac.Mac().act(Action(kind="scroll", direction="down"), SCREEN)
    mac.Mac().act(Action(kind="scroll", direction="up"), SCREEN)
    assert gui.calls[0][1][0] < 0 and gui.calls[1][1][0] > 0


def test_back_is_cmd_bracket(gui):
    mac.Mac().act(Action(kind="back"), SCREEN)
    assert gui.calls == [("hotkey", ("command", "["), {})]


def test_home_and_open_app(gui, runs):
    mac.Mac().act(Action(kind="home"), SCREEN)
    mac.Mac().act(Action(kind="open_app", text="Safari"), SCREEN)
    assert runs == [["open", "-a", "Finder"], ["open", "-a", "Safari"]]


def test_done_does_nothing(gui, runs):
    mac.Mac().act(Action(kind="done", text="All set"), SCREEN)
    assert gui.calls == [] and runs == []


def test_emergency_call_raises():
    with pytest.raises(NotImplementedError, match="phone"):
        mac.Mac().emergency_call()


def test_get_screen_without_macos_libs(monkeypatch):
    # On Linux AppKit/ApplicationServices are missing: should not crash.
    monkeypatch.setattr(mac, "_screenshot_png", lambda: b"PNG")
    s = mac.Mac().get_screen(with_screenshot=True)
    assert s.device == "mac" and s.elements == [] and s.screenshot_png == b"PNG"


def test_get_screen_ax_failure_keeps_screenshot(monkeypatch):
    monkeypatch.setattr(mac, "_frontmost_app", lambda: ("Safari", 123))
    monkeypatch.setattr(mac, "_screenshot_png", lambda: b"PNG")
    fake_as = types.ModuleType("ApplicationServices")

    def boom(pid):
        raise RuntimeError("no accessibility permission")

    fake_as.AXUIElementCreateApplication = boom
    monkeypatch.setitem(sys.modules, "ApplicationServices", fake_as)
    s = mac.Mac().get_screen(with_screenshot=True)
    assert s.app == "Safari" and s.elements == [] and s.screenshot_png == b"PNG"
