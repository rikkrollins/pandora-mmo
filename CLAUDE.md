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
  welcome messages) is expected, not broken. All `requests.post(...,
  timeout=...)` calls in `ai/*.py` are set to 200s specifically so they
  don't cut this off early — if you add a new Ollama call, match that.
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

**Standing authorization (from Coffee, 2026-07-09):** Claude Code may
git pull, commit, and push to this repo's `origin` (rikkrollins/
pandora-mmo) without asking permission first, for ordinary development
work on this project. This does NOT cover force-pushing, rewriting
history, or pushing to any repo/branch other than this one's normal
flow — those still need explicit confirmation. Whenever a change is
deployed (bot restarted with new code), post a short summary of what
changed to the Development topic (thread ID in `config.TOPIC_
DEVELOPMENT_ID`) — see `scripts/announce_deploy.py`.

GitHub auth: a personal access token lives in `.env` as `GITHUB_TOKEN`
(gitignored, never committed). It authenticates pushes via a Basic auth
header built from it — see git history around 2026-07-09 for the exact
pattern if it needs to be reconstructed. To rotate it: generate a new
token by hand at github.com/settings/tokens (GitHub does not allow a
token to mint its own replacement — this is a platform security rule,
not a missing feature), then run
`echo "ghp_..." | python3 scripts/rotate_github_token.py`.

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

## Resolved investigations (2026-07-09)

Both previously-open investigations were reproduced live (per the
testing convention above, through the real handlers) and fixed. All
temporary `DEBUG:` print statements from both investigations have been
removed — if you see `print(f"DEBUG:...")` anywhere in `bot.py` or
`ai/`, it's new, not leftover.

- **NPC dialogue silent failures.** Root cause had two layers:
  1. Every Ollama call timeout across `ai/*.py` (30s in
     `intent_parser.py`, 60s in `npc_agent.py`/`dm_agent.py`, 120s in
     `support_agent.py`) was shorter than the documented real latency
     above (30–160+s), so calls were frequently guaranteed to time out
     before the model could answer. All of these were bumped to 200s.
  2. With the model actually given time to respond, live reproduction
     caught it genuinely misclassifying "Say hello to grimsby" as
     `action: "chat"` while still correctly extracting
     `npc_name: "Grimsby"` — `"chat"` is intentionally silent by
     design, so this produced exactly the reported zero-reply bug.
     Fixed in `ai/intent_parser.py`'s `parse_intent()`: if the model
     returns `action: "chat"` but a known `npc_name` is present, it's
     now reclassified to `talk_npc`. Confirmed via repeated live runs
     through the real `adventure_master_handler`.
  3. Separately (defense in depth, not the confirmed cause): no
     `application.add_error_handler()` was registered anywhere, so ANY
     unhandled exception in ANY handler — not just this one — failed
     completely silently with no reply and no log trail. A global
     `_log_unhandled_error` handler is now registered in
     `build_application()`; it logs the full traceback via `logger` and
     best-effort tells the player something broke, instead of silence.
- **Development topic "restricted to owner" for the actual owner.**
  Live-reproduced: `_is_group_owner()`'s `getChatMember` call is wrapped
  in a broad `except Exception`, and on ANY failure (a transient
  network/API hiccup, not just genuinely not being the owner) it
  returned `False` — indistinguishable, from the player's side, from a
  real permission denial. `_is_group_owner()` now returns `None` (not
  `False`) when the check itself fails, and
  `development_topic_handler` gives a distinct "couldn't verify
  permissions, try again" message in that case, rather than the
  fixed "restricted to owner" line. The routing (`topics.is_development`,
  thread ID 41) and the ownership check itself were both confirmed
  correct in live testing — Telegram reports Coffee's status as
  `ChatMemberStatus.OWNER`, which does correctly compare equal to
  `"creator"`.
