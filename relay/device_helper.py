"""Cloud mode, Mac side: the DEVICE HELPER.

When the Relay brain runs on a cloud server (CLOUD_MODE=1, see deploy/VULTR.md),
this small program runs on your Mac. It connects OUT to the server's websocket,
then does whatever device calls the server sends: read the emulator's screen,
tap, type, open apps, move the emulator's GPS, and the same for the Mac.

Run it (on the Mac, from the repo folder, with the same RELAY_TOKEN in .env):

    python -m relay.device_helper --server https://1-2-3-4.sslip.io
    (or put RELAY_SERVER=https://1-2-3-4.sslip.io in .env and leave out --server)

It reconnects by itself forever (waiting a little longer after each failure),
so you can start it before the server and leave it running. Ctrl+C to stop.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time

from relay import config, remote, router


# ---------------------------------------------------------------------------
# Running one request (plain function, no network: easy to test)
# ---------------------------------------------------------------------------

def handle(message: dict, get_device=router.get_local_device, devices=remote.DEVICE_NAMES) -> dict:
    """Run one {id, device, method, args} request and return {id, result} or {id, error}.

    get_device is router.get_local_device: the REAL Phone()/Mac() on this Mac,
    even if this .env happens to say CLOUD_MODE=1. Tests pass a fake.
    """
    request_id = message.get("id")
    try:
        name = message.get("device")
        method = message.get("method")
        if name not in devices:
            raise RuntimeError(f"this helper doesn't control the {name!r} device")
        if method not in remote.REMOTE_METHODS:
            raise RuntimeError(f"method {method!r} is not allowed")
        args = remote.decode(message.get("args") or {})

        if method == "sync_location":
            # Not a device method: copy the Mac's location to the emulator, right here.
            from relay import location
            result = location.sync_emulator_to_mac()
        else:
            device = get_device(name)  # created on first use, then reused
            result = getattr(device, method)(**args)
        return {"id": request_id, "result": remote.encode(result)}
    except Exception as e:  # send every error back so the server can decide what to do
        return remote.error_reply(request_id, e)


# ---------------------------------------------------------------------------
# The connection
# ---------------------------------------------------------------------------

def websocket_url(server: str) -> str:
    """https://host -> wss://host/device/ws   (http -> ws; no scheme = https).

    Also forgives pasting the Shortcut URL (https://host/command).
    """
    url = server.strip().rstrip("/")
    for suffix in ("/command", "/health", "/device/ws"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
    if url.startswith("https://"):
        url = "wss://" + url[len("https://"):]
    elif url.startswith("http://"):
        url = "ws://" + url[len("http://"):]
    elif not url.startswith(("ws://", "wss://")):
        url = "wss://" + url
    return url + "/device/ws"


def status(line: str) -> None:
    print(f"[helper {time.strftime('%H:%M:%S')}] {line}", flush=True)


async def _answer(ws, message: dict, devices) -> None:
    """Run one request in a thread (device calls block for seconds) and send the reply."""
    label = f"{message.get('device')}.{message.get('method')}"
    reply = await asyncio.to_thread(handle, message, router.get_local_device, devices)
    if "error" in reply:
        status(f"{label} -> {reply['error']['type']}: {reply['error']['message']}")
    await ws.send(json.dumps(reply))


async def connect_once(url: str, token: str, devices: list[str]) -> None:
    """Connect, say hello, and answer requests until the connection drops."""
    import websockets  # pip install websockets (in requirements.txt)

    async with websockets.connect(
        url,
        additional_headers={"Authorization": f"Bearer {token}"},
        max_size=None,     # screenshots can be big
        open_timeout=15,
        ping_interval=20,  # keepalive so proxies don't drop an idle connection
    ) as ws:
        await ws.send(json.dumps({"type": "hello", "devices": devices}))
        status(f"connected to {url} (controlling: {', '.join(devices)})")
        running = set()  # keep references so tasks aren't garbage-collected mid-call
        async for raw in ws:
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            if message.get("type") == "welcome":
                status("server accepted this helper. Ready for commands.")
                continue
            if "id" not in message:
                continue
            task = asyncio.create_task(_answer(ws, message, devices))
            running.add(task)
            task.add_done_callback(running.discard)


def _why(e: Exception) -> str:
    """A friendly reason for a failed connection."""
    response = getattr(e, "response", None)
    code = getattr(response, "status_code", None)
    if code in (401, 403):
        return f"the server refused the token (HTTP {code}). RELAY_TOKEN in this Mac's .env must match the server's"
    if code == 404:
        return "HTTP 404: is the server running a Relay version with cloud mode?"
    if code:
        return f"HTTP {code}"
    return f"{type(e).__name__}: {e}" if str(e) else type(e).__name__


async def run_forever(server: str, token: str, devices: list[str]) -> None:
    url = websocket_url(server)
    delay = 1.0
    while True:
        status(f"connecting to {url} ...")
        started = time.monotonic()
        try:
            await connect_once(url, token, devices)
            status("connection closed by the server")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            status(f"couldn't stay connected: {_why(e)}")
        if time.monotonic() - started > 60:
            delay = 1.0  # we were connected for a while: retry quickly
        status(f"retrying in {delay:.0f} s")
        await asyncio.sleep(delay)
        delay = min(delay * 2, 30.0)  # 1, 2, 4, 8, 16, 30, 30, ...


def main() -> None:
    parser = argparse.ArgumentParser(description="Relay device helper: lets a cloud Relay server "
                                                 "control this Mac's emulator and the Mac itself.")
    parser.add_argument("--server", default=config.RELAY_SERVER,
                        help="the cloud server, e.g. https://1-2-3-4.sslip.io (or RELAY_SERVER in .env)")
    parser.add_argument("--devices", default="phone,mac",
                        help="which devices to offer, comma-separated (default: phone,mac)")
    args = parser.parse_args()

    if not args.server:
        parser.error("say where the server is: --server https://1-2-3-4.sslip.io (or RELAY_SERVER in .env)")
    devices = [d.strip() for d in args.devices.split(",") if d.strip() in remote.DEVICE_NAMES]
    if not devices:
        parser.error("--devices must list phone and/or mac")
    token = config.RELAY_TOKEN.strip()
    if not token or token == "change-me":
        status("WARNING: RELAY_TOKEN in .env is empty or the placeholder; the server will refuse it")

    try:
        asyncio.run(run_forever(args.server, token, devices))
    except KeyboardInterrupt:
        status("stopped")


if __name__ == "__main__":
    main()
