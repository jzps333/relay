"""Tests for relay/devices/mac.py. Run on Linux: no pyobjc, no real mouse."""
import io
import subprocess
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
    def press(self, *a, **k): self.calls.append(("press", a, k))


@pytest.fixture
def gui(monkeypatch):
    fake = FakePyAutoGUI()
    monkeypatch.setitem(sys.modules, "pyautogui", fake)
    return fake


@pytest.fixture
def runs(monkeypatch):
    calls = []

    def fake_run(cmd, **k):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(mac.subprocess, "run", fake_run)
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
    fake_as.AXIsProcessTrusted = lambda: True
    monkeypatch.setitem(sys.modules, "ApplicationServices", fake_as)
    s = mac.Mac().get_screen(with_screenshot=True)
    assert s.app == "Safari" and s.elements == [] and s.screenshot_png == b"PNG"


# --- new: permissions, AX value unpacking, focused window, screenshots ------

@pytest.fixture(autouse=True)
def reset_warnings():
    mac._warned.clear()
    yield
    mac._warned.clear()


def test_get_screen_no_accessibility_warns_once(monkeypatch, capsys):
    monkeypatch.setattr(mac, "_frontmost_app", lambda: ("Safari", 123))
    monkeypatch.setattr(mac, "_screenshot_png", lambda: b"PNG")
    monkeypatch.setattr(mac, "_ax_trusted", lambda: False)
    read = []
    monkeypatch.setattr(mac, "_read_elements", lambda pid: read.append(pid) or [])
    for _ in range(3):
        s = mac.Mac().get_screen(with_screenshot=True)
        assert s.elements == [] and s.screenshot_png == b"PNG" and s.app == "Safari"
    out = capsys.readouterr().out
    assert out.count("[mac]") == 1  # one clear message, not one per step
    assert "System Settings > Privacy & Security > Accessibility" in out and "Terminal" in out
    assert read == []  # didn't even try to read the tree


def test_get_screen_with_accessibility_reads_tree(monkeypatch):
    monkeypatch.setattr(mac, "_frontmost_app", lambda: ("Safari", 123))
    monkeypatch.setattr(mac, "_ax_trusted", lambda: True)
    els = [Element(id=0, text="OK", kind="button", bounds=(0, 0, 5, 5), clickable=True)]
    monkeypatch.setattr(mac, "_read_elements", lambda pid: els)
    s = mac.Mac().get_screen()
    assert s.elements == els and s.screenshot_png is None


class FakeStruct(tuple):
    """pyobjc CGPoint/CGSize structs can be indexed like tuples."""


def make_fake_as(attrs, getvalue=None):
    """Fake ApplicationServices: attrs maps attribute name -> (err, value)."""
    m = types.ModuleType("ApplicationServices")
    m.kAXValueTypeCGPoint, m.kAXValueTypeCGSize = 1, 2
    m.AXUIElementCopyAttributeValue = lambda node, name, out: attrs.get(name, (-25212, None))
    m.AXValueGetValue = getvalue or (lambda value, t, out: (True, FakeStruct(value[t])))
    return m


def test_ax_get_attr_unpacks_position_and_size(monkeypatch):
    axvalue = {1: (12.0, 34.0), 2: (800.0, 600.0)}  # what our fake AXValueGetValue reads
    fake = make_fake_as({
        "AXPosition": (0, axvalue), "AXSize": (0, axvalue), "AXTitle": (0, "Save"),
        "AXChildren": (0, ("a", "b")), "AXValue": (0, object()),
    })
    monkeypatch.setitem(sys.modules, "ApplicationServices", fake)
    assert mac._ax_get_attr(None, "AXPosition") == (12.0, 34.0)
    assert mac._ax_get_attr(None, "AXSize") == (800.0, 600.0)
    assert mac._ax_get_attr(None, "AXTitle") == "Save"
    assert mac._ax_get_attr(None, "AXChildren") == ["a", "b"]
    assert mac._ax_get_attr(None, "AXValue") is None  # odd objects are dropped, not printed
    assert mac._ax_get_attr(None, "AXDescription") is None  # error code -> None


def test_ax_get_attr_falls_back_to_repr(monkeypatch):
    class Val:
        def __str__(self):
            return "<AXValue 0x6000> {value = x:10.500000 y:-20.000000 type = kAXValueCGPointType}"

    def broken(*a):
        raise TypeError("pyobjc quirk")

    fake = make_fake_as({"AXPosition": (0, Val())}, getvalue=broken)
    monkeypatch.setitem(sys.modules, "ApplicationServices", fake)
    assert mac._ax_get_attr(None, "AXPosition") == (10.5, -20.0)


def test_parse_axvalue_repr_size():
    text = "<AXValue> {value = w:1512.000000 h:982.000000 type = kAXValueCGSizeType}"
    assert mac.parse_axvalue_repr(text, "size") == (1512.0, 982.0)
    assert mac.parse_axvalue_repr("garbage", "point") is None


def test_snapshot_uses_focused_window_and_menu_titles():
    menu_bar = node("AXMenuBar", size=None, children=[
        node("AXMenuBarItem", title="File", children=[
            node("AXMenu", children=[node("AXMenuItem", title="Hidden item")]),
        ]),
    ])
    window = node("AXWindow", title="Doc", size=(500, 400), children=[node("AXButton", title="OK")])
    app = node("AXApplication", size=None, children=[menu_bar, window])
    app["AXFocusedWindow"], app["AXMenuBar"] = window, menu_bar
    els = mac.snapshot_elements(app, fake_get)
    assert [e.text for e in els] == ["Doc", "OK", "File"]  # no hidden menu items
    assert [e.id for e in els] == [0, 1, 2]


def test_snapshot_without_window_walks_app():
    app = node("AXApplication", size=None, children=[node("AXButton", title="Go")])
    assert [e.text for e in mac.snapshot_elements(app, fake_get)] == ["Go"]


def test_shrink_png_downscales_retina():
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (3024, 1964), "red").save(buf, format="PNG")
    out = Image.open(io.BytesIO(mac.shrink_png(buf.getvalue(), 540)))
    assert out.size == (540, 350)
    small = io.BytesIO()
    Image.new("RGB", (300, 200)).save(small, format="PNG")
    assert Image.open(io.BytesIO(mac.shrink_png(small.getvalue(), 540))).size == (300, 200)


def test_frontmost_uses_window_list_when_no_accessibility(monkeypatch):
    fake_as = types.ModuleType("ApplicationServices")
    fake_as.AXIsProcessTrusted = lambda: False
    quartz = types.ModuleType("Quartz")
    quartz.kCGWindowListOptionOnScreenOnly, quartz.kCGWindowListExcludeDesktopElements = 1, 16
    quartz.kCGNullWindowID = 0
    quartz.CGWindowListCopyWindowInfo = lambda opts, wid: [
        {"kCGWindowLayer": 25, "kCGWindowOwnerName": "Dock", "kCGWindowOwnerPID": 1},
        {"kCGWindowLayer": 0, "kCGWindowAlpha": 1.0, "kCGWindowOwnerName": "Notes", "kCGWindowOwnerPID": 42},
    ]
    monkeypatch.setitem(sys.modules, "ApplicationServices", fake_as)
    monkeypatch.setitem(sys.modules, "Quartz", quartz)
    assert mac._frontmost_app() == ("Notes", 42)


# --- new: typing, apps, keys ------------------------------------------------

def test_type_non_ascii_uses_clipboard(gui, monkeypatch):
    calls = []

    def fake_run(cmd, **k):
        calls.append((cmd, k))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(mac.subprocess, "run", fake_run)
    mac.Mac().act(Action(kind="type", text="café 👍"), SCREEN)
    cmd, kwargs = calls[0]
    assert cmd == ["pbcopy"] and kwargs["input"] == "café 👍".encode("utf-8")
    assert kwargs["env"]["LANG"] == "en_US.UTF-8"
    assert gui.calls[-1][0] == "hotkey" and gui.calls[-1][1] == ("command", "v")
    assert not any(c[0] == "write" for c in gui.calls)


def test_start_app_failure_raises(monkeypatch):
    monkeypatch.setattr(mac.subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(
        cmd, 1, "", "Unable to find application named 'Nope'"))
    with pytest.raises(ValueError, match="Nope"):
        mac.Mac().start_app("Nope")


def test_close_app_escapes_name(runs):
    mac.Mac().close_app('Evil" to quit\ndo shell script "rm -rf ~')
    cmd = runs[0]
    assert cmd[:2] == ["osascript", "-e"]
    script = cmd[2]
    # The whole name stays inside ONE string literal: every quote in it is escaped.
    assert script.startswith('if application "Evil\\" to quit do shell script \\"rm -rf ~" is running')
    assert "\n" not in script


def test_applescript_string():
    assert mac.applescript_string('a"b\\c') == '"a\\"b\\\\c"'


def test_close_app_simple(runs):
    mac.Mac().close_app("Maps")
    assert runs == [["osascript", "-e",
                     'if application "Maps" is running then tell application "Maps" to quit']]


def test_volume_keys_use_osascript(runs):
    m = mac.Mac()
    m.key("volume_up")
    m.key("volume_down")
    m.key("mute")
    assert all(c[:2] == ["osascript", "-e"] for c in runs)
    assert "+ 6" in runs[0][2] and "- 6" in runs[1][2] and "muted" in runs[2][2]


def test_media_keys_post_system_events(monkeypatch):
    posted = []

    class FakeEvent:
        def __init__(self, args):
            self.args = args

        def CGEvent(self):
            return self

    appkit = types.ModuleType("AppKit")
    appkit.NSEvent = types.SimpleNamespace(
        otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_=(
            lambda *a: FakeEvent(a)))
    quartz = types.ModuleType("Quartz")
    quartz.kCGHIDEventTap = 0
    quartz.CGEventPost = lambda tap, ev: posted.append(ev.args)
    monkeypatch.setitem(sys.modules, "AppKit", appkit)
    monkeypatch.setitem(sys.modules, "Quartz", quartz)

    mac.Mac().key("next")
    assert len(posted) == 2  # key down + key up
    down, up = posted
    assert down[0] == 14 and down[6] == 8
    assert down[7] == (17 << 16) | (0xA << 8) and up[7] == (17 << 16) | (0xB << 8)


def test_enter_key(gui):
    mac.Mac().key("enter")
    assert gui.calls == [("press", ("enter",), {})]


def test_unknown_key_not_implemented():
    with pytest.raises(NotImplementedError, match="volume_up"):
        mac.Mac().key("brightness_up")
