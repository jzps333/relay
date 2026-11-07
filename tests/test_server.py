"""Server route tests with a fake Task (no devices, no AI, no ElevenLabs)."""
import pytest
from fastapi.testclient import TestClient

from relay import config, server, voice

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class FakeTask:
    """Behaves like agent.Task: 'confirm ...' commands pause on a question."""
    instances = []

    def __init__(self, text, device_name=None, last_device=None):
        self.id = f"t{len(FakeTask.instances)}"
        self.text = text
        self.device_name = device_name or "phone"
        self.status = "new"
        self.replies = []
        self.stopped = False
        FakeTask.instances.append(self)

    @property
    def finished(self):
        return self.status in ("done", "cancelled", "error")

    def start(self):
        self.status = "waiting" if self.text.startswith("confirm") else "done"
        return self

    def wait_until_settled(self, timeout):
        pass

    def reply(self, text):
        self.replies.append(text)
        self.status = "done"

    def stop(self):
        self.stopped = True
        self.status = "cancelled"

    def say(self):
        if self.status == "waiting":
            return "Are you sure?"
        if self.status == "cancelled":
            return "Okay, I stopped."
        return f"Did: {self.text}"


@pytest.fixture
def client(monkeypatch):
    FakeTask.instances = []
    monkeypatch.setattr(config, "RELAY_TOKEN", TOKEN)
    monkeypatch.setattr(server.agent, "Task", FakeTask)
    monkeypatch.setattr(voice, "speak", lambda text: None)  # no ElevenLabs
    monkeypatch.setattr(server, "_tasks", {})
    monkeypatch.setattr(server, "_current", None)
    monkeypatch.setattr(server, "_last_device", None)
    return TestClient(server.app)


def test_auth_required(client):
    assert client.post("/command", json={"text": "hi"}).status_code == 401
    bad = {"Authorization": "Bearer nope"}
    assert client.post("/command", json={"text": "hi"}, headers=bad).status_code == 401
    assert client.post("/stop").status_code == 401
    assert client.get("/audio/abc.mp3").status_code == 401


def test_command_returns_final_answer(client):
    r = client.post("/command", json={"text": "open maps", "device": None}, headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body == {"say": "Did: open maps", "audio_url": None, "needs_reply": False, "session_id": "t0"}


def test_bad_device_rejected(client):
    r = client.post("/command", json={"text": "x", "device": "toaster"}, headers=AUTH)
    assert r.status_code == 400


def test_confirmation_flow(client):
    r = client.post("/command", json={"text": "confirm buy"}, headers=AUTH).json()
    assert r["needs_reply"] is True and r["say"] == "Are you sure?"
    r2 = client.post("/reply", json={"session_id": r["session_id"], "text": "yes"}, headers=AUTH).json()
    assert r2["needs_reply"] is False
    assert FakeTask.instances[0].replies == ["yes"]


def test_reply_unknown_session(client):
    r = client.post("/reply", json={"session_id": "nope", "text": "yes"}, headers=AUTH)
    assert r.status_code == 404


def test_stop(client):
    client.post("/command", json={"text": "confirm buy"}, headers=AUTH)
    r = client.post("/stop", headers=AUTH).json()
    assert r["say"] == "Stopping."
    assert FakeTask.instances[0].stopped
    assert client.post("/stop", headers=AUTH).json()["say"] == "Nothing is running."


def test_new_command_stops_old_one(client):
    client.post("/command", json={"text": "confirm buy"}, headers=AUTH)
    client.post("/command", json={"text": "open maps"}, headers=AUTH)
    assert FakeTask.instances[0].stopped


def test_audio_url_and_file(client, monkeypatch, tmp_path):
    monkeypatch.setattr(voice, "AUDIO_DIR", tmp_path)
    (tmp_path / "abc123.mp3").write_bytes(b"ID3fake")
    monkeypatch.setattr(voice, "speak", lambda text: "abc123")
    body = client.post("/command", json={"text": "hi"}, headers=AUTH).json()
    assert body["audio_url"].endswith("/audio/abc123.mp3")
    r = client.get("/audio/abc123.mp3", headers=AUTH)
    assert r.status_code == 200 and r.content == b"ID3fake"
    assert client.get("/audio/missing.mp3", headers=AUTH).status_code == 404


def test_voice_without_key_returns_none(monkeypatch):
    monkeypatch.setattr(config, "ELEVENLABS_API_KEY", "")
    assert voice.speak("hello") is None
