"""Gemini brain: answers "what's on my screen?" for a blind user.

Uses the `google-genai` SDK (from google import genai). We send the screenshot as an image
part when we have one, otherwise just the numbered element list.
"""
from __future__ import annotations

from relay import config
from relay.types import Screen

PROMPT = (
    "You are the eyes of a blind user. Describe this {device} screen in 2 or 3 short, "
    "natural spoken sentences: which app it is, the main content, and the most useful "
    "things they could tap or do next. No lists, no markdown, no element numbers."
)

_client_instance = None


def _client():
    """Create the Gemini client once (lazy, so importing needs no key). Tests replace this."""
    global _client_instance
    if _client_instance is None:
        from google import genai

        _client_instance = genai.Client(api_key=config.GEMINI_API_KEY)
    return _client_instance


def describe_screen(screen: Screen) -> str:
    from google.genai import types

    prompt = PROMPT.format(device=screen.device)
    if screen.screenshot_png:
        # Image + a little text context (app name and elements help with small text).
        contents = [
            types.Part.from_bytes(data=screen.screenshot_png, mime_type="image/png"),
            prompt + "\n\nElement list for reference:\n" + screen.as_prompt(limit=60),
        ]
    else:
        contents = [prompt + "\n\nThere is no screenshot. Elements on screen:\n" + screen.as_prompt()]

    response = _client().models.generate_content(
        model=config.GEMINI_MODEL,
        contents=contents,
        config=types.GenerateContentConfig(max_output_tokens=300, temperature=0.3),
    )
    text = (response.text or "").strip()
    if not text:
        raise RuntimeError("Gemini returned an empty description")
    return text
