"""Navigate to a vague place: results list -> top real result -> turn-by-turn. No phone or keys."""
import pytest

from relay import skills
from relay.types import Action, Element, Screen

from tests.test_agent import env, run  # noqa: F401  (fixture + helper)


def _screen(*items):
    """items: (text, clickable) pairs -> a Maps Screen with ids 0, 1, 2..."""
    return Screen(device="phone", app="com.google.android.apps.maps",
                  elements=[Element(i, t, "button" if c else "text", (0, i, 10, i + 1), c)
                            for i, (t, c) in enumerate(items)])


RESULTS = _screen(
    ("nearest Starbucks", True), ("Open now", True), ("Top rated", True),
    ("Sponsored", False), ("Dunkin'", False), ("4.1", False), ("Coffee shop · 2.4 mi", False),
    ("Directions", True), ("Call", True),
    ("Starbucks", False), ("4.3", False), ("(812)", False), ("Coffee shop · 1201 W Broad St", False),
    ("Open · Closes 9 PM", False), ("Directions", True), ("Call", True),
    ("Starbucks", False), ("Coffee shop · 77 E Main St", False), ("Directions", True),
)
NAVIGATING = _screen(("Exit navigation", True), ("Re-center", True))
PREVIEW = _screen(("Steps", True), ("Start", True))


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(skills.time, "sleep", lambda s: None)
    monkeypatch.setattr(skills, "NAV_WAIT", 0)


# ---- pick_result ----

def test_pick_result_skips_sponsored_and_reads_address():
    place = skills.pick_result(RESULTS, "nearest Starbucks")
    assert (place.name, place.address, place.directions_id) == ("Starbucks", "1201 W Broad St", 14)


def test_pick_result_skips_search_box_but_keeps_same_named_result():
    screen = _screen(("Starbucks", True), ("Starbucks", False), ("88 Oak Ave", False), ("Directions", True))
    place = skills.pick_result(screen, "starbucks")
    assert (place.name, place.address) == ("Starbucks", "88 Oak Ave")
    assert skills.pick_result(_screen(("Starbucks", True), ("Directions", True)), "Starbucks") is None


def test_pick_result_none_without_rows():
    assert skills.pick_result(NAVIGATING, "x") is None
    assert skills.pick_result(_screen(("Sponsored", False), ("Ad place", False), ("Directions", True)), "x") is None


def test_pick_result_uses_directions_label_and_no_address():
    screen = _screen(("4.5", False), ("Directions to Blue Bottle Coffee", True))
    place = skills.pick_result(screen, "coffee")
    assert (place.name, place.address) == ("Blue Bottle Coffee", "")


# ---- start_top_result ----

class Maps:
    """Fake phone: shows `screens` in order (the last one stays)."""
    name = "phone"

    def __init__(self, *screens, after_uri=None):
        self.screens = list(screens)
        self.after_uri = after_uri
        self.calls = []

    def get_screen(self, with_screenshot=False):
        return self.screens.pop(0) if len(self.screens) > 1 else self.screens[0]

    def close_app(self, app):
        self.calls.append(("close", app))

    def open_uri(self, uri, package=None):
        self.calls.append(("uri", uri))
        if self.after_uri is not None:
            self.screens = [self.after_uri]

    def act(self, action, screen):
        self.calls.append(("tap", screen.elements[action.element_id].text))
        if screen.elements[action.element_id].text == "Start":
            self.screens = [NAVIGATING]


def test_relaunches_with_name_and_address():
    phone = Maps(RESULTS, after_uri=NAVIGATING)
    assert skills.start_top_result(phone, {"destination": "nearest Starbucks"}) == "Starbucks"
    assert ("uri", "google.navigation:q=Starbucks%2C%201201%20W%20Broad%20St&mode=d") in phone.calls


def test_no_address_taps_directions_then_start():
    phone = Maps(_screen(("Starbucks", False), ("Directions", True)), PREVIEW)
    assert skills.start_top_result(phone, {"destination": "starbucks"}) == "Starbucks"
    assert phone.calls == [("tap", "Directions"), ("tap", "Start")]


def test_relaunch_that_does_not_navigate_returns_none():
    phone = Maps(RESULTS)  # stays on the results list
    assert skills.start_top_result(phone, {"destination": "nearest Starbucks"}) is None


# ---- the agent ----

def _nav_device(env, screens, after_uri=None):
    dev = env.device
    dev.screens = list(screens)
    dev.uris = []
    dev.get_screen = lambda with_screenshot=False: dev.screens.pop(0) if len(dev.screens) > 1 else dev.screens[0]
    dev.close_app = lambda app: None

    def open_uri(uri, package=None):
        dev.uris.append(uri)
        if after_uri is not None and len(dev.uris) == 2:
            dev.screens = [after_uri]
    dev.open_uri = open_uri
    return dev


def test_agent_starts_top_result_without_screen_agent(env):
    dev = _nav_device(env, [RESULTS], after_uri=NAVIGATING)
    env.actions = [Action("back")]  # the screen agent must not run
    t = run("navigate to the nearest Starbucks")
    assert t.status == "done"
    assert t.say() == "Starting directions to Starbucks."
    assert dev.uris[1].startswith("google.navigation:q=Starbucks%2C%201201")
    assert dev.acted == []


def test_agent_falls_back_and_taps_start_instead_of_back(env):
    dev = _nav_device(env, [RESULTS])  # re-launch doesn't navigate: still the list
    dev.acted_texts = []

    def act(action, screen):
        dev.acted.append(action)
        label = screen.elements[action.element_id].text if action.element_id is not None else action.kind
        dev.acted_texts.append(label)
        if label == "Starbucks":
            dev.screens = [PREVIEW]
        elif label == "Start":
            dev.screens = [NAVIGATING]
    dev.act = act
    # The brain taps the result, then wrongly presses back on the preview.
    env.actions = [Action("tap", element_id=9), Action("back"), Action("back")]
    t = run("navigate to the nearest Starbucks")
    assert t.status == "done"
    assert dev.acted_texts == ["Starbucks", "Start"]
    assert t.say() == "Navigation started."
