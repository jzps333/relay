"""Relay's web server. The iPhone Shortcut talks to these routes.

Run it:   uvicorn relay.server:app --host 0.0.0.0 --port 8000

POST /command {"text", "device"}  -> start a task, wait up to ~25 s, return what to say
POST /reply   {"session_id","text"} -> answer a yes/no (or any) question the task asked
POST /stop                           -> stop whatever is running
GET  /audio/{id}.mp3                 -> ElevenLabs audio for a reply

Every route needs the header  Authorization: Bearer <RELAY_TOKEN>.
"""
from __future__ import annotations

import secrets
import threading

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from relay import agent, config, voice

# How long one HTTP request waits for the task. iOS Shortcuts give up around 30-60 s,
# so 25 s leaves headroom while usually being long enough to get the final answer.
WAIT_SECONDS = 25.0

app = FastAPI(title="Relay")

# --- state shared between requests -----------------------------------------
_lock = threading.Lock()
_tasks: dict[str, "agent.Task"] = {}   # session_id -> Task
_current: "agent.Task | None" = None   # the one task allowed to run at a time
_last_device: str | None = None        # so "scroll down" goes to the device you just used


# --- auth ------------------------------------------------------------------
def require_token(authorization: str = Header(default="")) -> None:
    expected = f"Bearer {config.RELAY_TOKEN}"
    # compare_digest avoids leaking the token through timing differences
    if not secrets.compare_digest(authorization.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="bad or missing token")


# --- request bodies --------------------------------------------------------
class CommandIn(BaseModel):
    text: str
    device: str | None = None   # "phone" | "mac" | null (null = let the router guess)


class ReplyIn(BaseModel):
    session_id: str
    text: str


# --- helpers ---------------------------------------------------------------
def _respond(task, request: Request) -> dict:
    """Turn a task's current state into the JSON the Shortcut expects."""
    say = task.say()
    audio_id = voice.speak(say)
    audio_url = f"{str(request.base_url).rstrip('/')}/audio/{audio_id}.mp3" if audio_id else None
    return {
        "say": say,
        "audio_url": audio_url,
        "needs_reply": task.status == "waiting",
        "session_id": task.id,
    }


# --- routes ----------------------------------------------------------------
@app.post("/command", dependencies=[Depends(require_token)])
def command(body: CommandIn, request: Request) -> dict:
    global _current, _last_device
    if body.device not in (None, "phone", "mac"):
        raise HTTPException(status_code=400, detail="device must be phone, mac or null")
    with _lock:
        if _current is not None and not _current.finished:
            _current.stop()  # a new command replaces the old one
        task = agent.Task(body.text, device_name=body.device, last_device=_last_device)
        _tasks[task.id] = task
        _current = task
        _last_device = task.device_name
    task.start()
    task.wait_until_settled(WAIT_SECONDS)
    return _respond(task, request)


@app.post("/reply", dependencies=[Depends(require_token)])
def reply(body: ReplyIn, request: Request) -> dict:
    task = _tasks.get(body.session_id)
    if task is None:
        raise HTTPException(status_code=404, detail="unknown session_id")
    if task.status == "waiting":
        task.reply(body.text)
        task.wait_until_settled(WAIT_SECONDS)
    elif not task.finished:
        # Still working from an earlier timeout: wait a bit more and report progress.
        task.wait_until_settled(WAIT_SECONDS)
    return _respond(task, request)


@app.post("/stop", dependencies=[Depends(require_token)])
def stop() -> dict:
    task = _current
    if task is not None and not task.finished:
        task.stop()
        return {"say": "Stopping.", "audio_url": None, "needs_reply": False, "session_id": task.id}
    return {"say": "Nothing is running.", "audio_url": None, "needs_reply": False, "session_id": ""}


@app.get("/audio/{audio_id}.mp3", dependencies=[Depends(require_token)])
def audio(audio_id: str) -> FileResponse:
    path = voice.audio_path(audio_id)
    if path is None:
        raise HTTPException(status_code=404, detail="no such audio")
    return FileResponse(path, media_type="audio/mpeg")
