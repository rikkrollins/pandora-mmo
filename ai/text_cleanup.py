"""
ai/text_cleanup.py
Shared helper used by every AI-calling module. Some local models (like
lfm2.5-thinking) emit their chain-of-thought wrapped in <think>...</think>
before the actual answer. That reasoning is meant for the model's own
use, not for players to read — this strips it out everywhere, once,
rather than duplicating the same regex in every ai/*.py file.
"""
import re

_THINK_TAG_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def strip_think_tags(text: str) -> str:
    """Remove any <think>...</think> block(s) and surrounding whitespace."""
    cleaned = _THINK_TAG_RE.sub("", text)
    return cleaned.strip()
