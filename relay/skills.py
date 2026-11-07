"""Skills: instant shortcuts for common requests.

The normal agent loop reads the screen and taps step by step (10-30 seconds).
For simple, common requests Android has a direct way in, so we skip the loop:

    "navigate to Starbucks"    -> Google Maps navigation intent      (~1 s)
    "pause the music"          -> media pause key                    (~1 s)
    "open Spotify"             -> start the app                      (~1 s)
    "close Instagram"          -> force-stop the app                 (~1 s)
    "turn the volume up"       -> volume key                         (~1 s)

route(goal) makes ONE quick call to a small, fast Claude model that either
picks a skill (with its details) or says "screen_agent" for everything else
("reply in my Instagram group chat..."), which goes to the normal loop.
"""
from __future__ import annotations

import logging
import time
from urllib.parse import quote

from relay import config

log = logging.getLogger("relay.skills")

# Each skill is a Claude tool. Claude picks one by calling it.
SKILL_TOOLS = [
    {
        "name": "navigate",
        "description": "Start turn-by-turn navigation in Google Maps to a place or address.",
        "input_schema": {
            "type": "object",
            "properties": {
                "destination": {"type": "string", "description": "Place name or address, e.g. 'nearest Starbucks'."},
                "mode": {"type": "string", "enum": ["driving", "walking", "bicycling"]},
            },
            "required": ["destination"],
        },
    },
    {
        "name": "media",
        "description": "Control music or podcasts playing in any app (Spotify, YouTube Music...): "
        "play, pause, skip to next, go to previous. Optionally open a music app first.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["play", "pause", "next", "previous"]},
                "app": {"type": "string", "description": "Music app to open first, e.g. 'spotify'. Omit if not said."},
            },
            "required": ["action"],
        },
    },
    {
        "name": "open_app",
        "description": "Just open an app, nothing else.",
        "input_schema": {
            "type": "object",
            "properties": {"app": {"type": "string", "description": "App name, e.g. 'spotify', 'instagram'."}},
            "required": ["app"],
        },
    },
    {
        "name": "close_app",
        "description": "Close (quit) an app.",
        "input_schema": {
            "type": "object",
            "properties": {"app": {"type": "string"}},
            "required": ["app"],
        },
    },
    {
        "name": "volume",
        "description": "Change the phone volume.",
        "input_schema": {
            "type": "object",
            "properties": {
                "direction": {"type": "string", "enum": ["up", "down", "mute"]},
                "steps": {"type": "integer", "description": "How many presses, 1-10. Default 3."},
            },
            "required": ["direction"],
        },
    },
    {
        "name": "screen_agent",
        "description": "Use this for EVERYTHING ELSE: anything inside an app (messages, group chats, "
        "email, ordering, searching, settings), or any request with more than one part that the "
        "other tools can't do alone.",
        "input_schema": {"type": "object", "properties": {}},
    },
]

ROUTER_SYSTEM = (
    "You route a user's spoken request for their phone. Pick the ONE tool that does the "
    "whole request by itself. If the request needs anything the tool can't do on its own "
    "(typing a message, choosing a chat, reading something, several steps), call screen_agent. "
    "Always answer by calling exactly one tool."
)

SKILL_NAMES = {t["name"] for t in SKILL_TOOLS} - {"screen_agent"}


def route(goal: str, device_name: str) -> tuple[str, dict] | None:
    """Return (skill_name, params) if a skill can do the whole goal, else None.

    Any error (no key, network) returns None so Relay falls back to the normal loop.
    """
    if device_name != "phone" or not config.ANTHROPIC_API_KEY:
        return None
    from relay.brains.claude import _client  # same lazily-created client

    start = time.perf_counter()
    try:
        response = _client().messages.create(
            model=config.CLAUDE_FAST_MODEL,
            max_tokens=256,
            system=ROUTER_SYSTEM,
            tools=SKILL_TOOLS,
            tool_choice={"type": "auto"},
            messages=[{"role": "user", "content": goal}],
        )
    except Exception as e:
        log.info("router error %r -> screen agent", e)
        return None
    ms = int((time.perf_counter() - start) * 1000)
    for block in response.content:
        if getattr(block, "type", None) == "tool_use":
            log.info("router picked %s %s ms=%d", block.name, dict(block.input), ms)
            if block.name in SKILL_NAMES:
                return block.name, dict(block.input)
            return None
    return None


def run(name: str, params: dict, device) -> str:
    """Do the skill on the phone and return a short sentence to speak."""
    if name == "navigate":
        dest = params["destination"]
        mode = {"walking": "w", "bicycling": "b"}.get(params.get("mode", ""), "d")
        device.open_uri(f"google.navigation:q={quote(dest)}&mode={mode}", package="com.google.android.apps.maps")
        return f"Starting directions to {dest}."

    if name == "media":
        action = params["action"]
        app = params.get("app")
        if app:
            device.start_app(app)
            time.sleep(1.5)  # give the music app a moment to load before pressing play
        device.key(action)
        said = {"play": "Playing", "pause": "Paused", "next": "Skipping to the next track",
                "previous": "Going back a track"}[action]
        return f"{said}{' on ' + app.title() if app else ''}."

    if name == "open_app":
        device.start_app(params["app"])
        return f"Opened {params['app'].title()}."

    if name == "close_app":
        device.close_app(params["app"])
        return f"Closed {params['app'].title()}."

    if name == "volume":
        key = {"up": "volume_up", "down": "volume_down", "mute": "mute"}[params["direction"]]
        presses = 1 if key == "mute" else max(1, min(10, int(params.get("steps") or 3)))
        for _ in range(presses):
            device.key(key)
        return "Muted." if key == "mute" else f"Volume {params['direction']}."

    raise ValueError(f"unknown skill {name!r}")
