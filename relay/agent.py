"""The agent loop: one spoken command -> a Task that runs in a background thread.

Order of business (CONTRACT.md "Agent loop rules"):
  1. Emergency -> device.emergency_call() right away, no AI.   Stop words -> cancel.
  2. "What's on my screen?" -> one Gemini description, no loop (fast path).
  3. A saved routine for this goal -> replay it (no AI). If it fails, fall through.
  4. Claude plans sub-goals, then per step:
        get_screen -> picker.choose_action -> safety check -> act -> narrate
     Stop flag checked every step, hard limit config.MAX_STEPS.
  5. Success -> save the steps as a routine for next time.

The web server talks to a Task through a few thread-safe methods:
  start(), wait_until_settled(), reply(text), stop(), say()
"""
from __future__ import annotations

import re
import threading
import time
import uuid
from typing import TYPE_CHECKING

from relay import config, router
from relay.types import Action, Screen

if TYPE_CHECKING:  # only for type hints; the real import happens lazily in _work()
    from relay.routines import Routine

SETTLE_SECONDS = 0.7  # pause after each action so we never read a half-changed screen
REPLY_TIMEOUT = float(config.__dict__.get("REPLY_TIMEOUT", 60.0))  # seconds to wait for an answer before cancelling

_YES = {"yes", "yeah", "yep", "yup", "sure", "ok", "okay", "confirm", "do it", "go ahead", "please"}
_DESCRIBE = re.compile(r"(?i)what'?s on (?:my|the) screen|describe (?:my |the )?screen|read (?:my |the )?screen")
# Rule 4: never type into these, even if a brain asks to.
_SECRET_FIELD = re.compile(r"(?i)password|passcode|\bpin\b|card number|credit card|cvv|cvc|security code")


class Cancelled(Exception):
    """Raised inside the loop when the user stops the task (or never answers)."""


def _is_yes(text: str) -> bool:
    # Use the shared safety check so "okay, don't" counts as NO, not yes.
    from relay import safety
    if hasattr(safety, "is_yes"):
        return safety.is_yes(text)  # is_yes already returns False when there's a "no"
    # Fallback (used by tests that swap in a fake safety module).
    t = text.lower().strip(" .!?")
    return t in _YES or t.startswith(("yes", "yeah", "sure"))


def _label(action: Action, screen: Screen) -> str:
    """Text of the element an action targets ("" if none)."""
    for el in screen.elements:
        if el.id == action.element_id:
            return el.text
    return ""


def _describe_action(action: Action, screen: Screen) -> str:
    """A short spoken update like "Tapping Send" for the narration list."""
    label = ""
    if action.element_id is not None and 0 <= action.element_id < len(screen.elements):
        label = screen.elements[action.element_id].text
    if action.kind == "tap":
        return f"Tapping {label}" if label else "Tapping"
    if action.kind == "type":
        return f"Typing {action.text}"
    if action.kind == "scroll":
        return f"Scrolling {action.direction or 'down'}"
    if action.kind == "open_app":
        return f"Opening {action.text}"
    if action.kind == "back":
        return "Going back"
    if action.kind == "home":
        return "Going home"
    return action.kind


def strip_passphrase(text: str) -> tuple[str, bool]:
    """Remove the unlock passphrase from what the user said.

    Returns (text without the passphrase, whether it was said). The passphrase is
    removed BEFORE anything goes to an AI, so it never shows up in prompts or logs.
    """
    phrase = config.RELAY_PASSPHRASE.strip().lower()
    if not phrase:
        return text, False
    pattern = re.compile(r"\b" + r"\W*".join(map(re.escape, phrase.split())) + r"\b[\s,.!]*", re.I)
    cleaned, n = pattern.subn("", text, count=1)
    return cleaned.strip(" ,."), n > 0


class Task:
    def __init__(self, text: str, device_name: str | None = None, last_device: str | None = None):
        self.id = uuid.uuid4().hex[:8]
        text, self.passphrase_said = strip_passphrase(text)
        self.original_text = text
        if device_name:
            self.device_name, self.goal = device_name, text.strip()
        else:
            self.device_name, self.goal = router.pick_device(text, last_device)

        # State the server reads. Every change goes through self._cv so waiters wake up.
        self.status = "new"          # new | running | waiting | done | cancelled | error
        self.narration: list[str] = []
        self.question: str | None = None   # set while status == "waiting"
        self.result: str | None = None     # final thing to say

        self._cv = threading.Condition()
        self._stop = threading.Event()
        self._reply_ready = threading.Event()
        self._reply_text = ""
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ server API
    def start(self) -> "Task":
        self._set(status="running")
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"task-{self.id}")
        self._thread.start()
        return self

    @property
    def finished(self) -> bool:
        return self.status in ("done", "cancelled", "error")

    def wait_until_settled(self, timeout: float) -> None:
        """Block until the task finishes or pauses on a question (or timeout)."""
        with self._cv:
            self._cv.wait_for(lambda: self.finished or self.status == "waiting", timeout)

    def wait_for_change(self, timeout: float) -> None:
        """Block until anything new happens (more narration, a question, finishing)."""
        with self._cv:
            before = (self.status, len(self.narration))
            self._cv.wait_for(lambda: (self.status, len(self.narration)) != before or self.finished, timeout)

    def reply(self, text: str) -> None:
        """The user's answer to self.question."""
        self._reply_text = text
        # Flip back to "running" right now so a server waiting on wait_until_settled()
        # doesn't immediately see the OLD "waiting" status and return early.
        if self.status == "waiting":
            self._set(status="running", question=None)
        self._reply_ready.set()

    def stop(self) -> None:
        self._stop.set()
        self._reply_ready.set()  # wake the loop if it's waiting for an answer

    def say(self) -> str:
        """The single most useful thing to speak right now."""
        if self.finished:
            return self.result or "Done."
        if self.status == "waiting" and self.question:
            return self.question
        if self.narration:
            return self.narration[-1] + ". I'm still working on it."
        return "Working on it."

    # ------------------------------------------------------------------ helpers
    def _set(self, **fields) -> None:
        with self._cv:
            for k, v in fields.items():
                setattr(self, k, v)
            self._cv.notify_all()

    def _narrate(self, line: str) -> None:
        with self._cv:
            self.narration.append(line)
            self._cv.notify_all()

    def _check_stop(self) -> None:
        if self._stop.is_set():
            raise Cancelled("Okay, I stopped.")

    def _ask(self, question: str) -> str:
        """Pause, speak `question`, and wait for /reply. Timeout or stop -> Cancelled."""
        self._reply_ready.clear()
        self._check_stop()  # stop() may have fired just before we cleared the event
        self._set(status="waiting", question=question)
        got = self._reply_ready.wait(REPLY_TIMEOUT)
        self._set(status="running", question=None)
        self._check_stop()
        if not got:
            raise Cancelled("I didn't hear back, so I cancelled.")
        from relay import safety
        if safety.is_stop(self._reply_text):
            raise Cancelled("Okay, I stopped.")
        return self._reply_text

    # ------------------------------------------------------------------ the loop
    def _run(self) -> None:
        try:
            result = self._work()
            self._set(status="done", result=result)
        except Cancelled as c:
            self._set(status="cancelled", result=str(c))
        except Exception as e:  # never let a crash leave the user hanging in silence
            print(f"[agent] task {self.id} crashed: {e!r}")
            self._set(status="error", result="Sorry, something went wrong and I had to stop.")

    def _work(self) -> str:
        from relay import safety, routines  # lazy: other agents write these modules

        # 1. Emergency first: no AI, no planning, no confirmation.
        if safety.is_emergency(self.original_text):
            phone = router.get_device("phone")  # emergency calls always go through the phone
            phone.emergency_call()
            return "Calling 911 now."
        if safety.is_stop(self.original_text):
            raise Cancelled("Okay, cancelled.")

        device = router.get_device(self.device_name)

        # 2. Locked emulator? Unlock only if the user said the passphrase.
        self._unlock_if_needed(device)
        if not self.goal:
            return "Unlocked. What would you like me to do?"

        # 3. Fast path: just describe the screen.
        if _DESCRIBE.search(self.goal):
            return self._describe(device)

        # 4. Skills: instant shortcuts (Maps, music, open/close apps, volume).
        from relay import skills
        picked = skills.route(self.goal, self.device_name)
        if picked is not None:
            name, params = picked
            try:
                said = skills.run(name, params, device)
            except Exception as e:
                print(f"[agent] skill {name} failed, using the screen agent: {e!r}")
            else:
                # A shortcut can land on a first-run screen, sign-in prompt or
                # permission popup instead of doing the thing. Check, and if
                # something is in the way, let the screen agent finish the job.
                blocker = skills.find_blocker(device) if name in skills.CHECK_AFTER else None
                if blocker is None:
                    return said
                self._narrate(f"Something popped up ({blocker}), handling it")
                # Usually the shortcut already worked and a popup just sits on top.
                # Clear ONLY the popups, then stop: don't redo the whole task.
                if self._clear_popups(device, safety, skills):
                    return said
                self.goal = (f"{self.goal}. A shortcut already opened the right app, but something "
                             f"is in the way. Never sign in. Allow permissions the request needs. "
                             f"If the request is already happening on screen, use done right away.")
                return self._loop(device, safety, routines, resumed=True)

        # 5. Saved routine?
        routine: Routine | None = routines.find(self.goal)
        resumed = False
        if routine is not None:
            self._narrate("Running your saved routine")
            if routines.replay(routine, device):
                return "Done. I used your saved routine."
            # replay() stops at a step it can't match OR one that needs confirmation,
            # possibly after earlier steps already ran. We DON'T start over: the loop
            # below picks up from whatever is on screen now (and asks the user live).
            self._narrate("Picking up from here")
            resumed = True

        # 6. Plan + step loop.
        return self._loop(device, safety, routines, resumed)

    def _clear_popups(self, device, safety, skills, max_rounds: int = 4) -> bool:
        """Tap away popups one at a time. True once none are left."""
        from relay.brains import picker
        goal = ("Dismiss the popup or first-run screen on top. Never sign in. Skip sign-in, ads and "
                "notification prompts (Skip / Not now / No thanks). ALLOW permissions the user's "
                f"request needs. The user's request: {self.goal}")
        history: list[Action] = []
        for _ in range(max_rounds):
            self._check_stop()
            screen = device.get_screen(with_screenshot=True)
            action = picker.choose_action(goal, "Tap the one button that dismisses the popup", screen, history)
            if action.kind not in ("tap", "back"):
                break
            if safety.needs_confirmation(action, screen):
                return False  # a risky button: let the full loop ask the user properly
            device.act(action, screen)
            self._narrate(_describe_action(action, screen))
            history.append(action)
            if skills.find_blocker(device, wait=SETTLE_SECONDS) is None:
                return True
        return False

    def _unlock_if_needed(self, device) -> None:
        if not hasattr(device, "is_locked") or not device.is_locked():
            return
        if config.RELAY_PASSPHRASE and not self.passphrase_said:
            raise Cancelled("Your phone is locked. Say your passphrase, then what you want.")
        self._narrate("Unlocking")
        if not device.unlock(config.EMULATOR_PIN):
            raise Cancelled("I couldn't unlock the phone. Check the PIN in your settings file.")

    def _describe(self, device) -> str:
        from relay.brains import gemini, claude
        screen = device.get_screen(with_screenshot=True)
        try:
            return gemini.describe_screen(screen)
        except Exception as e:
            print(f"[agent] gemini describe failed, using claude: {e!r}")
            return claude.describe_screen(screen)

    def _loop(self, device, safety, routines, resumed: bool = False) -> str:
        from relay.brains import claude, picker

        if resumed:
            # A routine already did part of the job; a fresh plan would redo those
            # steps, so give the picker one sub-goal: finish from the current screen.
            subgoals = [f"Finish this from the current screen (earlier steps are done): {self.goal}"]
        else:
            try:
                subgoals = claude.plan(self.goal, self.device_name) or [self.goal]
            except Exception as e:
                print(f"[agent] planning failed, using the goal as one step: {e!r}")
                subgoals = [self.goal]
        blocked = getattr(safety, "BLOCKED_PREFIX", "BLOCKED:")

        history: list[Action] = []
        done_steps: list[tuple[Screen, Action]] = []  # for saving a routine
        goal = self.goal  # grows with the user's answers to ask_user questions
        steps = 0
        final = "Done."

        repeats: dict[tuple, int] = {}  # (kind, element label, text) -> times done
        goal_finished = False
        for subgoal in subgoals:
            if goal_finished:
                break
            while True:
                self._check_stop()
                if steps >= config.MAX_STEPS:
                    raise Cancelled(f"I hit my limit of {config.MAX_STEPS} steps, so I stopped. "
                                    "Try breaking it into smaller requests.")
                steps += 1

                # Screenshot too, so Claude can SEE the screen (switches, colors,
                # icons) instead of asking a blind user what's on it.
                screen = device.get_screen(with_screenshot=True)
                action = picker.choose_action(goal, subgoal, screen, history)
                self._check_stop()

                # Jev can say a middle step is done (fast, no Claude call). On the
                # LAST step, Claude double-checks the screen and writes the summary.
                if action.kind == "done" and action.source == "jev" and subgoal == subgoals[-1]:
                    from relay.brains import claude
                    action = claude.pick_action(goal, subgoal, screen, history)

                if action.kind == "done":
                    final = action.text or final
                    history.append(action)
                    # Claude can say the whole goal is already done (steps often
                    # finish early), so we don't spend a call per leftover step.
                    goal_finished = action.goal_complete
                    break  # on to the next sub-goal

                if action.kind == "ask_user":
                    answer = self._ask(action.text or "Can you tell me more?")
                    goal = f"{goal} (user said: {answer})"
                    history.append(action)
                    continue

                # Rule 4: Relay never types secrets, whatever the brain says.
                if action.kind == "type" and self._is_secret_field(action, screen):
                    raise Cancelled("This needs a password or card number. For your safety I won't type "
                                    "those. Please enter it yourself, then ask me to continue.")

                question = safety.needs_confirmation(action, screen)
                if question and question.startswith(blocked):
                    # A refusal, not a question: stop and say why. Never ask the user.
                    raise Cancelled(question[len(blocked):].strip() or "I can't do that safely.")
                if question:
                    if not _is_yes(self._ask(question)):
                        raise Cancelled("Okay, I won't do that.")

                # Remember WHICH element was used (ids change every screen), so the
                # brain can see if it's repeating itself.
                label = _label(action, screen)
                sig = (action.kind, label, action.text)
                repeats[sig] = repeats.get(sig, 0) + 1
                if repeats[sig] >= 4:
                    raise Cancelled(f"I'm going in circles on {label or action.kind}, so I stopped. "
                                    "Something on the screen may need your help.")
                try:
                    device.act(action, screen)
                except ValueError as e:
                    # The devices raise ValueError when element_id isn't on the screen
                    # (the screen changed, or the brain picked a bad id). Count it as a
                    # failed step, tell the picker via history, and loop: the next pass
                    # re-reads the screen. It still counts toward MAX_STEPS.
                    print(f"[agent] step failed: {e}")
                    action.reason = f"FAILED: {e}"
                    history.append(action)
                    continue
                if label:
                    action.reason = f"on '{label}': {action.reason}"
                if repeats[sig] >= 2:
                    action.reason += " (REPEATED: this didn't work before, try something different)"
                history.append(action)
                done_steps.append((screen, action))
                self._narrate(_describe_action(action, screen))
                time.sleep(SETTLE_SECONDS)  # let the screen finish changing before reading it again

        # 5. It worked: remember it so next time is instant.
        if done_steps:
            try:
                routines.save(self.goal, self.device_name, done_steps)
            except Exception as e:
                print(f"[agent] couldn't save routine: {e!r}")
        return final

    @staticmethod
    def _is_secret_field(action: Action, screen: Screen) -> bool:
        if action.element_id is not None and 0 <= action.element_id < len(screen.elements):
            if _SECRET_FIELD.search(screen.elements[action.element_id].text):
                return True
        # Also catch the brain trying to type something that looks like a card number.
        digits = re.sub(r"[\s-]", "", action.text or "")
        return digits.isdigit() and 13 <= len(digits) <= 19
