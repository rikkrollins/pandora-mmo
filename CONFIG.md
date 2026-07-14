# Pandora MMO — Configuration Reference

Every knob you can turn, in one place. Two kinds:

- **`.env` settings** — edited in the file, need a bot restart to take
  effect.
- **Live Development-topic commands** — say them in Development, take
  effect instantly, no restart, survive a restart once set (stored in
  the database, not the file).

---

## 1. `.env` settings (edit the file, then restart the bot)

| Variable | Default | What it does |
|---|---|---|
| `BOT_TOKEN` | *(required)* | Your bot's Telegram token from @BotFather. |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Where the local Ollama server is. |
| `BUILD_MODEL` | `lfm2.5-thinking:latest` | Model used for intent classification (understanding what a player typed). |
| `DM_NARRATION_MODEL` | `lfm2.5-thinking:latest` | Model used for narration (the actual storytelling text). Can be a different model from `BUILD_MODEL` if you want. |
| `TOPIC_MAIN_ID` | `1` | Main topic's Telegram thread ID. |
| `TOPIC_SUPPORT_ID` | `22` | Support topic's thread ID. |
| `TOPIC_ADVENTURE_ID` | `23` | Adventure topic's thread ID — where the game itself is played. |
| `TOPIC_DEVELOPMENT_ID` | `41` | Development topic's thread ID — where these commands and dev Q&A live. |
| `TELEGRAM_CHAT_ID` | *(unset)* | The group's own chat ID. Only needed for `scripts/announce_deploy.py`. |
| `DB_PATH` | `pandora_mmo.db` | SQLite database file. **Never point this at the live file for testing** — use a throwaway path under `tests/tmp/`. |
| `STORY_MODE` | `5` | Narration length/style, 0 (shortest) to 10 (full novel-chapter prose). Currently `7` in this deployment. Prefer the live "set story mode to N" command below over editing this — no restart needed that way. |
| `SKILL_CHECK_DC` | `13` | The one fixed difficulty number used for every skill check in the game (deliberate — the AI never gets to invent a DC). |
| `NATURAL_HEALING_FULL_REST_HOURS` | `2` | Real-world hours of continuous resting needed to heal from 0 to full HP/spell slots. |
| `PARTY_MAX_MEMBERS` | `6` | Largest a single formed party (invite/accept) can grow to. |
| `MOLTBOOK_API_KEY` | *(unset)* | Enables Moltbook (AI-agent social network) integration. Unset = fully no-op. |
| `MOLTBOOK_HEARTBEAT_INTERVAL_SECONDS` | `900` | How often the bot checks Moltbook for notifications worth posting to Development. |
| `MOLTBOOK_SOCIAL_TICK_INTERVAL_SECONDS` | `1800` | How often the bot autonomously posts/comments/upvotes on Moltbook on its own. Can be paused live — see below. |
| `HOURLY_UPDATE_INTERVAL_SECONDS` | `3600` | How often the Adventure-topic status update (who's around, quest board) posts. Always lands on the real top of the hour regardless of this value's exact size. |
| `AI_PARTY_TICK_INTERVAL_SECONDS` | `900` | How often the autonomous AI party takes its next turn (one character at a time). Whether it runs at all is a live toggle — see below. |

---

## 2. Live Development-topic commands (instant, no restart)

Just type these as a normal message in the Development topic (owner-only, same as everything else there):

| Say this | What it does |
|---|---|
| `turn on tts` / `turn off tts` | Enables/disables optional voice narration via @TextTSBot (already in the group). When on, real narration — combat, skill checks, NPC dialogue, quest text — also gets read aloud in whatever voice is currently set for the group (Coffee picked "Alan, Australian"). Off by default. |
| `set story mode to N` (N = 0–10) | Overrides `STORY_MODE` live, no restart. Takes priority over the `.env` value until changed again. |
| `turn on ai party` / `turn off ai party` | Resumes/pauses the autonomous AI party's independent turns. Currently **off** (paused since 2026-07-12 while reliability for real players was the priority). Doesn't delete or reset the AI companions — just stops new autonomous turns. |
| `turn on moltbook` / `turn off moltbook` | Resumes/pauses the bot's autonomous Moltbook posting/commenting/upvoting. On by default whenever `MOLTBOOK_API_KEY` is set. |

All four are stored in the database (`game_settings` table) and survive a bot restart — once set, they stay set until changed again.

---

## Notes

- TTS is a third-party bot (@TextTSBot, ~10k monthly users), not something this project controls or hosts — it has its own 1000-character limit per `/tts` call (narration is truncated to fit) and its own voice options (change your own voice for the group by DMing it directly and using its `/language`/`/effect`/voice picker — whatever you set there is what the group hears).
- Everything in section 2 is deliberately **one shared setting for the whole game**, not per-player — these are group-wide toggles, not individual preferences.
