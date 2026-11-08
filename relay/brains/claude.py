"""Claude brain: plans sub-goals, picks actions when Jev is unsure, and describes screens.

We use the official `anthropic` Python SDK. To get *structured* output (not free text) we
use tool use: we define a tool whose input schema looks exactly like our data, and set
`tool_choice` "auto" plus an instruction so Claude "calls" that tool. The tool's input IS our answer.
"""
from __future__ import annotations

import base64
import json

from relay import config, playbooks
from relay.types import Action, Screen

# Every kind of action the devices understand (mirrors relay.types.Action.kind).
ACTION_KINDS = ["tap", "type", "scroll", "back", "home", "open_app", "done", "ask_user"]

SYSTEM_PROMPT = """You control a phone or Mac for a blind or motor-impaired user.
You see a numbered list of on-screen elements (and sometimes a screenshot).
Rules:
- Choose exactly ONE action at a time by calling the choose_action tool.
- To tap, use the element's [id] from the list. Elements marked * are clickable.
- To type, give the input element_id and the text.
- NEVER type passwords, PINs, or card numbers. Use ask_user instead and say why.
- Switches show their state in the list, e.g. "Dark theme [on]". Check it, and
  check the screenshot, to see whether an action worked.
- The user is blind: NEVER ask them what is on the screen or whether something
  changed. Look for yourself. Only use ask_user when you need a choice only they
  can make (which contact, which item), and say why.
- Look at "Last actions": if you're repeating the same tap, it isn't working.
  Try a different element, go back, or use done/ask_user instead of looping.
- STOP if something unexpected appears that the request didn't ask for: a login or
  "confirm it's you" / verification / captcha screen, a security or suspicious-
  activity warning, an account restriction, or a payment screen. Use ask_user and
  say exactly what appeared. Never try to get around these.
- Allow permissions the task needs (location for directions, mic for voice).
  Never sign in to anything.
- Stay inside the app the request is about. Never type an app's name into a Google or
  web search bar, and don't go to the home screen to look for it: Relay already opened
  the right app. Use that app's own search (magnifier) to find things.
- When the goal is finished, use done and put a short spoken summary in `text`
  (one sentence, plain words, it will be read aloud).
"""

# JSON schema for the forced tool. It mirrors the Action dataclass so parsing is trivial.
CHOOSE_ACTION_TOOL = {
    "name": "choose_action",
    "description": "Pick the single next action to take on the device.",
    "input_schema": {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ACTION_KINDS},
            "element_id": {
                "type": ["integer", "null"],
                "description": "Element [id] for tap or type. null otherwise.",
            },
            "text": {
                "type": ["string", "null"],
                "description": "Text to type, app name for open_app, question for ask_user, "
                "or spoken summary for done.",
            },
            "direction": {"type": ["string", "null"], "enum": ["up", "down", None]},
            "reason": {"type": "string", "description": "One short sentence: why this action."},
            "goal_complete": {
                "type": "boolean",
                "description": "With done: true if the user's WHOLE goal is finished "
                "(not just this sub-goal), so remaining sub-goals are skipped.",
            },
        },
        "required": ["kind", "reason"],
    },
}

PLAN_TOOL = {
    "name": "submit_plan",
    "description": "Submit the list of short sub-goals for the task.",
    "input_schema": {
        "type": "object",
        "properties": {
            "subgoals": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 2,
                "maxItems": 6,
            }
        },
        "required": ["subgoals"],
    },
}

_client_instance = None


def _client():
    """Create the Anthropic client the first time we need it (so importing needs no key).

    Tests replace this function with a fake.
    """
    global _client_instance
    if _client_instance is None:
        import anthropic  # imported here so tests never need the package configured

        _client_instance = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client_instance


def _tool_input(response, tool_name: str) -> dict:
    """Find the tool_use block in Claude's reply and return its input dict."""
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == tool_name:
            return dict(block.input)
    raise ValueError(f"Claude did not call {tool_name}")


def _call_tool(system: str, tool: dict, messages: list[dict], max_tokens: int = 1024,
               model: str | None = None) -> dict:
    """Ask Claude to answer by calling `tool`, and return the tool's input.

    Newer Claude models reject a FORCED tool_choice, so we leave it on "auto",
    tell Claude in the system prompt to always call the tool, and if it answers
    in plain text anyway we ask once more.
    """
    name = tool["name"]
    model = model or config.CLAUDE_MODEL
    system = f"{system}\n\nAlways answer by calling the {name} tool. Never reply with plain text."
    response = _client().messages.create(
        model=model, max_tokens=max_tokens, system=system,
        tools=[tool], tool_choice={"type": "auto"}, messages=messages,
    )
    try:
        return _tool_input(response, name)
    except ValueError:
        # Second try: show Claude what it said and remind it to use the tool.
        retry = messages + [
            {"role": "assistant", "content": response.content},
            {"role": "user", "content": f"Please call the {name} tool now."},
        ]
        response = _client().messages.create(
            model=model, max_tokens=max_tokens, system=system,
            tools=[tool], tool_choice={"type": "auto"}, messages=retry,
        )
        return _tool_input(response, name)


def _first_text(response) -> str:
    """Join the text blocks of a plain (non-tool) reply."""
    parts = [b.text for b in response.content if getattr(b, "type", None) == "text"]
    return " ".join(parts).strip()


def _image_block(png: bytes) -> dict:
    """Anthropic image content block from raw PNG bytes."""
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.standard_b64encode(png).decode("ascii"),
        },
    }


def _history_text(history: list[Action], n: int = 5) -> str:
    """Short summary of the last n actions, so Claude does not repeat itself."""
    if not history:
        return "(none yet)"
    lines = []
    for a in history[-n:]:
        lines.append(
            f"- {a.kind} text={a.text!r} dir={a.direction} ({a.source}) {a.reason}".rstrip()
        )
    return "\n".join(lines)


def plan(goal: str, device: str) -> list[str]:
    """Split the user's goal into 2-6 short sub-goals."""
    data = _call_tool(
        "You break a user's spoken request into 2 to 6 short, concrete steps "
        "for operating a device. Each step is a few words, e.g. 'Open WhatsApp'.",
        PLAN_TOOL,
        [{"role": "user", "content": f"Device: {device}\nGoal: {goal}"}],
        model=config.CLAUDE_FAST_MODEL,  # planning is easy; the fast model saves ~2 s
    )
    subgoals = [str(s).strip() for s in data.get("subgoals", [])]
    subgoals = [s for s in subgoals if s][:6]
    # Safety net: never return an empty plan; the goal itself is a valid single step.
    return subgoals or [goal]


def parse_action(data: dict) -> Action:
    """Turn the tool's input dict into an Action. Anything invalid becomes ask_user."""
    kind = data.get("kind")
    if kind not in ACTION_KINDS:
        return Action(
            kind="ask_user",
            text="Sorry, I got confused. Can you say that again?",
            source="claude",
            reason=f"invalid kind {kind!r}",
        )
    element_id = data.get("element_id")
    direction = data.get("direction")
    return Action(
        kind=kind,
        element_id=int(element_id) if element_id is not None else None,
        text=data.get("text"),
        direction=direction if direction in ("up", "down") else None,
        confidence=1.0,
        source="claude",
        reason=data.get("reason", ""),
        goal_complete=bool(data.get("goal_complete")) and kind == "done",
    )


def pick_action(goal: str, subgoal: str, screen: Screen, history: list[Action]) -> Action:
    """Ask Claude for the single next action (forced choose_action tool call)."""
    text = (
        f"Goal: {goal}\nCurrent sub-goal: {subgoal}\n\n"
        f"Screen elements:\n{screen.as_prompt()}\n\n"
        f"Last actions:\n{_history_text(history)}"
    )
    # If we have a playbook for the app on screen, add its tips (see relay/playbooks.py).
    tips = playbooks.tips_for(screen.app)
    if tips:
        text += f"\n\nTips for this app:\n{tips}"
    content: list[dict] = [{"type": "text", "text": text}]
    if screen.screenshot_png:
        content.append(_image_block(screen.screenshot_png))

    data = _call_tool(SYSTEM_PROMPT, CHOOSE_ACTION_TOOL, [{"role": "user", "content": content}])
    return parse_action(data)


def describe_screen(screen: Screen) -> str:
    """2-3 spoken sentences describing the screen (fallback for Gemini)."""
    content: list[dict] = [
        {
            "type": "text",
            "text": "Describe this screen for a blind user in 2 or 3 short spoken sentences. "
            "Say which app it is, the main content, and the most useful things they can tap. "
            "No lists, no markdown.\n\n" + screen.as_prompt(),
        }
    ]
    if screen.screenshot_png:
        content.append(_image_block(screen.screenshot_png))
    response = _client().messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=300,
        messages=[{"role": "user", "content": content}],
    )
    return _first_text(response)


if __name__ == "__main__":  # tiny manual check: python -m relay.brains.claude
    print(json.dumps(plan("text mom I'm running late", "phone")))
