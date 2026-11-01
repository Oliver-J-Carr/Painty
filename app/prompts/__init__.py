"""Prompt files, loaded once at import time into named constants.

Prompts use {{NAME}} placeholders (not str.format), so braces in the text
never need escaping. fill() swaps them for values.
"""

from pathlib import Path

_DIR = Path(__file__).parent


def _load(name: str) -> str:
    return (_DIR / f"{name}.md").read_text(encoding="utf-8").strip()


def fill(template: str, **values: object) -> str:
    """Replace each {{NAME}} with its value."""
    for name, value in values.items():
        template = template.replace("{{" + name + "}}", str(value))
    return template


PAINTER_PROMPT = _load("painter")
VISION_ON = _load("vision_on")
VISION_OFF = _load("vision_off")
