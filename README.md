# Relay

Relay is a voice agent for blind and motor-impaired people. You say "Hey Siri, Relay", ask for something
("text Mom I'm on my way", "what's on my screen?", "open my email on the Mac"), and Relay does it for you
by reading the screen and tapping/typing on an Android phone (emulator) or on your Mac, telling you what
it's doing as it goes. It asks before anything risky (sending, paying, deleting), refuses to type
passwords or card numbers, and "call 911" skips every AI step and dials right away.

## Quick start (on the Mac)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then fill in the keys and pick a long random RELAY_TOKEN
```

Start the Android emulator (Android Studio -> Device Manager -> play), then check ADB sees it:

```bash
adb devices                   # should list emulator-5554
python -m uiautomator2 init   # one-time: installs the helper app on the emulator
```

For Mac control, give your terminal **Accessibility** and **Screen Recording** permission
(System Settings -> Privacy & Security).

Run the server:

```bash
python -m relay.server   # prints the URL to put in your iPhone Shortcut
```

Try it without the phone:

```bash
curl -X POST http://localhost:8000/command \
  -H "Authorization: Bearer $RELAY_TOKEN" -H "Content-Type: application/json" \
  -d '{"text": "what is on my screen", "device": "phone"}'
```

Then build the iPhone Shortcut: see [shortcut/README.md](shortcut/README.md).

Tests (no devices or keys needed): `python -m pytest -q`

## How it works

The iPhone Shortcut dictates your words and POSTs them to the FastAPI server on the Mac
(`relay/server.py`). The router (`relay/router.py`) decides which device you mean ("on my Mac" -> Mac,
otherwise the last device used). Each command becomes a `Task` (`relay/agent.py`) running in a background
thread: emergencies dial immediately, stop words cancel, "what's on my screen" goes straight to Gemini,
and a saved routine (`relay/routines.py`) is replayed with no AI at all when one exists. Otherwise Claude
plans short sub-goals, and each step reads the screen (`relay/devices/`), asks the picker
(`relay/brains/picker.py`: a fast Jev model first, Claude when Jev is unsure), checks the action with
`relay/safety.py` (pausing to ask you "Send this to Mom?" when needed), then acts and narrates. A task
that works is saved as a routine so next time is instant. The server waits up to 25 seconds so one
request usually returns the final answer, turns the reply into speech with ElevenLabs (`relay/voice.py`),
and the Shortcut plays it, or uses iOS's own voice if no ElevenLabs key is set.
