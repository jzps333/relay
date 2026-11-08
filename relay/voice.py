"""Text -> speech with ElevenLabs. Saves audio/<id>.mp3 and returns the id.

If there's no API key (or ElevenLabs fails), speak() returns None and the iPhone
Shortcut falls back to iOS's built-in "Speak Text" action, so Relay never goes silent.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path

import httpx

from relay import config

AUDIO_DIR = Path("audio")
TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
MODEL_ID = "eleven_flash_v2_5"          # ElevenLabs' low-latency model (~75 ms)
DEFAULT_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"  # premade "Rachel": calm and clear, good for driving
KEEP_FILES = 20                         # only the newest 20 mp3s stay on disk
# ElevenLabs audio is quiet on a watch speaker. If ffmpeg is installed, make it louder:
# loudnorm evens out the level, then a limiter stops it clipping. VOICE_BOOST=0 turns it off.
VOICE_BOOST = os.getenv("VOICE_BOOST", "1") == "1"
# compressor squashes the quiet/loud gap, loudnorm pushes the level up, limiter prevents clipping
LOUD_FILTER = ("highpass=f=120,"   # cut rumble a watch speaker can't play anyway
               "acompressor=threshold=0.05:ratio=8:attack=3:release=50:makeup=4,"
               "loudnorm=I=-5:TP=-0.3:LRA=3,volume=3dB,alimiter=limit=0.98:level=false")  # max loudness


def louder(mp3: bytes) -> bytes:
    """Return a louder copy of `mp3`, or the original if ffmpeg isn't there or fails."""
    if not VOICE_BOOST or not shutil.which("ffmpeg"):
        return mp3
    try:
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
             "-af", LOUD_FILTER, "-ar", "44100", "-b:a", "128k", "-f", "mp3", "pipe:1"],
            input=mp3, capture_output=True, timeout=10, check=True,
        ).stdout
        return out or mp3
    except (subprocess.SubprocessError, OSError) as e:
        print(f"[voice] boost skipped: {e}")
        return mp3


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
    if not r.content:  # an empty mp3 would make the Shortcut play silence
        return None
    audio_id = uuid.uuid4().hex[:12]
    try:
        AUDIO_DIR.mkdir(exist_ok=True)
        (AUDIO_DIR / f"{audio_id}.mp3").write_bytes(louder(r.content))
    except OSError as e:  # disk full, no permission... fall back to the phone's voice
        print(f"[voice] couldn't save audio, phone will use its own voice: {e}")
        return None
    cleanup()
    return audio_id


def cleanup(keep: int | None = None) -> None:
    """Delete old mp3s so audio/ doesn't grow forever. Keeps the `keep` newest."""
    if keep is None:  # read it now (not at import) so tests/config can change it
        keep = KEEP_FILES
    try:
        # Newest first, by "last modified" time.
        files = sorted(AUDIO_DIR.glob("*.mp3"), key=lambda f: f.stat().st_mtime, reverse=True)
        for old in files[keep:]:
            old.unlink(missing_ok=True)
    except OSError as e:  # cleanup is housekeeping: never let it break a reply
        print(f"[voice] audio cleanup skipped: {e}")


def audio_path(audio_id: str) -> Path | None:
    """Where an id's mp3 lives, or None if it doesn't exist (or the id looks unsafe)."""
    if not audio_id.isalnum():  # blocks "../../etc/passwd" style ids
        return None
    p = AUDIO_DIR / f"{audio_id}.mp3"
    return p if p.exists() else None
