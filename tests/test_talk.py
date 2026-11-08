"""The /talk conversation loop: answers, waits, new requests, "Anything else?", endings."""
from relay import server
from tests.test_server import AUTH, FakeTask, client  # noqa: F401  (fixture)


def test_finished_task_asks_anything_else(client):
    r = client.post("/talk", json={"text": "open maps"}, headers=AUTH).json()
    assert r["say"].endswith("Anything else?")
    assert r["next"] == "more"


def test_followup_request_starts_new_task(client):
    first = client.post("/talk", json={"text": "open maps"}, headers=AUTH).json()
    r = client.post("/talk", json={"text": "next song", "session_id": first["session_id"]}, headers=AUTH).json()
    assert r["say"] == "Did: next song Anything else?"


def test_no_ends_the_conversation(client):
    first = client.post("/talk", json={"text": "open maps"}, headers=AUTH).json()
    for ending in ["no", "That's all.", "nope", "I'm good", ""]:
        r = client.post("/talk", json={"text": ending, "session_id": first["session_id"]}, headers=AUTH).json()
        assert r["next"] == "done" and r["say"] == "Okay, talk soon."


def test_question_gets_answered_via_talk(client):
    q = client.post("/talk", json={"text": "confirm send"}, headers=AUTH).json()
    assert q["next"] == "reply"
    r = client.post("/talk", json={"text": "yes", "session_id": q["session_id"]}, headers=AUTH).json()
    assert FakeTask.instances[-1].replies == ["yes"]
    assert r["next"] == "more"


def test_still_working_then_wait(client):
    r = client.post("/talk", json={"text": "slow task"}, headers=AUTH).json()
    assert r["next"] == "wait"
    r2 = client.post("/talk", json={"text": "", "session_id": r["session_id"]}, headers=AUTH).json()
    assert r2["next"] == "wait"  # the fake never finishes; it keeps reporting progress


def test_command_without_conversation_unchanged(client):
    r = client.post("/command", json={"text": "open maps"}, headers=AUTH).json()
    assert r["next"] == "done" and not r["say"].endswith("Anything else?")


def test_repeat_skips_the_question(client):
    client.post("/talk", json={"text": "open maps"}, headers=AUTH)
    r = client.post("/command", json={"text": "repeat that"}, headers=AUTH).json()
    assert r["say"] == "Did: open maps"


def test_nothing_heard_at_start_asks_again(client):
    r = client.post("/talk", json={"text": ""}, headers=AUTH).json()
    assert r["next"] == "more" and "didn't catch" in r["say"]
