"""Relay's web server. The iPhone Shortcut talks to these routes.

Run it:   python -m relay.server
          (prints the URL to put in the iPhone Shortcut, then starts uvicorn on 0.0.0.0:8000)
   or:    uvicorn relay.server:app --host 0.0.0.0 --port 8000

GET  /health                         -> {"ok": true, ...}  (no token needed; test it in Safari)
POST /command {"text", "device"}  -> start a task, wait up to ~45 s, return what to say
POST /reply   {"session_id","text"} -> answer a yes/no (or any) question the task asked
POST /stop                           -> stop whatever is running
GET  /audio/{id}.mp3                 -> ElevenLabs audio for a reply
GET  /dashboard                      -> live dashboard page for the projector
GET  /events/stream                  -> live events (Server-Sent Events) for the dashboard
GET  /events/recent                  -> the last ~100 events as JSON
WS   /device/ws                      -> cloud mode only: the Mac's device helper (relay/remote.py)

Every route except /health needs the header  Authorization: Bearer <RELAY_TOKEN>.
The three dashboard routes also work WITHOUT a token when opened on the Mac itself
(http://localhost:8000/dashboard), and accept ?token=<RELAY_TOKEN> from anywhere else.
"""
from __future__ import annotations

import json
import re
import os
import secrets
import socket
import threading
import time

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from relay import agent, config, events, remote, router, safety, skills, voice

# How long one HTTP request waits for the task before answering "still working".
# Shortcuts' Get Contents of URL waits about 60 s, so 45 s leaves headroom and lets
# most tasks (popups, opening apps, summaries) finish in a single request.
WAIT_SECONDS = float(os.getenv("COMMAND_WAIT_SECONDS", "45"))
# /talk answers sooner ("Still working...") so Siri doesn't give up and close the Shortcut.
TALK_WAIT_SECONDS = float(os.getenv("TALK_WAIT_SECONDS", "8"))
PORT = 8000
HEALTH_DEVICE_TIMEOUT = 1.5  # /health never waits longer than this for the emulator

app = FastAPI(title="Relay")
remote.register(app)  # /device/ws: the Mac's device helper connects here in cloud mode

# --- state shared between requests -----------------------------------------
_lock = threading.Lock()
_tasks: dict[str, "agent.Task"] = {}   # session_id -> Task
_current: "agent.Task | None" = None   # the one task allowed to run at a time
_last_device: str | None = None        # so "scroll down" goes to the device you just used
_last_answer: str | None = None        # last final answer we spoke, for "repeat that"

# "Repeat that": answered instantly from _last_answer, no task, no AI.
# Matched after skills._clean() removes filler ("hey relay, can you ... please").
REPEAT_PHRASES = {
    "repeat", "repeat that", "repeat it", "repeat yourself", "repeat that again",
    "say that again", "say it again", "say again", "come again",
    "what did you say", "what did you just say", "what was that",
    "sorry what", "pardon", "one more time",
}
NOTHING_TO_REPEAT = "I haven't said anything yet."


def is_repeat(text: str) -> bool:
    """True if the user just wants the last answer again."""
    return skills._clean(text).lower() in REPEAT_PHRASES


# --- auth ------------------------------------------------------------------
def _clean_token(header: str) -> str:
    """Forgive paste mistakes: spaces, line breaks, a missing or lowercase "Bearer"."""
    text = "".join(header.split())          # remove ALL whitespace, incl. line breaks
    if text.lower().startswith("bearer"):
        text = text[len("bearer"):]
    return text


def _why_rejected(got: str, want: str) -> str:
    """A hint for the server log. Never prints the token itself."""
    if not got:
        return "no token sent (is the Authorization header there?)"
    if len(got) != len(want):
        return (f"token is {len(got)} characters, expected {len(want)}. Check for extra text "
                f"(like 123456789) or a missing part")
    if got.lower() == want.lower():
        return "token differs only in capital letters. iPhone autocorrect may have changed one"
    first_diff = next(i for i, (a, b) in enumerate(zip(got, want)) if a != b)
    return f"token is the right length but differs at character {first_diff + 1}. Re-paste it"


def require_token(authorization: str = Header(default="")) -> None:
    want = config.RELAY_TOKEN.strip()
    got = _clean_token(authorization)
    # compare_digest avoids leaking the token through timing differences
    if not want or not secrets.compare_digest(got.encode(), want.encode()):
        print(f"[auth] rejected: {_why_rejected(got, want)}")
        raise HTTPException(status_code=401, detail="bad or missing token")


# --- dashboard auth --------------------------------------------------------
# The projector dashboard runs in a browser ON the Mac, so we let "this computer"
# in without a token. The catch: ngrok and Cloudflare tunnels also connect from
# 127.0.0.1, so a request from the internet LOOKS local. Tunnels always add
# headers saying who really sent it; if any of those are there, it's remote.
_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}
_FORWARD_HEADERS = {"x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "x-real-ip",
                    "forwarded", "cf-connecting-ip", "cf-ray", "cf-ipcountry", "cdn-loop", "true-client-ip"}


def is_forwarded(request: Request) -> bool:
    """True if a proxy/tunnel (ngrok, Cloudflare, ...) passed this request along."""
    for name in request.headers.keys():  # header names arrive lowercase
        if name in _FORWARD_HEADERS or name.startswith(("ngrok-", "x-ngrok")):
            return True
    return False


def is_local_request(request: Request) -> bool:
    """True only for a browser on this Mac, talking to us directly (no tunnel)."""
    host = request.client.host if request.client else ""
    return host in _LOCAL_HOSTS and not is_forwarded(request)


def require_dashboard_access(request: Request, token: str = Query(default=""),
                             authorization: str = Header(default="")) -> None:
    """Dashboard routes: free on this Mac, otherwise ?token=... or the Bearer header."""
    if is_local_request(request):
        return
    want = config.RELAY_TOKEN.strip()
    got = token.strip() or _clean_token(authorization)
    if not want or not secrets.compare_digest(got.encode(), want.encode()):
        print(f"[auth] dashboard rejected: {_why_rejected(got, want)}")
        raise HTTPException(status_code=401, detail="bad or missing token")


# --- request bodies --------------------------------------------------------
class CommandIn(BaseModel):
    text: str
    device: str | None = None   # "phone" | "mac" | null (null = let the router guess)
    conversation: bool = False  # true: end finished answers with "Anything else?"


class TalkIn(BaseModel):
    """One turn of a conversation. The Shortcut just keeps POSTing to /talk.

    - Relay asked a question -> `text` is the answer.
    - Relay is still working -> send empty text to hear the next update.
    - Otherwise -> `text` is a new request (or "no" / "that's all" to finish).
    """
    text: str = ""
    session_id: str = ""
    device: str | None = None
    conversation: bool = True


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


# Things people say to end a conversation after "Anything else?".
_ENDINGS = {"no", "nope", "nah", "no thanks", "no thank you", "thats all", "that's all",
            "thats it", "that's it", "im good", "i'm good", "im done", "i'm done", "nothing",
            "nothing else", "all done", "done", "bye", "goodbye", "stop", "cancel", "never mind",
            "nevermind", "no that's all", "no thats all", "no i'm good", "no im good", "thanks",
            "thank you", "no that's it"}


_delivered: set[str] = set()  # ids of finished tasks whose result was already spoken


def is_ending(text: str) -> bool:
    t = re.sub(r"[^a-z' ]", "", (text or "").lower()).strip()
    return t == "" or t in _ENDINGS


def _say_only(say: str, request: Request) -> dict:
    """A reply that isn't tied to a task (e.g. "Okay, talk soon.")."""
    say = plain_text(say)
    audio_id = voice.speak(say)
    audio_url = f"{str(request.base_url).rstrip('/')}/audio/{audio_id}.mp3" if audio_id else None
    return {"say": say, "audio_url": audio_url, "needs_reply": False, "next": "done", "session_id": ""}


def _respond(task, request: Request, conversation: bool = False) -> dict:
    """Turn a task's current state into the JSON the Shortcut expects.

    "say" is always plain text. "audio_url" is a full URL like
    http://192.168.1.20:8000/audio/ab12cd34ef56.mp3 (built from the address the
    phone used to reach us) or None when there's no ElevenLabs audio.
    """
    say = plain_text(task.say())
    ask_more = conversation and task.finished
    if ask_more:
        _remember_answer(say)  # "repeat that" repeats the answer, not the question
        say = f"{say} Anything else?"
    audio_id = voice.speak(say)
    audio_url = f"{str(request.base_url).rstrip('/')}/audio/{audio_id}.mp3" if audio_id else None
    if task.status == "waiting":
        next_step = "reply"   # Relay asked a question: listen, then POST /reply with the answer
    elif not task.finished:
        next_step = "wait"    # still working: POST /reply with empty text to hear the next update
    else:
        _delivered.add(task.id)  # its result has now been spoken once
        # finished: "more" = listen for another request; "done" = stop the Shortcut
        next_step = "more" if ask_more else "done"
        if not ask_more:
            _remember_answer(say)
        events.publish("answer", say=say, task_id=task.id)  # live dashboard
    return {
        "say": say,
        "audio_url": audio_url,
        "needs_reply": next_step == "reply",
        "next": next_step,
        "session_id": task.id,
    }


def _remember_answer(say: str) -> None:
    global _last_answer
    _last_answer = say


def _repeat_response(request: Request) -> dict:
    """Speak the last final answer again (instant: no task, no AI)."""
    say = _last_answer or NOTHING_TO_REPEAT
    events.publish("repeat", say=say)  # live dashboard
    audio_id = voice.speak(say)
    audio_url = f"{str(request.base_url).rstrip('/')}/audio/{audio_id}.mp3" if audio_id else None
    return {"say": say, "audio_url": audio_url, "needs_reply": False, "next": "done", "session_id": ""}


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
    body = {
        "ok": True,
        "device_default": config.DEFAULT_DEVICE,
        "emulator_connected": emulator_connected(),
    }
    if config.CLOUD_MODE:  # cloud mode: is the Mac's device helper connected?
        body["device_helper_connected"] = remote.hub.connected
    return body


@app.post("/command", dependencies=[Depends(require_token)])
def command(body: CommandIn, request: Request) -> dict:
    return _start(body.text, body.device, request, body.conversation)


def _start(text: str, device: str | None, request: Request, conversation: bool) -> dict:
    """Start a new task for `text` and answer once it finishes, pauses, or WAIT_SECONDS pass."""
    global _current, _last_device
    if device not in (None, "phone", "mac"):
        raise HTTPException(status_code=400, detail="device must be phone, mac or null")
    # Live dashboard: show what the user said (with the unlock passphrase removed).
    events.publish("heard", text=agent.strip_passphrase(text)[0], device=device)
    # "Repeat that" never starts a task, and never stops the one that's running.
    if is_repeat(text):
        return _repeat_response(request)
    with _lock:
        if _current is not None and not _current.finished:
            _current.stop()  # a new command replaces the old one
        task = agent.Task(text, device_name=device, last_device=_last_device)
        _tasks[task.id] = task
        _current = task
        _last_device = task.device_name
    task.start()
    task.wait_until_settled(TALK_WAIT_SECONDS if conversation else WAIT_SECONDS)
    return _respond(task, request, conversation)


@app.post("/talk", dependencies=[Depends(require_token)])
def talk(body: TalkIn, request: Request) -> dict:
    """The one endpoint a conversation loop needs (see shortcut/README.md)."""
    # Debug line: shows exactly what the Watch/iPhone sent (journalctl -u relay -f).
    print(f"[talk] heard={body.text!r} session={body.session_id or '-'} "
          f"current={getattr(_current, 'status', None)}", flush=True)
    task = _tasks.get(body.session_id) if body.session_id else None
    # Siri closed the Shortcut while Relay was asking "Should I send it?". On the next
    # run, a plain yes/no answers that question instead of starting something new.
    if task is None and _current is not None and _current.status == "waiting" \
            and (safety.is_yes(body.text) or safety.is_no(body.text)):
        task = _current
    # Re-running with no words while Relay is still busy: give the latest update.
    if task is None and not (body.text or "").strip() and _current is not None \
            and not _current.finished:
        task = _current
    # The task finished between two check-ins: speak its result before anything else,
    # so "Sent your email" isn't skipped straight to "Okay, talk soon."
    if task is not None and task.finished and task.id not in _delivered:
        return _respond(task, request, body.conversation)
    if task is not None and task.status == "waiting":
        task.reply(body.text)                     # answer to "Should I send it?"
        task.wait_until_settled(TALK_WAIT_SECONDS)
        return _respond(task, request, body.conversation)
    if task is not None and not task.finished:
        task.wait_until_settled(TALK_WAIT_SECONDS)  # still working: next update
        return _respond(task, request, body.conversation)
    if not (body.text or "").strip() and not body.session_id:
        # Started, but dictation caught nothing: say so and listen again.
        r = _say_only("Sorry, I didn't catch that. Go ahead.", request)
        r["next"] = "more"
        return r
    if is_ending(body.text):
        events.publish("heard", text=body.text, device=body.device)
        return _say_only("Okay, talk soon.", request)
    return _start(body.text, body.device, request, body.conversation)


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
        return {"say": "Stopping.", "audio_url": None, "needs_reply": False, "next": "done", "session_id": task.id}
    return {"say": "Nothing is running.", "audio_url": None, "needs_reply": False, "next": "done", "session_id": ""}


@app.get("/audio/{audio_id}.mp3", dependencies=[Depends(require_token)])
def audio(audio_id: str) -> FileResponse:
    path = voice.audio_path(audio_id)
    if path is None:
        raise HTTPException(status_code=404, detail="no such audio")
    return FileResponse(path, media_type="audio/mpeg")


# --- live dashboard ----------------------------------------------------------
DASHBOARD_FILE = os.path.join(os.path.dirname(__file__), "dashboard.html")
KEEPALIVE_SECONDS = 15.0   # send a comment line this often so proxies don't close an idle stream
STREAM_SECONDS = 300.0     # end each stream after 5 min; the browser reconnects by itself
WAIT_STEP_SECONDS = 1.0    # how long one wait for new events lasts before we check again


@app.get("/dashboard", response_class=HTMLResponse, dependencies=[Depends(require_dashboard_access)])
def dashboard() -> HTMLResponse:
    """The projector page. Open http://localhost:8000/dashboard on the Mac."""
    with open(DASHBOARD_FILE, encoding="utf-8") as f:
        return HTMLResponse(f.read(), headers={"Cache-Control": "no-store"})


@app.get("/events/recent", dependencies=[Depends(require_dashboard_access)])
def events_recent() -> dict:
    """The last ~100 events, so a freshly opened dashboard isn't empty."""
    return {"events": events.recent(100), "newest_id": events.newest_id(), "server_ts": time.time()}


def _sse(event: dict) -> str:
    """One Server-Sent Event. The browser's EventSource remembers `id` and sends it
    back as the Last-Event-ID header when it reconnects."""
    return f"id: {event['id']}\ndata: {json.dumps(event)}\n\n"


@app.get("/events/stream", dependencies=[Depends(require_dashboard_access)])
async def events_stream(request: Request, since: int | None = None,
                        last_event_id: str = Header(default="")) -> StreamingResponse:
    """Live events as text/event-stream. Resume with ?since=<id> or the Last-Event-ID header."""
    start = since
    if start is None:
        start = int(last_event_id) if last_event_id.strip().isdigit() else events.newest_id()

    async def stream():
        last = start
        # Tell the page how far the server's ids go. If the page remembers a
        # BIGGER id, the server restarted and the page should start over.
        yield "retry: 2000\n" + f"event: hello\ndata: {json.dumps({'newest_id': events.newest_id(), 'server_ts': time.time()})}\n\n"
        opened = last_sent = time.monotonic()
        while time.monotonic() - opened < STREAM_SECONDS:
            if await request.is_disconnected():
                break
            # wait_for_new blocks a thread, so it runs in the thread pool, not here.
            new = await run_in_threadpool(events.wait_for_new, last, WAIT_STEP_SECONDS)
            for event in new:
                last = event["id"]
                yield _sse(event)
            if new:
                last_sent = time.monotonic()
            elif time.monotonic() - last_sent >= KEEPALIVE_SECONDS:
                yield ": keepalive\n\n"  # lines starting with ":" are comments; browsers ignore them
                last_sent = time.monotonic()

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


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
    lines.append("Live dashboard (open on this Mac, e.g. for the projector):")
    lines.append(f"    http://localhost:{port}/dashboard")
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
    parser.add_argument("--tunnel", nargs="?", const="auto", choices=["auto", "ngrok", "cloudflare"],
                        help="also open a public https link: ngrok (same link every time) or cloudflare")
    args = parser.parse_args()

    print(startup_banner(lan_ips(), config.RELAY_TOKEN))
    proc = None
    if args.tunnel:
        from relay import tunnel
        print("[tunnel] starting public link...")
        proc, url, kind = tunnel.start_auto(PORT, args.tunnel)
        print(tunnel.banner(url, config.RELAY_TOKEN, kind))
    try:
        uvicorn.run(app, host="0.0.0.0", port=PORT)
    finally:
        if proc is not None:
            proc.terminate()  # close the public link when the server stops


if __name__ == "__main__":
    main()
