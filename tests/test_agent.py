"""Agent loop tests. No phone, Mac, or API keys: every outside module is a fake.

We put fake `relay.safety`, `relay.routines` and `relay.brains.*` modules into
sys.modules so these tests pass even before the other agents' files exist.
"""
import sys
import threading
import time
import types

import pytest

from relay import agent, config, router
from relay.types import Action, Element, Screen


class FakeDevice:
    def __init__(self, name="phone"):
        self.name = name
        self.acted: list[Action] = []
        self.called_911 = False
        self.screen = Screen(device=name, app="test", elements=[
            Element(0, "Send", "button", (0, 0, 10, 10), True),
            Element(1, "Password", "input", (0, 20, 10, 30), True),
        ])

    def get_screen(self, with_screenshot=False):
        return self.screen

    def act(self, action, screen):
        self.acted.append(action)

    def emergency_call(self):
        self.called_911 = True


@pytest.fixture
def env(monkeypatch):
    """Fake modules + a fake device. Tests tweak the `env` attributes."""
    dev = FakeDevice()
    e = types.SimpleNamespace(device=dev, actions=[], confirm=None, saved=[], routine=None, replay_ok=True)

    safety = types.ModuleType("relay.safety")
    safety.BLOCKED_PREFIX = "BLOCKED:"
    safety.is_emergency = lambda t: "911" in t or "emergency" in t.lower()
    safety.is_stop = lambda t: t.lower().strip() in ("stop", "cancel", "never mind")
    safety.needs_confirmation = lambda a, s: e.confirm(a, s) if e.confirm else None

    routines = types.ModuleType("relay.routines")
    routines.find = lambda goal: e.routine
    routines.save = lambda goal, device, steps: e.saved.append((goal, device, steps))
    routines.replay = lambda r, d: e.replay_ok

    def choose_action(goal, subgoal, screen, history):
        # Pop the next scripted action; default to tapping forever (for max-steps test).
        return e.actions.pop(0) if e.actions else Action("tap", element_id=0)

    picker = types.ModuleType("relay.brains.picker")
    picker.choose_action = choose_action
    claude = types.ModuleType("relay.brains.claude")
    claude.plan = lambda goal, device: [goal]
    claude.describe_screen = lambda s: "claude description"
    gemini = types.ModuleType("relay.brains.gemini")
    gemini.describe_screen = lambda s: "gemini description"

    import relay, relay.brains
    for name, mod in [("relay.safety", safety), ("relay.routines", routines),
                      ("relay.brains.picker", picker), ("relay.brains.claude", claude),
                      ("relay.brains.gemini", gemini)]:
        monkeypatch.setitem(sys.modules, name, mod)
        parent, attr = name.rsplit(".", 1)
        monkeypatch.setattr(sys.modules[parent], attr, mod, raising=False)
    e.gemini = gemini

    monkeypatch.setattr(router, "get_device", lambda name: dev)
    monkeypatch.setattr(agent, "REPLY_TIMEOUT", 2.0)
    return e


def run(text, timeout=5.0):
    t = agent.Task(text, device_name="phone").start()
    t.wait_until_settled(timeout)
    return t


def test_simple_success_saves_routine(env):
    env.actions = [Action("tap", element_id=0), Action("done", text="Message sent.")]
    t = run("text mom hi")
    assert t.status == "done"
    assert t.say() == "Message sent."
    assert t.narration == ["Tapping Send"]
    assert len(env.device.acted) == 1
    assert env.saved and env.saved[0][0] == "text mom hi"


def test_emergency_bypasses_everything(env):
    env.actions = []  # picker must never be called
    called = []
    sys.modules["relay.brains.picker"].choose_action = lambda *a: called.append(1)
    t = run("call 911")
    assert t.status == "done"
    assert env.device.called_911
    assert not called and not env.device.acted


def test_stop_word_cancels(env):
    t = run("never mind")
    assert t.status == "cancelled"
    assert not env.device.acted


def test_stop_flag_checked_every_step(env):
    # Picker is slow and never finishes; we stop mid-loop.
    def slow(*a):
        time.sleep(0.05)
        return Action("tap", element_id=0)
    sys.modules["relay.brains.picker"].choose_action = slow
    t = agent.Task("do stuff", device_name="phone").start()
    time.sleep(0.2)
    t.stop()
    t.wait_until_settled(3)
    assert t.status == "cancelled"
    n = len(env.device.acted)
    time.sleep(0.2)
    assert len(env.device.acted) == n  # nothing more happened after stopping


def test_max_steps(env, monkeypatch):
    monkeypatch.setattr(config, "MAX_STEPS", 5)
    t = run("loop forever")
    assert t.status == "cancelled"
    assert "limit" in t.say()
    assert len(env.device.acted) == 5
    assert not env.saved


def test_confirmation_yes_resumes(env):
    env.confirm = lambda a, s: "Send this message to Mom?"
    env.actions = [Action("tap", element_id=0), Action("done", text="Sent.")]
    t = run("text mom")
    assert t.status == "waiting"
    assert t.say() == "Send this message to Mom?"
    assert not env.device.acted  # paused BEFORE acting
    env.confirm = None
    t.reply("yes")
    t.wait_until_settled(5)
    assert t.status == "done" and t.say() == "Sent."
    assert len(env.device.acted) == 1


def test_confirmation_no_cancels(env):
    env.confirm = lambda a, s: "Buy this?"
    t = run("buy it")
    t.reply("no")
    t.wait_until_settled(5)
    assert t.status == "cancelled"
    assert not env.device.acted


def test_confirmation_timeout_cancels(env, monkeypatch):
    monkeypatch.setattr(agent, "REPLY_TIMEOUT", 0.2)
    env.confirm = lambda a, s: "Buy this?"
    t = run("buy it")
    assert t.status == "waiting"
    t.wait_until_settled(0.1)
    time.sleep(0.5)
    assert t.status == "cancelled"
    assert "didn't hear back" in t.say()


def test_ask_user_pauses_and_uses_answer(env):
    seen_goals = []
    script = [Action("ask_user", text="Which Mom?"), Action("done", text="Ok.")]
    def pick(goal, subgoal, screen, history):
        seen_goals.append(goal)
        return script.pop(0)
    sys.modules["relay.brains.picker"].choose_action = pick
    t = run("text mom")
    assert t.status == "waiting" and t.say() == "Which Mom?"
    t.reply("the first one")
    t.wait_until_settled(5)
    assert t.status == "done"
    assert "the first one" in seen_goals[-1]


def test_bad_element_id_is_retried(env, monkeypatch):
    real_act = env.device.act
    def act(action, screen):
        if action.element_id == 99:
            raise ValueError("element 99 not on screen")
        real_act(action, screen)
    monkeypatch.setattr(env.device, "act", act)
    seen = []
    script = [Action("tap", element_id=99), Action("tap", element_id=0), Action("done", text="Ok.")]
    def pick(goal, subgoal, screen, history):
        seen.append(list(history))
        return script.pop(0)
    sys.modules["relay.brains.picker"].choose_action = pick
    t = run("tap send")
    assert t.status == "done"
    assert [a.element_id for a in env.device.acted] == [0]
    assert "FAILED" in seen[1][0].reason  # picker saw the failure


def test_bad_element_id_counts_toward_max_steps(env, monkeypatch):
    monkeypatch.setattr(config, "MAX_STEPS", 3)
    env.device.act = lambda a, s: (_ for _ in ()).throw(ValueError("gone"))
    t = run("tap nothing")
    assert t.status == "cancelled" and "limit" in t.say()


def test_never_types_passwords(env):
    env.actions = [Action("type", element_id=1, text="hunter2")]
    t = run("log in")
    assert t.status == "cancelled"
    assert "password" in t.say()
    assert not env.device.acted


def test_routine_replay(env):
    env.routine = object()
    t = run("text mom")
    assert t.status == "done" and "routine" in t.say()
    assert not env.device.acted  # replay handled it, loop never ran


def test_routine_fail_resumes_without_replanning(env):
    env.routine, env.replay_ok = object(), False
    planned = []
    sys.modules["relay.brains.claude"].plan = lambda g, d: planned.append(g) or [g]
    subgoals = []
    def pick(goal, subgoal, screen, history):
        subgoals.append(subgoal)
        return Action("done", text="Fixed it.")
    sys.modules["relay.brains.picker"].choose_action = pick
    t = run("text mom")
    assert t.say() == "Fixed it."
    assert not planned  # continued from the current screen, no fresh plan
    assert "current screen" in subgoals[0]


def test_blocked_is_refusal_not_question(env):
    env.confirm = lambda a, s: "BLOCKED: I won't type a card number."
    env.actions = [Action("tap", element_id=0)]
    t = run("pay")
    assert t.status == "cancelled"  # never paused to ask
    assert t.say() == "I won't type a card number."
    assert not env.device.acted


def test_describe_screen_fast_path(env):
    t = run("what's on my screen")
    assert t.say() == "gemini description"


def test_describe_falls_back_to_claude(env):
    def boom(s):
        raise RuntimeError("no gemini key")
    env.gemini.describe_screen = boom
    t = run("describe the screen")
    assert t.say() == "claude description"


# --- router ---------------------------------------------------------------
def test_router_pick_device():
    assert router.pick_device("open Safari on my Mac", None) == ("mac", "open Safari")
    assert router.pick_device("check email on the laptop", "phone") == ("mac", "check email")
    assert router.pick_device("restart the computer", None)[0] == "mac"
    assert router.pick_device("text mom on the phone", "mac") == ("phone", "text mom")
    assert router.pick_device("open the android settings", "mac")[0] == "phone"
    assert router.pick_device("scroll down", "mac") == ("mac", "scroll down")
    assert router.pick_device("scroll down", None) == (config.DEFAULT_DEVICE, "scroll down")
