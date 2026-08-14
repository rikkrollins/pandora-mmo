"""
config.py
Loads environment variables (bot token, model names, topic IDs) and
exposes them as module-level constants for the rest of the app.
"""
import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# Ollama connection
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")

# Model routing: the build/tool-calling model vs. the narration model.
# These are intentionally separate so they can be swapped independently.
BUILD_MODEL = os.getenv("BUILD_MODEL", "lfm2.5-thinking:latest")
DM_NARRATION_MODEL = os.getenv("DM_NARRATION_MODEL", "lfm2.5-thinking:latest")

# Voice-message transcription (task #97, 2026-07-22) -- Groq's free
# Whisper endpoint, see ai/stt_groq.py. Empty/unset means STT is
# simply off, not an error -- same convention as every other optional
# integration here.
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

# Telegram forum topic thread IDs (real values for this group)
TOPIC_MAIN_ID = int(os.getenv("TOPIC_MAIN_ID", "1"))
TOPIC_SUPPORT_ID = int(os.getenv("TOPIC_SUPPORT_ID", "22"))
TOPIC_ADVENTURE_ID = int(os.getenv("TOPIC_ADVENTURE_ID", "23"))
TOPIC_DEVELOPMENT_ID = int(os.getenv("TOPIC_DEVELOPMENT_ID", "41"))

# Guild topics (task #77) -- created 2026-07-17 via
# scripts/create_guild_topics.py (a one-time infra script; DO NOT
# re-run it, it would create duplicates). Maps guild_id -> real
# Telegram thread id, same style as items.py/guilds.py's other
# id -> data dicts.
GUILD_TOPIC_IDS = {
    "adventurers_guild": int(os.getenv("TOPIC_ADVENTURERS_GUILD_ID", "0")) or None,
    "arcane_circle": int(os.getenv("TOPIC_ARCANE_CIRCLE_ID", "0")) or None,
    "silver_wardens": int(os.getenv("TOPIC_SILVER_WARDENS_ID", "0")) or None,
    # 2 new guild topics (2026-07-25, via scripts/create_new_guild_topics.py)
    "thieves_guild": int(os.getenv("TOPIC_THIEVES_GUILD_ID", "0")) or None,
    "faith_circle": int(os.getenv("TOPIC_FAITH_CIRCLE_ID", "0")) or None,
    # 2 more guild topics (2026-07-25, via scripts/create_forge_enchanters_topics.py)
    "forge_guild": int(os.getenv("TOPIC_FORGE_GUILD_ID", "0")) or None,
    "enchanters_guild": int(os.getenv("TOPIC_ENCHANTERS_GUILD_ID", "0")) or None,
}

# The group's own chat id (distinct from the topic thread ids above —
# both are required together to post into a specific topic via the Bot
# API). Only needed by scripts/announce_deploy.py; unset by default.
_raw_chat_id = os.getenv("TELEGRAM_CHAT_ID")
TELEGRAM_CHAT_ID = int(_raw_chat_id) if _raw_chat_id else None

# SQLite database file path
DB_PATH = os.getenv("DB_PATH", "pandora_mmo.db")

# Moltbook (social network for AI agents) — used to let AI agents discover
# and join Pandora MMO. Unset by default; the heartbeat check in bot.py
# no-ops without it.
MOLTBOOK_API_KEY = os.getenv("MOLTBOOK_API_KEY")
MOLTBOOK_HEARTBEAT_INTERVAL_SECONDS = int(os.getenv("MOLTBOOK_HEARTBEAT_INTERVAL_SECONDS", "900"))
# How often PandoraMMO_Bot autonomously posts/comments/upvotes on Moltbook
# on its own (2026-07-11, per Coffee's explicit full-autonomy direction).
MOLTBOOK_SOCIAL_TICK_INTERVAL_SECONDS = int(os.getenv("MOLTBOOK_SOCIAL_TICK_INTERVAL_SECONDS", "1800"))

# Hourly Adventure-topic status update (recent events, who's around, quest board)
HOURLY_UPDATE_INTERVAL_SECONDS = int(os.getenv("HOURLY_UPDATE_INTERVAL_SECONDS", "3600"))

# Narration length/style, 0 (shortest, utilitarian) to 10 (longest, full
# novel-chapter storybook prose). See ai/story_mode.py — scales every
# narration function's own baseline sentence count by the same factor,
# never changes WHAT gets narrated, only how much prose wraps around it.
STORY_MODE = int(os.getenv("STORY_MODE", "5"))

# How often (seconds) the autonomous AI party takes its next turn, one
# character at a time. 900 = 15 min, matching the other background-loop
# cadences (world heartbeat, hourly update).
AI_PARTY_TICK_INTERVAL_SECONDS = int(os.getenv("AI_PARTY_TICK_INTERVAL_SECONDS", "900"))

# Fixed DC for every skill check in the game (deliberate: the AI never
# gets to invent a difficulty number). Moved here from bot.py 2026-07-14
# so it's changeable via .env like everything else, without editing code.
SKILL_CHECK_DC = int(os.getenv("SKILL_CHECK_DC", "13"))

# Real-time hours of continuous resting/inactivity needed to heal a
# character from 0 to full HP/spell slots (see _apply_natural_healing).
# Moved here from bot.py 2026-07-14, same reason as SKILL_CHECK_DC.
NATURAL_HEALING_FULL_REST_HOURS = float(os.getenv("NATURAL_HEALING_FULL_REST_HOURS", "2"))

# Largest a single formed party (invite/accept, not the whole active
# player roster) can grow to. Moved here from db.py 2026-07-14, same
# reason as SKILL_CHECK_DC.
#
# Raised 6 -> 12 (2026-08-09, real live report, Coffee: "i cant invite
# more than 6 players to the party... we shud be able to have all the
# characters but then use party to pick with ones we want"). There are
# exactly 6 recruitable AI companions in campaign.json -- with the real
# human player themselves also counting toward this same total (see
# db.get_party_size, which counts every characters row with this
# party_id, not just AI ones), the old cap of 6 meant a solo player
# could only ever have 5 of the 6 companions along at once, one short
# of "all of them". This is deliberately independent of
# PARTY_ACTIVE_COMBAT_CAP just below (still 6) -- that's the real
# "pick who fights" bench/unbench system Coffee is describing; this
# constant only ever limited ROSTER membership, not combat size.
PARTY_MAX_MEMBERS = int(os.getenv("PARTY_MAX_MEMBERS", "12"))

# Largest a party's ACTIVE (non-benched) roster can be for a single
# fight (2026-07-31, per Coffee: parties can now grow past a
# comfortable battle size, so pick who's actually fighting -- everyone
# else in the party still shares in quest/combat rewards at the
# existing INACTIVE_PARTY_XP_SHARE rate, same as anyone off resting or
# elsewhere). Independent of PARTY_MAX_MEMBERS, which caps total
# membership, not how many of them fight at once.
PARTY_ACTIVE_COMBAT_CAP = int(os.getenv("PARTY_ACTIVE_COMBAT_CAP", "6"))

# Battle formations (2026-08-01, per Coffee: "character placement has
# an effect in battle... players in the back row have a higher evade%"
# + "allow us to customize the formations" + "enemies use battle
# formations too"). Applies symmetrically to party members AND enemies,
# since both sides' combat participant dicts carry the same
# formation_row field and go through the same targeting helper
# (_pick_formation_weighted_target in bot.py).
#
# FRONT_ROW_TARGET_CHANCE: odds an attacker's target pool is drawn from
# the front row when the front row still has anyone standing. The
# remainder is a real chance to still snipe the back row even then --
# matches Coffee's own "all players can still be targeted" -- and back
# row becomes the only pool once front row is wiped, same as before.
FRONT_ROW_TARGET_CHANCE = float(os.getenv("FRONT_ROW_TARGET_CHANCE", "0.8"))

# Flat AC bonus applied to a defender standing in the back row (rules/
# combat.py's resolve_attack, same additive slot hybrid_features.
# hybrid_ac_bonus already uses) -- this IS the "higher evade%" in this
# game's existing AC-based hit-resolution model, not a separate dodge
# mechanic.
BACK_ROW_AC_BONUS = int(os.getenv("BACK_ROW_AC_BONUS", "2"))

# Formation attack/defense percentages (2026-08-14, per Coffee: "front
# row used for aggressors, front row can have a boost to attack %, and
# back row can have a boost to def %"). Front row's own real damage
# bonus (rules/combat.formation_damage_bonus_pct, same multiplier-stack
# tier as the combat-subclass/guild-benefit % bonuses); back row's own
# genuine PERCENTAGE damage reduction on a landed hit -- distinct from
# BACK_ROW_AC_BONUS above, which only affects whether a hit lands at
# all, never how much it hurts once it does.
FRONT_ROW_DAMAGE_BONUS_PCT = int(os.getenv("FRONT_ROW_DAMAGE_BONUS_PCT", "10"))
BACK_ROW_DAMAGE_REDUCTION_PCT = int(os.getenv("BACK_ROW_DAMAGE_REDUCTION_PCT", "15"))

# Which campaigns/<id>/campaign.json to load (task #56). Previously
# hardcoded directly in bot.py with no .env override at all -- moved
# here so a new campaign folder can actually be activated without a
# code change, same convention as everything else in this file.
ACTIVE_CAMPAIGN = os.getenv("ACTIVE_CAMPAIGN", "default")
