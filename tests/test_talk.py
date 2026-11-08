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


def test_new_run_answers_pending_question(client):
    """Siri closed the Shortcut mid-question: a fresh run saying 'yes' answers it."""
    q = client.post("/talk", json={"text": "confirm send"}, headers=AUTH).json()
    assert q["next"] == "reply"
    r = client.post("/talk", json={"text": "yes send it"}, headers=AUTH).json()  # no session_id
    assert FakeTask.instances[-1].replies == ["yes send it"]
    assert r["next"] == "more"


def test_new_run_with_silence_reports_busy_task(client):
    client.post("/talk", json={"text": "slow task"}, headers=AUTH)
    r = client.post("/talk", json={"text": ""}, headers=AUTH).json()
    assert r["next"] == "wait"


def test_result_spoken_when_task_finishes_between_checkins(client):
    r = client.post("/talk", json={"text": "slow task"}, headers=AUTH).json()
    assert r["next"] == "wait"
    FakeTask.instances[-1].status = "done"       # finished while the Shortcut was talking
    r2 = client.post("/talk", json={"text": "", "session_id": r["session_id"]}, headers=AUTH).json()
    assert r2["next"] == "more" and "Okay, talk soon" not in r2["say"]
    r3 = client.post("/talk", json={"text": "", "session_id": r["session_id"]}, headers=AUTH).json()
    assert r3["say"] == "Okay, talk soon."      # only now does silence end it


# --- hangs: a stuck task, a missing Mac helper, slow audio --------------------
def test_stuck_task_is_stopped_after_max_seconds(client, monkeypatch):
    r = client.post("/talk", json={"text": "slow task"}, headers=AUTH).json()
    assert r["next"] == "wait"
    monkeypatch.setattr(server, "TASK_MAX_SECONDS", 0)
    r2 = client.post("/talk", json={"text": "", "session_id": r["session_id"]}, headers=AUTH).json()
    assert r2["say"] == server.TOO_LONG and r2["next"] == "more"
    assert FakeTask.instances[-1].stopped


def test_task_that_ignores_stop_doesnt_keep_the_loop_waiting(client, monkeypatch):
    """A task stuck in a device call stays 'running' even after stop()."""
    monkeypatch.setattr(FakeTask, "stop", lambda self: setattr(self, "stopped", True))
    r = client.post("/talk", json={"text": "slow task"}, headers=AUTH).json()
    server._started[r["session_id"]] -= 1000  # started long ago
    r2 = client.post("/talk", json={"text": "", "session_id": r["session_id"]}, headers=AUTH).json()
    assert r2["say"] == server.TOO_LONG and r2["next"] == "more"
    assert FakeTask.instances[-1].status == "running"
    # Silence now ends the chat instead of waiting on the stuck task again.
    r3 = client.post("/talk", json={"text": "", "session_id": r["session_id"]}, headers=AUTH).json()
    assert r3["say"] == "Okay, talk soon." and r3["next"] == "done"
    r4 = client.post("/talk", json={"text": ""}, headers=AUTH).json()  # fresh run, no session
    assert r4["next"] == "more" and "didn't catch" in r4["say"]
    # And a new request starts a new task.
    r5 = client.post("/talk", json={"text": "open maps", "session_id": r["session_id"]}, headers=AUTH).json()
    assert r5["say"] == "Did: open maps Anything else?" and len(FakeTask.instances) == 2


def test_running_task_under_the_cap_keeps_waiting(client):
    r = client.post("/talk", json={"text": "slow task"}, headers=AUTH).json()
    r2 = client.post("/talk", json={"text": "", "session_id": r["session_id"]}, headers=AUTH).json()
    assert r2["next"] == "wait" and not FakeTask.instances[-1].stopped


def test_mac_not_connected_in_cloud_mode(client, monkeypatch):
    monkeypatch.setattr(server.config, "CLOUD_MODE", True)
    monkeypatch.setattr(server.remote.hub, "_ws", None)
    r = client.post("/talk", json={"text": "open maps"}, headers=AUTH).json()
    assert r["say"] == server.MAC_NOT_CONNECTED and r["next"] == "done"
    assert FakeTask.instances == []  # no task started that would hang


def test_mac_connected_in_cloud_mode_starts_task(client, monkeypatch):
    monkeypatch.setattr(server.config, "CLOUD_MODE", True)
    monkeypatch.setattr(server.remote.hub, "_ws", object())
    r = client.post("/talk", json={"text": "open maps"}, headers=AUTH).json()
    assert r["say"] == "Did: open maps Anything else?"


def test_empty_answer_to_question_asks_again(client):
    """Dictation caught nothing: re-ask instead of answering the question with ''."""
    q = client.post("/talk", json={"text": "confirm send"}, headers=AUTH).json()
    r = client.post("/talk", json={"text": "", "session_id": q["session_id"]}, headers=AUTH).json()
    assert r["next"] == "reply" and r["say"] == "Are you sure?"
    assert FakeTask.instances[-1].replies == []


def test_slow_audio_doesnt_hold_up_the_reply(client, monkeypatch):
    import threading
    import time
    release = threading.Event()
    monkeypatch.setattr(server.voice, "speak", lambda text: release.wait(5) and "late")
    monkeypatch.setattr(server, "SPEAK_MAX_SECONDS", 0.1)
    start = time.monotonic()
    r = client.post("/talk", json={"text": "open maps"}, headers=AUTH).json()
    release.set()
    assert r["audio_url"] == "" and r["next"] == "more"
    assert time.monotonic() - start < 2.0


def test_resent_request_does_not_restart_task(client):
    """Broken Session variable: the Shortcut keeps re-sending the first request."""
    client.post("/talk", json={"text": "slow task"}, headers=AUTH)
    r = client.post("/talk", json={"text": "slow task"}, headers=AUTH).json()
    assert len(FakeTask.instances) == 1 and r["next"] == "wait"


def test_resent_request_reasks_question(client):
    client.post("/talk", json={"text": "confirm send"}, headers=AUTH)
    r = client.post("/talk", json={"text": "Confirm send."}, headers=AUTH).json()
    assert r["next"] == "reply" and FakeTask.instances[-1].replies == []


# --- things said in the middle of a conversation ------------------------------
def test_stop_while_running_stops_the_task(client):
    for phrase in ["stop", "cancel", "never mind", "okay, cancel that"]:
        r = client.post("/talk", json={"text": "slow task", "conversation": True}, headers=AUTH).json()
        assert r["next"] == "wait"
        task = FakeTask.instances[-1]
        s = client.post("/talk", json={"text": phrase, "session_id": r["session_id"],
                                       "conversation": True}, headers=AUTH).json()
        assert task.stopped, phrase
        assert s["say"] == "Okay, stopped. Anything else?" and s["next"] == "more"
        assert server._current is None
        # The stopped task's "Okay, I stopped." isn't spoken again; "no" now ends.
        e = client.post("/talk", json={"text": "no", "session_id": s["session_id"],
                                       "conversation": True}, headers=AUTH).json()
        assert e["say"] == "Okay, talk soon." and e["next"] == "done"


def test_stop_while_running_without_session_still_stops(client):
    client.post("/talk", json={"text": "slow task"}, headers=AUTH)
    r = client.post("/talk", json={"text": "stop", "conversation": True}, headers=AUTH).json()
    assert FakeTask.instances[-1].stopped and r["next"] == "more"


def test_stop_on_a_stuck_task_doesnt_keep_waiting(client, monkeypatch):
    monkeypatch.setattr(FakeTask, "stop", lambda self: setattr(self, "stopped", True))
    r = client.post("/talk", json={"text": "slow task"}, headers=AUTH).json()
    client.post("/talk", json={"text": "stop", "session_id": r["session_id"]}, headers=AUTH)
    assert FakeTask.instances[-1].status == "running"  # ignored stop()
    r2 = client.post("/talk", json={"text": "", "session_id": r["session_id"]}, headers=AUTH).json()
    assert r2["say"] == "Okay, talk soon." and r2["next"] == "done"


def test_stop_after_finished_task_still_ends(client):
    first = client.post("/talk", json={"text": "open maps"}, headers=AUTH).json()
    r = client.post("/talk", json={"text": "stop", "session_id": first["session_id"]}, headers=AUTH).json()
    assert r["say"] == "Okay, talk soon." and r["next"] == "done"


def test_repeat_in_talk_keeps_the_conversation_going(client):
    first = client.post("/talk", json={"text": "open maps", "conversation": True}, headers=AUTH).json()
    for phrase in ["repeat that", "say that again", "what did you say"]:
        r = client.post("/talk", json={"text": phrase, "session_id": first["session_id"],
                                       "conversation": True}, headers=AUTH).json()
        assert r["say"] == "Did: open maps" and r["next"] == "more"
    assert len(FakeTask.instances) == 1  # no task started


def test_repeat_while_running_is_a_check_in(client):
    r = client.post("/talk", json={"text": "slow task"}, headers=AUTH).json()
    r2 = client.post("/talk", json={"text": "repeat that", "session_id": r["session_id"]}, headers=AUTH).json()
    assert r2["next"] == "wait" and len(FakeTask.instances) == 1
    assert not FakeTask.instances[0].stopped


def test_repeat_during_question_asks_it_again(client):
    q = client.post("/talk", json={"text": "confirm send"}, headers=AUTH).json()
    r = client.post("/talk", json={"text": "say that again", "session_id": q["session_id"]}, headers=AUTH).json()
    assert r["next"] == "reply" and r["say"] == "Are you sure?"
    assert FakeTask.instances[-1].replies == []


def test_im_done_after_anything_else_ends(client):
    first = client.post("/talk", json={"text": "open maps"}, headers=AUTH).json()
    for ending in ["I'm done", "No.", "That's all, thanks."]:
        r = client.post("/talk", json={"text": ending, "session_id": first["session_id"]}, headers=AUTH).json()
        assert r["say"] == "Okay, talk soon." and r["next"] == "done"


def test_thanks_after_finished_task_ends(client):
    first = client.post("/talk", json={"text": "open maps"}, headers=AUTH).json()
    for thanks in ["thanks", "Thank you!", "thank you so much", "Okay, thanks."]:
        r = client.post("/talk", json={"text": thanks, "session_id": first["session_id"]}, headers=AUTH).json()
        assert r["say"] == "Okay, talk soon." and r["next"] == "done"
    assert len(FakeTask.instances) == 1


def test_new_request_replaces_running_task(client):
    r = client.post("/talk", json={"text": "slow task"}, headers=AUTH).json()
    old = FakeTask.instances[-1]
    r2 = client.post("/talk", json={"text": "open maps", "session_id": r["session_id"]}, headers=AUTH).json()
    assert old.stopped
    assert r2["say"] == "Did: open maps Anything else?" and r2["next"] == "more"
    assert r2["session_id"] != r["session_id"]


def test_check_ins_while_running_dont_replace_the_task(client):
    """Silence, a re-sent request, a stale yes, 'okay', 'thanks' are not new requests."""
    r = client.post("/talk", json={"text": "slow task"}, headers=AUTH).json()
    for text in ["", "Slow task.", "yes", "okay", "thanks", "no"]:
        r2 = client.post("/talk", json={"text": text, "session_id": r["session_id"]}, headers=AUTH).json()
        assert r2["next"] == "wait", text
    assert len(FakeTask.instances) == 1 and not FakeTask.instances[0].stopped


def test_new_request_after_timeout_still_starts(client):
    r = client.post("/talk", json={"text": "slow task"}, headers=AUTH).json()
    server._started[r["session_id"]] -= 1000
    r2 = client.post("/talk", json={"text": "", "session_id": r["session_id"]}, headers=AUTH).json()
    assert r2["say"] == server.TOO_LONG
    r3 = client.post("/talk", json={"text": "open maps", "session_id": r["session_id"]}, headers=AUTH).json()
    assert r3["say"] == "Did: open maps Anything else?"


def test_short_utterances_never_dial_911():
    from relay import safety
    for text in ["nine", "one", "nine one", "nein", "call", "help", "call mom", "9", "91"]:
        assert not safety.is_emergency(text), text
    for text in ["call 911", "nine one one", "9 1 1"]:
        assert safety.is_emergency(text), text
