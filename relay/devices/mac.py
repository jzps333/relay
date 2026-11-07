"""Mac device: lets Relay see and control the Mac it is running on.

SETUP (one time, on the Mac):
    pip install mss pyautogui pyobjc-framework-ApplicationServices pyobjc-framework-Cocoa
    Then open System Settings > Privacy & Security and turn on BOTH of these for
    Terminal (or your IDE, e.g. VS Code / PyCharm - whichever app runs the server):
      - Accessibility     -> needed to read the UI tree and to click / type
      - Screen Recording  -> needed for screenshots
    Restart Terminal / the IDE after granting, macOS only applies it on relaunch.

How it works:
    * get_screen() takes a screenshot with `mss` and reads the accessibility (AX)
      tree of the frontmost app with `pyobjc`. Each useful AX node becomes an Element.
    * act() moves the real mouse/keyboard with `pyautogui`.

The macOS-only libraries are imported INSIDE the methods, so this file can still be
imported (and tested) on Linux / CI where they are not installed.

Coordinates: AX gives positions in "points" (not Retina pixels) and pyautogui on
macOS also clicks in points, so element bounds can be clicked directly. Note the
screenshot itself is in pixels (2x on Retina), so it is bigger than the bounds.
"""
from __future__ import annotations

import subprocess
from typing import Any, Callable

from relay.types import Action, Element, Screen

# Limits for walking the accessibility tree (big apps like Safari have thousands of nodes).
MAX_NODES = 400
MAX_DEPTH = 12

# How many "clicks" of the wheel one scroll action does. macOS clicks are small.
SCROLL_AMOUNT = 10

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


# ---------------------------------------------------------------------------
# macOS glue (only runs on a Mac)
# ---------------------------------------------------------------------------


def _ax_get_attr(node: Any, name: str) -> Any:
    """Read one AX attribute and convert it to a plain Python value (for flatten_tree)."""
    import ApplicationServices as AS  # lazy: macOS only

    try:
        err, value = AS.AXUIElementCopyAttributeValue(node, name, None)
    except Exception:
        return None  # one broken node shouldn't kill the whole snapshot
    if err != 0 or value is None:  # 0 == kAXErrorSuccess
        return None
    if name == "AXPosition":
        ok, point = AS.AXValueGetValue(value, AS.kAXValueCGPointType, None)
        return (point.x, point.y) if ok else None
    if name == "AXSize":
        ok, size = AS.AXValueGetValue(value, AS.kAXValueCGSizeType, None)
        return (size.width, size.height) if ok else None
    if name == "AXChildren":
        return list(value)
    return value


def _frontmost_app() -> tuple[str, int | None]:
    """(app name, process id) of the app in front, e.g. ("Safari", 1234)."""
    from AppKit import NSWorkspace  # lazy: macOS only

    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    if app is None:
        return "unknown", None
    return str(app.localizedName()), int(app.processIdentifier())


def _screenshot_png() -> bytes | None:
    """PNG of the main display, or None if Screen Recording permission is missing."""
    try:
        import mss
        import mss.tools

        with mss.mss() as sct:
            shot = sct.grab(sct.monitors[1])  # monitors[0] = all screens, [1] = main
            return mss.tools.to_png(shot.rgb, shot.size)
    except Exception:
        return None


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
                import ApplicationServices as AS

                root = AS.AXUIElementCreateApplication(pid)
                elements = flatten_tree(root, _ax_get_attr)
            except Exception:
                # Usually means Accessibility permission isn't granted. We still
                # return the screenshot so Claude can work from pixels.
                elements = []

        png = _screenshot_png() if with_screenshot else None
        return Screen(device="mac", app=app_name, elements=elements, screenshot_png=png)

    def act(self, action: Action, screen: Screen) -> None:
        import pyautogui  # lazy: needs a real display

        kind = action.kind

        if kind == "tap":
            x, y = center(find_element(screen, action.element_id).bounds)
            pyautogui.click(x, y)

        elif kind == "type":
            # Click the field first so it has keyboard focus, then type.
            if action.element_id is not None:
                x, y = center(find_element(screen, action.element_id).bounds)
                pyautogui.click(x, y)
            # Note: pyautogui.write only types plain ASCII reliably.
            pyautogui.write(action.text or "", interval=0.02)

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
            subprocess.run(["open", "-a", "Finder"], check=False)

        elif kind == "open_app":
            if not action.text:
                raise ValueError("open_app needs the app name in action.text")
            subprocess.run(["open", "-a", action.text], check=False)

        elif kind in ("done", "ask_user"):
            pass  # nothing to do on the screen; the agent loop speaks these

        else:
            raise ValueError(f"Unknown action kind: {kind}")

    def emergency_call(self) -> None:
        raise NotImplementedError("Emergency calls go through the phone")
