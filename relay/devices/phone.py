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
}


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


def _center(el: Element) -> tuple[int, int]:
    x1, y1, x2, y2 = el.bounds
    return ((x1 + x2) // 2, (y1 + y2) // 2)


def _find(screen: Screen, element_id: int | None) -> Element:
    """Look up an element by id in the screen snapshot the action was chosen on."""
    for el in screen.elements:
        if el.id == element_id:
            return el
    raise ValueError(f"element {element_id} is not on this screen")


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
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            png = buffer.getvalue()
        return Screen(device=self.name, app=app, elements=elements, screenshot_png=png)

    # ---- doing things ----

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
            self.d.send_keys(action.text or "", clear=True)

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
            package = APP_PACKAGES.get(name.lower(), name)  # unknown -> treat as package
            self.d.app_start(package)

        # "done" and "ask_user" are handled by the agent loop, not the device.

    def emergency_call(self) -> None:
        # DIAL (not CALL) opens the dialer with 911 typed in; the user presses
        # the call button themselves, so nothing is ever dialed by accident.
        self.d.shell("am start -a android.intent.action.DIAL -d tel:911")
