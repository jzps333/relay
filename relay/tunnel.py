"""Cloudflare tunnel: give the Relay server a public https link.

Why: the iPhone can't always reach the Mac directly (a VPN like Tailscale on the
phone, hackathon Wi-Fi that blocks devices from seeing each other, or being on
cellular in the car). A tunnel gives the Mac a link like
https://random-words.trycloudflare.com that works from anywhere.

Safety: anyone could find the link, but every route needs your RELAY_TOKEN,
so without it they just get "401". Keep the token secret.

Needs cloudflared:  brew install cloudflared
"""
from __future__ import annotations

import re
import shutil
import subprocess
import threading
import time

_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def find_url(line: str) -> str | None:
    """Pull the trycloudflare link out of one line of cloudflared's output."""
    m = _URL_RE.search(line)
    return m.group(0) if m else None


def start(port: int, timeout: float = 30.0) -> tuple[subprocess.Popen | None, str | None]:
    """Start a quick tunnel to localhost:<port>. Returns (process, public_url).

    (None, None) if cloudflared isn't installed; (process, None) if no link
    showed up in time (the tunnel may still come up; check its output).
    """
    exe = shutil.which("cloudflared")
    if exe is None:
        return None, None
    proc = subprocess.Popen(
        [exe, "tunnel", "--no-autoupdate", "--url", f"http://localhost:{port}"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    found: list[str] = []

    def read_output() -> None:
        # cloudflared prints the link in a box among its log lines. Keep reading
        # forever (in the background) so its output pipe never fills up.
        for line in proc.stdout:  # type: ignore[union-attr]
            if not found and (url := find_url(line)):
                found.append(url)

    threading.Thread(target=read_output, daemon=True).start()
    deadline = time.monotonic() + timeout
    while not found and time.monotonic() < deadline and proc.poll() is None:
        time.sleep(0.2)
    return proc, (found[0] if found else None)


def banner(url: str | None, token: str) -> str:
    if url is None:
        return ("\n[tunnel] No public link yet. Is cloudflared installed?  brew install cloudflared\n"
                "         (The server still works on your Wi-Fi address above.)\n")
    short = (token[:4] + "…") if token else "(not set!)"
    return (
        "\n" + "=" * 60 + "\n"
        "PUBLIC LINK (works from anywhere, even with your VPN on):\n"
        f"    Shortcut server address:  {url}\n"
        f"    Test on the iPhone:       {url}/health\n"
        f"    Token: Bearer {short}\n"
        "This link changes every time you restart. Update the Shortcut's Server text.\n"
        + "=" * 60 + "\n"
    )
