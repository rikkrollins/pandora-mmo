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
_UNCLOSED_THINK_RE = re.compile(r"<think>.*", re.DOTALL | re.IGNORECASE)


def strip_think_tags(text: str) -> str:
    """
    Remove any <think>...</think> block(s) and surrounding whitespace.

    Also strips a truncated block that's missing its closing tag --
    confirmed live 2026-07-17: capping num_predict for speed (see
    ai/dm_agent.py's _NARRATION_OPTIONS, ai/support_agent.py,
    ai/intent_parser.py) can cut generation off mid-thought, before the
    model ever emits </think>. The paired regex above requires a
    closing tag to match at all, so without this fallback the ENTIRE
    raw chain-of-thought leaked straight to a real player instead of
    being stripped -- caught via a live-Ollama test run of an
    unrelated feature (/redo), not something anyone was looking for.
    """
    cleaned = _THINK_TAG_RE.sub("", text)
    cleaned = _UNCLOSED_THINK_RE.sub("", cleaned)
    return cleaned.strip()


_MARKDOWN_EMPHASIS_RE = re.compile(r"\*\*(.+?)\*\*|\*(.+?)\*|__(.+?)__|_(.+?)_|`(.+?)`")
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "]+",
    flags=re.UNICODE,
)
TTS_CHAR_LIMIT = 1000  # TextTSBot's own stated /tts limit


def to_speakable_text(text: str, char_limit: int = TTS_CHAR_LIMIT) -> str:
    """
    Strips markdown emphasis markers and emoji so a TTS engine reads
    prose instead of literal asterisks/underscores, and truncates to
    TextTSBot's own stated 1000-character /tts limit (2026-07-14).
    """
    def _unwrap(m: re.Match) -> str:
        return next(g for g in m.groups() if g is not None)

    cleaned = _MARKDOWN_EMPHASIS_RE.sub(_unwrap, text)
    cleaned = _EMOJI_RE.sub("", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:char_limit]
