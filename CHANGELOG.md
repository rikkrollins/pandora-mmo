# Changelog

All notable changes to Pandora MMO are documented here.

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
