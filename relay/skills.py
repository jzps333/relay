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
import re
import time
from urllib.parse import quote

from relay import config, events

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
        "name": "email_summary",
        "description": "Read the user's Gmail inbox and give a short spoken summary "
        "(e.g. 'summarize my emails', 'what came in overnight', 'anything important in my inbox').",
        "input_schema": {
            "type": "object",
            "properties": {
                "timeframe": {"type": "string", "enum": ["overnight", "today", "this week", "recent"]},
            },
        },
    },
    {
        "name": "notifications",
        "description": "Read the phone's notifications out loud: new texts, messages and app alerts "
        "(e.g. 'read my notifications', 'any new messages', 'what did I miss', 'read my texts').",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "news_briefing",
        "description": "Read posts on X (Twitter) and give a short spoken briefing: overnight market/"
        "macro news from news accounts, or what's new in the user's Following feed. Use for "
        "'what happened overnight', 'market news', 'what's Walter Bloomberg saying', "
        "'what's new on my timeline'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "accounts": {"type": "array", "items": {"type": "string"},
                             "description": "X handles the user named, without @. Omit to use the defaults."},
                "group": {"type": "string",
                          "description": "A named group of accounts from the user's settings, e.g. 'macro' or 'micro'."},
                "feed": {"type": "string", "enum": ["accounts", "following"],
                         "description": "'following' = the user's own timeline; 'accounts' = specific news accounts."},
                "topic": {"type": "string", "description": "What to focus on, e.g. 'markets', 'tech', 'anything'."},
            },
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


# ---------------------------------------------------------------------------
# Keyword fast path: obvious commands skip the ~2 s router call.
# ---------------------------------------------------------------------------
# The rule here is "when in doubt, return None". A miss only costs the 2 s
# router call; a wrong match does the wrong thing on the user's phone.

# Polite words people say before the real command: "hey relay, can you pause".
_LEADING_FILLER = re.compile(
    r"^(?:hey relay|ok relay|okay relay|relay|hey|ok|okay|please|"
    r"can you|could you|would you|will you|just)\b[\s,]*",
    re.I,
)
# ...and after it: "pause the music please".
_TRAILING_FILLER = re.compile(r"[\s,]*\b(?:please|thanks|thank you|now)$", re.I)

# Whole commands that map straight to a skill. The text must match EXACTLY
# (after filler is removed), so "play my liked songs" never matches "play".
_EXACT = {
    # X / news briefing
    "what happened overnight": ("news_briefing", {"feed": "accounts", "topic": "markets"}),
    "what happened in the markets overnight": ("news_briefing", {"feed": "accounts", "topic": "markets"}),
    "market news": ("news_briefing", {"feed": "accounts", "topic": "markets"}),
    "give me the market news": ("news_briefing", {"feed": "accounts", "topic": "markets"}),
    "overnight news": ("news_briefing", {"feed": "accounts", "topic": "markets"}),
    "what's new on my timeline": ("news_briefing", {"feed": "following", "topic": "anything"}),
    "whats new on my timeline": ("news_briefing", {"feed": "following", "topic": "anything"}),
    "summarize my timeline": ("news_briefing", {"feed": "following", "topic": "anything"}),
    "summarize my twitter": ("news_briefing", {"feed": "following", "topic": "anything"}),
    # inbox summary
    "summarize my emails": ("email_summary", {"timeframe": "recent"}),
    "summarize my email": ("email_summary", {"timeframe": "recent"}),
    "summarize my inbox": ("email_summary", {"timeframe": "recent"}),
    "read my emails": ("email_summary", {"timeframe": "recent"}),
    "check my email": ("email_summary", {"timeframe": "recent"}),
    "check my emails": ("email_summary", {"timeframe": "recent"}),
    "what's in my inbox": ("email_summary", {"timeframe": "recent"}),
    "whats in my inbox": ("email_summary", {"timeframe": "recent"}),
    "what came in overnight": ("email_summary", {"timeframe": "overnight"}),
    "summarize my emails from overnight": ("email_summary", {"timeframe": "overnight"}),
    "give me a summary of my inbox": ("email_summary", {"timeframe": "recent"}),
    # notifications
    "read my notifications": ("notifications", {}),
    "read notifications": ("notifications", {}),
    "read me my notifications": ("notifications", {}),
    "any notifications": ("notifications", {}),
    "any new notifications": ("notifications", {}),
    "do i have any notifications": ("notifications", {}),
    "check my notifications": ("notifications", {}),
    "any new messages": ("notifications", {}),
    "any messages": ("notifications", {}),
    "do i have any new messages": ("notifications", {}),
    "what did i miss": ("notifications", {}),
    "read my texts": ("notifications", {}),
    "read my messages": ("notifications", {}),
    "any new texts": ("notifications", {}),
    # media
    "pause": ("media", {"action": "pause"}),
    "pause music": ("media", {"action": "pause"}),
    "pause the music": ("media", {"action": "pause"}),
    "pause the song": ("media", {"action": "pause"}),
    "stop the music": ("media", {"action": "pause"}),
    "stop music": ("media", {"action": "pause"}),
    "play": ("media", {"action": "play"}),
    "resume": ("media", {"action": "play"}),
    "resume music": ("media", {"action": "play"}),
    "resume the music": ("media", {"action": "play"}),
    "unpause": ("media", {"action": "play"}),
    "next": ("media", {"action": "next"}),
    "next song": ("media", {"action": "next"}),
    "next track": ("media", {"action": "next"}),
    "skip": ("media", {"action": "next"}),
    "skip song": ("media", {"action": "next"}),
    "skip this song": ("media", {"action": "next"}),
    "skip the song": ("media", {"action": "next"}),
    "skip track": ("media", {"action": "next"}),
    "previous": ("media", {"action": "previous"}),
    "previous song": ("media", {"action": "previous"}),
    "previous track": ("media", {"action": "previous"}),
    "go back a song": ("media", {"action": "previous"}),
    # volume
    "volume up": ("volume", {"direction": "up"}),
    "volume down": ("volume", {"direction": "down"}),
    "turn it up": ("volume", {"direction": "up"}),
    "turn it down": ("volume", {"direction": "down"}),
    "turn the volume up": ("volume", {"direction": "up"}),
    "turn the volume down": ("volume", {"direction": "down"}),
    "turn up the volume": ("volume", {"direction": "up"}),
    "turn down the volume": ("volume", {"direction": "down"}),
    "louder": ("volume", {"direction": "up"}),
    "quieter": ("volume", {"direction": "down"}),
    "mute": ("volume", {"direction": "mute"}),
}

# "open spotify", "launch the google maps app". Group 2 = the app name:
# 1 to 3 words, and the pattern must reach the END of the text ($), so
# anything extra after the app name means no match.
_APP_WORD = r"[a-z0-9][a-z0-9.'&+-]*"
_APP_NAME = rf"({_APP_WORD}(?:\s+{_APP_WORD}){{0,2}}?)"
_OPEN = re.compile(rf"^(?:open|launch)\s+(?:the\s+)?{_APP_NAME}(?:\s+app)?$", re.I)
_CLOSE = re.compile(rf"^(?:close|quit|exit)\s+(?:out of\s+)?(?:the\s+)?{_APP_NAME}(?:\s+app)?$", re.I)

# Words that never appear in an app name but DO show up in multi-part or
# in-app requests ("open my email", "open spotify and play...", "open chat with mom").
_NOT_APP_WORDS = {"and", "then", "to", "on", "in", "with", "for", "my", "a", "an", "the",
                  "at", "of", "from", "it", "this", "that", "all", "everything", "me",
                  "up", "your", "his", "her", "our", "their",
                  # Things you open/close that aren't apps ("exit navigation", "close tabs").
                  "navigation", "directions", "tab", "tabs", "window", "windows",
                  "notifications", "keyboard"}

# "navigate to Starbucks", "take me to 123 Main St". Group 1 = destination.
_NAVIGATE = re.compile(r"^(?:navigate to|take me to|get directions to|directions to|drive to)\s+(.+)$", re.I)
# A destination containing these is probably a two-part request
# ("navigate to work and text mom I'm on my way"), so let the LLM decide.
_MULTI_PART = re.compile(r"\b(?:and|then|also|after|text|call|message|tell|send)\b|[,;]", re.I)


def _clean(goal: str) -> str:
    """Remove filler words and end punctuation; keep the original letter case."""
    text = " ".join(goal.split())  # collapse extra spaces
    while True:
        before = text
        text = text.strip(" .!?,")
        text = _LEADING_FILLER.sub("", text)
        text = _TRAILING_FILLER.sub("", text)
        if text == before:
            return text


def _app_name(pattern: re.Pattern, text: str) -> str | None:
    """App name if `text` is exactly "<verb> <app name>", else None."""
    m = pattern.match(text)
    if not m:
        return None
    app = m.group(1).lower()
    if any(word in _NOT_APP_WORDS for word in app.split()):
        return None
    return app


def match_keywords(goal: str) -> tuple[str, dict] | None:
    """Instantly recognise obvious one-step commands, no AI needed.

    Returns (skill_name, params) in the same shape as route(), or None if the
    goal isn't an obvious single command (then the LLM router decides).
    """
    text = _clean(goal)
    if not text:
        return None
    result = None

    exact = _EXACT.get(text.lower())
    if exact is not None:
        name, params = exact
        result = (name, dict(params))  # copy so callers can't change the table
    elif (app := _app_name(_OPEN, text)) is not None:
        result = ("open_app", {"app": app})
    elif (app := _app_name(_CLOSE, text)) is not None:
        result = ("close_app", {"app": app})
    elif (m := _NAVIGATE.match(text)) and not _MULTI_PART.search(m.group(1)):
        destination = m.group(1).strip(" .!?,")
        if destination:
            result = ("navigate", {"destination": destination})

    if result is not None:
        log.info("keyword match %s %s", result[0], result[1])
        events.publish("route", how="keyword", name=result[0])  # live dashboard
    return result


def route(goal: str, device_name: str) -> tuple[str, dict] | None:
    """Return (skill_name, params) if a skill can do the whole goal, else None.

    Obvious commands are matched by keywords first (instant, works without a
    key). Everything else asks the fast LLM router. Any error (no key,
    network) returns None so Relay falls back to the normal loop.
    """
    if device_name != "phone":
        return None
    picked = match_keywords(goal)
    if picked is not None:
        return picked
    if not config.ANTHROPIC_API_KEY:
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
                events.publish("route", how="skill", name=block.name, ms=ms)  # live dashboard
                return block.name, dict(block.input)
            # screen_agent: the agent announces that itself when the step loop starts.
            return None
    return None


def run(name: str, params: dict, device) -> str:
    """Do the skill on the phone and return a short sentence to speak."""
    if name == "navigate":
        dest = params["destination"]
        mode = {"walking": "w", "bicycling": "b"}.get(params.get("mode", ""), "d")
        # Directions need location. Grant it up front so Maps never asks
        # (and the agent can't accidentally tap "Don't allow").
        if hasattr(device, "grant_location"):
            device.grant_location("com.google.android.apps.maps")
            # Put the emulator where the Mac really is (otherwise it's in California).
            from relay import location
            if location.sync_emulator_to_mac():
                time.sleep(1.0)  # give the emulator's GPS a moment to report the new spot
        # Quit Maps first: if an old route is still running, a new request just
        # gets ignored and you keep the old directions.
        device.close_app("maps")
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


# Skills whose app might show a popup or first-run screen instead of the result.
CHECK_AFTER = {"navigate", "open_app"}

# Button words that mean "something is in the way".
_BLOCKER_WORDS = ("skip", "sign in", "not now", "no thanks", "allow", "accept", "agree",
                  "got it", "continue", "while using the app", "only this time")


def find_blocker(device, wait: float = 2.0) -> str | None:
    """After a skill runs, return the text of a popup/onboarding button if one is showing.

    Checks every half second for up to `wait` seconds, so a fast app costs ~0.5 s,
    not the full wait.
    """
    deadline = time.monotonic() + wait
    while True:
        time.sleep(min(0.5, wait))
        try:
            screen = device.get_screen()
        except Exception:
            return None
        for el in screen.elements:
            text = (el.text or "").strip().lower()
            if el.clickable and any(text == w or text.startswith(w) for w in _BLOCKER_WORDS):
                return el.text
        # The app has drawn real content and nothing is in the way: done.
        if len(screen.elements) > 3 or time.monotonic() >= deadline:
            return None


# Words that only show up once turn-by-turn navigation is actually running.
def navigation_running(device) -> bool:
    """True if Maps is in turn-by-turn mode (not a list of search results)."""
    try:
        screen = device.get_screen()
    except Exception:
        return True  # can't tell: don't make things worse
    texts = " | ".join((el.text or "").lower() for el in screen.elements)
    if "directions" in texts and ("sponsored" in texts or "add stop" in texts or "call" in texts):
        return False  # that's the search results list
    return any(w in texts for w in ("exit navigation", "close navigation", "re-center", "recenter"))



# ---------------------------------------------------------------------------
# Inbox summary
# ---------------------------------------------------------------------------

def collect_inbox(device, pages: int = 3) -> list[str]:
    return collect_screen_text(device, pages)


def collect_screen_text(device, pages: int = 3) -> list[str]:
    """Read the Gmail inbox list off the screen, scrolling `pages` times.

    Gmail's inbox rows expose sender, subject, snippet and time as text, so the
    element list is enough; no screenshots needed (faster, cheaper).
    """
    from relay.types import Action

    seen: list[str] = []
    for page in range(pages):
        screen = device.get_screen()
        for el in screen.elements:
            text = (el.text or "").strip()
            if len(text) > 2 and text not in seen:
                seen.append(text)
        if page < pages - 1:
            device.act(Action(kind="scroll", direction="down"), screen)
            time.sleep(0.6)
    return seen


SUMMARY_SYSTEM = (
    "You summarize an email inbox for someone who is DRIVING and will only HEAR your answer. "
    "You get the raw text of the Gmail inbox list (senders, subjects, snippets, times; plus some "
    "button labels to ignore). Rules:\n"
    "- Start with how many emails matter, then the most important first (people > school/work > "
    "bills/alerts > promotions). Lump promotions and newsletters into one short mention.\n"
    "- Each item: who it's from and the gist in a few words.\n"
    "- NEVER read out verification codes, one-time passwords, 2FA codes, or passwords; just say "
    "'a login code from X'.\n"
    "- Plain spoken sentences, no lists or symbols. Under 90 words.\n"
    "- If the inbox looks empty or you can't tell, say so briefly."
)


def summarize_inbox(lines: list[str], timeframe: str = "recent") -> str:
    """Turn the inbox text into a short spoken summary."""
    from datetime import datetime

    now = datetime.now().strftime("%A %I:%M %p")
    focus = {"overnight": "Only emails that arrived since about 6 PM yesterday.",
             "today": "Only emails from today.",
             "this week": "Emails from this week."}.get(timeframe, "The newest emails.")
    log.info("inbox: read %d lines", len(lines))
    return _spoken_summary(SUMMARY_SYSTEM, f"It is {now}. {focus}\n\nInbox text:\n" + "\n".join(lines[:250]),
                           "I couldn't read your inbox.")


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

NOTIFY_SYSTEM = (
    "You read a phone's notifications out loud to someone who is DRIVING or blind and will only "
    "HEAR your answer. You get one line per notification: how long ago, the app's package name, "
    "the title (usually the sender) and the text. Rules:\n"
    "- People and messages first: say who and what, e.g. 'You got a text from Mom: where are you?'\n"
    "- Then other alerts, most important first. Group app spam, promotions and repeats into one "
    "short mention ('plus a few alerts from Instagram').\n"
    "- Say app names in plain words (Messages, WhatsApp, Gmail), never package names.\n"
    "- NEVER read out verification codes, one-time passwords, 2FA codes, or passwords; just say "
    "'a login code from X'.\n"
    "- Plain spoken sentences, no lists or symbols. Under 80 words."
)

NO_NOTIFICATIONS = "No new notifications."


def _ago(ms: int | None, now: float) -> str:
    """Post time in ms -> '5 min ago' (or '' if unknown)."""
    if not ms:
        return ""
    minutes = max(0, int(now - ms / 1000) // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} min ago"
    return f"{minutes // 60} h ago"


def notification_lines(items: list[dict], now: float | None = None) -> list[str]:
    """One readable line per notification, for the AI (and for follow-up context)."""
    now = time.time() if now is None else now
    lines = []
    for n in items:
        when = _ago(n.get("time"), now)
        head = f"[{when}] " if when else ""
        lines.append(f"{head}{n.get('package', '')} | {n.get('title', '')}: {n.get('text', '')}".strip())
    return lines


def summarize_notifications(items: list[dict]) -> str:
    """Turn the notification list into a short spoken summary."""
    if not items:
        return NO_NOTIFICATIONS  # no AI needed
    lines = notification_lines(items)
    log.info("notifications: read %d", len(lines))
    return _spoken_summary(NOTIFY_SYSTEM, "Notifications, newest first:\n" + "\n".join(lines[:100]),
                           "I couldn't read your notifications.")


# ---------------------------------------------------------------------------
# X (Twitter) news briefing
# ---------------------------------------------------------------------------

X_PACKAGE = "com.twitter.android"

NEWS_SYSTEM = (
    "You turn raw text read off the X (Twitter) app into a short spoken news briefing for "
    "someone who is DRIVING and will only HEAR it. The text mixes posts, handles, times like "
    "'3h', and button labels to ignore. Rules:\n"
    "- Most important first. Group repeats of the same story into one.\n"
    "- Say who reported it ('Walter Bloomberg reports...') and roughly when if shown.\n"
    "- These are posts, not verified facts: attribute them, don't state them as certain.\n"
    "- Plain spoken sentences, no lists, symbols, hashtags or links. Under 120 words.\n"
    "- If nothing matches the focus, say so in one sentence."
)


def _tap_text(device, label: str) -> bool:
    """Tap the first clickable element whose text is exactly `label` (case-insensitive)."""
    from relay.types import Action

    screen = device.get_screen()
    for el in screen.elements:
        if el.clickable and (el.text or "").strip().lower() == label.lower():
            device.act(Action(kind="tap", element_id=el.id), screen)
            time.sleep(1.0)
            return True
    return False


def collect_x(device, feed: str, accounts: list[str], pages: int | None = None) -> list[str]:
    """Read posts off X: specific accounts' profiles, or the Following timeline."""
    lines: list[str] = []
    if feed == "following" or not accounts:
        device.start_app("x")
        time.sleep(2.0)
        _tap_text(device, "Following")  # the timeline tab; "For you" is the default
        lines += collect_screen_text(device, pages)
    else:
        pages = pages or config.NEWS_PAGES_PER_ACCOUNT
        for handle in accounts[:config.MAX_NEWS_ACCOUNTS]:  # cap it so a briefing stays quick
            # Opening the profile link jumps straight to that account in the X app.
            device.open_uri(f"https://x.com/{handle}", package=X_PACKAGE)
            time.sleep(2.5)
            lines.append(f"--- posts from @{handle} ---")
            lines += collect_screen_text(device, pages)
    return lines


def summarize_news(lines: list[str], topic: str = "markets") -> str:
    from datetime import datetime

    now = datetime.now().strftime("%A %I:%M %p")
    focus = ("Focus on markets and the economy: stocks, futures, rates, the Fed, inflation, jobs, "
             "oil, big company news, geopolitics that moves markets. Prefer the last ~12 hours."
             if topic in ("markets", "macro", "economy", "") else f"Focus on: {topic}.")
    log.info("x: read %d lines", len(lines))
    return _spoken_summary(NEWS_SYSTEM, f"It is {now}. {focus}\n\nText from X:\n" + "\n".join(lines[:400]),
                           "I couldn't read anything useful from X.")


def _spoken_summary(system: str, user_text: str, fallback: str) -> str:
    """Ask Claude for a short spoken summary, robustly.

    Tries the fast model first with plenty of room (some models think before
    answering, and a small limit can end with no text at all), then the main
    model if the answer comes back empty.
    """
    from relay.brains.claude import _client

    for model in dict.fromkeys([config.CLAUDE_FAST_MODEL, config.CLAUDE_MODEL]):
        try:
            response = _client().messages.create(
                model=model, max_tokens=2000, system=system,
                messages=[{"role": "user", "content": user_text}],
            )
        except Exception as e:
            log.warning("summary with %s failed: %r", model, e)
            continue
        text = " ".join(b.text for b in response.content if getattr(b, "type", None) == "text").strip()
        if text:
            return text
        log.warning("summary with %s came back empty (stop_reason=%s, blocks=%s)", model,
                    getattr(response, "stop_reason", "?"),
                    [getattr(b, "type", "?") for b in response.content])
    return fallback
