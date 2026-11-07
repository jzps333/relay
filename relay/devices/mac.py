"""Mac device: lets Relay see and control the Mac it is running on.

SETUP (one time, on the Mac):
    pip install mss pyautogui pillow pyobjc-framework-ApplicationServices \
        pyobjc-framework-Cocoa pyobjc-framework-Quartz
    Then open System Settings > Privacy & Security and turn on BOTH of these for
    Terminal (or your IDE, e.g. VS Code / PyCharm - whichever app runs the server):
      - Accessibility     -> needed to read the UI tree and to click / type
      - Screen Recording  -> needed for screenshots (without it you only see the wallpaper)
    Quit and reopen Terminal / the IDE after granting: macOS only applies it on relaunch.
    The first time Relay quits an app, macOS also asks "Terminal wants to control <app>"
    (Automation permission) - click OK.

How it works:
    * get_screen() takes a screenshot with `mss` and reads the accessibility (AX)
      tree of the frontmost app with `pyobjc`. Each useful AX node becomes an Element.
    * act() moves the real mouse/keyboard with `pyautogui`.

The macOS-only libraries are imported INSIDE the functions, so this file can still be
imported (and tested) on Linux / CI where they are not installed.

Coordinates: AX gives positions in "points" (not Retina pixels), with (0, 0) at the
top-left of the main screen, and pyautogui on macOS also clicks in points. So element
bounds are clicked directly. The screenshot is only for the AI to LOOK at (it is shrunk
to config.SCREENSHOT_WIDTH), it is never used to work out where to click.
"""
from __future__ import annotations

import io
import os
import re
import subprocess
from dataclasses import replace
from typing import Any, Callable

from relay import config
from relay.types import Action, Element, Screen

# Limits for walking the accessibility tree (big apps like Safari have thousands of nodes).
MAX_NODES = 400
MAX_DEPTH = 12

# How many "clicks" of the wheel one scroll action does. macOS clicks are small.
SCROLL_AMOUNT = 10

# How much one volume_up / volume_down changes the volume (macOS volume is 0-100).
VOLUME_STEP = 6

# ---------------------------------------------------------------------------
# Pure helpers (no macOS needed, so the tests can call them with fake nodes)
# ---------------------------------------------------------------------------

# AX role name -> our Element.kind
ROLE_KINDS = {
    "AXButton": "button",
    "AXMenuButton": "button",
    "AXPopUpButton": "button",
    "AXCheckBox": "button",
    "AXRadioButton": "button",
    "AXLink": "button",
    "AXMenuItem": "button",
    "AXMenuBarItem": "button",
    "AXTab": "button",
    "AXDisclosureTriangle": "button",
    "AXTextField": "input",
    "AXTextArea": "input",
    "AXSearchField": "input",
    "AXComboBox": "input",
    "AXSecureTextField": "input",  # password box; safety.py decides what to do with it
    "AXStaticText": "text",
    "AXHeading": "text",
    "AXImage": "image",
}

# Kinds the user can click on.
CLICKABLE_KINDS = {"button", "input"}


def role_to_kind(role: str | None) -> str:
    """Map an AX role like "AXButton" to "button" / "input" / "text" / "image" / "other"."""
    return ROLE_KINDS.get(role or "", "other")


def node_text(title: Any, description: Any, value: Any) -> str:
    """Pick the best human-readable label: title, then description, then value."""
    for candidate in (title, description, value):
        # AXValue can be a number (e.g. a slider) or None, so convert and strip.
        if candidate is not None and str(candidate).strip():
            return str(candidate).strip()
    return ""


def flatten_tree(
    root: Any,
    get_attr: Callable[[Any, str], Any],
    max_nodes: int = MAX_NODES,
    max_depth: int = MAX_DEPTH,
) -> list[Element]:
    """Walk an accessibility tree and return a flat list of useful Elements.

    `get_attr(node, name)` returns a plain Python value for an attribute:
      "AXChildren" -> list of nodes, "AXRole"/"AXTitle"/"AXDescription"/"AXValue" -> str,
      "AXPosition" -> (x, y), "AXSize" -> (w, h). Missing attributes -> None.
    On a real Mac this is backed by pyobjc; in tests it reads from fake dicts.

    We visit at most `max_nodes` nodes and go at most `max_depth` levels deep.
    A node is kept only if it is on screen (has a size) and has text or is clickable,
    so empty layout boxes (AXGroup, AXSplitGroup...) don't waste the LLM's attention.
    """
    elements: list[Element] = []
    visited = 0
    # Breadth-first search: a queue of (node, depth). BFS keeps the top-level
    # controls (toolbars, buttons) before deep nested stuff if we hit the limit.
    queue: list[tuple[Any, int]] = [(root, 0)]

    while queue and visited < max_nodes:
        node, depth = queue.pop(0)
        visited += 1

        role = get_attr(node, "AXRole")
        kind = role_to_kind(role)
        # Never read the value of a password box (it would leak to the LLM).
        value = None if role == "AXSecureTextField" else get_attr(node, "AXValue")
        text = node_text(get_attr(node, "AXTitle"), get_attr(node, "AXDescription"), value)
        pos = get_attr(node, "AXPosition")
        size = get_attr(node, "AXSize")
        clickable = kind in CLICKABLE_KINDS

        if pos is not None and size is not None and size[0] > 0 and size[1] > 0:
            if text or clickable:
                x, y = int(pos[0]), int(pos[1])
                w, h = int(size[0]), int(size[1])
                elements.append(
                    Element(
                        id=len(elements),  # id = index in this snapshot
                        text=text,
                        kind=kind,
                        bounds=(x, y, x + w, y + h),
                        clickable=clickable,
                    )
                )

        # Queue the children for later, unless we are already too deep.
        if depth < max_depth:
            for child in get_attr(node, "AXChildren") or []:
                queue.append((child, depth + 1))

    return elements


def snapshot_elements(
    app: Any,
    get_attr: Callable[[Any, str], Any],
    max_nodes: int = MAX_NODES,
) -> list[Element]:
    """Elements of an app: its focused window first, then the top menu bar titles.

    Why not just flatten_tree(app)? The app's children are the menu bar AND its windows.
    Every closed menu (File, Edit, View...) still has all its items in the tree, so a
    plain walk spends most of the 400-node budget on hidden menu items and never reaches
    the window. So we walk the focused window with the full budget, then add only the
    menu bar's top row ("File", "Edit"...) with depth 1.
    """
    window = get_attr(app, "AXFocusedWindow")
    elements = flatten_tree(window if window is not None else app, get_attr, max_nodes=max_nodes)

    menu_bar = get_attr(app, "AXMenuBar")
    if window is not None and menu_bar is not None:
        menus = flatten_tree(menu_bar, get_attr, max_nodes=50, max_depth=1)
        elements += menus

    # Re-number so id == position in the combined list (find_element relies on unique ids).
    return [replace(e, id=i) for i, e in enumerate(elements)]


def center(bounds: tuple[int, int, int, int]) -> tuple[int, int]:
    """Middle point of (x1, y1, x2, y2)."""
    x1, y1, x2, y2 = bounds
    return (x1 + x2) // 2, (y1 + y2) // 2


def find_element(screen: Screen, element_id: int | None) -> Element:
    """Look up an element by id; raise a clear error if the brain picked a bad id."""
    for e in screen.elements:
        if e.id == element_id:
            return e
    raise ValueError(f"No element with id {element_id} on this screen")


def applescript_string(text: str) -> str:
    """Turn any text into a safe AppleScript string literal, quotes included.

    Inside AppleScript "..." strings only backslash and double quote are special,
    so we escape those two. Newlines are replaced with spaces (no app name has one).
    Example: 'My "Cool" App' -> '"My \\"Cool\\" App"'
    """
    clean = text.replace("\r", " ").replace("\n", " ")
    clean = clean.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{clean}"'


def parse_axvalue_repr(text: str, kind: str) -> tuple[float, float] | None:
    """Backup plan for reading an AXValue: parse its printed form.

    str() of an AXPosition looks like "<AXValue 0x...> {value = x:12.000000 y:34.000000 type =
    kAXValueCGPointType}" and an AXSize like "... w:800.000000 h:600.000000 ...".
    Only used if AXValueGetValue itself fails, so a pyobjc quirk doesn't blind us.
    """
    keys = ("x", "y") if kind == "point" else ("w", "h")
    match = re.search(rf"\b{keys[0]}:(-?[\d.]+)\s+{keys[1]}:(-?[\d.]+)", text)
    return (float(match.group(1)), float(match.group(2))) if match else None


def shrink_png(png: bytes, width: int) -> bytes:
    """Scale a PNG down to `width` pixels wide (keeping its shape), like phone.py does.

    On a Retina Mac the screenshot is 2x the screen size in points (e.g. 3024 px for a
    1512-point-wide screen). The AI only needs to SEE it, so a small image is plenty
    and uploads much faster.
    """
    from PIL import Image

    image = Image.open(io.BytesIO(png))
    if image.width > width:
        ratio = width / image.width
        image = image.resize((width, max(1, int(image.height * ratio))))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


# Messages we already printed, so a missing permission is reported ONCE, not every step.
_warned: set[str] = set()


def _warn_once(key: str, message: str) -> None:
    if key not in _warned:
        _warned.add(key)
        print(f"[mac] {message}")


AX_HELP = (
    "Accessibility permission is missing, so I can only see the screenshot (no buttons list) "
    "and clicks/typing may not work. Fix: System Settings > Privacy & Security > Accessibility "
    "> turn on Terminal (or the IDE running Relay), then quit and reopen it."
)
SCREEN_HELP = (
    "Screen Recording permission is missing, so screenshots only show the wallpaper. Fix: "
    "System Settings > Privacy & Security > Screen Recording > turn on Terminal (or the IDE "
    "running Relay), then quit and reopen it."
)

# ---------------------------------------------------------------------------
# macOS glue (only runs on a Mac)
# ---------------------------------------------------------------------------

# Attributes whose value should be plain text/numbers. Anything else (another AX object,
# an AXValue struct...) would print as junk like "<AXUIElement 0x...>", so we drop it.
_TEXT_ATTRS = {"AXRole", "AXTitle", "AXDescription", "AXValue"}


def _ax_get_attr(node: Any, name: str) -> Any:
    """Read one AX attribute and convert it to a plain Python value (for flatten_tree).

    pyobjc turns the C "out" parameter into a return value, so
    AXUIElementCopyAttributeValue(node, name, None) returns (error_code, value).
    """
    import ApplicationServices as AS  # lazy: macOS only

    try:
        err, value = AS.AXUIElementCopyAttributeValue(node, name, None)
    except Exception:
        return None  # one broken node shouldn't kill the whole snapshot
    if err != 0 or value is None:  # 0 == kAXErrorSuccess
        return None
    if name == "AXPosition":
        return _axvalue_pair(AS, value, "point")
    if name == "AXSize":
        return _axvalue_pair(AS, value, "size")
    if name == "AXChildren":
        return list(value)
    if name in _TEXT_ATTRS and not isinstance(value, (str, int, float)):
        return None
    return value


def _axvalue_pair(AS: Any, value: Any, kind: str) -> tuple[float, float] | None:
    """Unpack an AXValue holding a CGPoint ("point") or CGSize ("size") into (a, b).

    pyobjc wraps AXValueGetValue by hand: AXValueGetValue(value, type, None) returns
    (ok, struct). The struct can be indexed like a tuple: point[0] = x, point[1] = y.
    The type constant is kAXValueTypeCGPoint in new SDKs (kAXValueCGPointType is the old
    name); both equal 1 (CGSize = 2), so we fall back to the number.
    """
    if kind == "point":
        type_code = getattr(AS, "kAXValueTypeCGPoint", getattr(AS, "kAXValueCGPointType", 1))
    else:
        type_code = getattr(AS, "kAXValueTypeCGSize", getattr(AS, "kAXValueCGSizeType", 2))
    try:
        ok, struct = AS.AXValueGetValue(value, type_code, None)
        if ok and struct is not None:
            return (float(struct[0]), float(struct[1]))
    except Exception:
        pass
    return parse_axvalue_repr(str(value), kind)


def _ax_trusted() -> bool:
    """True if this process (really: Terminal / the IDE) has Accessibility permission."""
    import ApplicationServices as AS  # lazy: macOS only

    return bool(AS.AXIsProcessTrusted())


def _frontmost_app() -> tuple[str, int | None]:
    """(app name, process id) of the app in front, e.g. ("Safari", 1234).

    Careful: NSWorkspace.frontmostApplication() only updates when the main thread's
    run loop runs, and Relay's server never runs one (and works in background threads),
    so it would keep returning whatever app was in front at startup. So we ask:
      1. Accessibility (needs permission): the system-wide "AXFocusedApplication".
      2. The window list: the top normal window (layer 0) belongs to the front app.
      3. NSWorkspace, as a last resort.
    """
    try:
        import ApplicationServices as AS

        if AS.AXIsProcessTrusted():
            err, app = AS.AXUIElementCopyAttributeValue(
                AS.AXUIElementCreateSystemWide(), "AXFocusedApplication", None
            )
            if err == 0 and app is not None:
                err, pid = AS.AXUIElementGetPid(app, None)  # (error, pid) in pyobjc
                if err == 0:
                    return str(_ax_get_attr(app, "AXTitle") or "unknown"), int(pid)
    except Exception:
        pass

    try:
        import Quartz

        options = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
        # The list is ordered front-to-back. Layer 0 = normal app windows
        # (the menu bar, Dock and pop-ups live on higher layers).
        for window in Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID) or []:
            if window.get("kCGWindowLayer") == 0 and window.get("kCGWindowAlpha", 1) > 0:
                return str(window.get("kCGWindowOwnerName", "unknown")), int(window["kCGWindowOwnerPID"])
    except Exception:
        pass

    from AppKit import NSWorkspace  # lazy: macOS only

    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    if app is None:
        return "unknown", None
    return str(app.localizedName()), int(app.processIdentifier())


def _read_elements(pid: int) -> list[Element]:
    """Accessibility elements of the app with this process id."""
    import ApplicationServices as AS  # lazy: macOS only

    app = AS.AXUIElementCreateApplication(pid)
    # Electron apps (VS Code, Slack, Discord...) hide their UI tree unless asked.
    # Harmless for every other app (they just return an error we ignore).
    try:
        AS.AXUIElementSetAttributeValue(app, "AXManualAccessibility", True)
    except Exception:
        pass
    return snapshot_elements(app, _ax_get_attr)


def _screenshot_png() -> bytes | None:
    """PNG of the main display, shrunk to config.SCREENSHOT_WIDTH. None if it fails."""
    try:
        import Quartz

        # Without Screen Recording permission macOS doesn't fail, it just leaves the
        # windows out of the picture. Warn so the user knows why the AI is blind.
        if not Quartz.CGPreflightScreenCaptureAccess():
            _warn_once("screen", SCREEN_HELP)
    except Exception:
        pass
    try:
        import mss
        import mss.tools

        with mss.mss() as sct:
            shot = sct.grab(sct.monitors[1])  # monitors[0] = all screens, [1] = main
            png = mss.tools.to_png(shot.rgb, shot.size)
        return shrink_png(png, config.SCREENSHOT_WIDTH)
    except Exception as e:
        _warn_once("screenshot", f"Screenshot failed: {e!r}")
        return None


def _paste_text(text: str) -> None:
    """Type any text (emoji, accents, Chinese...) by putting it on the clipboard and pasting.

    pbcopy picks the text encoding from the LANG setting; if LANG isn't UTF-8 (e.g. when
    started from some IDEs) accents turn into junk, so we set it for this one command.
    Note: this replaces whatever was on the clipboard.
    """
    import pyautogui

    env = {**os.environ, "LANG": "en_US.UTF-8"}
    subprocess.run(["pbcopy"], input=text.encode("utf-8"), env=env, check=True)
    pyautogui.hotkey("command", "v", interval=0.05)


# AppleScript for each volume key. `set volume` needs no extra permission (unlike fake
# key presses), and is exact. Volume is 0-100; macOS clamps it for us.
_VOLUME_SCRIPTS = {
    "volume_up": f"set volume output volume ((output volume of (get volume settings)) + {VOLUME_STEP})",
    "volume_down": f"set volume output volume ((output volume of (get volume settings)) - {VOLUME_STEP})",
    "mute": "set volume with output muted",
}

# Media keys (the F7/F8/F9 keys) as macOS "special key" numbers (NX_KEYTYPE_* in
# IOKit's ev_keymap.h). There is only ONE play key and it toggles play/pause, so
# "play" and "pause" both press it.
_MEDIA_KEYS = {
    "play": 16,        # NX_KEYTYPE_PLAY
    "pause": 16,
    "play_pause": 16,
    "next": 17,        # NX_KEYTYPE_NEXT
    "previous": 18,    # NX_KEYTYPE_PREVIOUS
}


def _press_media_key(code: int) -> None:
    """Press a media key by posting the same system event the real keyboard sends.

    pyautogui can't do this on macOS (its "playpause" key does nothing there), so we
    build the event with pyobjc (already installed with pyautogui). Same trick as
    pyautogui's own _specialKeyEvent: an NSEvent of type "system defined" (14),
    subtype 8, with the key number and down (0xA) / up (0xB) packed into data1.
    Needs the Accessibility permission, like all fake input.
    """
    import AppKit
    import Quartz

    for flags, state in ((0xA00, 0xA), (0xB00, 0xB)):  # key down, then key up
        event = AppKit.NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
            14,  # NSEventTypeSystemDefined
            (0, 0),
            flags,
            0,
            0,
            None,
            8,  # NX_SUBTYPE_AUX_CONTROL_BUTTONS
            (code << 16) | (state << 8),
            -1,
        )
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event.CGEvent())


class Mac:
    """Device implementation for the Mac (see relay/devices/base.py for the interface)."""

    name = "mac"

    def get_screen(self, with_screenshot: bool = False) -> Screen:
        try:
            app_name, pid = _frontmost_app()
        except Exception:
            app_name, pid = "unknown", None

        elements: list[Element] = []
        if pid is not None:
            try:
                if _ax_trusted():
                    elements = _read_elements(pid)
                else:
                    # No permission: say how to fix it (once), keep going with pixels only.
                    _warn_once("ax", AX_HELP)
            except Exception as e:
                # A crash while reading one app's tree shouldn't stop the task: the
                # screenshot is still returned so Claude can work from pixels.
                _warn_once("ax-error", f"Couldn't read the accessibility tree: {e!r}")
                elements = []

        png = _screenshot_png() if with_screenshot else None
        return Screen(device="mac", app=app_name, elements=elements, screenshot_png=png)

    def act(self, action: Action, screen: Screen) -> None:
        import pyautogui  # lazy: needs a real display

        kind = action.kind

        if kind == "tap":
            # Bounds are in points, and pyautogui clicks in points on macOS: no scaling.
            x, y = center(find_element(screen, action.element_id).bounds)
            pyautogui.click(x, y)

        elif kind == "type":
            # Click the field first so it has keyboard focus, then type.
            if action.element_id is not None:
                x, y = center(find_element(screen, action.element_id).bounds)
                pyautogui.click(x, y)
            text = action.text or ""
            if text.isascii():
                pyautogui.write(text, interval=0.02)
            else:
                # pyautogui.write only knows keys on a US keyboard, so "café" or "👍"
                # would come out wrong. Paste those instead.
                _paste_text(text)

        elif kind == "scroll":
            # pyautogui: positive = scroll up, negative = scroll down.
            amount = SCROLL_AMOUNT if action.direction == "up" else -SCROLL_AMOUNT
            if action.element_id is not None:
                x, y = center(find_element(screen, action.element_id).bounds)
                pyautogui.scroll(amount, x=x, y=y)
            else:
                pyautogui.scroll(amount)

        elif kind == "back":
            pyautogui.hotkey("command", "[")  # "Back" in Safari, Finder, Chrome...

        elif kind == "home":
            self.start_app("Finder")

        elif kind == "open_app":
            if not action.text:
                raise ValueError("open_app needs the app name in action.text")
            self.start_app(action.text)

        elif kind in ("done", "ask_user"):
            pass  # nothing to do on the screen; the agent loop speaks these

        else:
            raise ValueError(f"Unknown action kind: {kind}")

    # ---- quick system actions (used by skills, same names as the phone) ----

    def start_app(self, name: str) -> None:
        """Open (or bring to the front) an app by name, e.g. "Safari". Case doesn't matter.

        Raises ValueError if macOS can't find the app, so the agent can try something else.
        """
        result = subprocess.run(["open", "-a", name], capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise ValueError(f"Couldn't open {name!r}: {(result.stderr or '').strip() or 'unknown app'}")

    def close_app(self, name: str) -> None:
        """Politely quit an app (like Cmd+Q, so it can save), only if it is running.

        `is running` checks without launching it (a plain `tell ... to quit` could open
        the app just to quit it). The name is escaped so quotes can't break the script.
        """
        app = applescript_string(name)
        script = f"if application {app} is running then tell application {app} to quit"
        result = subprocess.run(
            ["osascript", "-e", script], capture_output=True, text=True, check=False, timeout=15
        )
        if result.returncode != 0:
            raise ValueError(f"Couldn't quit {name!r}: {(result.stderr or '').strip()}")

    def key(self, name: str) -> None:
        """Press a named key, same names as the phone: play, pause, play_pause, next,
        previous, volume_up, volume_down, mute, enter, wakeup."""
        if name in _VOLUME_SCRIPTS:
            subprocess.run(["osascript", "-e", _VOLUME_SCRIPTS[name]], check=False, timeout=15)
        elif name in _MEDIA_KEYS:
            _press_media_key(_MEDIA_KEYS[name])
        elif name == "enter":
            import pyautogui

            pyautogui.press("enter")
        elif name == "wakeup":
            # "The user is active": turns the display on if it was asleep.
            subprocess.run(["caffeinate", "-u", "-t", "1"], check=False)
        else:
            raise NotImplementedError(
                f"Key {name!r} isn't supported on the Mac. Supported: "
                + ", ".join(sorted({*_VOLUME_SCRIPTS, *_MEDIA_KEYS, "enter", "wakeup"}))
            )

    def emergency_call(self) -> None:
        raise NotImplementedError("Emergency calls go through the phone")
