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
        self.original_text = text  # same attribute name as the real agent.Task
        self.device_name = device_name or "phone"
        self.status = "new"
        self.replies = []
        self.stopped = False
        FakeTask.instances.append(self)

    @property
    def finished(self):
        return self.status in ("done", "cancelled", "error")

    def start(self):
        if self.original_text.startswith("confirm"):
            self.status = "waiting"
        elif self.original_text.startswith("slow"):
            self.status = "running"   # still going when the server stops waiting
        else:
            self.status = "done"
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
        return f"Did: {self.original_text}"


@pytest.fixture
def client(monkeypatch):
    FakeTask.instances = []
    monkeypatch.setattr(config, "RELAY_TOKEN", TOKEN)
    monkeypatch.setattr(server.agent, "Task", FakeTask)
    monkeypatch.setattr(voice, "speak", lambda text: None)  # no ElevenLabs
    monkeypatch.setattr(server, "_tasks", {})
    monkeypatch.setattr(server, "_current", None)
    monkeypatch.setattr(server, "_last_device", None)
    monkeypatch.setattr(server, "_delivered", set())
    monkeypatch.setattr(server, "_started", {})
    monkeypatch.setattr(server, "_gave_up", set())
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
    assert body == {"say": "Did: open maps", "audio_url": "", "needs_reply": False, "next": "done", "session_id": "t0"}


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
    # Absolute URL (TestClient's base URL is http://testserver), so the Shortcut can fetch it as-is.
    assert body["audio_url"] == "http://testserver/audio/abc123.mp3"
    r = client.get("/audio/abc123.mp3", headers=AUTH)
    assert r.status_code == 200 and r.content == b"ID3fake"
    assert client.get("/audio/missing.mp3", headers=AUTH).status_code == 404


def test_voice_without_key_returns_none(monkeypatch):
    monkeypatch.setattr(config, "ELEVENLABS_API_KEY", "")
    assert voice.speak("hello") is None


def test_audio_url_uses_the_address_the_phone_used(client, monkeypatch):
    monkeypatch.setattr(voice, "speak", lambda text: "abc123")
    lan = TestClient(server.app, base_url="http://192.168.1.20:8000")
    body = lan.post("/command", json={"text": "hi"}, headers=AUTH).json()
    assert body["audio_url"] == "http://192.168.1.20:8000/audio/abc123.mp3"


def test_say_is_plain_text(client, monkeypatch):
    monkeypatch.setattr(FakeTask, "say", lambda self: "**Sent** it:\n- to `Mom`\n# done")
    body = client.post("/command", json={"text": "x"}, headers=AUTH).json()
    assert body["say"] == "Sent it: to Mom done"
    assert body["audio_url"] == ""


def test_plain_text_never_empty():
    assert server.plain_text("") == "Done."
    assert server.plain_text(None) == "Done."
    assert server.plain_text("See [the docs](http://x.y)") == "See the docs"


# --- /health -----------------------------------------------------------------
@pytest.fixture
def fresh_health(monkeypatch):
    monkeypatch.setattr(server, "_health_thread", None)
    monkeypatch.setattr(server, "_health_result", {"connected": False})


def test_health_no_auth_and_connected(client, monkeypatch, fresh_health):
    monkeypatch.setattr(config, "DEFAULT_DEVICE", "phone")
    monkeypatch.setattr(server.router, "get_device", lambda name: object())
    r = client.get("/health")  # no Authorization header on purpose
    assert r.status_code == 200
    assert r.json() == {"ok": True, "device_default": "phone", "emulator_connected": True}


def test_health_when_emulator_errors(client, monkeypatch, fresh_health):
    def boom(name):
        raise RuntimeError("no adb")
    monkeypatch.setattr(server.router, "get_device", boom)
    body = client.get("/health").json()
    assert body["ok"] is True and body["emulator_connected"] is False


def test_health_is_fast_when_emulator_hangs(client, monkeypatch, fresh_health):
    import threading
    import time
    release = threading.Event()
    monkeypatch.setattr(server.router, "get_device", lambda name: release.wait(5))
    monkeypatch.setattr(server, "HEALTH_DEVICE_TIMEOUT", 0.1)
    monkeypatch.setattr(server.emulator_connected, "__defaults__", (0.1,))
    start = time.monotonic()
    body = client.get("/health").json()
    release.set()  # let the stuck helper thread finish
    assert body["emulator_connected"] is False
    assert time.monotonic() - start < 1.0


# --- launcher banner ---------------------------------------------------------
def test_banner_shows_url_and_hides_token():
    text = server.startup_banner(["192.168.1.20"], "supersecrettoken")
    assert "http://192.168.1.20:8000/command" in text
    assert "http://192.168.1.20:8000/health" in text
    assert "supe\u2026" in text and "supersecrettoken" not in text
    assert "WARNING" not in text


def test_banner_warns_on_weak_token():
    for weak in ("change-me", "", "pick-a-long-random-string"):
        assert "WARNING" in server.startup_banner(["10.0.0.5"], weak)
    assert "Couldn't find a LAN IP" in server.startup_banner([], "x" * 30)


def test_lan_ips_never_raises():
    ips = server.lan_ips()
    assert isinstance(ips, list) and all(not ip.startswith("127.") for ip in ips)


def test_token_forgives_paste_mistakes(monkeypatch):
    from relay import server
    monkeypatch.setattr(server.config, "RELAY_TOKEN", "e38Yabc")
    for header in ["Bearer e38Yabc", "bearer e38Yabc", "Bearer  e38Yabc ", "Bearer\ne38Yabc", "e38Yabc"]:
        server.require_token(header)  # no exception


def test_token_rejection_explains_why(monkeypatch, capsys):
    import pytest
    from fastapi import HTTPException
    from relay import server
    monkeypatch.setattr(server.config, "RELAY_TOKEN", "e38Yabc")
    with pytest.raises(HTTPException):
        server.require_token("Bearer 123456789\ne38Yabc")
    out = capsys.readouterr().out
    assert "expected 7" in out and "e38Yabc" not in out


def test_next_tells_the_shortcut_what_to_do(client):
    r = client.post("/command", json={"text": "slow task"}, headers=AUTH).json()
    assert r["next"] == "wait" and r["needs_reply"] is False
    r = client.post("/command", json={"text": "confirm send"}, headers=AUTH).json()
    assert r["next"] == "reply" and r["needs_reply"] is True
    r = client.post("/reply", json={"session_id": r["session_id"], "text": "yes"}, headers=AUTH).json()
    assert r["next"] == "done"
