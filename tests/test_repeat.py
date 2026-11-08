"""'Repeat that': the server answers from memory, instantly, without starting a task."""
import pytest

from relay import server
from tests.test_server import AUTH, FakeTask, client  # noqa: F401  (client is a pytest fixture)


@pytest.mark.parametrize("said", ["repeat that", "Say that again.", "what did you say?",
                                  "hey relay, can you repeat that please", "Come again"])
def test_is_repeat(said):
    assert server.is_repeat(said)


@pytest.mark.parametrize("said", ["repeat the last song", "say hi to mom", "what did mom say"])
def test_not_repeat(said):
    assert not server.is_repeat(said)


def test_repeat_returns_last_final_answer(client):
    client.post("/command", json={"text": "open maps"}, headers=AUTH)
    r = client.post("/command", json={"text": "say that again"}, headers=AUTH)
    assert r.json() == {"say": "Did: open maps", "audio_url": None, "needs_reply": False,
                        "next": "done", "session_id": ""}
    assert len(FakeTask.instances) == 1  # no new task for the repeat


def test_repeat_before_anything_was_said(client):
    r = client.post("/command", json={"text": "repeat that"}, headers=AUTH)
    assert r.json()["say"] == "I haven't said anything yet."
    assert FakeTask.instances == []


def test_questions_and_progress_are_not_remembered(client):
    client.post("/command", json={"text": "open maps"}, headers=AUTH)          # final: remembered
    client.post("/command", json={"text": "confirm send"}, headers=AUTH)       # a question, not final
    r = client.post("/command", json={"text": "repeat that"}, headers=AUTH)
    assert r.json()["say"] == "Did: open maps"


def test_repeat_does_not_stop_running_task(client):
    client.post("/command", json={"text": "slow thing"}, headers=AUTH)
    client.post("/command", json={"text": "what did you say"}, headers=AUTH)
    assert FakeTask.instances[0].stopped is False


def test_repeat_makes_audio(client, monkeypatch):
    spoken = []
    monkeypatch.setattr(server.voice, "speak", lambda text: spoken.append(text) or "abc123")
    monkeypatch.setattr(server, "_last_answer", "Paused.")
    r = client.post("/command", json={"text": "repeat"}, headers=AUTH)
    assert spoken == ["Paused."]
    assert r.json()["audio_url"].endswith("/audio/abc123.mp3")
