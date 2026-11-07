"""The Device interface that every device (phone, mac) must follow.

A Protocol is like a "shape" for a class: any class that has these
attributes and methods counts as a Device, even without inheriting from it.
The agent loop only talks to devices through these three methods.
"""
from __future__ import annotations

from typing import Protocol

from relay.types import Action, Screen


class Device(Protocol):
    name: str  # "phone" | "mac"

    def get_screen(self, with_screenshot: bool = False) -> Screen:
        """Return what is on the screen right now."""
        ...

    def act(self, action: Action, screen: Screen) -> None:
        """Execute tap / type / scroll / back / home / open_app."""
        ...

    def emergency_call(self) -> None:
        """Phone: dial 911 via the system dialer. Mac: raise NotImplementedError."""
        ...
