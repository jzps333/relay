"""Decides WHICH device a command is for, and keeps one live device object per name.

"open Safari on my Mac"   -> ("mac",   "open Safari")
"text mom on the phone"   -> ("phone", "text mom")
"scroll down"             -> whatever device was used last (or config.DEFAULT_DEVICE)
"""
from __future__ import annotations

import re
import threading

from relay import config

# Phrases we strip out of the command once they have told us the device.
# (?i) = case-insensitive. \b = word boundary so "macaroni" doesn't match "mac".
_MAC_PHRASES = [
    r"(?i)\s*\bon (?:my |the )?(?:mac|macbook|laptop|computer)\b",
]
_PHONE_PHRASES = [
    r"(?i)\s*\bon (?:my |the )?(?:phone|android)\b",
]
# Bare words that pick a device but stay in the text ("open the computer's settings").
_MAC_WORDS = r"(?i)\b(?:mac|macbook|laptop|computer)\b"
_PHONE_WORDS = r"(?i)\b(?:android)\b"


def _strip(patterns: list[str], text: str) -> tuple[bool, str]:
    """Remove every matching phrase. Returns (matched_anything, cleaned_text)."""
    matched = False
    for p in patterns:
        text, n = re.subn(p, "", text)
        matched = matched or n > 0
    return matched, " ".join(text.split())  # tidy double spaces


def pick_device(text: str, last: str | None) -> tuple[str, str]:
    """Return (device_name, cleaned_text) for a spoken command."""
    found, cleaned = _strip(_MAC_PHRASES, text)
    if found:
        return "mac", cleaned
    found, cleaned = _strip(_PHONE_PHRASES, text)
    if found:
        return "phone", cleaned
    if re.search(_MAC_WORDS, text):
        return "mac", text.strip()
    if re.search(_PHONE_WORDS, text):
        return "phone", text.strip()
    return (last or config.DEFAULT_DEVICE), text.strip()


# --- device cache -----------------------------------------------------------
# Connecting to ADB / the Mac takes time, so we build each device once and reuse it.
_devices: dict = {}
_lock = threading.Lock()


def get_device(name: str):
    """Return the device to use for `name` ("phone" or "mac").

    Normal (local) mode: the real Phone() / Mac() on this computer.
    Cloud mode (CLOUD_MODE=1): a RemoteDevice that forwards every call to the
    device helper running on your Mac (see relay/remote.py).
    """
    if config.CLOUD_MODE:
        from relay import remote
        return remote.get_remote_device(name)
    return get_local_device(name)


def get_local_device(name: str):
    """Return the shared Phone() or Mac() instance, creating it on first use.

    Always local, even in cloud mode: the Mac's device helper uses this.
    """
    with _lock:
        if name not in _devices:
            # Imported here (not at the top) so the server still starts on a machine
            # where, say, the Mac libraries aren't installed.
            if name == "mac":
                from relay.devices.mac import Mac
                _devices[name] = Mac()
            elif name == "phone":
                from relay.devices.phone import Phone
                _devices[name] = Phone()
            else:
                raise ValueError(f"unknown device: {name!r}")
        return _devices[name]
