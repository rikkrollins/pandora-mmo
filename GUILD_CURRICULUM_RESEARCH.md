# Guild curriculum research (source: Coffee, pasted via Development topic, 2026-09-29)

Explicitly framed as "research and future implementation," not a build
request — kept here for later reference and scoping, same pattern as
`FEATURE_WISHLIST.md`. Not implemented, not scheduled. A future pass
through this should treat it as inspiration/direction, not a literal
spec: pick real, scoped pieces (probably starting with one guild, one
tier) rather than attempting the whole 1-100 curriculum for both
guilds at once.

---

## The Forge Guild — advanced curriculum (levels 20-100)

Current state: a structured training guild with named lessons
("Proving the Steel") that award Gold + XP. Proposed direction for
levels 20-100: stop being "more lessons about forging" and instead
teach systems the player will actually use, via 4 tiers:

- **20-40, Master Smith**: Forge Quality (Material + Recipe +
  Technique + Forge Quality = Item, producing Fine/Superior/
  Masterwork/Exceptional/Perfect grades), 7 exotic metals (Embersteel,
  Frostsilver, Umbral Iron, Storm Bronze, Sunsteel, etc.) each with
  real properties, branching weapon specialization (same material +
  different technique = different named result), tempering
  (Hard/Balanced/Flexible tradeoffs), a Personal Masterwork the player
  builds for themselves, capped by a dungeon exam (The Furnace Beneath
  the Mountain) granting Master Smith Certification.
- **40-60, Arcane Enchanter**: magical affinity discovery (not player-
  chosen — the item reveals what it's compatible with), rune crafting
  where enchantments become mechanical behaviors (Fury Rune: damage up
  as HP drops; Echo Rune: chance to repeat previous attack), elemental
  fusion for normally-incompatible combos (Fire+Ice=Steam, etc.), soul
  enchantment (weapon absorbs the wielder's own combat behavior into a
  Soul Trait), cursed/risk-reward items, capped by a multi-room dungeon
  exam (The Enchanter's Labyrinth).
- **60-80, Arcane Artificer**: artifacts built from 3 components (Body
  + Heart + Soul), monster-part components (Dragon Heart, Demon Eye,
  etc. — bosses become crafting-ingredient sources, not just loot
  drops), multi-fragment artifact-hunting quests, artifact XP/evolution
  (a weapon that gains new abilities as it's used rather than being
  replaced), quest-crafted "impossible" materials, capped by a no-
  combat crafting-knowledge dungeon (The Celestial Foundry).
- **80-100, Legendary Forgemaster**: fully player-designed legendary
  items (no recipe — the game asks what/powers/represents/costs),
  world-boss-sourced crafting (the whole boss becomes ingredients),
  reality-tier crafting (items that cut barriers, survive dimensional
  attacks, rewind their own durability), 5-piece artifact sets with a
  full-set transformation bonus, player-named weapons that accumulate
  real history (kills, owners, failures) into a unique identity, a
  teaching-the-apprentice capstone quest, and a final "no recipe at
  all" Level 100 ceremony quest that records a permanent Forgemaster
  profile (signature weapon, favorite element, etc.).

**Structural idea, not just content**: 5 parallel disciplines
(Weaponsmithing / Armorsmithing / Enchanting / Artifact Engineering /
Forbidden Forging) rather than one linear track, so the guild feels
like a real profession with specialization choices. Also proposed:
separate **Forge Guild Rank** from character level (a Level 100
character can still have unfinished guild rank progression), unlocking
secret recipes/materials/vendors/rival-smith content — giving a reason
to keep adding Forge content past Level 100 without breaking the level
curve. Post-100 endgame idea: the guild stops handing out recipes and
starts handing out open-ended crafting *problems* ("a dragon's fire
destroys every weapon brought against it — make something that
survives it") as repeatable Master Forgemaster Contracts.

**Every quest reward should teach a real system, not just award XP** —
explicit table mapping each quest to the mechanic it unlocks (Forge
Quality, Exotic Materials, Tempering, Magical Affinity, Rune System,
Element Fusion, Soul Weapons, Curses, Artifact Construction, Boss
Materials, Artifact Restoration, Legendary Crafting, Custom Item
Creation, Teaching/Specialization).

## The Enchanters' Guild — advanced curriculum (levels 20-100)

Proposed as deliberately distinct from the Forge Guild: "Forge Guild
asks what can you build; Enchanters' Guild asks what can you make it
do." Progression: Apprentice → Rune Adept → Arcane Enchanter → Master
Enchanter → Grand Enchanter.

- **20-40, Rune Adept**: rune modifiers (a rune that changes what
  ANOTHER enchantment can become, not an effect itself), magical
  resonance (same-element combos amplify: Fire+Fire=Greater Flame;
  cross-element combos produce named results: Lightning+Water=Chain
  Conduction), Arcane Capacity as a real budget items must be built
  within (can't just stack unlimited enchantments), rune circuits
  (conditional logic: HP<25% → Execution Rune → Shadow Rune → Instant
  Death Chance), intentional enchantment failure sometimes producing a
  genuinely new category (Fire+Lightning failure → Plasma), capped by
  a rune-identification dungeon exam (The Infinite Library).
- **40-60, Arcane Enchanter**: elemental fusion becoming full new
  "enchantment schools," enchantments that grant an ability rather
  than a stat bonus (every 5th attack launches a fire wave), trigger/
  conditional enchantments (on-hit, on-kill, on-low-HP effects),
  sentient enchantments that make real-time decisions ("if my master's
  HP falls below 30%, protect him" becomes a magical companion),
  cursed/forbidden enchantments with real drawbacks (Blood Pact: +100%
  damage but drains HP), capped by a 10-floor school-themed dungeon
  exam (The Tower of a Thousand Spells).
- **60-80, Master Enchanter**: concept enchantments (literal Silence/
  Fear/Memory/Time as enchantable properties, not damage types), full
  spell storage inside items (a ring containing an actual Fireball
  cast), enchantment extraction/transfer/trading as its own economy,
  living enchantments that can partially leave the item and fight
  alongside the player, enchantments that evolve through use (a
  Fireball that becomes a Living Spell after enough kills/behavior),
  capped by an astral-manipulation dungeon exam (The Astral
  Observatory) whose final challenge is inventing something that's
  never existed before.
- **80-100, Grand Enchanter**: reality-scale enchantment (pocket
  dimensions inside small objects, temporal weapons/shields/curses,
  soul enchantment/storage/binding/transfer), a real "First Discovery"
  system — if a player is the first to ever combine a specific
  element+mechanic combo, it becomes a named, permanently-recorded
  discovery, explicitly proposed as a server-wide announcement moment
  ("🌟 WORLD DISCOVERY! [Player] has discovered the Temporal Storm
  enchantment!"), 7-effect grand artifacts that risk "Magical
  Catastrophe" on failure, a teaching-the-apprentice capstone, and a
  Level 100 ceremony quest with no recipe at all, recording a permanent
  Grand Enchanter profile.

**Cross-guild synergy, explicitly the point of keeping both guilds
separate**: Forge Guild produces the physical item (Body/stats/Arcane
Capacity budget); Enchanters' Guild installs runes into that budget to
produce a named combined result (example given: a plain Dragonbone
Sword + Inferno/Resonance/Expansion runes + a Blood Pact →
"Dragonstorm," with a real proc ability, a passive, and an HP-threshold
trigger). The two guilds are meant to depend on each other rather than
compete.

---

## How to use this file later

Both curriculums are far larger in scope than any single feature this
project has shipped at once (Grapple, the narrative-expansion chapters,
etc. were each single, bounded pieces). Before building anything from
this: pick ONE small, real, testable slice (e.g. just Forge Quality at
levels 20-22, or just Rune Modifiers at levels 20-22) and confirm scope
with Coffee before writing content for an entire tier, let alone an
entire guild — same "sample first, confirm, then build" discipline
already used for the Chapters 1-8 narrative expansion project. Many of
the deeper mechanics described here (Arcane Capacity budgets, artifact
XP/evolution, sentient/conditional enchantments, a server-wide "First
Discovery" broadcast) would be genuinely new systems, not reskins of
`ENCHANT_RECIPES`/`enchant_item`'s existing mechanics — check current
`items.py`/`rules/crafting.py` state before assuming any given piece is
either "already halfway built" or "entirely new."
