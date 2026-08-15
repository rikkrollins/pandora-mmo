"""
ai/narration_cache.py
A local reuse cache for ROUTINE combat narration only (2026-08-13, per
Coffee: "make this run as fast as it can... if u have to save
pregenerated narrations or text for common actions or scenarios and it
will make it fast do so, keep all this in a obfuscated folder"). Every
real Ollama narration call (ai/dm_agent.narrate_action) costs ~46-73s
on this CPU-only VPS (see CLAUDE.md) — the single highest-frequency one
in the whole game is the routine "attacker hits/misses defender for N
damage" combat line bot.py calls on nearly every turn. This never
touches WHAT happened (the rules layer already decided hit/miss/damage
before this is ever called, per this game's own AI-narrates-facts-
already-decided design) — it only reuses a PREVIOUSLY REAL,
Ollama-generated line of prose for the same rough outcome shape,
rotating among a small pool per bucket so repeats don't read as
obviously robotic.

Deliberately narrow, so the actual "long and entertaining storyline"
(Coffee's own earlier, still-standing direction — see ai/dm_agent.py's
2026-07-17 comment) is never shortchanged: any reaction-trigger flag
(Shield, Uncanny Dodge, Relentless Endurance, Death Ward, Dark One's
Blessing, a hybrid proc), a boss's own turn, or enemy banter always
calls Ollama fresh, never reuses a cached line — those are rare,
narratively significant moments that deserve unique prose every time.

Storage lives outside the git tree entirely (gitignored, see
.gitignore), under a deliberately unlabeled directory name, so nobody
browsing the repo can read ahead on narration text, quest content, or
story flavor by grepping it.
"""
import os
import random
import sqlite3

CACHE_DIR = os.path.join(os.path.dirname(__file__), "..", ".pdrx8k2f")
CACHE_DB_PATH = os.path.join(CACHE_DIR, "n.db")

MAX_VARIANTS_PER_KEY = 8
MIN_VARIANTS_BEFORE_REUSE = 3
CACHE_HIT_RATE = 0.85

_RELIABILITY_DISQUALIFYING_FLAGS = (
    "relentless_endurance_triggered", "death_ward_triggered",
    "dark_ones_blessing_gained", "shield_reaction_triggered",
    "uncanny_dodge_triggered", "hybrid_bonus_damage",
    "hybrid_temp_hp_gained", "hybrid_self_heal_gained",
)


def _damage_tier(dmg: int) -> str:
    if dmg <= 0:
        return "0"
    if dmg <= 5:
        return "low"
    if dmg <= 15:
        return "mid"
    if dmg <= 30:
        return "high"
    return "massive"


def cache_key(character: dict, mechanical_result: dict, include_banter: bool) -> str | None:
    """
    A coarse, deterministic reuse key for a ROUTINE combat outcome, or
    None if this call must never be cached (banter, a boss's own turn,
    or any special reaction trigger firing this time).
    """
    if include_banter or character.get("is_boss"):
        return None
    if any(mechanical_result.get(flag) for flag in _RELIABILITY_DISQUALIFYING_FLAGS):
        return None

    if mechanical_result.get("critical_fail") or mechanical_result.get("raw_roll") == 1:
        outcome = "fumble"
    elif not mechanical_result.get("hit", True):
        outcome = "miss"
    elif mechanical_result.get("critical_hit") or mechanical_result.get("raw_roll") == 20:
        outcome = "crit"
    else:
        outcome = "hit"

    actor = character.get("char_class") or "monster"
    dmg_tier = _damage_tier(mechanical_result.get("damage_dealt", 0))
    return f"{actor}:{outcome}:{dmg_tier}"


def _connect() -> sqlite3.Connection:
    os.makedirs(CACHE_DIR, exist_ok=True)
    conn = sqlite3.connect(CACHE_DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS variants (key TEXT NOT NULL, text TEXT NOT NULL)")
    return conn


# Real live bug (2026-08-15, dev-topic reports, recurring): cache_key()
# buckets purely on {actor class}:{outcome}:{damage tier} -- e.g. every
# Warlock's routine mid-damage hit shares ONE bucket regardless of which
# Warlock is swinging or which monster is on the other end. The stored
# TEXT itself, though, is real Ollama prose that hard-names whoever was
# actually fighting at generation time ("Ravenloft hits Goblin Shaman for
# 18 damage"). Replaying that text verbatim against a later, different
# attacker/defender pair (a different Warlock, or the same Warlock now
# fighting a Crystal Spider) keeps the ORIGINAL fight's names baked in --
# this was the true root cause of "why does it still say Goblin Shaman."
# Fix: store text with the real names swapped for stable placeholders,
# and re-fill them with whoever's ACTUALLY fighting at lookup time, so a
# cached line stays fully reusable but is never stale about identity.
def _placeholder_text(text: str, actor_name: str | None, defender_name: str | None) -> str:
    if defender_name:
        text = text.replace(defender_name, "{DEFENDER}")
    if actor_name:
        text = text.replace(actor_name, "{ACTOR}")
    return text


def _fill_placeholders(text: str, actor_name: str | None, defender_name: str | None) -> str:
    return (
        text.replace("{ACTOR}", actor_name or "the attacker")
        .replace("{DEFENDER}", defender_name or "the target")
    )


def lookup(key: str | None, actor_name: str | None = None, defender_name: str | None = None) -> str | None:
    """
    Returns a random previously-stored variant for `key`, or None if
    this shouldn't be (or can't be) reused this time — a real Ollama
    call is always the caller's fallback on None. Only reuses once a
    bucket has built up real variety (MIN_VARIANTS_BEFORE_REUSE), and
    even once warm, still lets CACHE_HIT_RATE's remainder keep calling
    Ollama fresh so a bucket can keep growing new variants over time
    rather than freezing at whatever it first collected. `actor_name`/
    `defender_name` are THIS turn's real names, filled into the stored
    text's placeholders so a reused line never names last time's fighters.
    """
    if key is None:
        return None
    with _connect() as conn:
        rows = conn.execute("SELECT text FROM variants WHERE key = ?", (key,)).fetchall()
    if len(rows) < MIN_VARIANTS_BEFORE_REUSE:
        return None
    if random.random() > CACHE_HIT_RATE:
        return None
    text = random.choice(rows)[0]
    return _fill_placeholders(text, actor_name, defender_name)


def remember(key: str | None, text: str, actor_name: str | None = None, defender_name: str | None = None) -> None:
    """Stores a real Ollama-generated narration line for future reuse under `key`, capped at MAX_VARIANTS_PER_KEY so a bucket doesn't grow unbounded. Names are placeholder-ized first (see lookup) so reuse never carries a stale identity."""
    if key is None or not text:
        return
    text = _placeholder_text(text, actor_name, defender_name)
    with _connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM variants WHERE key = ?", (key,)).fetchone()[0]
        if count >= MAX_VARIANTS_PER_KEY:
            return
        conn.execute("INSERT INTO variants (key, text) VALUES (?, ?)", (key, text))
