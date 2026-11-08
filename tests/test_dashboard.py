"""Live dashboard: the event bus, the /dashboard + /events routes, and who may open them."""
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from relay import config, events, server

TOKEN = "dash-token-123"


# --- event bus ---------------------------------------------------------------
def test_publish_and_since():
    a = events.publish("heard", text="open maps", device="phone")
    b = events.publish("step", text="Opening Maps")
    assert (a, b) == (1, 2)
    all_events = events.since(0)
    assert [e["kind"] for e in all_events] == ["heard", "step"]
    assert all_events[0]["text"] == "open maps" and isinstance(all_events[0]["ts"], float)
    assert [e["id"] for e in events.since(1)] == [2]
    assert events.since(2) == []
    assert events.newest_id() == 2


def test_ring_buffer_keeps_only_the_newest():
    for i in range(events.MAX_EVENTS + 50):
        events.publish("step", text=str(i))
    kept = events.since(0)
    assert len(kept) == events.MAX_EVENTS
    assert kept[-1]["text"] == str(events.MAX_EVENTS + 49)
    assert kept[0]["id"] == 51  # the first 50 were forgotten
    assert len(events.recent(100)) == 100


def test_since_after_server_restart_sends_everything():
    events.publish("step", text="a")
    # The page remembers id 900 from before a restart; ids are back at 1 now.
    assert [e["text"] for e in events.since(900)] == ["a"]


def test_publish_never_raises_and_drops_secrets():
    class Weird:
        def __str__(self):
            raise RuntimeError("no")

    eid = events.publish("step", text="x" * 5000, token="abc", pin="1234",
                         screenshot=b"\x89PNG", bad=Weird())
    assert eid is not None
    [e] = events.since(0)
    assert len(e["text"]) <= events.MAX_TEXT
    for key in ("token", "pin", "screenshot", "bad"):
        assert key not in e


def test_publish_cannot_overwrite_id_or_kind():
    events.publish("step", id=99, ts=0)
    [e] = events.since(0)
    assert e["id"] == 1 and e["kind"] == "step" and e["ts"] > 0


def test_wait_for_new_wakes_on_publish():
    threading.Timer(0.05, lambda: events.publish("step", text="later")).start()
    start = time.monotonic()
    got = events.wait_for_new(0, timeout=2)
    assert [e["text"] for e in got] == ["later"]
    assert time.monotonic() - start < 1


def test_wait_for_new_times_out_empty():
    events.publish("step")
    assert events.wait_for_new(1, timeout=0.05) == []


# --- routes --------------------------------------------------------------------
@pytest.fixture
def fast_stream(monkeypatch):
    """Make each SSE stream end after a moment so TestClient can read it whole."""
    monkeypatch.setattr(config, "RELAY_TOKEN", TOKEN)
    monkeypatch.setattr(server, "STREAM_SECONDS", 0.3)
    monkeypatch.setattr(server, "WAIT_STEP_SECONDS", 0.05)


def local_client():
    return TestClient(server.app, client=("127.0.0.1", 50000))


def remote_client():
    return TestClient(server.app, client=("203.0.113.9", 50000))


def sse_events(body: str) -> list[dict]:
    """Parse the `data:` lines of a text/event-stream body (skipping the hello event)."""
    out = []
    for block in body.split("\n\n"):
        lines = block.splitlines()
        if any(line.startswith("event: hello") for line in lines):
            continue
        for line in lines:
            if line.startswith("data: "):
                out.append(json.loads(line[len("data: "):]))
    return out


def test_dashboard_page_served_locally(fast_stream):
    r = local_client().get("/dashboard")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    page = r.text
    assert "<title>Relay Live</title>" in page and "/events/stream" in page
    assert ".innerHTML" not in page  # event text is only ever set with textContent


def test_recent_returns_json(fast_stream):
    events.publish("heard", text="open maps")
    events.publish("step", text="Opening Maps")
    body = local_client().get("/events/recent").json()
    assert [e["kind"] for e in body["events"]] == ["heard", "step"]
    assert body["newest_id"] == 2


def test_stream_is_event_stream_with_published_events(fast_stream):
    events.publish("heard", text="text mom")
    events.publish("step", text="Opening Messages")
    with local_client().stream("GET", "/events/stream?since=0") as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        body = r.read().decode()
    assert "event: hello" in body and "id: 1\n" in body
    assert [e["text"] for e in sse_events(body)] == ["text mom", "Opening Messages"]


def test_stream_resumes_after_since_and_last_event_id(fast_stream):
    for word in ("a", "b", "c"):
        events.publish("step", text=word)
    with local_client().stream("GET", "/events/stream?since=2") as r:
        assert [e["text"] for e in sse_events(r.read().decode())] == ["c"]
    with local_client().stream("GET", "/events/stream", headers={"Last-Event-ID": "1"}) as r:
        assert [e["text"] for e in sse_events(r.read().decode())] == ["b", "c"]


def test_stream_delivers_events_published_while_open(fast_stream):
    events.publish("step", text="old")  # already happened: not sent without ?since
    threading.Timer(0.1, lambda: events.publish("step", text="live")).start()
    with local_client().stream("GET", "/events/stream") as r:
        assert [e["text"] for e in sse_events(r.read().decode())] == ["live"]


def test_stream_sends_keepalive(fast_stream, monkeypatch):
    monkeypatch.setattr(server, "KEEPALIVE_SECONDS", 0.0)
    with local_client().stream("GET", "/events/stream") as r:
        assert ": keepalive" in r.read().decode()


# --- who may open the dashboard ------------------------------------------------
ROUTES = ["/dashboard", "/events/recent", "/events/stream"]


@pytest.mark.parametrize("path", ROUTES)
def test_localhost_needs_no_token(fast_stream, path):
    assert local_client().get(path).status_code == 200
    ipv6 = TestClient(server.app, client=("::1", 50000))
    assert ipv6.get(path).status_code == 200


@pytest.mark.parametrize("path", ROUTES)
def test_remote_needs_token(fast_stream, path):
    c = remote_client()
    assert c.get(path).status_code == 401
    assert c.get(path, params={"token": "wrong"}).status_code == 401
    assert c.get(path, params={"token": TOKEN}).status_code == 200
    assert c.get(path, headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200


@pytest.mark.parametrize("header", [
    {"X-Forwarded-For": "198.51.100.7"},          # ngrok and most proxies
    {"X-Forwarded-Proto": "https"},
    {"Forwarded": "for=198.51.100.7"},
    {"CF-Connecting-IP": "198.51.100.7"},         # Cloudflare tunnel
    {"ngrok-skip-browser-warning": "1"},
    {"X-Real-IP": "198.51.100.7"},
])
def test_tunnel_traffic_from_127_is_treated_as_remote(fast_stream, header):
    # ngrok/cloudflared connect from 127.0.0.1, but add these headers. Must need the token.
    c = local_client()
    for path in ROUTES:
        assert c.get(path, headers=header).status_code == 401
        assert c.get(path, headers=header, params={"token": TOKEN}).status_code == 200


def test_dashboard_auth_does_not_open_other_routes(fast_stream):
    # Localhost is only special for the dashboard: /command still needs the Bearer token.
    assert local_client().post("/command", json={"text": "hi"}).status_code == 401


def test_empty_token_never_matches(fast_stream, monkeypatch):
    monkeypatch.setattr(config, "RELAY_TOKEN", "")
    assert remote_client().get("/events/recent", params={"token": ""}).status_code == 401


# --- server publishes what it hears and says ------------------------------------
def test_command_publishes_heard_and_answer(monkeypatch):
    from tests.test_server import AUTH, FakeTask
    from tests.test_server import TOKEN as SERVER_TOKEN
    from relay import voice

    FakeTask.instances = []
    monkeypatch.setattr(config, "RELAY_TOKEN", SERVER_TOKEN)
    monkeypatch.setattr(config, "RELAY_PASSPHRASE", "open sesame")
    monkeypatch.setattr(server.agent, "Task", FakeTask)
    monkeypatch.setattr(voice, "speak", lambda text: None)
    monkeypatch.setattr(server, "_tasks", {})
    monkeypatch.setattr(server, "_current", None)
    c = TestClient(server.app)
    c.post("/command", json={"text": "open sesame, open maps"}, headers=AUTH)
    c.post("/command", json={"text": "repeat that"}, headers=AUTH)
    got = [(e["kind"], e.get("text") or e.get("say")) for e in events.since(0)]
    assert got == [("heard", "open maps"), ("answer", "Did: open sesame, open maps"),
                   ("heard", "repeat that"), ("repeat", "Did: open sesame, open maps")]
    assert "sesame" not in json.dumps(events.since(0)[0])  # passphrase never shown on the projector


def test_banner_shows_dashboard_url():
    assert "http://localhost:8000/dashboard" in server.startup_banner(["192.168.1.20"], "x" * 30)
