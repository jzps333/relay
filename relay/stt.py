"""Speech-to-text for audio the Watch/iPhone Shortcut records ("Record Audio" action).

Apple's "Dictate Text" is broken on the Watch, so the Shortcut POSTs the raw recording
to /talk/audio and we transcribe it here.

Primary: ElevenLabs Scribe. Fallback: Gemini (same client as relay/brains/gemini.py).
`transcribe` never raises: on total failure it returns "" (treated as silence).
"""
from __future__ import annotations

import httpx

from relay import config

ELEVENLABS_STT_URL = "https://api.elevenlabs.io/v1/speech-to-text"
GEMINI_PROMPT = "Transcribe this speech exactly. Reply with only the words spoken."

_MIME = {
    "m4a": "audio/mp4",
    "mp4": "audio/mp4",
    "wav": "audio/wav",
    "mp3": "audio/mpeg",
    "caf": "audio/x-caf",
}


def guess_mime(filename: str) -> str:
    ext = (filename or "").rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""
    return _MIME.get(ext, "audio/mp4")


def _elevenlabs(audio: bytes, filename: str) -> str:
    if not config.ELEVENLABS_API_KEY:
        raise RuntimeError("no ELEVENLABS_API_KEY")
    r = httpx.post(
        ELEVENLABS_STT_URL,
        headers={"xi-api-key": config.ELEVENLABS_API_KEY},
        files={"file": (filename, audio, guess_mime(filename))},
        data={"model_id": "scribe_v1"},
        timeout=20,
    )
    r.raise_for_status()
    return (r.json().get("text") or "").strip()


def _gemini(audio: bytes, filename: str) -> str:
    if not config.GEMINI_API_KEY:
        raise RuntimeError("no GEMINI_API_KEY")
    from google.genai import types

    from relay.brains import gemini

    response = gemini._client().models.generate_content(
        model=config.GEMINI_MODEL,
        contents=[types.Part.from_bytes(data=audio, mime_type=guess_mime(filename)), GEMINI_PROMPT],
        config=types.GenerateContentConfig(max_output_tokens=2048, temperature=0.0)  # room for thinking tokens,
    )
    return (response.text or "").strip()


def transcribe(audio: bytes, filename: str = "audio.m4a") -> str:
    """Return the words spoken in `audio`, or "" if nothing could be transcribed."""
    if not audio:
        return ""
    for name, fn in (("elevenlabs", _elevenlabs), ("gemini", _gemini)):
        try:
            text = fn(audio, filename)
            if text:
                return text
            print(f"[stt] {name} returned no text", flush=True)
        except Exception as e:  # noqa: BLE001  (never raise to the caller)
            print(f"[stt] {name} failed: {e!r}", flush=True)
    return ""
