# Pandora MMO — Project Context for Claude Code

Read this before doing anything else in this repo. It exists so you (a
fresh Claude Code session with no memory of prior chats) have the same
context a long-running conversation would have built up.

## What this is

A text-based, natural-language D&D 5E multiplayer game running in a
Telegram group, across four topics: Main, Adventure, Support, and
Development. No slash commands are required for gameplay — everything
happens in plain English via natural-language intent classification.
Dev/owner is "Coffee" (@Coffee13333). Bot: @PandoraMMO_Bot. Runs on an
OVHcloud Canada VPS (hostname `vps-250247fb`, 8 vCores/24GB RAM/200GB
NVMe, user `debian`, repo at `/home/debian/pandora_mmo`) — migrated
here 2026-08-08 from a Debian laptop (pandora@openclaw), which Coffee
has since powered off and retired; don't suggest failing over to "the
laptop" as if it's still standing by. CPU-only inference via local
Ollama, same as before the migration — no GPU on this plan either.

Current version: see `VERSION`. Full history: `CHANGELOG.md`.

**Design philosophy, stated explicitly (2026-07-10):** Pandora MMO is a
D&D 5E MMO for a Telegram group — human players and AI-driven players
sit at the same table under the same rules. The game deliberately
doesn't check whether whoever's typing in Adventure is human or an AI
agent; it only ever looks at *what* they said. To a new player, this
looks like an ordinary text D&D game at first — the game never
announces that its NPCs and companions are anything more than
characters — until they notice those characters remember them, go
about their own business, wander, and talk to each other whether or
not anyone's watching (see the living-world system below). That's
intentional narrative texture, not a technical claim about the models
themselves.

**Security boundary, and why it already holds (audited 2026-07-10):**
Any AI-driven player is welcome to join and play like anyone else, but
NOTHING typed into this game — by a human or an AI — can ever reach
code execution, file access, or a direct database write, and this is
true by construction, not by a prompt telling the model to behave:
- There is no `eval`, `exec`, `os.system`, `subprocess`, or dynamic
  import anywhere in this codebase (confirmed by grep, keep it that
  way — don't add one to "fix" something quickly).
- Every SQL statement in `db.py` uses parameterized `?` placeholders;
  the one f-string-built query (`PRAGMA table_info({table})`) only
  ever receives a hardcoded literal, never user or AI input.
- AI output only ever does one of two things: (1) becomes narration
  TEXT displayed to players (dm_agent, npc_agent, dev_agent,
  support_agent — none of these write anywhere), or (2) gets checked
  against `ai/intent_parser.py`'s fixed `valid_actions` allowlist
  before `bot.py` will dispatch it to a handler — anything outside
  that list is rejected back to the deterministic keyword fallback.
  A "jailbroken" model response can therefore, at absolute worst,
  produce weird narration text or a misclassified (but still
  allowlisted) game action — never code execution, never a direct
  state mutation, never a bypass of the rules engine.
- If you ever add a new AI-facing feature, preserve this: AI output
  earns a spot in a narration string or an existing allowlisted action
  path — never a raw file path, shell command, SQL fragment, or a new
  action dispatched without first being validated against a fixed set.

## Critical infrastructure facts — read these before debugging anything

- **Working Ollama model**: `lfm2.5-thinking:latest` (both
  `BUILD_MODEL` and `DM_NARRATION_MODEL` in `.env`/`config.py`).
  `qwen2.5-coder:3b` reliably fails to load on this hardware — don't
  suggest switching back to it.
- **Ollama response time is genuinely tens of seconds per call** on
  this CPU-only VPS — ~46–73s measured for a typical narration call
  post-migration (2026-08-08), faster than the old laptop's documented
  30–160+s but still real, not instant. Silence for under ~2 minutes
  after an action that needs AI narration (spell casting, NPC dialogue,
  welcome messages) is expected, not broken. All `requests.post(...,
  timeout=...)` calls in `ai/*.py` are set to 200s specifically so they
  don't cut this off early — if you add a new Ollama call, match that.
  Still single-slot (`OLLAMA_NUM_PARALLEL` is a confirmed no-op for
  `lfm2.5-thinking`'s architecture) — never run the regression suite or
  any other Ollama-touching test/benchmark while a real player might be
  active, it will queue behind/in front of live traffic on the same
  instance.
- **Always `cd ~/pandora_mmo` before running `python3 bot.py`** — keep
  this habit even though the old laptop's stray Trash-copy confusion
  doesn't apply to this server.
- **Telegram topic IDs**: Main=1 (but `message_thread_id` is `None` for
  Main via the API — `topics.is_main()` must handle both), Support=22,
  Adventure=23, Development=41.

## Deployment

**The live bot runs under a systemd user service**, discovered
2026-07-16 the hard way: `pandora-mmo-bot.service`
(`~/.config/systemd/user/pandora-mmo-bot.service`), `Restart=on-failure`,
`RestartSec=5`, `ExecStart=/usr/bin/python3 bot.py`,
`WorkingDirectory=~/pandora_mmo`, logs appended to `bot_live_tmp.log`
same as before. This did NOT exist when the manual pkill/nohup
procedure below was originally written, and manually killing the
process now just makes systemd relaunch it 5 seconds later — worse,
`pkill -f '^python3 bot\.py'` (anchored) does NOT match systemd's
`/usr/bin/python3 bot.py` full-path invocation, so the OLD manual
"atomic restart" procedure silently failed to kill the real process
while a second one started, causing duplicate processes and a
`telegram.error.Conflict: terminated by other getUpdates request` loop
until both stray PIDs were hunted down and `kill -9`'d one at a time.

**Redeploy the correct way now:**
```bash
cd ~/pandora_mmo
git pull                              # once this repo is the live source
systemctl --user restart pandora-mmo-bot.service
sleep 3
systemctl --user status pandora-mmo-bot.service --no-pager
pgrep -fac "python3 bot.py"           # must be exactly 1
tail -5 bot_live_tmp.log              # confirm getUpdates flowing, no Conflict
```
No pkill, no nohup, no disown, no `run_in_background` needed — systemd
owns the process lifecycle now. If you ever see MORE than one
`python3 bot.py` process (e.g. from a stray manual start), find and
`kill -9` the extra PID(s) individually rather than any broad pkill,
then let `systemctl --user status` confirm systemd's own instance is
the one still standing.

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

**Never test against the live `pandora_mmo.db`** once it has real
player data. Point `DB_PATH` at a throwaway file under `tests/tmp/`
(gitignored) instead, e.g.:
`DB_PATH=tests/tmp/whatever.db python3 bot_test_tmp.py`. Delete the
throwaway `.py` script and the `.db`/`.db-journal` files when done,
same as any other `*_tmp.py`.

**Deploy safety, live production bot:**
- Redeploys go through `systemctl --user restart pandora-mmo-bot.service`
  (see Deployment section above) — NOT a manual pkill/nohup, now that a
  systemd user service owns the process. Log file is still
  `bot_live_tmp.log`, still append-mode (systemd's own
  `StandardOutput=append:...` config), so history needed to answer "did
  you see the message I sent?" is preserved across restarts same as
  before. After restarting, always verify exactly one `python3 bot.py`
  process (`pgrep -fac`) and check the log tail for a clean `getUpdates`
  200 (not a `Conflict` loop) before considering the deploy done.
- Before a redeploy, post a heads-up to the group (Development topic)
  that an update is coming with a rough ETA, via
  `scripts/announce_deploy.py`, so players aren't caught by a silent
  restart. The bot should otherwise stay online continuously.

## Known limitations (see SETUP_GUIDE.md for the full, current list)

- Reactions (2026-07-15, real, not a bolt-on): `resolve_attack()`
  already fully resolves an attack roll before any damage is rolled or
  applied, so that's the real checkpoint both plug into — Shield
  (Wizard/Sorcerer, auto-triggers when the hit isn't a crit and a real
  spell slot is available, turning it into a miss if it actually would
  have mattered) and Uncanny Dodge (Rogue level 5+, halves confirmed
  damage). Both share one reaction per round via `reaction_used_round`
  on the participant dict. Opportunity attacks were already
  implemented (2026-07-13, `_do_flee`'s "opportunity attacks as you
  break away" block) before this doc was updated to say so. Counterspell
  IS now implemented (2026-08-13, per Coffee, dev-bridge: "Certain
  enemies definitely need to have magic.. lets get this working" —
  reported right after hitting this exact limitation live): any monster
  or boss with a real `known_spells` list in `campaigns/default/
  campaign.json` gets a real `MONSTER_SPELLCAST_CHANCE` (40%) chance
  each turn to cast a real damage spell instead of attacking, via
  `bot._maybe_monster_cast_spell` — through the exact same rules-layer
  pipeline (`spells_module.resolve_damage_spell` +
  `apply_damage_type_modifier` + `elemental_overflow_heal`) a player's
  own cast already uses. Fully data-driven, not hardcoded to specific
  monster names — as of a 2026-08-14 audit, 21 monsters actually carry
  `known_spells` (the original 3 shaman variants, plus 18 of the 22
  real bosses); this doc previously undersold that as "a first slice,
  3 shaman-flavored monsters," which was already stale by the time
  most bosses got spells added. The 4 bosses without `known_spells`
  (`goblin_boss`, `colosseum_champion`, `the_unbegun`, `the_unasked`)
  are pure-melee by design, not a gap — each has its own other real
  signature mechanic instead (see the next bullet). Any real party
  member who knows Counterspell, has a spell slot, and hasn't used
  their reaction this round auto-negates a monster's cast (same
  `reaction_used_round` economy as Shield/Uncanny Dodge).
- Boss signature mechanics (flag-driven, `campaigns/default/
  campaign.json` → copied onto the live combat participant dict in
  `bot.py` → checked at a real `rules/combat.py`/`bot.py` checkpoint —
  see `bot._boss_ability_facts` for the full current list fed to
  narration): as of Synergy Phase 9 (2026-08-14), every one of the 22
  real bosses has at least one real flag or `known_spells`, not just
  the two originals (`adapts_to_damage` on The Unasked,
  `extra_attack_when_enraged` on The Unbegun). Also now real:
  `counters_sneak_attack`/`counters_rage`/`counters_backstab` (a boss
  that's "learned" to blunt one specific class mechanic after the first
  hit lands, same shape each time), `resists_dot_stacking` (halves the
  Hex/Hunter's Mark bonus die), `echoes_damage_type` (a free backlash to
  the attacker on the 3rd hit of the same damage type), and
  `resists_forge_guild` (negates half of the Forge Guild's own +10%
  weapon bonus specifically, checked right after that bonus is applied
  — not folded into resistance math, which runs before the bonus even
  exists). This is still not exhaustive — most bosses still share
  generic mechanics; this is which ones have a real, individual one.
- Skill checks use one fixed DC (13) for every situation — deliberate,
  to avoid the AI inventing difficulty numbers.
- Resting ("I rest"/"heal up" and "take a rest"/going inactive) is NOT
  an instant full heal — changed 2026-07-11 per Coffee's direction.
  Both now share one real-time-gated mechanic: going inactive starts a
  `rest_started_at` clock, and `_apply_natural_healing()` in bot.py
  heals HP/spell slots in proportion to real-world elapsed time on
  reactivation, capped at full after `NATURAL_HEALING_FULL_REST_HOURS`
  (currently 2h). Deliberate: resting was previously a free instant
  full heal, which undercut needing potions/healing spells for
  in-the-moment recovery. Still not full 5E short/long rest rules
  (no distinction between the two, no hit-dice spending) — that's
  still a simplification, just no longer an instant one.

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
