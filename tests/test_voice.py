"""voice.py tests: no network, no real ElevenLabs."""
import os
import time

import httpx

from relay import config, voice


class FakeResponse:
    content = b"ID3fake-mp3"

    def raise_for_status(self):
        pass


def test_no_key_returns_none(monkeypatch):
    monkeypatch.setattr(config, "ELEVENLABS_API_KEY", "")
    assert voice.speak("hello") is None


def test_empty_text_returns_none(monkeypatch):
    monkeypatch.setattr(config, "ELEVENLABS_API_KEY", "k")
    assert voice.speak("") is None


def test_elevenlabs_error_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ELEVENLABS_API_KEY", "k")
    monkeypatch.setattr(voice, "AUDIO_DIR", tmp_path)

    def fail(*a, **kw):
        raise httpx.ConnectError("offline")
    monkeypatch.setattr(voice.httpx, "post", fail)
    assert voice.speak("hello") is None
    assert list(tmp_path.iterdir()) == []


def test_speak_saves_file(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ELEVENLABS_API_KEY", "k")
    monkeypatch.setattr(voice, "AUDIO_DIR", tmp_path)
    monkeypatch.setattr(voice.httpx, "post", lambda *a, **kw: FakeResponse())
    audio_id = voice.speak("hello")
    assert audio_id and audio_id.isalnum()
    assert voice.audio_path(audio_id).read_bytes() == b"ID3fake-mp3"


def test_cleanup_keeps_newest(monkeypatch, tmp_path):
    monkeypatch.setattr(voice, "AUDIO_DIR", tmp_path)
    now = time.time()
    for i in range(25):
        f = tmp_path / f"a{i:02d}.mp3"
        f.write_bytes(b"x")
        os.utime(f, (now + i, now + i))  # a24 is the newest
    voice.cleanup(keep=20)
    left = sorted(p.stem for p in tmp_path.glob("*.mp3"))
    assert len(left) == 20 and left[0] == "a05" and left[-1] == "a24"


def test_speak_triggers_cleanup(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ELEVENLABS_API_KEY", "k")
    monkeypatch.setattr(voice, "AUDIO_DIR", tmp_path)
    monkeypatch.setattr(voice, "KEEP_FILES", 3)
    monkeypatch.setattr(voice.httpx, "post", lambda *a, **kw: FakeResponse())
    for _ in range(5):
        voice.speak("hi")
    assert len(list(tmp_path.glob("*.mp3"))) == 3


def test_audio_path_blocks_unsafe_ids(tmp_path, monkeypatch):
    monkeypatch.setattr(voice, "AUDIO_DIR", tmp_path)
    assert voice.audio_path("../etc/passwd") is None
    assert voice.audio_path("missing") is None
