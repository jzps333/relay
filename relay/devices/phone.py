"""Phone device: controls an Android emulator through uiautomator2.

uiautomator2 ("u2") talks to the emulator over ADB. The two things we use most:
  - d.dump_hierarchy()  -> an XML string describing every view on screen
  - d.click(x, y), d.send_keys(...), d.swipe(...), d.press(...) -> to act

The XML parsing lives in a plain function, parse_hierarchy(), so we can test
it with a fake XML string and no emulator.
"""
from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ET

from relay import config
from relay.types import Action, Element, Screen

# Bounds in the XML look like "[0,63][1080,210]" -> x1, y1, x2, y2.
_BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")

# Friendly app names -> Android package names, so "open Gmail" works.
APP_PACKAGES = {
    "instagram": "com.instagram.android",
    "gmail": "com.google.android.gm",
    "chrome": "com.android.chrome",
    "messages": "com.google.android.apps.messaging",
    "phone": "com.google.android.dialer",
    "settings": "com.android.settings",
    "doordash": "com.dd.doordash",
    "whatsapp": "com.whatsapp",
    "youtube": "com.google.android.youtube",
    "maps": "com.google.android.apps.maps",
    "camera": "com.android.camera2",  # the stock emulator camera
    "photos": "com.google.android.apps.photos",
    "google maps": "com.google.android.apps.maps",
    "spotify": "com.spotify.music",
    "snapchat": "com.snapchat.android",
    "tiktok": "com.zhiliaoapp.musically",
    "uber": "com.ubercab",
    "waze": "com.waze",
    "outlook": "com.microsoft.office.outlook",
    "calendar": "com.google.android.calendar",
    "clock": "com.google.android.deskclock",
    "play store": "com.android.vending",
    "x": "com.twitter.android",
    "twitter": "com.twitter.android",
}

# Android key codes used by the skills (see `adb shell input keyevent`).
KEYCODES = {
    "wakeup": "KEYCODE_WAKEUP",
    "enter": "KEYCODE_ENTER",
    "play": "KEYCODE_MEDIA_PLAY",
    "pause": "KEYCODE_MEDIA_PAUSE",
    "play_pause": "KEYCODE_MEDIA_PLAY_PAUSE",
    "next": "KEYCODE_MEDIA_NEXT",
    "previous": "KEYCODE_MEDIA_PREVIOUS",
    "volume_up": "KEYCODE_VOLUME_UP",
    "volume_down": "KEYCODE_VOLUME_DOWN",
    "mute": "KEYCODE_VOLUME_MUTE",
}

# Text in `dumpsys window` that means the lock screen is showing.
_LOCKED_RE = re.compile(r"(mDreamingLockscreen|isKeyguardShowing|mShowingLockscreen|mKeyguardShowing)=true")


def package_for(name: str) -> str:
    """App name -> Android package. Unknown names are treated as a package already."""
    return APP_PACKAGES.get(name.strip().lower(), name.strip())


def parse_bounds(raw: str) -> tuple[int, int, int, int] | None:
    """Turn "[x1,y1][x2,y2]" into a tuple, or None if it doesn't match."""
    match = _BOUNDS_RE.fullmatch(raw.strip())
    if not match:
        return None
    x1, y1, x2, y2 = (int(n) for n in match.groups())
    return (x1, y1, x2, y2)


def _kind_for(class_name: str, clickable: bool, has_text: bool) -> str:
    """Decide what kind of element this is, for the AI prompt."""
    if "EditText" in class_name:
        return "input"  # checked first: text fields are often clickable too
    if clickable:
        return "button"
    if "ImageView" in class_name and not has_text:
        return "image"
    return "text"


def _fallback_label(node) -> str:
    """Label for a clickable node with no text of its own.

    1. Text of its children (a row whose child says "Wi-Fi" -> "Wi-Fi").
    2. Its resource-id, made readable ("com.x:id/wifi_toggle" -> "wifi toggle").
    """
    child_texts = []
    for child in node.iter("node"):
        if child is node:
            continue
        t = (child.get("text") or child.get("content-desc") or "").strip()
        if t and t not in child_texts:
            child_texts.append(t)
        if len(child_texts) == 3:
            break
    if child_texts:
        return " / ".join(child_texts)

    res_id = node.get("resource-id") or ""
    if "/" in res_id:
        return res_id.split("/")[-1].replace("_", " ")
    return ""


def parse_hierarchy(xml: str) -> list[Element]:
    """Parse a uiautomator XML dump into a flat list of useful Elements.

    We keep a node if it is clickable, OR has text / content-desc, OR is a
    text field (EditText). Everything else (empty layout containers) is noise.
    Ids are given in order 0, 1, 2, ... so the AI can say "tap 3".
    """
    root = ET.fromstring(xml)
    elements: list[Element] = []

    # root.iter("node") walks every <node> in the tree, depth-first.
    for node in root.iter("node"):
        class_name = node.get("class", "")
        clickable = node.get("clickable") == "true"
        is_input = "EditText" in class_name
        # Visible text first; otherwise the accessibility label (content-desc);
        # text fields can also have a grey "hint" like "Search".
        text = (node.get("text") or node.get("content-desc") or "").strip()
        if not text and is_input:
            text = (node.get("hint") or "").strip()
        if not text and clickable:
            # Many icon buttons have no label of their own. Without one the AI
            # sees "button:" and can't tell what it does, so borrow a label.
            text = _fallback_label(node)

        # A clickable row often holds the real switch as a child. Copy the
        # child's on/off state onto the row so tapping the row is clearly a toggle.
        if clickable and node.get("checkable") != "true":
            for child in node.iter("node"):
                if child is not node and child.get("checkable") == "true":
                    state = "on" if child.get("checked") == "true" else "off"
                    text = f"{text} [{state}]".strip() if text else f"switch [{state}]"
                    break

        if not (clickable or text or is_input):
            continue

        # Switches and checkboxes: say whether they're on, so the AI never
        # has to ask a blind user "is it on?".
        if node.get("checkable") == "true":
            state = "on" if node.get("checked") == "true" else "off"
            text = f"{text} [{state}]".strip() if text else f"switch [{state}]"

        bounds = parse_bounds(node.get("bounds", ""))
        if bounds is None:
            continue
        x1, y1, x2, y2 = bounds
        if x2 <= x1 or y2 <= y1:
            continue  # zero-size = not actually visible

        elements.append(
            Element(
                id=len(elements),
                text=text,
                kind=_kind_for(class_name, clickable, bool(text)),
                bounds=bounds,
                clickable=clickable,
            )
        )
    return elements


# ---------------------------------------------------------------------------
# Notifications: parsing `adb shell dumpsys notification --noredact`
# ---------------------------------------------------------------------------
# The dump has one block per notification, starting with a line like
#   NotificationRecord(0x0a1b2c3d: pkg=com.whatsapp user=UserHandle{0} id=1 ...
# and, inside it, lines such as
#   flags=0x10
#   android.title=String (Mom)              <- newer Android
#   android.text=where are you? (java.lang.String)   <- older Android
#   mCreationTimeMs=1731090000000

# Apps whose notifications are never worth reading out loud.
SKIP_NOTIFICATION_PACKAGES = {
    "android",                      # "USB debugging connected" and similar
    "com.android.systemui",         # screenshots, battery, etc.
    "com.android.shell",            # bug reports
    "com.github.uiautomator",       # Relay's own helper app (uiautomator2)
    "com.github.uiautomator.test",
}

# Notification flag bits (from Android's Notification class).
_FLAG_ONGOING = 0x02            # "Playing music", "Navigation running"
_FLAG_FOREGROUND_SERVICE = 0x40  # an app saying "I'm running in the background"
_FLAG_GROUP_SUMMARY = 0x200     # "3 new messages" header; the real messages are separate

_RECORD_RE = re.compile(r"NotificationRecord\(0x[0-9a-f]+: pkg=(\S+)")
_KEY_RE = re.compile(r"\bkey=(\S+)")
_FLAGS_RE = re.compile(r"\bflags=(0x[0-9a-fA-F]+)")
_TIME_RE = re.compile(r"\b(?:mCreationTimeMs|postTime|when)=(\d{10,})")
# Everything after these headings is snoozed or old history, not "new".
_END_SECTIONS = ("Snoozed notifications", "mArchive", "Notification Archive")


def _extra_value(raw: str) -> str:
    """Clean an extras value: 'String (Mom)' or 'Mom (java.lang.String)' -> 'Mom'."""
    raw = raw.strip()
    if raw in ("", "null"):
        return ""
    m = re.fullmatch(r"\w*(?:String|CharSequence)\w* \((.*)\)", raw, re.S)   # newer format
    if m:
        return m.group(1).strip()
    m = re.fullmatch(r"(.*) \((?:java\.lang\.|android\.text\.)?\w*String\w*\)", raw, re.S)  # older
    if m:
        return m.group(1).strip()
    return raw


def _extra(block: str, name: str) -> str:
    """Value of one extras field in a notification block, e.g. android.title."""
    m = re.search(rf"^\s*{re.escape(name)}=(.*)$", block, re.M)
    return _extra_value(m.group(1)) if m else ""


def parse_notifications(dump: str) -> list[dict]:
    """Turn a `dumpsys notification --noredact` dump into a list of dicts.

    Each dict: {"package", "title", "text", "time"} where time is the post time
    in milliseconds since 1970 (or None if the dump didn't show it).
    Ongoing, foreground-service, group-summary and system notifications are
    skipped. Newest first.
    """
    for heading in _END_SECTIONS:
        cut = dump.find(heading)
        if cut != -1:
            dump = dump[:cut]

    starts = [m.start() for m in _RECORD_RE.finditer(dump)]
    results: list[dict] = []
    seen_keys: set[str] = set()
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(dump)
        block = dump[start:end]
        package = _RECORD_RE.match(block).group(1)
        if package in SKIP_NOTIFICATION_PACKAGES:
            continue

        # The same notification can be listed twice (e.g. "Enqueued" list).
        key_match = _KEY_RE.search(block)
        key = key_match.group(1) if key_match else block[:200]
        if key in seen_keys:
            continue
        seen_keys.add(key)

        flags_match = _FLAGS_RE.search(block)
        flags = int(flags_match.group(1), 16) if flags_match else 0
        if flags & (_FLAG_ONGOING | _FLAG_FOREGROUND_SERVICE | _FLAG_GROUP_SUMMARY):
            continue

        title = _extra(block, "android.title")
        # The long version (bigText) has the whole message; fall back to the short one.
        text = _extra(block, "android.bigText") or _extra(block, "android.text")
        if not title and not text:
            continue

        time_match = _TIME_RE.search(block)
        results.append({
            "package": package,
            "title": title,
            "text": text,
            "time": int(time_match.group(1)) if time_match else None,
        })

    results.sort(key=lambda n: n["time"] or 0, reverse=True)  # newest first
    return results


def _center(el: Element) -> tuple[int, int]:
    x1, y1, x2, y2 = el.bounds
    return ((x1 + x2) // 2, (y1 + y2) // 2)


def _find(screen: Screen, element_id: int | None) -> Element:
    """Look up an element by id in the screen snapshot the action was chosen on."""
    for el in screen.elements:
        if el.id == element_id:
            return el
    raise ValueError(f"element {element_id} is not on this screen")


def _adb_input_escape(text: str) -> str:
    """`adb shell input text` needs spaces as %s and shell symbols escaped."""
    out = []
    for ch in text:
        if ch == " ":
            out.append("%s")
        elif ch in "\\'\"`$&|;<>()*?!#~[]{}":
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


class Phone:
    """Implements the Device protocol (see base.py) for an Android emulator."""

    name = "phone"

    def __init__(self, device=None):
        # Tests pass a fake `device`; real use connects to the emulator.
        # The import is inside here so tests never need a phone attached.
        if device is None:
            import uiautomator2 as u2

            device = u2.connect(config.ADB_SERIAL or None)  # None = first device
        self.d = device

    # ---- reading the screen ----

    def get_screen(self, with_screenshot: bool = False) -> Screen:
        elements = parse_hierarchy(self.d.dump_hierarchy())
        app = self.d.app_current().get("package", "")
        png = None
        if with_screenshot:
            # uiautomator2 3.x returns a Pillow image (the old format="raw"
            # option was removed), so we save it into memory as PNG bytes.
            image = self.d.screenshot()
            # Shrink it: the AI only needs to SEE the screen (taps use element
            # bounds, not pixels), and a small image uploads much faster.
            if image.width > config.SCREENSHOT_WIDTH:
                ratio = config.SCREENSHOT_WIDTH / image.width
                image = image.resize((config.SCREENSHOT_WIDTH, int(image.height * ratio)))
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            png = buffer.getvalue()
        return Screen(device=self.name, app=app, elements=elements, screenshot_png=png)

    # ---- doing things ----

    def _type_text(self, text: str) -> None:
        """Type into the focused field.

        Newer Android versions broke u2's send_keys(clear=True) (it crashes with
        InputManager.getInstance). So: 1) set the focused field's text directly
        through accessibility; 2) if that fails, fall back to `adb input text`.
        """
        try:
            field = self.d(focused=True)
            if field.exists:
                field.set_text(text)
                return
        except Exception:
            pass
        self.d.shell(f"input text {_adb_input_escape(text)}")

    def act(self, action: Action, screen: Screen) -> None:
        kind = action.kind

        if kind == "tap":
            x, y = _center(_find(screen, action.element_id))
            self.d.click(x, y)

        elif kind == "type":
            # Tap the field first (if one was given) so the keyboard focuses it.
            if action.element_id is not None:
                x, y = _center(_find(screen, action.element_id))
                self.d.click(x, y)
            self._type_text(action.text or "")

        elif kind == "scroll":
            width, height = self.d.window_size()
            x = width // 2
            low, high = int(height * 0.75), int(height * 0.25)
            # To see content further DOWN, the finger moves UP the screen.
            if action.direction == "up":
                self.d.swipe(x, high, x, low, 0.3)
            else:
                self.d.swipe(x, low, x, high, 0.3)

        elif kind == "back":
            self.d.press("back")

        elif kind == "home":
            self.d.press("home")

        elif kind == "open_app":
            name = (action.text or "").strip()
            self.d.app_start(package_for(name))

        # "done" and "ask_user" are handled by the agent loop, not the device.

    # ---- quick system actions (used by skills and unlocking) ----

    def _shell(self, cmd: str) -> str:
        """Run an adb shell command and return its text output."""
        result = self.d.shell(cmd)
        return getattr(result, "output", result) or ""

    def key(self, name: str) -> None:
        """Press a named key from KEYCODES, e.g. key("next")."""
        self._shell(f"input keyevent {KEYCODES[name]}")

    def start_app(self, name: str) -> None:
        self.d.app_start(package_for(name))

    def close_app(self, name: str) -> None:
        self.d.app_stop(package_for(name))

    def open_uri(self, uri: str, package: str | None = None) -> None:
        """Open a link/intent URI, e.g. 'google.navigation:q=Starbucks'."""
        cmd = f"am start -a android.intent.action.VIEW -d '{uri}'"
        if package:
            cmd += f" -p {package}"
        self._shell(cmd)

    def grant_location(self, package: str) -> None:
        """Give an app location permission (dev-device shortcut, no popup)."""
        for perm in ("ACCESS_FINE_LOCATION", "ACCESS_COARSE_LOCATION"):
            self._shell(f"pm grant {package} android.permission.{perm}")

    def notifications(self) -> list[dict]:
        """The notifications in the shade right now (see parse_notifications)."""
        # --noredact shows the real message text instead of "Contents hidden".
        return parse_notifications(self._shell("dumpsys notification --noredact"))

    def is_locked(self) -> bool:
        """True if the lock screen is showing (or the screen is off)."""
        try:
            if not self.d.info.get("screenOn", True):
                return True
        except Exception:
            pass
        return bool(_LOCKED_RE.search(self._shell("dumpsys window")))

    def unlock(self, pin: str = "") -> bool:
        """Wake the screen, swipe up, type the PIN if there is one. Returns True if unlocked.

        The PIN goes straight from .env to ADB. It never passes through an AI.
        """
        import time

        self.key("wakeup")
        time.sleep(0.4)
        width, height = self.d.window_size()
        self.d.swipe(width // 2, int(height * 0.85), width // 2, int(height * 0.25))
        time.sleep(0.6)
        if pin:
            self._shell(f"input text {pin}")
            self.key("enter")
            time.sleep(0.8)
        return not self.is_locked()

    def emergency_call(self) -> None:
        # DIAL (not CALL) opens the dialer with 911 typed in; the user presses
        # the call button themselves, so nothing is ever dialed by accident.
        self.d.shell("am start -a android.intent.action.DIAL -d tel:911")
