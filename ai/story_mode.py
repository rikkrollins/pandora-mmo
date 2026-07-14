"""
ai/story_mode.py
Scales narration length and prose style across every narration function
in ai/dm_agent.py (and, if added later, ai/npc_agent.py) according to
config.STORY_MODE (0-10): 0 is the shortest, most utilitarian prose;
10 is a full novel-chapter storybook style. Each narration function has
its own baseline sentence-count range (a skill check is naturally
shorter than a welcome message) — this scales that baseline by a single
shared factor, so raising STORY_MODE makes everything longer together,
proportionally, rather than needing per-function tuning.

Never affects WHAT is narrated, only how much prose wraps around it —
every preamble in dm_agent.py still gets the same "narrate ONLY the
real, provided facts" instruction regardless of story mode.
"""
import config
import db

# Index i = STORY_MODE level i. 5 is a modest step up from this
# project's original fixed baselines (per Coffee's "longer, a bit more
# detailed" request); 0 is terse; 10 is a full novel-chapter length.
_FACTORS = [0.35, 0.5, 0.65, 0.8, 0.95, 1.15, 1.4, 1.7, 2.1, 2.6, 3.2]


def _level() -> int:
    """
    A live db.game_settings override (2026-07-14, via the Development-
    topic "set story mode to N" command) takes priority over
    config.STORY_MODE's .env value -- lets Coffee try a different
    narration length immediately, without a redeploy, exactly what he
    asked for when testing STORY_MODE=10 earlier. Falls back to the
    .env default if no live override has ever been set.
    """
    override = db.get_setting("story_mode")
    if override is not None:
        try:
            return max(0, min(10, int(override)))
        except ValueError:
            pass
    return max(0, min(10, getattr(config, "STORY_MODE", 5)))


def scaled_sentences(base_min: int, base_max: int) -> str:
    """e.g. scaled_sentences(4, 6) at STORY_MODE=5 -> '5-7 sentences'."""
    factor = _FACTORS[_level()]
    lo = max(1, round(base_min * factor))
    hi = max(lo + 1, round(base_max * factor))
    return f"{lo}-{hi} sentences"


def style_directive() -> str:
    level = _level()
    if level <= 1:
        return "Keep the prose minimal and strictly utilitarian — just the essential facts, no embellishment."
    if level <= 3:
        return "Keep the prose concise, with a light touch of atmosphere."
    if level <= 6:
        return "Write with a suspenseful, storybook tone — vivid but economical, never padded."
    if level <= 8:
        return "Write in a rich, novelistic style — linger on sensory detail, mood, and tension before resolving the moment."
    return (
        "Write in a lush, suspenseful, novel-chapter storybook style — take your "
        "time building atmosphere, sensory detail, and tension, the way a "
        "chapter of a fantasy novel would, before landing on the moment itself."
    )
