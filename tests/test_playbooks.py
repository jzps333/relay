"""Playbook tests. The Claude client is faked, so no API key is needed."""
from types import SimpleNamespace

from relay import playbooks
from relay.brains import claude
from relay.types import Element, Screen


def test_tips_for_known_and_unknown_apps():
    assert "paper-plane" in playbooks.tips_for("com.instagram.android")
    assert playbooks.tips_for("com.example.unknown") == ""


def test_doordash_tips_say_never_order():
    assert "NEVER place an order" in playbooks.tips_for("com.dd.doordash")


def _fake_client(monkeypatch):
    """Replace the Anthropic client with one that records the request."""
    seen = {}

    def create(**kwargs):
        seen.update(kwargs)
        block = SimpleNamespace(type="tool_use", name="choose_action",
                                input={"kind": "back", "reason": "test"})
        return SimpleNamespace(content=[block])

    monkeypatch.setattr(claude, "_client",
                        lambda: SimpleNamespace(messages=SimpleNamespace(create=create)))
    return seen


def _screen(app):
    return Screen(device="phone", app=app,
                  elements=[Element(0, "Home", "button", (0, 0, 10, 10), True)])


def test_pick_action_includes_tips_for_known_app(monkeypatch):
    seen = _fake_client(monkeypatch)
    claude.pick_action("text sam", "open DMs", _screen("com.instagram.android"), [])
    text = seen["messages"][0]["content"][0]["text"]
    assert "Tips for this app:\n" in text
    assert playbooks.tips_for("com.instagram.android") in text


def test_pick_action_no_tips_for_unknown_app(monkeypatch):
    seen = _fake_client(monkeypatch)
    claude.pick_action("g", "s", _screen("com.example.unknown"), [])
    assert "Tips for this app" not in seen["messages"][0]["content"][0]["text"]
