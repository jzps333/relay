"""Cloud mode tests: RemoteDevice, the hub, the /device/ws websocket, the Mac helper.

Fakes only: no network, no emulator, no Mac.
"""
import asyncio
import json
import threading

import pytest
from fastapi.testclient import TestClient

from relay import config, device_helper, remote, router, server, skills
from relay.types import Action, Element, Screen

TOKEN = "test-token"


def sample_screen(png=b"\x89PNG fake") -> Screen:
    return Screen(device="phone", app="com.spotify.music",
                  elements=[Element(0, "Play", "button", (1, 2, 3, 4), True)], screenshot_png=png)


class FakeTransport:
    """Stands in for the hub. Passes each request to `answer` and records it."""

    def __init__(self, answer):
        self.answer = answer
        self.sent = []

    def request(self, message, timeout):
        json.dumps(message)  # must be plain JSON, like on the real websocket
        self.sent.append(message)
        return self.answer(message)


class FakePhone:
    """Records calls like a Phone() would receive them."""
    name = "phone"

    def __init__(self):
        self.calls = []

    def get_screen(self, with_screenshot=False):
        self.calls.append(("get_screen", with_screenshot))
        return sample_screen(b"png!" if with_screenshot else None)

    def act(self, action, screen):
        self.calls.append(("act", action, screen))

    def start_app(self, name):
        self.calls.append(("start_app", name))

    def is_locked(self):
        return False

    def unlock(self, pin=""):
        self.calls.append(("unlock", pin))
        return True


# --- JSON encoding ------------------------------------------------------------

def test_encode_decode_round_trip():
    screen = sample_screen()
    action = Action(kind="tap", element_id=0, reason="play it")
    data = json.loads(json.dumps(remote.encode({"screen": screen, "action": action})))
    back = remote.decode(data)
    assert back["screen"] == screen            # bounds come back as a tuple, png as bytes
    assert back["action"] == action
    assert isinstance(data["screen"]["screenshot_png"]["b64"], str)


# --- RemoteDevice over a fake transport ------------------------------------------

def test_round_trip_through_helper_dispatch():
    """RemoteDevice -> JSON -> device_helper.handle -> fake phone -> JSON -> back."""
    phone = FakePhone()
    transport = FakeTransport(lambda m: json.loads(json.dumps(
        device_helper.handle(json.loads(json.dumps(m)), get_device=lambda name: phone))))
    dev = remote.make_remote_device("phone", transport=transport)

    screen = dev.get_screen(with_screenshot=True)
    assert screen.screenshot_png == b"png!" and screen.elements[0].bounds == (1, 2, 3, 4)
    dev.act(Action(kind="tap", element_id=0), screen)
    dev.start_app("spotify")
    assert dev.unlock("1234") is True

    assert transport.sent[0]["method"] == "get_screen" and transport.sent[0]["device"] == "phone"
    kind, action, sent_screen = phone.calls[1]
    assert action == Action(kind="tap", element_id=0)
    assert sent_screen.screenshot_png is None      # act() doesn't upload the screenshot again
    assert sent_screen.elements == screen.elements
    assert ("start_app", "spotify") in phone.calls and ("unlock", "1234") in phone.calls


def test_remote_mac_has_no_phone_only_methods():
    """The agent/skills use hasattr(); a remote Mac must look like a local Mac."""
    mac = remote.make_remote_device("mac", transport=FakeTransport(lambda m: {}))
    phone = remote.make_remote_device("phone", transport=FakeTransport(lambda m: {}))
    for method in ("is_locked", "grant_location", "open_uri", "notifications", "sync_location"):
        assert not hasattr(mac, method)
        assert hasattr(phone, method)


@pytest.mark.parametrize("kind, expected", [
    ("ValueError", ValueError), ("NotImplementedError", NotImplementedError),
    ("KeyError", RuntimeError), ("ConnectionError", RuntimeError),
])
def test_remote_errors_are_mapped(kind, expected):
    transport = FakeTransport(lambda m: {"id": m["id"], "error": {"type": kind, "message": "nope"}})
    dev = remote.make_remote_device("phone", transport=transport)
    with pytest.raises(expected, match="nope"):
        dev.key("next")


def test_helper_turns_device_errors_into_replies():
    class Broken:
        def act(self, action, screen):
            raise ValueError("element 9 is not on the screen")

    message = {"id": "a1", "device": "phone", "method": "act",
               "args": remote.encode({"action": Action(kind="tap", element_id=9), "screen": sample_screen()})}
    reply = device_helper.handle(message, get_device=lambda name: Broken())
    assert reply == {"id": "a1", "error": {"type": "ValueError", "message": "element 9 is not on the screen"}}


def test_helper_refuses_unknown_methods_and_devices():
    reply = device_helper.handle({"id": "1", "device": "phone", "method": "__init__"},
                                 get_device=lambda name: FakePhone())
    assert "not allowed" in reply["error"]["message"]
    reply = device_helper.handle({"id": "2", "device": "mac", "method": "key", "args": {"name": "next"}},
                                 get_device=lambda name: FakePhone(), devices=["phone"])
    assert reply["error"]["type"] == "RuntimeError"


def test_helper_sync_location_runs_locally(monkeypatch):
    from relay import location
    monkeypatch.setattr(location, "sync_emulator_to_mac", lambda: True)
    reply = device_helper.handle({"id": "s", "device": "phone", "method": "sync_location"},
                                 get_device=lambda name: pytest.fail("no device needed"))
    assert reply == {"id": "s", "result": True}


def test_websocket_url():
    assert device_helper.websocket_url("https://1-2-3-4.sslip.io/") == "wss://1-2-3-4.sslip.io/device/ws"
    assert device_helper.websocket_url("http://localhost:8000") == "ws://localhost:8000/device/ws"
    assert device_helper.websocket_url("1-2-3-4.sslip.io/command") == "wss://1-2-3-4.sslip.io/device/ws"


# --- the hub: worker thread <-> asyncio loop -----------------------------------

class LoopThread:
    """A real asyncio loop running in a background thread, like uvicorn's."""

    def __enter__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        return self.loop

    def __exit__(self, *exc):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(2)
        self.loop.close()


class FakeSocket:
    """Records sent text; optionally answers through the hub like the helper would."""

    def __init__(self, hub, answer=None):
        self.hub, self.answer, self.sent = hub, answer, []

    async def send_text(self, text):
        self.sent.append(json.loads(text))
        if self.answer:
            self.hub.deliver(self.answer(json.loads(text)))

    async def close(self):
        pass


def test_hub_request_from_a_thread_gets_the_reply():
    hub = remote.Hub()
    with LoopThread() as loop:
        hub.attach(FakeSocket(hub, lambda m: {"id": m["id"], "result": True}), loop, ["phone"])
        dev = remote.RemotePhone("phone", transport=hub, timeout=2)
        assert dev.is_locked() is True


def test_hub_times_out():
    hub = remote.Hub()
    with LoopThread() as loop:
        hub.attach(FakeSocket(hub, answer=None), loop, ["phone"])  # never answers
        dev = remote.RemotePhone("phone", transport=hub, timeout=0.2)
        with pytest.raises(RuntimeError, match="didn't answer"):
            dev.key("next")
    assert hub._pending == {}  # nothing left waiting


def test_hub_without_helper_and_disconnect():
    hub = remote.Hub()
    with pytest.raises(RuntimeError, match="No device helper connected"):
        remote.RemotePhone("phone", transport=hub).key("next")
    with LoopThread() as loop:
        ws = FakeSocket(hub)
        hub.attach(ws, loop, ["mac"])
        with pytest.raises(RuntimeError, match="isn't controlling the phone"):
            remote.RemotePhone("phone", transport=hub).key("next")
        # A call waiting when the helper disconnects fails right away (not after the timeout).
        threading.Timer(0.2, hub.detach, args=[ws]).start()
        with pytest.raises(RuntimeError, match="disconnected"):
            remote.RemoteDevice("mac", transport=hub, timeout=5).key("next")
    assert not hub.connected


# --- router: RemoteDevice only in cloud mode --------------------------------------

def test_router_is_local_by_default(monkeypatch):
    monkeypatch.setattr(config, "CLOUD_MODE", False)
    fake = object()
    monkeypatch.setattr(router, "_devices", {"phone": fake})
    assert router.get_device("phone") is fake


def test_router_returns_remote_device_in_cloud_mode(monkeypatch):
    monkeypatch.setattr(config, "CLOUD_MODE", True)
    monkeypatch.setattr(router, "_devices", {"phone": object()})  # must NOT be used
    fake_hub = remote.Hub()
    monkeypatch.setattr(remote, "hub", fake_hub)
    with pytest.raises(RuntimeError, match="python -m relay.device_helper"):
        router.get_device("phone")
    fake_hub._ws = object()  # pretend a helper is connected
    assert isinstance(router.get_device("phone"), remote.RemotePhone)
    mac = router.get_device("mac")
    assert isinstance(mac, remote.RemoteDevice) and mac.name == "mac"
    with pytest.raises(ValueError):
        router.get_device("tablet")


# --- skills: location sync goes through the device when it can ----------------------

def test_navigate_syncs_location_on_the_helper(monkeypatch):
    from relay import location
    monkeypatch.setattr(location, "sync_emulator_to_mac", lambda: pytest.fail("must not run on the server"))
    monkeypatch.setattr(skills.time, "sleep", lambda s: None)
    calls = []
    transport = FakeTransport(lambda m: (calls.append(m["method"]), {"id": m["id"], "result": True})[1])
    skills.run("navigate", {"destination": "Starbucks"}, remote.make_remote_device("phone", transport=transport))
    assert calls == ["grant_location", "sync_location", "close_app", "open_uri"]


# --- the /device/ws websocket ----------------------------------------------------------

@pytest.fixture
def ws_client(monkeypatch):
    monkeypatch.setattr(config, "RELAY_TOKEN", TOKEN)
    fresh = remote.Hub()
    monkeypatch.setattr(remote, "hub", fresh)
    return TestClient(server.app), fresh


@pytest.mark.parametrize("how", ["query", "header"])
def test_websocket_accepts_good_token(ws_client, how):
    client, hub = ws_client
    kwargs = {"headers": {"Authorization": f"Bearer {TOKEN}"}} if how == "header" else {}
    url = f"/device/ws?token={TOKEN}" if how == "query" else "/device/ws"
    with client.websocket_connect(url, **kwargs) as ws:
        ws.send_text(json.dumps({"devices": ["phone", "mac"]}))
        assert ws.receive_json() == {"type": "welcome", "devices": ["phone", "mac"]}
        assert hub.connected and hub.devices == ["phone", "mac"]
    # Leaving the block closes the socket; the server detaches the helper.
    for _ in range(50):
        if not hub.connected:
            break
        threading.Event().wait(0.02)
    assert not hub.connected


@pytest.mark.parametrize("url, headers", [
    ("/device/ws", {}),
    ("/device/ws?token=wrong", {}),
    ("/device/ws", {"Authorization": "Bearer wrong"}),
])
def test_websocket_rejects_bad_token(ws_client, url, headers):
    from starlette.websockets import WebSocketDisconnect
    client, hub = ws_client
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(url, headers=headers) as ws:
            ws.receive_text()
    assert not hub.connected


def test_websocket_full_request_through_server(ws_client):
    """A worker thread calls RemoteDevice; the test plays the helper on the websocket."""
    client, hub = ws_client
    with client.websocket_connect(f"/device/ws?token={TOKEN}") as ws:
        ws.send_text(json.dumps({"devices": ["phone"]}))
        ws.receive_json()  # welcome
        result = {}
        worker = threading.Thread(target=lambda: result.update(
            screen=remote.RemotePhone("phone", transport=hub, timeout=5).get_screen(with_screenshot=True)))
        worker.start()
        request = ws.receive_json()
        assert request["method"] == "get_screen" and request["args"] == {"with_screenshot": True}
        ws.send_text(json.dumps(device_helper.handle(request, get_device=lambda name: FakePhone())))
        worker.join(5)
    assert result["screen"].screenshot_png == b"png!"


def test_health_reports_helper_only_in_cloud_mode(ws_client, monkeypatch):
    client, hub = ws_client
    monkeypatch.setattr(server, "emulator_connected", lambda: False)
    monkeypatch.setattr(config, "CLOUD_MODE", False)
    assert "device_helper_connected" not in client.get("/health").json()
    monkeypatch.setattr(config, "CLOUD_MODE", True)
    assert client.get("/health").json()["device_helper_connected"] is False
