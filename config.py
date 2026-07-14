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

# Telegram forum topic thread IDs (real values for this group)
TOPIC_MAIN_ID = int(os.getenv("TOPIC_MAIN_ID", "1"))
TOPIC_SUPPORT_ID = int(os.getenv("TOPIC_SUPPORT_ID", "22"))
TOPIC_ADVENTURE_ID = int(os.getenv("TOPIC_ADVENTURE_ID", "23"))
TOPIC_DEVELOPMENT_ID = int(os.getenv("TOPIC_DEVELOPMENT_ID", "41"))

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
PARTY_MAX_MEMBERS = int(os.getenv("PARTY_MAX_MEMBERS", "6"))
