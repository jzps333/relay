"""POST /talk/audio (Watch records audio instead of dictating) and relay.stt."""
import httpx
import pytest

from relay import config, server, stt
from tests.test_server import AUTH, FakeTask, client  # noqa: F401  (fixture)

AUDIO = b"\x00" * 5000  # big enough to count as a real recording


@pytest.fixture
def heard(monkeypatch):
    """Set what the fake transcriber 'hears'; records what it was given."""
    state = {"text": "open maps", "calls": []}

    def fake(audio, filename="audio.m4a"):
        state["calls"].append((len(audio), filename))
        return state["text"]

    monkeypatch.setattr(server.stt, "transcribe", fake)
    return state


def test_multipart_upload_starts_task(client, heard):
    r = client.post("/talk/audio", headers=AUTH,
                    files={"file": ("rec.m4a", AUDIO, "audio/mp4")},
                    data={"conversation": "true"}).json()
    assert r["say"].endswith("Anything else?")
    assert r["next"] == "more"
    assert r["heard"] == "open maps"
    assert heard["calls"] == [(len(AUDIO), "rec.m4a")]
    assert FakeTask.instances[-1].text == "open maps"


def test_raw_body_answers_pending_question(client, heard):
    q = client.post("/talk", json={"text": "confirm send"}, headers=AUTH).json()
    assert q["next"] == "reply"
    heard["text"] = "yes"
    r = client.post(f"/talk/audio?session_id={q['session_id']}&conversation=true",
                    content=AUDIO, headers={**AUTH, "Content-Type": "audio/mp4"}).json()
    assert FakeTask.instances[-1].replies == ["yes"]
    assert r["next"] == "more"
    assert r["heard"] == "yes"


def test_octet_stream_body_accepted(client, heard):
    r = client.post("/talk/audio", content=AUDIO,
                    headers={**AUTH, "Content-Type": "application/octet-stream"}).json()
    assert r["heard"] == "open maps" and r["next"] == "more"


def test_tiny_audio_is_silence(client, heard):
    r = client.post("/talk/audio", content=b"\x00" * 10,
                    headers={**AUTH, "Content-Type": "audio/mp4"}).json()
    assert "didn't catch" in r["say"] and r["next"] == "more"
    assert r["heard"] == ""
    assert heard["calls"] == []  # never sent to STT


def test_empty_multipart_is_silence(client, heard):
    r = client.post("/talk/audio", headers=AUTH,
                    files={"file": ("rec.m4a", b"", "audio/mp4")}).json()
    assert "didn't catch" in r["say"] and r["heard"] == ""


def test_audio_requires_token(client, heard):
    r = client.post("/talk/audio", content=AUDIO, headers={"Content-Type": "audio/mp4"})
    assert r.status_code == 401


# --- relay.stt -------------------------------------------------------------

class _FakeResp:
    def __init__(self, text):
        self._text = text

    def raise_for_status(self):
        pass

    def json(self):
        return {"text": self._text}


class _FakeGemini:
    def __init__(self, text=None, exc=None):
        self.text, self.exc, self.calls = text, exc, []
        self.models = self

    def generate_content(self, model, contents, config):
        self.calls.append(contents)
        if self.exc:
            raise self.exc
        return type("R", (), {"text": self.text})()


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setattr(config, "ELEVENLABS_API_KEY", "el-key")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "g-key")


def test_stt_uses_elevenlabs_first(monkeypatch, keys):
    seen = {}

    def fake_post(url, **kw):
        seen.update(url=url, **kw)
        return _FakeResp("  hello there \n")

    monkeypatch.setattr(httpx, "post", fake_post)
    gem = _FakeGemini(text="wrong")
    monkeypatch.setattr("relay.brains.gemini._client", lambda: gem)
    assert stt.transcribe(AUDIO, "rec.m4a") == "hello there"
    assert seen["url"] == stt.ELEVENLABS_STT_URL
    assert seen["headers"] == {"xi-api-key": "el-key"}
    assert seen["data"] == {"model_id": "scribe_v1"}
    assert seen["files"]["file"][1] == AUDIO
    assert gem.calls == []


def test_stt_falls_back_to_gemini(monkeypatch, keys):
    def boom(*a, **kw):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "post", boom)
    gem = _FakeGemini(text=" turn on the lights ")
    monkeypatch.setattr("relay.brains.gemini._client", lambda: gem)
    assert stt.transcribe(AUDIO, "rec.wav") == "turn on the lights"
    part = gem.calls[0][0]
    assert part.inline_data.mime_type == "audio/wav"
    assert gem.calls[0][1] == stt.GEMINI_PROMPT


def test_stt_no_elevenlabs_key_goes_to_gemini(monkeypatch, keys):
    monkeypatch.setattr(config, "ELEVENLABS_API_KEY", "")
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: pytest.fail("should not call ElevenLabs"))
    monkeypatch.setattr("relay.brains.gemini._client", lambda: _FakeGemini(text="hi"))
    assert stt.transcribe(AUDIO) == "hi"


def test_stt_total_failure_returns_empty(monkeypatch, keys):
    def boom(*a, **kw):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "post", boom)
    monkeypatch.setattr("relay.brains.gemini._client", lambda: _FakeGemini(exc=RuntimeError("quota")))
    assert stt.transcribe(AUDIO) == ""


def test_stt_empty_audio_returns_empty():
    assert stt.transcribe(b"") == ""


@pytest.mark.parametrize("name,mime", [("a.m4a", "audio/mp4"), ("a.WAV", "audio/wav"),
                                       ("a.mp3", "audio/mpeg"), ("a.caf", "audio/x-caf"),
                                       ("noext", "audio/mp4")])
def test_guess_mime(name, mime):
    assert stt.guess_mime(name) == mime
