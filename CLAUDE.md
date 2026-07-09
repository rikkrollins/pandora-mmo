# Pandora MMO — Project Context for Claude Code

Read this before doing anything else in this repo. It exists so you (a
fresh Claude Code session with no memory of prior chats) have the same
context a long-running conversation would have built up.

## What this is

A text-based, natural-language D&D 5E multiplayer game running in a
Telegram group, across four topics: Main, Adventure, Support, and
Development. No slash commands are required for gameplay — everything
happens in plain English via natural-language intent classification.
Dev/owner is "Coffee" (@Coffee13333). Bot: @PandoraMMO_Bot. Runs on a
Debian laptop (pandora@openclaw), CPU-only inference via local Ollama.

Current version: see `VERSION`. Full history: `CHANGELOG.md`.

## Critical infrastructure facts — read these before debugging anything

- **Working Ollama model**: `lfm2.5-thinking:latest` (both
  `BUILD_MODEL` and `DM_NARRATION_MODEL` in `.env`/`config.py`).
  `qwen2.5-coder:3b` reliably fails to load on this hardware — don't
  suggest switching back to it.
- **Ollama response time is genuinely 30–160+ seconds per call** on
  this CPU. This is normal, not a bug. Silence for under ~2 minutes
  after an action that needs AI narration (spell casting, NPC dialogue,
  welcome messages) is expected, not broken.
- **OpenClaw gateway must stay stopped** — `sudo systemctl stop
  openclaw-gateway.service && sudo systemctl disable
  openclaw-gateway.service`. If it's running, it polls the same bot
  token and causes a 409 Conflict.
- **Always `cd ~/pandora_mmo` before running `python3 bot.py`** — an
  old copy in the Trash has caused confusion before.
- **Telegram topic IDs**: Main=1 (but `message_thread_id` is `None` for
  Main via the API — `topics.is_main()` must handle both), Support=22,
  Adventure=23, Development=41.

## Deployment

```bash
pkill -9 -f "python3 bot.py"
cd ~/pandora_mmo
git pull                    # once this repo is the live source
python3 bot.py
```

## Architecture

- **Rules are pure Python, deterministic, real dice math** —
  `rules/dice.py`, `rules/combat.py`, `rules/leveling.py`. The AI layer
  (`ai/dm_agent.py`, `ai/npc_agent.py`, `ai/intent_parser.py`,
  `ai/support_agent.py`) only narrates outcomes that have ALREADY been
  decided by the rules layer — it never decides hit/miss, damage, or
  any numeric outcome itself. Never let AI narration invent a game
  fact; always compute it in `rules/` first and hand the result to the
  narrator as ground truth.
- **`<think>` tags** from `lfm2.5-thinking` are stripped everywhere via
  `ai/text_cleanup.py` before reaching players.
- **`sessions.py`** holds all in-memory combat state, keyed by
  `chat_id`, with a per-chat `asyncio.Lock` — every handler touching a
  session MUST acquire `sessions.get_lock(chat_id)` first, or
  concurrent players will corrupt turn order.
- **Conditions** (`prone`, `poisoned`) live only on the in-memory
  participant dict, not the database — they reset when combat ends.
  This is deliberate, not an oversight.
- **Support agent is grounded in the real item/spell/guild catalogs**
  (`ai/support_agent.py` builds this from `items.py`/`spells.py`/
  `guilds.py` at call time) — it must never be allowed to answer from
  general D&D knowledge, only from what's actually implemented, or it
  will hallucinate content that doesn't exist in this build (this
  happened once already; the fix is the grounding block in that file —
  don't remove it).

## Testing convention — always follow this, don't skip it

Every change in this project has been verified with a REAL executed
test before being called done — actual dice rolls, actual database
reads, actual simulated Telegram updates through the real handler
functions (see the pattern of throwaway `*_tmp.py` scripts used
throughout this project's history: build a fake `Update`/`Context`,
call the real `bot.adventure_master_handler`, assert on real output).
Never claim something works based on reading the code; run it. Several
real bugs (HP not persisting to the DB, an infinite loop when a
stabilized player faced a target-less enemy, a narration mismatch for
skill checks) were only caught this way — reasoning about the code
alone missed all three.

## Known limitations (see SETUP_GUIDE.md for the full, current list)

- Reactions (Shield, Counterspell, opportunity attacks) are NOT
  implemented — doing this properly requires restructuring
  `resolve_attack()`'s atomic roll-and-apply-damage step, not a
  bolt-on. Don't rush this.
- Skill checks use one fixed DC (13) for every situation — deliberate,
  to avoid the AI inventing difficulty numbers.
- Rest is a simplified full-heal, not full short/long rest rules.

## Currently open investigation

There's an unresolved report: an NPC dialogue message ("Say hello to
grimsby") produced NO reply at all — not even the `"..."` fallback that
`ai/npc_agent.py`'s `talk_to_npc()` should always produce on an Ollama
timeout. Temporary debug logging was added to `bot.py` (line ~1500,
right after the `parse_intent` call) and `ai/intent_parser.py` (lines
~196/198, around the JSON extraction) to capture the raw model
response and parsed intent for this exact phrase — logging is
currently STILL IN PLACE, not yet removed. If you're picking this up:
reproduce it, read the DEBUG lines, find out whether the model is
misclassifying this as `action: "chat"` (which by design produces no
reply) versus a JSON-parsing failure versus something else — then
remove these two debug prints once resolved.

## Other debug logging still present — verify before removing

Separately, `bot.py` also still has THREE older debug print statements
around `development_topic_handler` (search for `DEBUG:` — you'll find
them near "entered", "_is_group_owner returned", and "router
thread_id=..."). These were added during an earlier investigation into
the Development topic not responding. It's unclear from this file
alone whether that issue was ever confirmed fixed — check recent real
playtesting evidence (ask the user, or look for it) before assuming
these are safe to delete. If the Development topic has been working
reliably in actual play, these are just forgotten cleanup and can be
removed; if not, they're your starting point for that investigation
too.
