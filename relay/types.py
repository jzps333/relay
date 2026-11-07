from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class Element:
    id: int
    text: str
    kind: str  # "button" | "input" | "text" | "image" | "other"
    bounds: tuple[int, int, int, int]
    clickable: bool


@dataclass
class Screen:
    device: str  # "phone" | "mac"
    app: str
    elements: list[Element] = field(default_factory=list)
    screenshot_png: bytes | None = None

    def as_prompt(self, limit: int = 120) -> str:
        """Numbered element list for LLM prompts."""
        lines = [f"device={self.device} app={self.app}"]
        for e in self.elements[:limit]:
            tap = "*" if e.clickable else " "
            lines.append(f"[{e.id}]{tap} {e.kind}: {e.text}")
        return "\n".join(lines)


@dataclass
class Action:
    kind: str  # tap | type | scroll | back | home | open_app | done | ask_user
    element_id: int | None = None
    text: str | None = None
    direction: str | None = None
    confidence: float = 1.0
    source: str = ""
    reason: str = ""
    goal_complete: bool = False  # brain says the WHOLE goal is finished, skip remaining steps
