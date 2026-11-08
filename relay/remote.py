"""Cloud mode, server side: control the phone/Mac from a server in the cloud.

Normally the whole Relay server runs on your Mac. In cloud mode (CLOUD_MODE=1)
the "brain" (AI calls, skills, workflows, ElevenLabs) runs on a cloud server,
and a small DEVICE HELPER on your Mac (relay/device_helper.py) does the actual
tapping, typing and screenshots:

    iPhone Shortcut --https--> cloud server (this file)  <--websocket-- Mac helper
                                                                 |
                                                         emulator / the Mac

The Mac connects OUT to the server, so the Mac needs no tunnel or open port.

How one device call travels:
    1. The agent (running in a worker THREAD) calls device.start_app("spotify")
       on a RemoteDevice.
    2. RemoteDevice sends {"id", "device", "method", "args"} over the websocket.
       The websocket lives in the server's asyncio event loop, so the thread hands
       the send to that loop with asyncio.run_coroutine_threadsafe.
    3. The helper runs Phone().start_app("spotify") and replies {"id", "result"}
       or {"id", "error": {"type", "message"}}.
    4. The websocket loop finds the waiting call by its id and wakes it up.
"""
from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import json
import secrets
import threading
import uuid
from dataclasses import asdict, replace

from fastapi import WebSocket, WebSocketDisconnect

from relay import config
from relay.types import Action, Element, Screen

NO_HELPER = "No device helper connected. Run python -m relay.device_helper on your Mac."
DEVICE_NAMES = ("phone", "mac")
HELLO_TIMEOUT = 10.0  # seconds a new helper has to send its hello

# The only device methods the helper will run (so the server can't ask it to
# call anything else, like private attributes).
REMOTE_METHODS = {
    "get_screen", "act", "emergency_call", "key", "start_app", "close_app",      # phone + mac
    "open_uri", "grant_location", "notifications", "is_locked", "unlock",        # phone only
    "sync_location",                                                              # helper itself
}


# ---------------------------------------------------------------------------
# JSON <-> Screen / Element / Action
# ---------------------------------------------------------------------------
# JSON only knows dicts, lists, strings, numbers, true/false/null. Our dataclasses
# become dicts with a "__type__" tag so the other side can rebuild them, and
# bytes (the screenshot PNG) become base64 text.

def encode(value):
    """Turn a Python value (maybe holding Screens/Actions/bytes) into plain JSON data."""
    if isinstance(value, Screen):
        return {"__type__": "Screen", "device": value.device, "app": value.app,
                "elements": [encode(e) for e in value.elements],
                "screenshot_png": encode(value.screenshot_png)}
    if isinstance(value, Element):
        data = asdict(value)
        data["bounds"] = list(value.bounds)
        return {"__type__": "Element", **data}
    if isinstance(value, Action):
        return {"__type__": "Action", **asdict(value)}
    if isinstance(value, bytes):
        return {"__type__": "bytes", "b64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, dict):
        return {k: encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [encode(v) for v in value]
    return value  # str, int, float, bool, None


def decode(value):
    """The reverse of encode()."""
    if isinstance(value, list):
        return [decode(v) for v in value]
    if not isinstance(value, dict):
        return value
    kind = value.get("__type__")
    if kind == "Screen":
        return Screen(device=value["device"], app=value["app"],
                      elements=[decode(e) for e in value.get("elements", [])],
                      screenshot_png=decode(value.get("screenshot_png")))
    if kind == "Element":
        return Element(id=value["id"], text=value["text"], kind=value["kind"],
                       bounds=tuple(value["bounds"]), clickable=value["clickable"])
    if kind == "Action":
        fields = {k: v for k, v in value.items() if k != "__type__"}
        return Action(**fields)
    if kind == "bytes":
        return base64.b64decode(value["b64"])
    return {k: decode(v) for k, v in value.items()}


def error_reply(request_id, exc: BaseException) -> dict:
    """The JSON the helper sends back when a device call raised."""
    return {"id": request_id, "error": {"type": type(exc).__name__, "message": str(exc)}}


def raise_remote_error(error: dict) -> None:
    """Re-raise a helper's error on the server.

    ValueError stays ValueError: the agent loop treats it as "that step failed,
    look at the screen again". NotImplementedError stays too (e.g. no 911 on a Mac).
    Everything else becomes RuntimeError.
    """
    kind = error.get("type", "Error")
    message = error.get("message", "")
    if kind == "ValueError":
        raise ValueError(message)
    if kind == "NotImplementedError":
        raise NotImplementedError(message)
    raise RuntimeError(f"device helper error ({kind}): {message}")


# ---------------------------------------------------------------------------
# The hub: the one connected helper, and the calls waiting for its answers
# ---------------------------------------------------------------------------

class Hub:
    """Holds the helper's websocket and matches replies to waiting calls by id."""

    def __init__(self):
        self._lock = threading.Lock()
        self._ws = None                 # the helper's websocket (None = no helper)
        self._loop = None               # the asyncio loop that websocket lives in
        self._send_lock = None          # one message at a time on the websocket
        self.devices: list[str] = []    # what the helper said it can control
        # request id -> Future that the waiting thread blocks on
        self._pending: dict[str, concurrent.futures.Future] = {}

    @property
    def connected(self) -> bool:
        return self._ws is not None

    def attach(self, ws, loop, devices: list[str], send_lock=None) -> None:
        """A helper said hello. A newer helper replaces an older one."""
        with self._lock:
            old = self._ws
            self._ws, self._loop, self.devices = ws, loop, list(devices)
            self._send_lock = send_lock or asyncio.Lock()
        if old is not None and old is not ws:
            self._fail_all("the device helper reconnected")
            asyncio.run_coroutine_threadsafe(_quiet_close(old), loop)

    def detach(self, ws) -> None:
        """The helper's websocket closed. Fail every call still waiting on it."""
        with self._lock:
            if self._ws is not ws:
                return  # an old connection that was already replaced
            self._ws, self._loop, self.devices = None, None, []
        self._fail_all("the device helper disconnected")

    def _fail_all(self, why: str) -> None:
        with self._lock:
            pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(RuntimeError(why))

    def deliver(self, reply: dict) -> None:
        """A reply arrived from the helper: wake up the call with the same id."""
        with self._lock:
            future = self._pending.pop(str(reply.get("id")), None)
        if future is not None and not future.done():
            future.set_result(reply)

    def request(self, message: dict, timeout: float) -> dict:
        """Send one request and BLOCK until its reply comes back (or `timeout`).

        Call this from a normal thread (the agent's worker thread, FastAPI's
        thread pool), never from inside the event loop: it would wait for itself.
        """
        with self._lock:
            ws, loop, send_lock = self._ws, self._loop, self._send_lock
            if ws is None:
                raise RuntimeError(NO_HELPER)
            if message["device"] not in self.devices:
                raise RuntimeError(f"The device helper isn't controlling the {message['device']} "
                                   f"(it offered: {', '.join(self.devices) or 'nothing'}).")
            future: concurrent.futures.Future = concurrent.futures.Future()
            self._pending[message["id"]] = future
        try:
            if _in_loop(loop):
                raise RuntimeError("Hub.request was called inside the event loop; use a thread")

            async def send():
                async with send_lock:
                    await ws.send_text(json.dumps(message))

            asyncio.run_coroutine_threadsafe(send(), loop).result(timeout)
            return future.result(timeout)
        except concurrent.futures.TimeoutError:
            raise RuntimeError(f"The device helper didn't answer {message['method']} "
                               f"within {timeout:g} seconds.") from None
        finally:
            with self._lock:
                self._pending.pop(message["id"], None)


def _in_loop(loop) -> bool:
    try:
        return asyncio.get_running_loop() is loop
    except RuntimeError:
        return False  # no loop running in this thread: good


async def _quiet_close(ws) -> None:
    try:
        await ws.close()
    except Exception:
        pass


hub = Hub()  # the server's one hub


# ---------------------------------------------------------------------------
# RemoteDevice: looks like Phone()/Mac() to the agent, but runs on the helper
# ---------------------------------------------------------------------------

class RemoteDevice:
    """Methods both the phone and the Mac have. Each call goes to the helper.

    `transport` is anything with request(message, timeout) -> reply. Normally the
    hub; tests pass a fake one.
    """

    def __init__(self, name: str, transport=None, timeout: float | None = None):
        self.name = name
        self._transport = transport
        self._timeout = timeout

    def _call(self, method: str, **args):
        message = {"id": uuid.uuid4().hex, "device": self.name, "method": method, "args": encode(args)}
        timeout = self._timeout if self._timeout is not None else config.REMOTE_TIMEOUT
        reply = (self._transport or hub).request(message, timeout)
        if reply.get("error"):
            raise_remote_error(reply["error"])
        return decode(reply.get("result"))

    # --- the Device interface (relay/devices/base.py) ---
    def get_screen(self, with_screenshot: bool = False) -> Screen:
        return self._call("get_screen", with_screenshot=with_screenshot)

    def act(self, action: Action, screen: Screen) -> None:
        # The device only needs the element list (for tap positions), so don't
        # upload the screenshot back to the Mac.
        self._call("act", action=action, screen=replace(screen, screenshot_png=None))

    def emergency_call(self) -> None:
        self._call("emergency_call")

    # --- quick actions used by skills (same names on phone and Mac) ---
    def key(self, name: str) -> None:
        self._call("key", name=name)

    def start_app(self, name: str) -> None:
        self._call("start_app", name=name)

    def close_app(self, name: str) -> None:
        self._call("close_app", name=name)


class RemotePhone(RemoteDevice):
    """Extra methods only the Android phone has.

    They live in a subclass on purpose: the agent and skills check things like
    hasattr(device, "is_locked"), and a remote Mac must answer "no" just like a
    local Mac() does.
    """

    def open_uri(self, uri: str, package: str | None = None) -> None:
        self._call("open_uri", uri=uri, package=package)

    def grant_location(self, package: str) -> None:
        self._call("grant_location", package=package)

    def notifications(self) -> list[dict]:
        return self._call("notifications")

    def is_locked(self) -> bool:
        return bool(self._call("is_locked"))

    def unlock(self, pin: str = "") -> bool:
        return bool(self._call("unlock", pin=pin))

    def sync_location(self) -> bool:
        """Copy the Mac's location to the emulator, ON the Mac (see relay/location.py)."""
        return bool(self._call("sync_location"))


def make_remote_device(name: str, transport=None, timeout: float | None = None) -> RemoteDevice:
    """RemotePhone for "phone", RemoteDevice for "mac"."""
    if name == "phone":
        return RemotePhone(name, transport, timeout)
    if name == "mac":
        return RemoteDevice(name, transport, timeout)
    raise ValueError(f"unknown device: {name!r}")


def get_remote_device(name: str) -> RemoteDevice:
    """What router.get_device returns in cloud mode."""
    if name not in DEVICE_NAMES:
        raise ValueError(f"unknown device: {name!r}")
    if not hub.connected:
        raise RuntimeError(NO_HELPER)
    return make_remote_device(name)  # cheap: it holds no connection of its own


# ---------------------------------------------------------------------------
# The websocket the helper connects to:  wss://<server>/device/ws
# ---------------------------------------------------------------------------

def _token_from(websocket: WebSocket) -> str:
    """?token=... or the Authorization: Bearer ... header (spaces forgiven)."""
    query = websocket.query_params.get("token", "").strip()
    if query:
        return query
    header = "".join(websocket.headers.get("authorization", "").split())
    if header.lower().startswith("bearer"):
        header = header[len("bearer"):]
    return header


async def device_ws(websocket: WebSocket) -> None:
    want = config.RELAY_TOKEN.strip()
    got = _token_from(websocket)
    if not want or not secrets.compare_digest(got.encode(), want.encode()):
        print("[remote] device helper rejected: bad or missing token")
        await websocket.close(code=1008)  # 1008 = policy violation
        return
    await websocket.accept()

    # First message must be the hello: {"devices": ["phone", "mac"]}
    try:
        hello = json.loads(await asyncio.wait_for(websocket.receive_text(), HELLO_TIMEOUT))
        devices = [d for d in hello.get("devices", []) if d in DEVICE_NAMES]
    except (asyncio.TimeoutError, ValueError, AttributeError, WebSocketDisconnect):
        await _quiet_close(websocket)
        return
    if not devices:
        await websocket.close(code=1008)
        return

    hub.attach(websocket, asyncio.get_running_loop(), devices)
    await websocket.send_text(json.dumps({"type": "welcome", "devices": devices}))
    print(f"[remote] device helper connected: {', '.join(devices)}")
    try:
        while True:
            try:
                reply = json.loads(await websocket.receive_text())
            except ValueError:
                continue  # not JSON: ignore it
            if isinstance(reply, dict) and "id" in reply:
                hub.deliver(reply)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        hub.detach(websocket)
        print("[remote] device helper disconnected")


def register(app) -> None:
    """Add the /device/ws route to the FastAPI app (called from server.py)."""
    app.add_api_websocket_route("/device/ws", device_ws)
