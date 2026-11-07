"""Relay's web server. The iPhone Shortcut talks to these routes.

Run it:   python -m relay.server
          (prints the URL to put in the iPhone Shortcut, then starts uvicorn on 0.0.0.0:8000)
   or:    uvicorn relay.server:app --host 0.0.0.0 --port 8000

GET  /health                         -> {"ok": true, ...}  (no token needed; test it in Safari)
POST /command {"text", "device"}  -> start a task, wait up to ~25 s, return what to say
POST /reply   {"session_id","text"} -> answer a yes/no (or any) question the task asked
POST /stop                           -> stop whatever is running
GET  /audio/{id}.mp3                 -> ElevenLabs audio for a reply

Every route except /health needs the header  Authorization: Bearer <RELAY_TOKEN>.
"""
from __future__ import annotations

import re
import secrets
import socket
import threading

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from relay import agent, config, router, voice

# How long one HTTP request waits for the task. iOS Shortcuts give up around 30-60 s,
# so 25 s leaves headroom while usually being long enough to get the final answer.
WAIT_SECONDS = 25.0
PORT = 8000
HEALTH_DEVICE_TIMEOUT = 1.5  # /health never waits longer than this for the emulator

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
def plain_text(text: str | None) -> str:
    """Make `text` safe for iOS "Speak Text": no markdown symbols, one line, never empty.

    The AI sometimes answers with **bold**, `code`, # headings or "- " bullets.
    Speak Text would read those out loud ("asterisk asterisk"), so we strip them.
    """
    t = str(text or "")
    t = re.sub(r"```.*?```", " ", t, flags=re.S)              # drop code blocks
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)           # [label](link) -> label
    t = re.sub(r"(?m)^\s*(?:#+|[-*\u2022]|\d+[.)])\s+", "", t)  # headings, bullets, "1."
    t = re.sub(r"[*`#>|]", "", t)                              # leftover markdown symbols
    t = re.sub(r"\s+", " ", t).strip()                         # newlines/tabs -> one space
    return t or "Done."


def _respond(task, request: Request) -> dict:
    """Turn a task's current state into the JSON the Shortcut expects.

    "say" is always plain text. "audio_url" is a full URL like
    http://192.168.1.20:8000/audio/ab12cd34ef56.mp3 (built from the address the
    phone used to reach us) or None when there's no ElevenLabs audio.
    """
    say = plain_text(task.say())
    audio_id = voice.speak(say)
    audio_url = f"{str(request.base_url).rstrip('/')}/audio/{audio_id}.mp3" if audio_id else None
    return {
        "say": say,
        "audio_url": audio_url,
        "needs_reply": task.status == "waiting",
        "session_id": task.id,
    }


# The emulator check runs in a helper thread so /health can give up after a
# moment instead of hanging. We keep one thread around so repeated /health
# calls don't pile up new threads while ADB is stuck.
_health_thread: threading.Thread | None = None
_health_result = {"connected": False}


def _check_emulator() -> None:
    try:
        router.get_device("phone")  # connects once, then it's cached (cheap)
        _health_result["connected"] = True
    except Exception:  # no emulator, no ADB, missing library... all mean "not connected"
        _health_result["connected"] = False


def emulator_connected(timeout: float = HEALTH_DEVICE_TIMEOUT) -> bool:
    """Best-effort "is the Android emulator reachable?". Never raises, never waits long."""
    global _health_thread
    try:
        if _health_thread is None or not _health_thread.is_alive():
            _health_result["connected"] = False
            _health_thread = threading.Thread(target=_check_emulator, daemon=True)
            _health_thread.start()
        _health_thread.join(timeout)
        if _health_thread.is_alive():  # still trying after `timeout` seconds
            return False
        return bool(_health_result["connected"])
    except Exception:
        return False


# --- routes ----------------------------------------------------------------
@app.get("/health")
def health() -> dict:
    """No token needed: open http://<mac-ip>:8000/health in Safari to test the connection."""
    return {
        "ok": True,
        "device_default": config.DEFAULT_DEVICE,
        "emulator_connected": emulator_connected(),
    }


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


# --- launcher: python -m relay.server ---------------------------------------
def lan_ips() -> list[str]:
    """The Mac's Wi-Fi/Ethernet IP addresses (like 192.168.1.20), best effort.

    Trick: "connecting" a UDP socket sends no packets, but makes the OS pick the
    network card it would use to reach the internet, and we read that card's IP.
    We also ask for every IP tied to the computer's hostname.
    """
    ips: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))  # no data is actually sent
            ips.append(s.getsockname()[0])
    except OSError:
        pass  # no network at all
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.append(info[4][0])
    except OSError:
        pass
    # Drop duplicates and 127.x (that's "this computer", useless to the phone).
    return [ip for i, ip in enumerate(ips) if ip not in ips[:i] and not ip.startswith("127.")]


def token_hint(token: str) -> str:
    """Show only the start of the token, so it isn't printed in full on screen."""
    return token[:4] + "\u2026" if token else "(empty)"


def token_is_weak(token: str) -> bool:
    return token.strip() in ("", "change-me", "pick-a-long-random-string")


def startup_banner(ips: list[str], token: str, port: int = PORT) -> str:
    """The text printed before the server starts (a separate function so tests can check it)."""
    lines = ["", "=" * 60, "Relay server", "=" * 60]
    if ips:
        lines.append("Mac's IP address(es) on this network: " + ", ".join(ips))
        lines.append("Put this URL in the iPhone Shortcut (Get Contents of URL):")
        for ip in ips:
            lines.append(f"    http://{ip}:{port}/command")
        lines.append("Test from the iPhone in Safari (must say \"ok\": true):")
        lines.append(f"    http://{ips[0]}:{port}/health")
    else:
        lines.append("Couldn't find a LAN IP. Is the Mac on Wi-Fi?")
        lines.append("Find it in System Settings -> Wi-Fi -> Details -> IP address.")
    lines.append(f"Token: RELAY_TOKEN from .env (starts with {token_hint(token)})")
    lines.append(f"    Shortcut header:  Authorization: Bearer {token_hint(token)}")
    if token_is_weak(token):
        lines += [
            "",
            "!" * 60,
            "!! WARNING: RELAY_TOKEN is empty or still the placeholder.",
            "!! Anyone on your Wi-Fi could control your phone and Mac.",
            "!! Put a long random value in .env, e.g. run:",
            "!!     python -c \"import secrets; print(secrets.token_urlsafe(24))\"",
            "!" * 60,
        ]
    lines += ["If macOS asks to allow incoming connections for Python, click Allow.", "=" * 60, ""]
    return "\n".join(lines)


def main() -> None:
    import argparse

    import uvicorn  # installed with FastAPI's server extras (see requirements.txt)

    parser = argparse.ArgumentParser(description="Run the Relay server.")
    parser.add_argument("--tunnel", action="store_true",
                        help="also open a public https link with Cloudflare (needs: brew install cloudflared)")
    args = parser.parse_args()

    print(startup_banner(lan_ips(), config.RELAY_TOKEN))
    proc = None
    if args.tunnel:
        from relay import tunnel
        print("[tunnel] starting Cloudflare tunnel...")
        proc, url = tunnel.start(PORT)
        print(tunnel.banner(url, config.RELAY_TOKEN))
    try:
        uvicorn.run(app, host="0.0.0.0", port=PORT)
    finally:
        if proc is not None:
            proc.terminate()  # close the public link when the server stops


if __name__ == "__main__":
    main()
