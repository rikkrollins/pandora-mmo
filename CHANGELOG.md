# Changelog

All notable changes to Pandora MMO are documented here.

## [1.2.0] — AI party fixes, quest system hardening, and reliable uptime

### AI-companion identity bug (the big one)
- **Fixed a real ID collision that was quietly corrupting the
  autonomous AI party.** `create_ai_companion`'s synthetic ID counter
  used to reset to a hardcoded `-1000` on every process restart. That
  collided a brand-new autonomous party member (Zara Windrift) with an
  already-existing recruited companion NPC (Sera) that had claimed
  `-1000` in an earlier process. The collision silently rebound Sera's
  active-character slot to Zara, which corrupted the AI party's
  round-robin turn order (Bram Ashfield never got a single autonomous
  turn all session) and explained the AI party's confusing behavior —
  it wasn't inventing content, it was grounded in one character's real
  location while its action landed on a different character standing
  somewhere else. The counter now derives from what's actually in the
  database, so this can't recur; Sera's existing data has been
  repaired (her own ID restored, her own active-character slot back,
  autonomous flag reset).

### AI party / autonomous companions
- The AI party's action prompt no longer names real, copyable
  places/objects in its examples — the small local model was parroting
  those exact examples verbatim regardless of whether they were true,
  which is what caused the repeated "can't get there directly" spam.
  Examples now use situational placeholders instead.
- Moving to your own current location now gives a clear "you're
  already at X" reply instead of the generic can't-get-there message.
- The AI party's situation facts now include sub-locations reachable
  via `descends_to`/`ascends_to` (e.g. a tavern cellar), a hand-authored
  story quest on offer at the location (previously only area board
  bounties were mentioned), a pending branching-quest decision and its
  real choice labels, and what a shop actually has for sale — all
  previously missing, all now grounded in real data.
- The prompt now knows "accept the quest" is a real, expected action.
- **AI-controlled party members can now actually complete quests they
  accept.** A leftover exclusion (from before this game had autonomous
  AI players) blocked every `is_ai` character from ever getting credit
  for a monster kill, even for a `defeat_monster` quest they personally
  held — contradicting this project's own design philosophy that an
  AI-driven party member plays under exactly the same rules as a human
  one.

### Bug fixes
- Resting no longer gets interrupted by repeating "I rest" — it used
  to fully wake the character (losing partial healing) before putting
  them right back to sleep.
- Character-creation progress (or any other mid-flow player state) no
  longer gets silently wiped by a routine bot restart — `user_data` now
  persists across restarts.
- Stat-assignment questions ("help me assign my rolled stats") now get
  a real, deterministic answer computed directly from the character's
  actual class and race — highest rolls to that class's real 5E
  priority stats, real racial bonus applied — instead of asking the
  model to reason it out. It was previously giving bad advice (once
  suggesting a full class change) with no real bonus data to work
  from, and even after grounding the prompt with real data, consistently
  exceeded the response timeout. Same "rules decide, AI narrates"
  principle already used for XP-to-level and active-character lookups.
- "Who is my active character" now classifies correctly instead of
  falling through ungrounded.
- Quest-accepted narration now includes the character's name, matching
  the existing convention for other action messages in the shared
  Adventure feed.

### Infrastructure
- The bot is now supervised by a `systemd --user` service
  (`pandora-mmo-bot.service`, enabled for boot/login persistence)
  instead of a manually launched process — restarts are now near-
  instant and the bot's uptime no longer depends on any particular
  Claude Code session staying open.

## [1.1.0] — Living world: lockpicking, crafting, factions, memory, and more

### New gameplay systems
- **Lockpicking** — real chests and doors, gated behind a genuine DC-13
  DEX check (rules/dice.py, same fixed-DC convention as every other
  skill check). Chests grant real loot/gold; locked doors permanently
  open a shortcut once picked. State is in-memory (resets on restart),
  the same deliberate simplification as combat conditions.
- **Gathering & crafting** — resource nodes at specific locations grant
  raw materials on a successful ability check; a new deterministic
  `rules/crafting.py` module resolves real recipes (material check +
  ability roll decide success, never invented by narration). Failed
  crafts don't consume materials, so a bad roll isn't punished twice.
- **Fog-of-war map** — `visited_locations` is now tracked per character
  and persisted to the DB; "show me the map" renders only what's
  actually been explored, with unexplored connections shown as a count
  rather than a name.
- **Character slots** — a telegram user can now own multiple characters
  (new `character_id` primary key + `active_characters` table). Create
  additional characters without deleting existing ones, list your
  roster, switch your active character, or delete one — the oldest
  remaining character becomes active automatically if the active one is
  deleted.
- **Class/race spell grants on level-up** — leveling up now actually
  grants new spells as they're unlocked (`spells.py`'s
  `SPELL_LEVEL_UNLOCK_CHAR_LEVEL` table), not just at character
  creation. Applies to AI companions too, since they level through the
  same `db.add_xp`. Tiefling's Infernal Legacy trait now genuinely
  grants the Thaumaturgy cantrip at creation.
- **Fast-travel waypoints** — any location a character has actually
  visited becomes a warpable waypoint ("warp to the crossroads
  tavern"), skipping the walk. Ordinary on-foot `move` (fully narrated,
  connection-by-connection) is unchanged; this is a pure convenience
  layered on top of the same visited-locations data as the map.

### Living AI world-NPCs
- NPCs now have a real **alignment** and **disposition** (friendly,
  neutral, or hostile), authored per-NPC in campaign.json. Arriving at
  a location with one has a chance of an unprompted, in-character
  ambient reaction — not just a reply to something the player said.
- **Hostile NPCs are real threats** — a hostile NPC encountered this way
  provokes a genuine, dice-resolved combat encounter through the exact
  same combat engine as any other fight; the AI only narrates the
  provocation, never the outcome. A defeated named antagonist doesn't
  respawn.
- **Persistent, per-player NPC memory** — a new `npc_relationships`
  table tracks rapport (`affinity`) and specific remembered events per
  (player, NPC) pair, surviving restarts. NPCs reference this in both
  ordinary conversation and ambient lines, so a shopkeeper genuinely
  remembers what a specific player has done.
- **Real consequences** — stealing is a genuine DC-15 DEX check
  (harder than an ordinary skill check). Getting caught permanently
  bans that player from the shopkeeper's shop (`shop.buy_item` now
  checks this), tanks affinity, and is remembered as a specific event.
- **Factions** — NPCs belong to factions (`campaign.json`'s new
  `factions` catalog) with their own standing per player
  (`faction_standing` table). Defeating a faction member in combat, or
  stealing from one, sours the whole faction's standing with that
  player — and a badly-soured faction can turn even its friendly/
  neutral members hostile toward that specific player.
- **Proficiency growth** — "the more you do something, the better you
  get": repeated real use of an ability in skill checks, lockpicking,
  gathering, or crafting earns a small, capped bonus over time
  (`rules/proficiency.py`), applied only to these non-combat checks,
  never to attack rolls.

### Fixes found via live testing
- The AI intent classifier was unreliable against explicit keyword
  triggers once the action list grew large (e.g. "my characters"
  misread as starting combat, "pick the lock" misread as passing a
  turn) — the keyword fallback's classification is now trusted whenever
  it has any non-default opinion, not just for brand-new action types.
- Locked-door destinations were invisible to `move` entirely (not even
  reachable-but-blocked) because they weren't included in the
  destination search — fixed so a locked connection is a real, nameable
  travel target that reports as blocked until picked.
- A single-lockable location could match the wrong kind of lockable
  (a chest matching "door" phrasing, or vice versa) via an overly
  generic fallback — now kind-specific.

## [1.0.0] — Initial tagged release

### Core game
- Natural-language play in the Adventure topic — no slash commands required
- Character creation: name, race, class, 4d6-drop-lowest ability score rolling
- Real 5E racial ability bonuses and traits (Human, Elf, Dwarf, Halfling,
  Half-Elf, Dragonborn, Tiefling)
- Real 5E level-1 class features for all 12 classes
- Turn-based combat with initiative, real dice-based attack rolls, and a
  visually structured combat log (round/turn announcements, critical
  hit/fumble banners, real roll numbers always shown)
- Death saving throws, fully reconnected: natural 1 = two failures,
  natural 20 = instant revival, 3 successes = stable, 3 failures = death
- Multi-enemy encounters with individually numbered, targetable enemies
- Conditions: prone (via a real contested shove check) and poisoned
  (inflicted by certain monsters), both with real advantage/disadvantage
  mechanical effects
- Non-combat skill checks (sneaking, persuading, climbing, etc.) rolled
  against a real ability modifier and DC
- Spell slots: real 5E level-1 allotments per class, actually spent and
  restored on rest
- Saving throws for area spells (Fireball, Lightning Bolt, Burning Hands)
  — half damage on a successful DEX save, per real 5E rules
- Leveling: real HP growth per level and Ability Score Improvements at
  levels 4/8/12/16/19
- XP awarded on real monster defeats, sourced from actual 5E XP-by-CR
  values
- Backpack, shop (buy/sell), and guild systems
- AI-controlled party companions and recruitable NPCs
- A hidden, spoiler-free campaign storyline across a 3-layer world
  (surface / underground / sky)

### AI & reliability
- Natural-language intent classification with a robust keyword fallback
  when the model is unreachable
- Dungeon Master narration and NPC dialogue via a local Ollama model,
  with dramatic tone calibrated to how good or bad each dice roll was
- `<think>` reasoning tags stripped from all model output before it
  reaches players
- Per-chat locking to prevent concurrent players from corrupting the
  same combat session
- Concurrent update processing so one slow handler can never freeze the
  whole bot
- A universal "cancel" / "stop" / "reset" escape hatch, including
  force-ending a stuck combat session

### Meta
- Development topic restricted to the real Telegram group owner
  (checked live via the Bot API)
- Support topic open to everyone, with an AI assistant grounded in the
  actual item/spell/guild data (never inventing content that doesn't
  exist in this build)
- `/version` and `/changelog` commands

### Known limitations (see SETUP_GUIDE.md for the full list)
- Reactions (Shield, Counterspell, opportunity attacks) are not yet
  implemented — this needs real changes to the combat resolution step,
  not a quick bolt-on.
- Conditions reset when combat ends rather than persisting with full
  duration tracking.
- Rest is a simplified full-heal, not full 5E short/long rest rules.
