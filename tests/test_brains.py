"""Brain tests. Every network call is replaced with a fake, so no API keys are needed."""
from types import SimpleNamespace

import pytest

from relay import config
from relay.brains import claude, gemini, jev, picker
from relay.types import Action, Element, Screen


def make_screen(png=None):
    return Screen(
        device="phone",
        app="com.whatsapp",
        elements=[
            Element(0, "Search", "input", (0, 0, 100, 40), True),
            Element(1, "Mom", "button", (0, 50, 100, 90), True),
            Element(2, "Chats", "text", (0, 100, 100, 140), False),
        ],
        screenshot_png=png,
    )


# ---------- Claude ----------

class FakeMessages:
    """Records the request and returns a canned response."""

    def __init__(self, content):
        self.content = content
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(content=self.content)


def fake_claude(monkeypatch, content):
    msgs = FakeMessages(content)
    monkeypatch.setattr(claude, "_client", lambda: SimpleNamespace(messages=msgs))
    return msgs


def tool_block(name, data):
    return SimpleNamespace(type="tool_use", name=name, input=data)


def test_claude_tool_output_parses_to_action(monkeypatch):
    msgs = fake_claude(monkeypatch, [tool_block("choose_action", {
        "kind": "tap", "element_id": 1, "text": None, "direction": None, "reason": "open Mom chat"})])
    history = [Action(kind="home", source="jev")] * 7
    a = claude.pick_action("text mom", "open chat", make_screen(png=b"\x89PNG"), history)

    assert a == Action(kind="tap", element_id=1, confidence=1.0, source="claude", reason="open Mom chat")
    # forced tool choice + screenshot sent as an image block
    assert msgs.kwargs["tool_choice"] == {"type": "auto"}  # forced choice is rejected by newer models
    blocks = msgs.kwargs["messages"][0]["content"]
    assert blocks[1]["type"] == "image"
    # only the last 5 actions are sent
    assert blocks[0]["text"].count("- home") == 5


def test_claude_invalid_kind_becomes_ask_user(monkeypatch):
    fake_claude(monkeypatch, [tool_block("choose_action", {"kind": "explode", "reason": "?"})])
    a = claude.pick_action("g", "s", make_screen(), [])
    assert a.kind == "ask_user" and a.source == "claude"


def test_claude_plan(monkeypatch):
    fake_claude(monkeypatch, [tool_block("submit_plan", {"subgoals": ["Open WhatsApp", "Open Mom chat", "Send"]})])
    assert claude.plan("text mom", "phone") == ["Open WhatsApp", "Open Mom chat", "Send"]


def test_claude_describe_screen(monkeypatch):
    fake_claude(monkeypatch, [SimpleNamespace(type="text", text="You are in WhatsApp.")])
    assert claude.describe_screen(make_screen()) == "You are in WhatsApp."


# ---------- Gemini ----------

def test_gemini_describe_uses_element_list_without_screenshot(monkeypatch):
    seen = {}

    def generate_content(**kw):
        seen.update(kw)
        return SimpleNamespace(text=" WhatsApp is open. ")

    monkeypatch.setattr(gemini, "_client", lambda: SimpleNamespace(models=SimpleNamespace(generate_content=generate_content)))
    assert gemini.describe_screen(make_screen()) == "WhatsApp is open."
    assert "[1]* button: Mom" in seen["contents"][0]


# ---------- Jev ----------

def choice(label, probs, conf=0.5):
    return {"type": "choice", "choice": label, "probabilities": probs, "confidence": conf}


def test_jev_payload_shape():
    payload, labels, cands = jev.build_payload('search "pizza"', "search", make_screen(), [])
    assert set(payload) == {"model", "state", "questions"}
    q = payload["questions"]
    assert set(q["kind"]["criteria"]) == set(claude.ACTION_KINDS)
    assert set(q["element"]["criteria"]) == {"e0", "e1"}  # non-clickable text skipped
    assert labels == {"e0": 0, "e1": 1}
    assert cands == ["pizza"]


def test_jev_confidence_is_product_of_probabilities(monkeypatch):
    data = {"answers": {
        "kind": choice("tap", {"tap": 0.9, "back": 0.1}),
        "element": choice("e1", {"e0": 0.2, "e1": 0.8}),
        "direction": choice("down", {"down": 0.5, "up": 0.5}),
    }}
    monkeypatch.setattr(jev, "_call_jev", lambda payload: data)
    a = jev.pick_action("text mom", "open chat", make_screen(), [])
    assert (a.kind, a.element_id, a.source) == ("tap", 1, "jev")
    assert a.confidence == pytest.approx(0.72)


def test_jev_type_maps_candidate_text(monkeypatch):
    data = {"answers": {
        "kind": choice("type", {"type": 1.0}),
        "element": choice("e0", {"e0": 0.9}),
        "direction": choice("down", {"down": 1.0}),
        "text": choice("t0", {"t0": 0.95, "none_of_these": 0.05}),
    }}
    monkeypatch.setattr(jev, "_call_jev", lambda payload: data)
    a = jev.pick_action('search "pizza"', "search", make_screen(), [])
    assert (a.kind, a.element_id, a.text) == ("type", 0, "pizza")
    assert a.confidence == pytest.approx(0.855)


def test_jev_falls_back_to_confidence_field(monkeypatch):
    data = {"answers": {"kind": {"type": "choice", "choice": "back", "confidence": 0.6}}}
    monkeypatch.setattr(jev, "_call_jev", lambda payload: data)
    assert jev.pick_action("g", "s", make_screen(), []).confidence == pytest.approx(0.6)


@pytest.mark.parametrize("answers", [
    {"kind": choice("done", {"done": 0.99})},              # needs a spoken sentence
    {"kind": {"type": "refusal"}},                         # Jev refused
    {"kind": choice("tap", {"tap": 1}), "element": choice("e99", {"e99": 1})},  # bad id
    {"kind": choice("type", {"type": 1}), "element": choice("e0", {"e0": 1})},  # no text candidates
])
def test_jev_unusable_answers_raise(monkeypatch, answers):
    monkeypatch.setattr(jev, "_call_jev", lambda payload: {"answers": answers})
    with pytest.raises(jev.JevError):
        jev.pick_action("g", "s", make_screen(), [])


def test_call_jev_http_errors_raise_jeverror(monkeypatch):
    monkeypatch.setattr(config, "JEV_API_KEY", "k")
    monkeypatch.setattr(jev.httpx, "post", lambda *a, **k: SimpleNamespace(status_code=429, text="slow down"))
    with pytest.raises(jev.JevError, match="429"):
        jev._call_jev({})

    def boom(*a, **k):
        raise jev.httpx.ConnectTimeout("timeout")

    monkeypatch.setattr(jev.httpx, "post", boom)
    with pytest.raises(jev.JevError, match="network"):
        jev._call_jev({})


def test_call_jev_without_key_raises(monkeypatch):
    monkeypatch.setattr(config, "JEV_API_KEY", "")
    with pytest.raises(jev.JevError):
        jev._call_jev({})


# ---------- Picker ----------

CLAUDE_ACTION = Action(kind="tap", element_id=1, source="claude")


@pytest.fixture
def brains(monkeypatch):
    """Fake both brains. Set calls['jev'] to an Action or an Exception."""
    calls = {"jev": None, "jev_n": 0, "claude_n": 0}
    monkeypatch.setattr(config, "JEV_API_KEY", "k")
    monkeypatch.setattr(config, "JEV_MIN_CONFIDENCE", 0.75)

    def fake_jev(*a):
        calls["jev_n"] += 1
        if isinstance(calls["jev"], Exception):
            raise calls["jev"]
        return calls["jev"]

    def fake_claude(*a):
        calls["claude_n"] += 1
        return CLAUDE_ACTION

    monkeypatch.setattr(jev, "pick_action", fake_jev)
    monkeypatch.setattr(claude, "pick_action", fake_claude)
    return calls


def choose():
    return picker.choose_action("g", "s", make_screen(), [])


def test_picker_uses_confident_jev(brains):
    brains["jev"] = Action(kind="tap", element_id=0, confidence=0.9, source="jev")
    assert choose().source == "jev"
    assert brains["claude_n"] == 0


def test_picker_low_confidence_falls_back(brains):
    brains["jev"] = Action(kind="tap", element_id=0, confidence=0.5, source="jev")
    assert choose() is CLAUDE_ACTION


def test_picker_jev_error_falls_back(brains):
    brains["jev"] = jev.JevError("down")
    assert choose() is CLAUDE_ACTION


def test_picker_long_text_falls_back(brains):
    brains["jev"] = Action(kind="type", element_id=0, text="x" * 21, confidence=0.99, source="jev")
    assert choose() is CLAUDE_ACTION


def test_picker_missing_key_skips_jev(brains, monkeypatch):
    monkeypatch.setattr(config, "JEV_API_KEY", "")
    assert choose() is CLAUDE_ACTION
    assert brains["jev_n"] == 0
