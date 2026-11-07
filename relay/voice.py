"""Text -> speech with ElevenLabs. Saves audio/<id>.mp3 and returns the id.

If there's no API key (or ElevenLabs fails), speak() returns None and the iPhone
Shortcut falls back to iOS's built-in "Speak Text" action, so Relay never goes silent.
"""
from __future__ import annotations

import uuid
from pathlib import Path

import httpx

from relay import config

AUDIO_DIR = Path("audio")
TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
MODEL_ID = "eleven_flash_v2_5"          # ElevenLabs' low-latency model (~75 ms)
DEFAULT_VOICE_ID = "JBFqnCBsd6RMkjVDRZzb"  # premade "George" voice, used if none is set


def speak(text: str) -> str | None:
    """Make an mp3 of `text`. Returns its id (file is audio/<id>.mp3) or None."""
    if not text or not config.ELEVENLABS_API_KEY:
        return None
    voice_id = config.ELEVENLABS_VOICE_ID or DEFAULT_VOICE_ID
    try:
        r = httpx.post(
            TTS_URL.format(voice_id=voice_id),
            params={"output_format": "mp3_44100_128"},
            headers={"xi-api-key": config.ELEVENLABS_API_KEY, "Accept": "audio/mpeg"},
            json={"text": text, "model_id": MODEL_ID},
            timeout=15,
        )
        r.raise_for_status()
    except httpx.HTTPError as e:
        print(f"[voice] ElevenLabs failed, phone will use its own voice: {e}")
        return None
    AUDIO_DIR.mkdir(exist_ok=True)
    audio_id = uuid.uuid4().hex[:12]
    (AUDIO_DIR / f"{audio_id}.mp3").write_bytes(r.content)
    return audio_id


def audio_path(audio_id: str) -> Path | None:
    """Where an id's mp3 lives, or None if it doesn't exist (or the id looks unsafe)."""
    if not audio_id.isalnum():  # blocks "../../etc/passwd" style ids
        return None
    p = AUDIO_DIR / f"{audio_id}.mp3"
    return p if p.exists() else None
