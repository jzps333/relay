"""'Read my notifications': dump parsing, the skill, and the agent handler. No emulator, no AI."""
from types import SimpleNamespace

from relay import agent, context, skills
from relay.devices.phone import Phone, parse_notifications
from tests.test_agent import env, run  # noqa: F401  (env is a pytest fixture)

# A trimmed but realistic `adb shell dumpsys notification --noredact` from an
# Android 14 emulator: a text from Mom, a WhatsApp group message, a login code,
# plus things that must be skipped (USB debugging, Spotify "now playing",
# a foreground service, a group summary, uiautomator's own, a snoozed one).
SAMPLE_DUMP = """\
Current Notification Manager state:
  Notification List:
    NotificationRecord(0x0e5c1a2b: pkg=android user=UserHandle{-1} id=26 tag=null importance=2 key=-1|android|26|null|1000: Notification(channel=DEVELOPER_IMPORTANT shortcut=null contentView=null vibrate=null sound=null defaults=0x0 flags=0x2 color=0xff607d8b vis=PUBLIC))
      uid=1000 userId=-1
      opPkg=android
      flags=0x2
      key=-1|android|26|null|1000
      notification=
        extras={
          android.title=String (USB debugging connected)
          android.text=String (Tap to turn off USB debugging)
        }
      mCreationTimeMs=1731090000000
    NotificationRecord(0x0b7d9e11: pkg=com.google.android.apps.messaging user=UserHandle{0} id=12 tag=null importance=4 key=0|com.google.android.apps.messaging|12|null|10123: Notification(channel=conversations shortcut=mom contentView=null vibrate=null sound=null defaults=0x0 flags=0x10 color=0xff1a73e8 category=msg actions=2 vis=PRIVATE))
      uid=10123 userId=0
      opPkg=com.google.android.apps.messaging
      flags=0x10
      key=0|com.google.android.apps.messaging|12|null|10123
      seen=false
      notification=
        pri=1
        extras={
          android.title=String (Mom)
          android.reduced.images=Boolean (true)
          android.subText=null
          android.template=String (android.app.Notification$MessagingStyle)
          android.text=String (where are you?)
          android.messages=Parcelable[] (1)
        }
      mCreationTimeMs=1731093000000
      mUpdateTimeMs=1731093000000
    NotificationRecord(0x05a0c3d4: pkg=com.whatsapp user=UserHandle{0} id=1 tag=null importance=4 key=0|com.whatsapp|1|null|10200: Notification(channel=group_chat_defaults_3 shortcut=null contentView=null vibrate=null sound=null defaults=0x0 flags=0x18 color=0xff075e54 category=msg vis=PRIVATE))
      flags=0x18
      key=0|com.whatsapp|1|null|10200
      notification=
        extras={
          android.title=Study Group (java.lang.String)
          android.text=Sam: quiz moved to 3 (java.lang.String)
          android.bigText=Sam: quiz moved to 3pm, bring calculators (java.lang.String)
        }
      mCreationTimeMs=1731092000000
    NotificationRecord(0x0c1d2e3f: pkg=com.whatsapp user=UserHandle{0} id=0 tag=null importance=4 key=0|com.whatsapp|0|summary|10200: Notification(channel=group_chat_defaults_3 contentView=null vibrate=null sound=null defaults=0x0 flags=0x218 vis=PRIVATE))
      flags=0x218
      key=0|com.whatsapp|0|summary|10200
      notification=
        extras={
          android.title=String (WhatsApp)
          android.text=String (2 new messages)
        }
      mCreationTimeMs=1731092000001
    NotificationRecord(0x0d4e5f60: pkg=com.spotify.music user=UserHandle{0} id=4 tag=null importance=2 key=0|com.spotify.music|4|null|10150: Notification(channel=playback_channel contentView=null vibrate=null sound=null defaults=0x0 flags=0x62 category=transport vis=PUBLIC))
      flags=0x62
      key=0|com.spotify.music|4|null|10150
      notification=
        extras={
          android.title=String (Blinding Lights)
          android.text=String (The Weeknd)
        }
      mCreationTimeMs=1731091000000
    NotificationRecord(0x0f0a1b2c: pkg=com.example.tracker user=UserHandle{0} id=9 tag=null importance=1 key=0|com.example.tracker|9|null|10300: Notification(channel=svc contentView=null vibrate=null sound=null defaults=0x0 flags=0x40 vis=PUBLIC))
      flags=0x40
      key=0|com.example.tracker|9|null|10300
      notification=
        extras={
          android.title=String (Tracker is running)
        }
    NotificationRecord(0x01112131: pkg=com.github.uiautomator user=UserHandle{0} id=1 tag=null importance=2 key=0|com.github.uiautomator|1|null|10400: Notification(channel=default contentView=null vibrate=null sound=null defaults=0x0 flags=0x10 vis=PUBLIC))
      flags=0x10
      key=0|com.github.uiautomator|1|null|10400
      notification=
        extras={
          android.title=String (ATX Agent)
          android.text=String (Running)
        }
    NotificationRecord(0x02223242: pkg=com.google.android.gm user=UserHandle{0} id=77 tag=null importance=3 key=0|com.google.android.gm|77|null|10110: Notification(channel=mail contentView=null vibrate=null sound=null defaults=0x0 flags=0x10 category=email vis=PRIVATE))
      flags=0x10
      key=0|com.google.android.gm|77|null|10110
      notification=
        extras={
          android.title=String (Chase)
          android.text=SpannableString (Your Chase verification code is 482913)
        }
      mCreationTimeMs=1731091500000
  Enqueued Notification List:
    NotificationRecord(0x0b7d9e11: pkg=com.google.android.apps.messaging user=UserHandle{0} id=12 tag=null importance=4 key=0|com.google.android.apps.messaging|12|null|10123: Notification(channel=conversations flags=0x10))
      flags=0x10
      key=0|com.google.android.apps.messaging|12|null|10123
        extras={
          android.title=String (Mom)
          android.text=String (where are you?)
        }
  Snoozed notifications:
    NotificationRecord(0x03334353: pkg=com.instagram.android user=UserHandle{0} id=5 tag=null importance=3 key=0|com.instagram.android|5|null|10500: Notification(channel=ig flags=0x10))
      flags=0x10
        extras={
          android.title=String (Instagram)
          android.text=String (snoozed thing)
        }
"""


# ---- parse_notifications ----

def test_parse_keeps_real_notifications_newest_first():
    got = parse_notifications(SAMPLE_DUMP)
    assert got == [
        {"package": "com.google.android.apps.messaging", "title": "Mom", "text": "where are you?",
         "time": 1731093000000},
        {"package": "com.whatsapp", "title": "Study Group",
         "text": "Sam: quiz moved to 3pm, bring calculators", "time": 1731092000000},  # bigText wins
        {"package": "com.google.android.gm", "title": "Chase",
         "text": "Your Chase verification code is 482913", "time": 1731091500000},
    ]


def test_parse_skips_system_ongoing_service_summary_own_and_snoozed():
    packages = [n["package"] for n in parse_notifications(SAMPLE_DUMP)]
    for skipped in ("android", "com.spotify.music", "com.example.tracker", "com.github.uiautomator",
                    "com.instagram.android"):
        assert skipped not in packages
    titles = [n["title"] for n in parse_notifications(SAMPLE_DUMP)]
    assert "WhatsApp" not in titles          # the group summary
    assert titles.count("Mom") == 1          # duplicate in the Enqueued list


def test_parse_empty_or_garbage():
    assert parse_notifications("") == []
    assert parse_notifications("Current Notification Manager state:\n  Notification List:\n") == []
    assert parse_notifications("nothing useful here") == []


def test_parse_missing_time_is_none():
    dump = ("NotificationRecord(0x1: pkg=com.whatsapp user=UserHandle{0} id=1 key=0|a|1: Notification(flags=0x10))\n"
            "  extras={\n    android.title=String (Dad)\n    android.text=String (call me)\n  }\n")
    assert parse_notifications(dump) == [{"package": "com.whatsapp", "title": "Dad", "text": "call me", "time": None}]


def test_phone_notifications_runs_dumpsys():
    calls = []
    fake_u2 = SimpleNamespace(shell=lambda cmd: calls.append(cmd) or SimpleNamespace(output=SAMPLE_DUMP))
    got = Phone(device=fake_u2).notifications()
    assert calls == ["dumpsys notification --noredact"]
    assert got[0]["title"] == "Mom"


# ---- the skill ----

def test_notification_keywords():
    for said in ("read my notifications", "Hey Relay, any new messages?", "what did I miss",
                 "read my texts please", "any notifications"):
        assert skills.match_keywords(said) == ("notifications", {}), said
    assert "notifications" in skills.SKILL_NAMES
    assert skills.match_keywords("read my texts from mom and reply") is None


def test_summarize_no_notifications_skips_ai(monkeypatch):
    monkeypatch.setattr(skills, "_spoken_summary", lambda *a: (_ for _ in ()).throw(AssertionError("no AI")))
    assert skills.summarize_notifications([]) == "No new notifications."


def test_summarize_notifications_prompt(monkeypatch):
    seen = {}

    def fake(system, user_text, fallback):
        seen.update(system=system, user=user_text)
        return "You got a text from Mom: where are you?"

    monkeypatch.setattr(skills, "_spoken_summary", fake)
    out = skills.summarize_notifications(parse_notifications(SAMPLE_DUMP))
    assert out == "You got a text from Mom: where are you?"
    assert "Mom: where are you?" in seen["user"]
    assert "NEVER read out verification codes" in seen["system"]
    assert "login code from X" in seen["system"]
    assert "Under 80 words" in seen["system"]


def test_notification_lines_show_age():
    items = [{"package": "com.whatsapp", "title": "Dad", "text": "call me", "time": 1_000_000_000_000}]
    assert skills.notification_lines(items, now=1_000_000_000 + 300) == ["[5 min ago] com.whatsapp | Dad: call me"]
    assert skills.notification_lines([{"package": "p", "title": "T", "text": "x", "time": None}]) == ["p | T: x"]


# ---- the agent handler ----

def test_agent_reads_notifications_and_remembers_them(env, monkeypatch):
    env.device.notifications = lambda: parse_notifications(SAMPLE_DUMP)
    monkeypatch.setattr(skills, "_spoken_summary", lambda *a: "You got a text from Mom: where are you?")
    t = run("read my notifications")
    assert t.status == "done"
    assert t.result == "You got a text from Mom: where are you?"
    assert env.device.acted == []                      # nothing tapped
    ctx = context.get()
    assert ctx["source"] == "notifications"
    assert ctx["summary"] == "You got a text from Mom: where are you?"
    assert any("Mom: where are you?" in line for line in ctx["lines"])


def test_agent_no_notifications(env):
    env.device.notifications = lambda: []
    t = run("any notifications")
    assert t.result == "No new notifications."
    assert context.get() is None                       # nothing worth remembering


def test_workflow_step_can_read_notifications(env, monkeypatch):
    from relay import workflows, safety
    env.device.notifications = lambda: []
    flow = workflows.Workflow(name="x", phrases=[], steps=[workflows.Step(skill="notifications", intro="First.")])
    task = agent.Task("x", device_name="phone")
    assert task._run_workflow(env.device, safety, skills, flow) == "First. No new notifications."
