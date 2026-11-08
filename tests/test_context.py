"""Follow-ups: the last summary is remembered for 15 minutes and given to the screen agent."""
import time

from relay import context, skills
from relay.types import Action
from tests.test_agent import env, run  # noqa: F401  (env is a pytest fixture)


def test_remember_and_get():
    context.remember("Two emails: Prof. Lee moved the exam, Chase statement.", ["Prof. Lee", "Exam moved"], "email")
    ctx = context.get()
    assert ctx["summary"].startswith("Two emails")
    assert ctx["lines"] == ["Prof. Lee", "Exam moved"]
    assert ctx["source"] == "email"


def test_expires_after_15_minutes():
    context.remember("Old summary.")
    now = time.time()
    assert context.get(now=now + 14 * 60) is not None
    assert context.get(now=now + 16 * 60) is None
    assert context.add_to_goal("reply to the second one", now=now + 16 * 60) == "reply to the second one"


def test_lines_are_capped_and_empty_summary_ignored():
    context.remember("S", [f"line {i}" for i in range(1000)])
    assert len(context.get()["lines"]) == context.MAX_LINES
    context.clear()
    context.remember("   ", ["x"])
    assert context.get() is None


def test_add_to_goal_uses_summary_only_and_caps_length():
    context.remember("Mom texted: where are you? " * 100, ["RAW LINE SHOULD NOT APPEAR"])
    goal = context.add_to_goal("reply to Mom saying I'll be late")
    assert goal.startswith("Context from what you just told the user (they may refer to it): Mom texted")
    assert goal.endswith("reply to Mom saying I'll be late")
    assert "RAW LINE" not in goal
    assert len(goal) < context.MAX_GOAL_CHARS + 200


def test_no_context_leaves_goal_alone():
    assert context.add_to_goal("text mom hi") == "text mom hi"


# ---- in the agent ----

def _capture_goals(env):
    """Record the goal the picker and planner see."""
    import sys
    seen = {"picker": [], "plan": []}
    picker = sys.modules["relay.brains.picker"]
    claude = sys.modules["relay.brains.claude"]
    old_choose = picker.choose_action
    picker.choose_action = lambda goal, sub, screen, hist: (seen["picker"].append(goal), old_choose(goal, sub, screen, hist))[1]
    claude.plan = lambda goal, device: (seen["plan"].append(goal), [goal])[1]
    return seen


def test_screen_agent_goal_gets_fresh_context(env):
    seen = _capture_goals(env)
    context.remember("You have 2 emails: first from Chase, second from Professor Lee about the exam.", ["raw"], "email")
    env.actions = [Action("tap", element_id=0), Action("done", text="Replied.")]
    t = run("reply to the second one saying I'll be late")
    assert t.status == "done"
    for goal in (seen["plan"][0], seen["picker"][0]):
        assert "Context from what you just told the user" in goal
        assert "second from Professor Lee" in goal
        assert goal.endswith("reply to the second one saying I'll be late")
    assert env.saved == []  # depends on the summary, so never saved as a routine


def test_screen_agent_goal_without_context_is_unchanged(env):
    seen = _capture_goals(env)
    env.actions = [Action("tap", element_id=0), Action("done", text="Sent.")]
    run("text mom hi")
    assert seen["picker"][0] == "text mom hi"
    assert len(env.saved) == 1


def test_email_summary_stores_context(env, monkeypatch):
    env.device.start_app = lambda name: None
    monkeypatch.setattr(skills, "find_blocker", lambda device, wait=2.0: None)
    monkeypatch.setattr(skills, "collect_inbox", lambda device: ["Professor Lee", "Exam moved"])
    monkeypatch.setattr(skills, "summarize_inbox", lambda lines, tf: "One email from Professor Lee.")
    t = run("summarize my emails")
    assert t.result == "One email from Professor Lee."
    ctx = context.get()
    assert (ctx["source"], ctx["summary"], ctx["lines"]) == ("email", "One email from Professor Lee.",
                                                             ["Professor Lee", "Exam moved"])


def test_news_briefing_stores_context(env, monkeypatch):
    monkeypatch.setattr(skills, "find_blocker", lambda device, wait=2.0: None)
    monkeypatch.setattr(skills, "collect_x", lambda device, feed, accounts: ["@DeItaone: Fed holds"])
    monkeypatch.setattr(skills, "summarize_news", lambda lines, topic: "The Fed held rates.")
    t = run("market news")
    assert t.result == "The Fed held rates."
    assert context.get()["source"] == "news"
