"""Deliver the canonical narrator guide without its skill metadata."""
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
GUIDE_PATH = PROJECT / "skills" / "voice-rpg" / "SKILL.md"


def load_guide():
    source = GUIDE_PATH.read_text(encoding="utf-8")
    if source.startswith("---\n"):
        parts = source.split("\n---\n", 1)
        if len(parts) != 2:
            raise ValueError("The narrator guide has unclosed frontmatter.")
        source = parts[1]
    if not source.strip():
        raise ValueError("The narrator guide is empty.")
    return source.strip() + "\n"
