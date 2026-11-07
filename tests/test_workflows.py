import json

import pytest

from relay import agent, config, workflows


@pytest.fixture
def wf_file(tmp_path, monkeypatch):
    path = tmp_path / "workflows.json"
    path.write_text(json.dumps({
        "account_groups": {"macro": ["@DeItaone", "FirstSquawk"], "micro": ["unusual_whales"]},
        "workflows": [{
            "name": "morning outlook",
            "phrases": ["give me my morning outlook", "morning outlook"],
            "steps": [
                {"skill": "news_briefing", "intro": "Big picture.", "params": {"group": "macro", "topic": "macro"}},
                {"skill": "news_briefing", "intro": "Stocks.", "params": {"group": "micro", "topic": "micro"}},
            ],
        }],
    }))
    monkeypatch.setattr(config, "WORKFLOWS_PATH", str(path))
    return path


def test_match_ignores_filler_and_case(wf_file):
    assert workflows.match("Hey Relay, give me my morning outlook!").name == "morning outlook"
    assert workflows.match("give me my morning outlook and text mom") is None


def test_groups_resolve_to_handles(wf_file):
    assert workflows.resolve_accounts({"group": "macro"}) == ["DeItaone", "FirstSquawk"]
    assert workflows.resolve_accounts({"accounts": ["@zerohedge"]}) == ["zerohedge"]
    monkeypatch_default = list(config.NEWS_ACCOUNTS)
    assert workflows.resolve_accounts({"group": "nope"}) == monkeypatch_default


def test_example_file_is_valid_and_used_as_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKFLOWS_PATH", str(tmp_path / "missing.json"))
    names = [w.name for w in workflows.all_workflows()]
    assert "morning briefing" in names and "morning outlook" in names


def test_workflow_runs_steps_in_order(wf_file, monkeypatch):
    task = agent.Task("morning outlook", device_name="phone")
    seen = []

    def fake_news(device, safety, skills, params):
        seen.append(params["group"])
        return f"{params['group']} news."

    monkeypatch.setattr(task, "_news_briefing", fake_news)
    from relay import safety, skills
    out = task._run_workflow(object(), safety, skills, workflows.match("morning outlook"))
    assert seen == ["macro", "micro"]
    assert out == "Big picture. macro news. Stocks. micro news."
