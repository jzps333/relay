"""Check every API key Relay uses, and how much credit is left. Run before a demo:

    python -m relay.check_keys

It reads the same .env as Relay. Each check is tiny (a balance lookup, or a 1-token
test call), so running it costs almost nothing. It never prints the keys themselves.
"""
from __future__ import annotations

import httpx

from relay import config

TIMEOUT = 15
LOW_DOLLARS = 2.0      # warn when a prepaid balance drops under this
LOW_CHARACTERS = 5000  # warn when ElevenLabs has fewer characters left than this

OK, WARN, FAIL = "OK  ", "LOW ", "FAIL"


def _error_text(r: httpx.Response) -> str:
    """The provider's own error message, kept short."""
    try:
        body = r.json()
        err = body.get("error", body) if isinstance(body, dict) else body
        if isinstance(err, dict):
            err = err.get("message") or err.get("detail") or err
        return str(err)[:160]
    except ValueError:
        return r.text[:160]


def check_openrouter() -> tuple[str, str]:
    """Jev runs through OpenRouter: show the key's spend limit and the account balance."""
    key = config.JEV_API_KEY
    if not key:
        return FAIL, "no JEV_API_KEY in .env"
    headers = {"Authorization": f"Bearer {key}"}
    try:
        r = httpx.get("https://openrouter.ai/api/v1/key", headers=headers, timeout=TIMEOUT)
        if r.status_code != 200:
            return FAIL, f"key rejected ({r.status_code}): {_error_text(r)}"
        info = r.json().get("data", {})
        parts, status = [], OK
        remaining = info.get("limit_remaining")
        if remaining is not None:
            parts.append(f"key limit left ${remaining:.2f}")
            if remaining < LOW_DOLLARS:
                status = WARN
        c = httpx.get("https://openrouter.ai/api/v1/credits", headers=headers, timeout=TIMEOUT)
        if c.status_code == 200:
            d = c.json().get("data", {})
            left = float(d.get("total_credits", 0)) - float(d.get("total_usage", 0))
            parts.append(f"account balance ${left:.2f}")
            if left < LOW_DOLLARS:
                status = WARN
        return status, ", ".join(parts) or "key works"
    except httpx.HTTPError as e:
        return FAIL, f"couldn't reach OpenRouter: {e}"


def check_anthropic() -> tuple[str, str]:
    """Claude: a 1-token test call. An empty balance comes back as an error message."""
    key = config.ANTHROPIC_API_KEY
    if not key:
        return FAIL, "no ANTHROPIC_API_KEY in .env"
    try:
        r = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
            json={"model": config.CLAUDE_FAST_MODEL, "max_tokens": 1,
                  "messages": [{"role": "user", "content": "hi"}]},
            timeout=TIMEOUT,
        )
        if r.status_code == 200:
            return OK, "key works (balance: check Billing in the Claude Console)"
        return FAIL, f"{r.status_code}: {_error_text(r)}"
    except httpx.HTTPError as e:
        return FAIL, f"couldn't reach Anthropic: {e}"


def check_gemini() -> tuple[str, str]:
    """Gemini: a 1-token test call. 429 means a rate limit or quota was hit."""
    key = config.GEMINI_API_KEY
    if not key:
        return FAIL, "no GEMINI_API_KEY in .env"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{config.GEMINI_MODEL}:generateContent"
    try:
        r = httpx.post(url, params={"key": key}, timeout=TIMEOUT,
                       json={"contents": [{"parts": [{"text": "hi"}]}],
                             "generationConfig": {"maxOutputTokens": 1}})
        if r.status_code == 200:
            return OK, "key works"
        if r.status_code == 429:
            return WARN, f"rate limit or quota hit: {_error_text(r)}"
        return FAIL, f"{r.status_code}: {_error_text(r)}"
    except httpx.HTTPError as e:
        return FAIL, f"couldn't reach Gemini: {e}"


def check_elevenlabs() -> tuple[str, str]:
    """ElevenLabs: characters used vs. the plan's limit."""
    key = config.ELEVENLABS_API_KEY
    if not key:
        return WARN, "no ELEVENLABS_API_KEY (the phone will use its own voice)"
    try:
        r = httpx.get("https://api.elevenlabs.io/v1/user/subscription",
                      headers={"xi-api-key": key}, timeout=TIMEOUT)
        if r.status_code != 200:
            # Keys made with limited permissions can't read the plan: say where to look.
            return WARN, (f"can't read the quota ({r.status_code}): {_error_text(r)}. "
                          "Check Subscription on elevenlabs.io")
        d = r.json()
        left = int(d.get("character_limit", 0)) - int(d.get("character_count", 0))
        status = WARN if left < LOW_CHARACTERS else OK
        return status, f"{left:,} characters left this month"
    except httpx.HTTPError as e:
        return FAIL, f"couldn't reach ElevenLabs: {e}"


CHECKS = [
    ("Jev (OpenRouter)", check_openrouter),
    ("Claude", check_anthropic),
    ("Gemini", check_gemini),
    ("ElevenLabs", check_elevenlabs),
]


def main() -> int:
    worst = OK
    for name, check in CHECKS:
        try:
            status, detail = check()
        except Exception as e:  # a surprise in one check must not hide the others
            status, detail = FAIL, f"check crashed: {e!r}"
        print(f"[{status}] {name:<17} {detail}")
        if status == FAIL or (status == WARN and worst == OK):
            worst = status
    print({OK: "\nAll good for the demo.",
           WARN: "\nWorks, but top up anything marked LOW before the demo.",
           FAIL: "\nFix anything marked FAIL before the demo."}[worst])
    return 1 if worst == FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
