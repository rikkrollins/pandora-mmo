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


# Real live bug (2026-08-14, dev-topic screenshot report, combat
# narration): ai/dm_agent.py's combat/skill-check prompts include the
# real mechanical result as a raw Python dict repr (e.g. "{'raw_roll':
# 9, 'attacker': ...}") so the model has real ground-truth facts to
# narrate faithfully -- but nothing stops it from occasionally quoting
# a literal internal field name instead of just using the value
# naturally ("The raw_roll of 9 thudded through him" was a real,
# reported live occurrence). No legitimate English sentence contains a
# snake_case (underscore-joined) word, so any token matching that shape
# is unambiguously a leaked internal identifier, never a false
# positive -- stripped outright here, same "never trust the model
# alone to follow an instruction perfectly" deterministic-strip
# philosophy as strip_think_tags above.
_SNAKE_CASE_LEAK_RE = re.compile(r"\b[a-z][a-z]*(?:_[a-z]+)+\b")


def strip_internal_jargon(text: str) -> str:
    """Remove any leaked snake_case internal field name and tidy the resulting whitespace/punctuation spacing."""
    cleaned = _SNAKE_CASE_LEAK_RE.sub("", text)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\s+([,.!?;:])", r"\1", cleaned)
    return cleaned.strip()


# Real live bug (2026-08-15, dev-bridge screenshot, Coffee circled it):
# a routine combat miss narrated as literally "(stub)" -- the model's
# entire response for that call, verbatim, no think-tags or jargon
# involved, just a bare placeholder-shaped token reaching a real
# player. Neither strip_think_tags nor strip_internal_jargon catches
# this (there's nothing snake_case or thinking-tagged about it) -- this
# is a distinct failure mode: the model produced a degenerate,
# non-prose completion instead of narration. No legitimate narration
# sentence is ONE bracketed/parenthesized placeholder word, so this is
# a safe, narrow catch, same "never trust the model alone" philosophy
# as the checks above. Caller treats a match as an empty response and
# falls back to the deterministic template, same as an Ollama error.
_PLACEHOLDER_TEXT_RE = re.compile(
    r"^[\(\[]?\s*(stub|placeholder|todo|tbd|n/?a|xxx|lorem ipsum|insert (?:text|narration|description)(?: here)?|"
    r"\.{3,})\s*[\)\]]?\.?$",
    re.IGNORECASE,
)


def is_placeholder_text(text: str) -> bool:
    """True if `text` is a bare placeholder token (e.g. "(stub)") rather than real narration prose."""
    return bool(_PLACEHOLDER_TEXT_RE.match(text.strip()))
