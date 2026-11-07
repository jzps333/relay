"""Tests for relay/devices/phone.py. No emulator needed: we use a fake device."""
from relay.devices.phone import Phone, parse_bounds, parse_hierarchy
from relay.types import Action

SAMPLE_XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node class="android.widget.FrameLayout" text="" content-desc="" clickable="false" bounds="[0,0][1080,2400]">
    <node class="android.widget.LinearLayout" text="" content-desc="" clickable="false" bounds="[0,0][1080,200]" />
    <node class="android.widget.Button" text="Send" content-desc="" clickable="true" bounds="[100,200][300,300]" />
    <node class="android.widget.EditText" text="" content-desc="" hint="Search" clickable="true" bounds="[0,400][1080,500]" />
    <node class="android.widget.ImageView" text="" content-desc="" clickable="false" bounds="[0,600][100,700]" />
    <node class="android.widget.ImageView" text="" content-desc="Profile photo" clickable="false" bounds="[0,800][100,900]" />
    <node class="android.widget.TextView" text="Hello" content-desc="" clickable="false" bounds="[0,1000][500,1100]" />
    <node class="android.widget.TextView" text="Hidden" content-desc="" clickable="false" bounds="[0,0][0,0]" />
  </node>
</hierarchy>"""


class FakeDevice:
    """Records every call so tests can check what Phone asked the device to do."""

    def __init__(self):
        self.calls = []

    def dump_hierarchy(self):
        return SAMPLE_XML

    def app_current(self):
        return {"package": "com.example.app", "activity": ".Main"}

    def screenshot(self):
        # A tiny real Pillow image, like uiautomator2 returns.
        from PIL import Image

        return Image.new("RGB", (2, 2), "white")

    def window_size(self):
        return (1000, 2000)

    def click(self, x, y):
        self.calls.append(("click", x, y))

    def send_keys(self, text, clear=False):
        self.calls.append(("send_keys", text, clear))

    def swipe(self, x1, y1, x2, y2, duration=None):
        self.calls.append(("swipe", x1, y1, x2, y2))

    def press(self, key):
        self.calls.append(("press", key))

    def app_start(self, package):
        self.calls.append(("app_start", package))

    def shell(self, cmd):
        self.calls.append(("shell", cmd))


# ---- parse_hierarchy ----

def test_parse_bounds():
    assert parse_bounds("[1,2][30,40]") == (1, 2, 30, 40)
    assert parse_bounds("garbage") is None


def test_parse_hierarchy_keeps_only_useful_nodes():
    els = parse_hierarchy(SAMPLE_XML)
    # Empty layouts, the plain ImageView and the zero-size TextView are dropped.
    assert [e.text for e in els] == ["Send", "Search", "Profile photo", "Hello"]
    assert [e.id for e in els] == [0, 1, 2, 3]


def test_parse_hierarchy_kinds_and_bounds():
    send, search, photo, hello = parse_hierarchy(SAMPLE_XML)
    assert send.kind == "button" and send.clickable
    assert send.bounds == (100, 200, 300, 300)
    assert search.kind == "input"  # EditText wins over clickable
    assert photo.kind == "text"  # ImageView WITH a label counts as text
    assert hello.kind == "text" and not hello.clickable


def test_image_without_text_is_image():
    xml = (
        '<hierarchy><node class="android.widget.ImageView" text="" '
        'content-desc="" clickable="true" bounds="[0,0][10,10]" /></hierarchy>'
    )
    # clickable -> button (clickable takes priority over image)
    assert parse_hierarchy(xml)[0].kind == "button"
    xml = xml.replace('clickable="true"', 'clickable="false"').replace(
        'content-desc=""', 'content-desc="x"'
    )
    assert parse_hierarchy(xml)[0].kind == "text"


# ---- get_screen / act ----

def make_phone():
    fake = FakeDevice()
    return Phone(device=fake), fake


def test_get_screen():
    phone, _ = make_phone()
    screen = phone.get_screen()
    assert screen.device == "phone"
    assert screen.app == "com.example.app"
    assert len(screen.elements) == 4
    assert screen.screenshot_png is None
    png = phone.get_screen(with_screenshot=True).screenshot_png
    assert png.startswith(b"\x89PNG")  # real PNG bytes


def test_tap_clicks_center():
    phone, fake = make_phone()
    screen = phone.get_screen()
    phone.act(Action(kind="tap", element_id=0), screen)
    assert fake.calls == [("click", 200, 250)]


def test_type_taps_then_sends_keys():
    phone, fake = make_phone()
    screen = phone.get_screen()
    phone.act(Action(kind="type", element_id=1, text="pizza"), screen)
    assert fake.calls == [("click", 540, 450), ("send_keys", "pizza", True)]


def test_type_without_element():
    phone, fake = make_phone()
    phone.act(Action(kind="type", text="hi"), phone.get_screen())
    assert fake.calls == [("send_keys", "hi", True)]


def test_scroll_directions():
    phone, fake = make_phone()
    screen = phone.get_screen()
    phone.act(Action(kind="scroll", direction="down"), screen)
    phone.act(Action(kind="scroll", direction="up"), screen)
    # down: finger moves up (y 1500 -> 500); up: the reverse
    assert fake.calls == [("swipe", 500, 1500, 500, 500), ("swipe", 500, 500, 500, 1500)]


def test_back_and_home():
    phone, fake = make_phone()
    screen = phone.get_screen()
    phone.act(Action(kind="back"), screen)
    phone.act(Action(kind="home"), screen)
    assert fake.calls == [("press", "back"), ("press", "home")]


def test_open_app_known_and_unknown():
    phone, fake = make_phone()
    screen = phone.get_screen()
    phone.act(Action(kind="open_app", text="Gmail"), screen)
    phone.act(Action(kind="open_app", text="com.foo.bar"), screen)
    assert fake.calls == [
        ("app_start", "com.google.android.gm"),
        ("app_start", "com.foo.bar"),
    ]


def test_done_does_nothing():
    phone, fake = make_phone()
    phone.act(Action(kind="done", text="All set"), phone.get_screen())
    assert fake.calls == []


def test_tap_missing_element_raises():
    phone, _ = make_phone()
    try:
        phone.act(Action(kind="tap", element_id=99), phone.get_screen())
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_emergency_call_uses_dial_not_call():
    phone, fake = make_phone()
    phone.emergency_call()
    assert fake.calls == [("shell", "am start -a android.intent.action.DIAL -d tel:911")]


def test_unlabeled_button_borrows_child_text_or_resource_id():
    from relay.devices.phone import parse_hierarchy
    xml = """<hierarchy><node class="android.widget.FrameLayout" clickable="true" bounds="[0,0][100,100]" text="" content-desc="">
      <node class="android.widget.TextView" clickable="false" bounds="[0,0][50,50]" text="Wi-Fi" content-desc=""/>
    </node>
    <node class="android.widget.ImageButton" clickable="true" bounds="[0,200][100,300]" text="" content-desc="" resource-id="com.android.systemui:id/settings_button"/>
    </hierarchy>"""
    els = parse_hierarchy(xml)
    assert els[0].text == "Wi-Fi"
    assert els[-1].text == "settings button"


def test_switch_state_is_in_the_label():
    from relay.devices.phone import parse_hierarchy
    xml = """<hierarchy>
    <node class="android.widget.Switch" clickable="true" checkable="true" checked="true" bounds="[0,0][100,100]" text="Dark theme" content-desc=""/>
    <node class="android.widget.Switch" clickable="true" checkable="true" checked="false" bounds="[0,200][100,300]" text="" content-desc=""/>
    </hierarchy>"""
    els = parse_hierarchy(xml)
    assert els[0].text == "Dark theme [on]"
    assert els[1].text == "switch [off]"
