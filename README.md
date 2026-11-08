# Relay

**A hands-free voice agent for anyone who can't use a screen:** blind users all the time, drivers on the
road. Say what you want, in any app. Relay reads the screen, taps and types for you, and tells you what
it's doing. It asks before it sends or pays, and it never makes you look.

"Hey Siri, hands-free mode" -> "text Mom I'm on my way" -> *"About to tap Send in Messages. Should I do
it?"* -> "yes" -> *"Sent."*

Relay drives an Android phone (an emulator over ADB) or your Mac. The iPhone (or Apple Watch) is just the
remote: a Shortcut that listens, sends your words to the server, and plays the answer.

## What it can do

Simple commands skip the AI entirely (a keyword match in `relay/skills.py`) and run in about a second.
Anything the keywords don't catch goes to a fast Claude router that picks a skill, or hands it to the
screen agent.

| Skill | Say something like | How it's done |
|---|---|---|
| Navigation | "navigate to Starbucks", "take me to 123 Main St" | Google Maps navigation intent; the emulator's GPS is set to the Mac's real location first |
| Music | "pause", "next song", "go back a song", "play on Spotify" | Media keys, optionally opening a music app first |
| Open / close apps | "open Spotify", "close Instagram" | Starts or force-stops the app |
| Volume | "turn it up", "volume down", "mute" | Volume keys |
| Inbox summary | "summarize my emails", "what came in overnight", "check my email" | Opens Gmail, reads the inbox list, Claude writes a spoken summary (never reads out login codes) |
| X news briefing | "what happened overnight", "market news", "what's new on my timeline" | Reads posts from chosen X accounts (default `DeItaone`) or your Following feed, then summarizes and attributes them |
| Workflows | "give me my morning briefing", "morning outlook" | Several skills in a row, defined in `workflows.json` (see below) |
| Describe the screen | "what's on my screen?" | One Gemini description (Claude if Gemini fails) |
| Any app (screen agent) | "reply in my Instagram group chat that I'll be late", "turn on dark mode in Settings" | The agent loop: plan, read the screen, pick a step, check safety, act, narrate |
| Mac control | "open my email on my Mac", "open Safari on the laptop" | Same agent loop on macOS (accessibility tree + screenshots, mouse and keyboard) |
| Passphrase unlock | "open sesame, open Spotify" | If the emulator is locked, Relay types `EMULATOR_PIN` itself; the AI never sees it |
| Stop | "stop", "cancel that", "never mind" | Cancels the running task |
| Emergency | "call 911", "I fell and I'm hurt" | Skips every AI step and dials right away |

**Workflows.** Copy `workflows.example.json` to `workflows.json` and edit it (Relay uses the example
until you do). It defines named X account groups and the phrases that trigger multi-step runs. The
example ships two: **morning briefing** (overnight email, then market news from the `macro` group) and
**morning outlook** (macro news from `macro`, then company news from `micro`). Steps can use
`email_summary`, `news_briefing`, or any of the phone skills above.

**Gets faster with use.** A screen-agent task that succeeds is saved to `routines.json`; next time you say
the same thing it is replayed with no AI calls. Per-app tips (`relay/playbooks.py`) help the agent in
Instagram, WhatsApp, Gmail, Maps, Spotify, DoorDash, Messages, Settings, YouTube and Chrome.

### Safety rules

- **Asks before anything you can't undo**: tapping Send, Post, Pay, Buy, Checkout, Confirm, Delete, Call,
  Transfer, Submit, Share, Book, Follow/Unfollow, Block, Report or Request pauses with a spoken question.
  Only a clear "yes" goes ahead; anything that sounds like no ("no", "wait", "don't", "hold on") cancels.
- **Never types secrets**: password, PIN, card number, CVV, SSN and account-number fields are refused,
  and so is any typed text that looks like a card number or SSN. Relay asks you to enter it yourself.
- **Emergency first**: "911", "emergency", "call an ambulance/the police", or "help" plus a word about
  being hurt dials immediately, with no AI and no confirmation.
- **Stop always works**: "stop", "cancel", "never mind" end the task; a new command replaces the old one.
- **Bounded**: at most `MAX_STEPS` (25) steps per task, and it stops if it repeats the same action 4 times.
- **Locked by token**: every route except `/health` needs `Authorization: Bearer <RELAY_TOKEN>`.
- **Unlock only with the passphrase** (if `RELAY_PASSPHRASE` is set). The PIN is typed by code over ADB.
- Summaries never read out verification codes, and news posts are attributed, not stated as fact.

## Architecture

```
 iPhone / Apple Watch Shortcut  ("hands-free mode": dictate -> POST -> play audio or Speak Text)
            |  https + Bearer token
            v
 Tunnel (ngrok permanent link, or Cloudflare quick link)        relay/tunnel.py
            |
            v
 FastAPI server  /command /reply /stop /audio /health           relay/server.py
            |    (waits up to 45 s per request; ElevenLabs voice: relay/voice.py)
            v
 Router: which device? ("on my Mac" -> Mac, else last used)     relay/router.py
            |
            v
 Task (background thread)                                       relay/agent.py
   1. emergency -> dial  |  stop words -> cancel
   2. locked? unlock with passphrase + PIN
   3. "what's on my screen" -> Gemini
   4. workflow match -> run its steps                           relay/workflows.py
   5. skill (keywords, else Claude fast router) -> run it        relay/skills.py
   6. saved routine -> replay, no AI                            relay/routines.py
   7. agent loop:
        Claude plans sub-goals
        each step: read screen -> picker -> safety check -> act -> narrate
            picker: Jev first (fast decisions via OpenRouter),  relay/brains/picker.py, jev.py
                    Claude when Jev is unsure or text is long   relay/brains/claude.py
            safety: confirm / refuse                            relay/safety.py
            |
            v
 Devices                                                        relay/devices/
   phone.py: Android emulator via uiautomator2 / adb
   mac.py:   macOS accessibility tree + screenshots, pyautogui
```

The brains, by job:

- **Jev** (TypeSafe AI, via the OpenRouter Decisions API): fast, typed step decisions with a calibrated
  confidence. Used when confidence is at least `JEV_MIN_CONFIDENCE` and no long text needs writing.
- **Claude**: planning sub-goals, the hard steps Jev isn't sure about, checking the final step, routing
  free-form requests to skills, and writing the inbox and news summaries.
- **Gemini**: describing the screen for "what's on my screen?".

## Quick start (on the Mac)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                       # fill in keys and a long random RELAY_TOKEN
cp workflows.example.json workflows.json   # optional: your own workflows and X accounts
```

Make a token with `python -c "import secrets; print(secrets.token_urlsafe(24))"`.

Start the Android emulator (Android Studio -> Device Manager -> play), then:

```bash
adb devices                   # should list emulator-5554
python -m uiautomator2 init   # one-time: installs the helper app on the emulator
```

For Mac control, give your terminal **Accessibility** and **Screen Recording** permission
(System Settings -> Privacy & Security).

Install a tunnel (once): `brew install ngrok` plus `ngrok config add-authtoken <token>` from a free
ngrok.com account (permanent link), or `brew install cloudflared` (link changes every restart). Then:

```bash
python -m relay.server --tunnel    # prints the PUBLIC LINK for the Shortcut
```

Try it without the iPhone:

```bash
curl -X POST https://your-name.ngrok-free.app/command \
  -H "Authorization: Bearer $RELAY_TOKEN" -H "ngrok-skip-browser-warning: 1" \
  -H "Content-Type: application/json" \
  -d '{"text": "what is on my screen"}'

python -m relay.cli "open settings and turn on dark mode"     # or straight from the terminal
python -m relay.cli "open Safari" --device mac -v
```

Then build the iPhone Shortcut: **[shortcut/README.md](shortcut/README.md)**.

### API

| Route | Body | Does |
|---|---|---|
| `GET /health` | | `{"ok": true, "device_default", "emulator_connected"}`, no token needed |
| `POST /command` | `{"text", "device"?}` | Starts a task (`device` = `phone`, `mac` or omitted to let the router guess) |
| `POST /reply` | `{"session_id", "text"}` | Answers a question; empty `text` on a running task waits up to 45 s for the next update |
| `POST /stop` | | Stops the running task |
| `GET /audio/{id}.mp3` | | The ElevenLabs audio for a reply |

`/command` and `/reply` return `say`, `audio_url`, `needs_reply`, `next` (`reply` / `wait` / `done`) and
`session_id`.

## Settings (`.env`)

| Variable | Default | What it does |
|---|---|---|
| `RELAY_TOKEN` | `change-me` | Required on every request. Use a long random value. |
| `ANTHROPIC_API_KEY` | | Claude: planning, hard steps, skill routing, summaries |
| `CLAUDE_MODEL` | `claude-sonnet-5-5` | Main Claude model |
| `CLAUDE_FAST_MODEL` | `claude-haiku-5-5` | Fast model for routing, planning and summaries |
| `JEV_API_KEY` | | Jev key (an OpenRouter key). Empty = Claude picks every step |
| `JEV_BASE_URL` | | Empty = OpenRouter Decisions API |
| `JEV_MODEL` | `typesafe/jev-1.14` | Jev model |
| `JEV_MIN_CONFIDENCE` | `0.75` | Below this, Claude takes the step instead |
| `GEMINI_API_KEY` | | Gemini: "what's on my screen?" |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Gemini model |
| `ELEVENLABS_API_KEY` | | Natural voice. Empty = the iPhone's Speak Text |
| `ELEVENLABS_VOICE_ID` | | Empty = the premade "Rachel" voice |
| `DEFAULT_DEVICE` | `phone` | Device used when you don't say one |
| `ADB_SERIAL` | | Which emulator; empty = the first one |
| `EMULATOR_PIN` | | Emulator lock-screen PIN, typed by code |
| `RELAY_PASSPHRASE` | | Phrase that allows unlocking, e.g. `open sesame` |
| `NEWS_ACCOUNTS` | `DeItaone` | Default X accounts for news briefings (comma-separated, no @) |
| `MAX_NEWS_ACCOUNTS` | `6` | Most accounts read in one briefing |
| `NEWS_PAGES_PER_ACCOUNT` | `2` | Screens scrolled per account |
| `WORKFLOWS_PATH` | `workflows.json` | Workflows file (falls back to `workflows.example.json`) |
| `ROUTINES_PATH` | `routines.json` | Where successful tasks are saved for replay |
| `MAX_STEPS` | `25` | Step limit per task |
| `REPLY_TIMEOUT` | `60` | Seconds Relay waits for your answer before cancelling |
| `COMMAND_WAIT_SECONDS` | `45` | How long one request waits before answering "still working" |
| `SCREENSHOT_WIDTH` | `540` | Screenshots are shrunk to this width before going to the AI |
| `SYNC_MAC_LOCATION` | `true` | Copy the Mac's location to the emulator before directions |

## Testing

```bash
python -m pytest -q
```

The tests use fakes, so they need no emulator, Mac GUI or API keys. They cover the server, agent loop,
safety rules, skills fast path, unlock, workflows, routines, brains, devices, voice, location and tunnel.

GitHub Actions (`.github/workflows/tests.yml`) runs them on every push and pull request, then posts the
result (repo, commit, branch, pass/fail, run link and the end of the pytest log) to a **Grok Bot**
webhook. Set the `GROK_BOT_WEBHOOK_URL` and `GROK_BOT_WEBHOOK_KEY` repo secrets to turn that on; without
them the report step is skipped and the tests still run.

## Built with

- **Claude**: the core agent (planning, hard steps, skill routing, summaries), and **Claude Code**,
  which helped build it.
- **Cursor + Grok Bot** (SpaceXSI): a Grok Bot automation reports every CI run.
- **Gemini API**: describes the screen.
- **ElevenLabs**: the spoken voice (`eleven_flash_v2_5`, low latency).
- **Jev** (TypeSafe AI, via OpenRouter): fast, calibrated step decisions.
