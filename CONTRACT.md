# Relay — shared interfaces (every module codes against this)

Relay: voice agent for blind / motor-impaired users. iPhone (Shortcut, AirPods) is the remote.
The Mac runs a FastAPI server; an agent loop controls either an Android emulator (ADB) or the Mac itself.

Python 3.11+. Keys come from `.env` via `relay/config.py`. No module imports another module's internals —
only the names below.

## Layout and owners

| Path | Owner agent |
| --- | --- |
| `relay/config.py`, `relay/types.py` | already written (do not change signatures) |
| `relay/devices/base.py`, `relay/devices/phone.py` | A — phone |
| `relay/devices/mac.py` | B — mac |
| `relay/brains/jev.py`, `relay/brains/claude.py`, `relay/brains/gemini.py`, `relay/brains/picker.py` | C — brains |
| `relay/server.py`, `relay/voice.py`, `relay/router.py`, `relay/agent.py`, `shortcut/README.md` | D — server + loop |
| `relay/safety.py`, `relay/routines.py` | E — safety + routines |

Each agent also writes its own tests in `tests/test_<module>.py` that run WITHOUT a phone, Mac GUI or API keys
(use fakes). Do not edit files you do not own; if you need a change to a shared type, note it in your final report.

## Types (`relay/types.py`)

```python
@dataclass
class Element:
    id: int            # index in this screen snapshot
    text: str          # visible text or content-description
    kind: str          # "button" | "input" | "text" | "image" | "other"
    bounds: tuple[int, int, int, int]  # x1, y1, x2, y2
    clickable: bool

@dataclass
class Screen:
    device: str                 # "phone" | "mac"
    app: str                    # foreground app / package
    elements: list[Element]
    screenshot_png: bytes | None = None

@dataclass
class Action:
    kind: str          # "tap" | "type" | "scroll" | "back" | "home" | "open_app" | "done" | "ask_user"
    element_id: int | None = None
    text: str | None = None      # for type / open_app / ask_user / done (final spoken result)
    direction: str | None = None # "up" | "down" for scroll
    confidence: float = 1.0
    source: str = ""             # "jev" | "claude" | "routine"
    reason: str = ""
```

## Device interface (`relay/devices/base.py`, implemented by phone.py and mac.py)

```python
class Device(Protocol):
    name: str                                   # "phone" | "mac"
    def get_screen(self, with_screenshot: bool = False) -> Screen: ...
    def act(self, action: Action, screen: Screen) -> None: ...   # executes tap/type/scroll/back/home/open_app
    def emergency_call(self) -> None: ...       # phone: dial 911 via the system dialer; mac: raise NotImplementedError
```

## Brains (`relay/brains/`)

```python
# claude.py
def plan(goal: str, device: str) -> list[str]                       # short sub-goals
def pick_action(goal: str, subgoal: str, screen: Screen, history: list[Action]) -> Action
def describe_screen(screen: Screen) -> str                           # fallback describer
# jev.py
def pick_action(goal: str, subgoal: str, screen: Screen, history: list[Action]) -> Action  # sets confidence
# gemini.py   (Best Use of Gemini API prize)
def describe_screen(screen: Screen) -> str                           # "what's on my screen?"
# picker.py
def choose_action(goal, subgoal, screen, history) -> Action          # Jev first, Claude if confidence < config.JEV_MIN_CONFIDENCE or Jev errors
```

## Safety + routines

```python
# safety.py
def is_emergency(utterance: str) -> bool        # "911", "emergency", "call an ambulance"...
def is_stop(utterance: str) -> bool             # "stop", "cancel", "never mind"
def needs_confirmation(action: Action, screen: Screen) -> str | None   # spoken question, or None if safe
# routines.py   (saved routines: replay a task that worked, no AI calls)
def find(goal: str) -> Routine | None
def save(goal: str, device: str, steps: list[tuple[Screen, Action]]) -> None
def replay(routine: Routine, device: Device) -> bool   # match each step's element by text, not id; False if a step can't be matched
```

## Server (`relay/server.py`, FastAPI)

- `POST /command` body `{"text": str, "device": "phone" | "mac" | null}` -> `{"say": str, "audio_url": str | null, "needs_reply": bool, "session_id": str}`
- `POST /reply` body `{"session_id": str, "text": str}` (answers a confirmation question)
- `POST /stop` stops the running task
- `GET /audio/{id}.mp3` ElevenLabs audio of the last reply
- Bearer token check using `config.RELAY_TOKEN` on every route.

## Agent loop rules (`relay/agent.py`)

1. Emergency -> `device.emergency_call()` immediately, no LLM. Stop -> cancel.
2. Routine exists -> replay; fall back to the loop if it fails.
3. Claude plans sub-goals; per step: get_screen -> picker.choose_action -> safety check -> act -> narrate.
4. Hard stop at `config.MAX_STEPS` (25). Never type passwords or card numbers; stop and tell the user.
5. On success, save the routine.
