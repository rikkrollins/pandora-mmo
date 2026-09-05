"""
rules/labyrinth.py

The Labyrinth (2026-09-01, Phase L1, per Coffee: "start the labyrinth
architecture" -- a live, procedurally-generated, ever-deepening
dungeon unlocked by defeating colosseum_champion, run by bot.py at
request time, never authored offline).

Deliberately NOT rules/dungeon_evolve.py's heavier pipeline: no
build_location_grid/dungeon_audit retry loop (this needs to run in
well under a second, not retry against 7 structural checks meant for
permanent, hand-polishable content). Reuses only dungeon_evolve.
_candidate_monsters, for a wide, non-Remnant, non-headline-boss monster
pool -- depth is unbounded, so unlike evolve_dungeon's finite
next-chapter-band lookup, monster CHOICE here stays catalog-agnostic;
ALL of the difficulty comes from a multiplicative scale bot.py's own
_build_labyrinth_enemy applies to whichever real monster template gets
picked (same stat_mult-on-a-template shape bot.py's _build_echo_enemy
already proves safe), never baked into this module's own room data.

Phase L3 (2026-09-02, per Coffee, live and explicit: "make floors
persistent and interconnected... make each level segments 5 levels
then the waypoints break... generate a new one when characters choose
to go further... 5 levels of honeycombing") REPLACES Phase L1/L2's
single-floor-at-a-time, discard-on-descend model. The old model's own
"no backtracking across floors" was a real, deliberate constraint at
the time -- Coffee's own later ask explicitly reverses it. The new
unit of generation is a SEGMENT: SEGMENT_SIZE (5) real floors,
generated together, genuinely interconnected via the same descends_to/
ascends_to convention the real overworld already uses for multi-story
buildings -- a party can freely walk up and down between any of a
live segment's 5 floors, real backtracking, real cross-floor puzzles
(a switch on one floor can gate a reward on another -- `_SWITCH_STATE`
was already chat-scoped, never room-scoped, so this needs zero new
plumbing beyond generating the rooms that way). Depth still never
caps -- what caps is how much lives in storage at once: reaching a
segment's own checkpoint floor (its 5th, a real safe waypoint: full
heal, full spell-slot refill, an ASI point, a wandering trader's shop,
a real achievement) and choosing to go deeper "breaks the waypoint" --
the whole segment is discarded and a brand new one generated -- so the
game can go infinitely deep while only ever storing one segment's
worth of rooms per active run. Room ids are now floor-namespaced
(`f{floor}_...`) since multiple floors are genuinely alive in storage
at once, unlike the old single-floor model.
"""
import math
import random

from rules import dungeon_audit
from rules.dungeon_evolve import _candidate_monsters

# Real live follow-up (2026-09-05, per Coffee: "reducing repetitive
# filler rooms in favor of fewer, more purposeful rooms"). Direct
# measurement before this change: with the old (3, 5) range plus
# _BRANCH_DEPTH_WEIGHTS' own real ~2.1-extra-room mean per branch, a
# typical floor generated ~12-13 side rooms total -- but each of the 5
# themes above only has 5 real room_names/room_descriptions pairs,
# cycled by plain index (see _new_side_room's own name_index), so a
# single floor routinely repeated every one of those 5 names 2-3 times
# each (Coffee's own literal example: "The Ticking Vault 2/7/12/17").
# Narrowed to fewer BRANCHES specifically, not shorter ones -- v1.27.515
# just widened _BRANCH_DEPTH_WEIGHTS for real "longer interconnectable
# pathways," and shortening branches back down would undo that. Fewer,
# longer branches instead of many short ones: same real corridor feel,
# roughly a third less total rooms (and therefore a third less name
# repetition) per floor.
_SIDE_ROOM_COUNT_RANGE = (2, 4)

# Real Zelda-style branching topology (2026-09-03, per Coffee, dev-
# bridge: 3 real Link's Awakening dungeon maps -- "I want the levels to
# be more explorable, travelable with paths, puzzles, mini bosses...
# make an algorithm to accomplish this"). Confirmed by direct code read
# before this: generate_floor was PURE hub-and-spoke (every side room
# AND the stairs connected straight to the hub) -- a player could walk
# to the stairs in one hop, engaging zero real content, the opposite of
# every real reference map studied (all three showed genuine branching,
# key-gated side areas, and a mini-boss standing between the entrance
# and the exit). Real external research (Metazelda, github.com/tcoxon/
# metazelda, and the academic graph-based lock-and-key literature it's
# cited in) confirms the actual technique: rooms are graph nodes, keys/
# switches are placed in an order that's solvable BY CONSTRUCTION (a
# lock is only ever wired to a switch that already exists in an
# already-reachable, unlocked branch) -- no retry/audit loop needed,
# matching this module's own hard "must run in well under a second"
# requirement. See [[project_zelda_dungeon_algorithm_research]] for the
# full research writeup.
# Real live request (2026-09-05, per Coffee: "make longer
# interconnectable pathways like the samples i gave the other day" --
# following up on the same "Zelda Dungeon Path & Gateway System"
# reference research above). Confirmed by direct code read before this
# change: a branch topped out at 2 extra rooms (3 total, root
# included), so main_chain -- the one guaranteed hub-to-stairs path --
# could never run longer than 3 rooms even at its statistical best.
# Every real reference map Coffee shared shows genuine multi-room
# hallways, not a 1-2-room hop. Widened to 0-4 extra rooms with the
# weight shifted toward real depth (mean ~2.1 extra rooms now, vs.
# ~0.75 before) -- still probabilistic (a few short branches keep
# reading as real dead-ends, matching every reference map's own mix of
# long halls and short side rooms) rather than a fixed length, which
# would make every floor feel identically shaped.
_BRANCH_DEPTH_WEIGHTS = ([0, 1, 2, 3, 4], [0.10, 0.20, 0.25, 0.25, 0.20])
_BRANCH_GATE_CHANCE = 0.35
_MINIBOSS_CHANCE = 0.3

# Real gap found while confirming Bottle Grotto coverage (2026-09-03,
# per Coffee: "are you sure everything is included to make a dungeon
# like the bottle grotto or face shrine or the Eagle's Tower?"):
# pressure plates and breakable walls/floors have had real, generic,
# already-shipped bot.py dispatch since v1.27.450/452 (_do_activate_
# pressure_plate, _do_break_obstacle -- both confirmed CAMPAIGN-
# agnostic, already reused as-is here) but were NEVER actually PLACED
# anywhere in the live game -- not by dungeon_evolve.py's own
# generator, not by hand in campaign.json, and not here. Bottle
# Grotto's own real signature is "grab a nearby pot and step on the
# lift to make it fall" (a pressure plate) plus general ALTTP secret-
# wall/floor puzzles -- this closes that gap for the Labyrinth.
_PRESSURE_PLATE_CHANCE = 0.3
_BREAKABLE_CHANCE = 0.3

# Real live feature (2026-09-04, per Coffee: "you can have locked
# doors requireing 'keys'... keys can be dropped by enemies or found
# in another room" -- Phase B of the combat-gating work). A third,
# independent kind of branch gate alongside the elemental switch and
# pressure plate above, deliberately never the same branch as either
# (see key_gate_eligible's own exclusion at its usage site) -- unlike
# those two, this one is never pickable at all (bot._do_lockpick's
# requires_key_item branch returns unconditionally before it can ever
# fall through to the generic DC13 roll), so the key itself, placed in
# a real, different, already-reachable branch, is the only way through.
_KEY_GATE_CHANCE = 0.3

# Real live feature (2026-09-04, per Coffee: "have the mini boss, and
# boss and scatter them around, have it collected by battle and by
# chests" -- "use it in variations with the other mechanics", not the
# main one). A fourth, independent branch-gate kind, mirroring the key
# gate immediately above but needing a variable count of a fungible
# "labyrinth_rune" item instead of one specific unique key. Lower than
# _KEY_GATE_CHANCE since this is one option among several, not a
# default.
_RUNE_GATE_CHANCE = 0.2

# Real live follow-up (2026-09-03, Coffee: "do all of them" -- warps,
# a floor-altering puzzle, and an owl-statue-style hint, after the same
# 3 Link's Awakening maps). Warps are a real, one-way-declared-but-
# mutually-added shortcut between two rooms that AREN'T already
# adjacent in the branch graph -- stored as `room["warps"]` (a
# separate field from `connections`, since a warp's own map line is
# drawn differently -- a real line straight across the floor, not a
# doorway gap between grid-adjacent cells) rather than pretending it's
# an ordinary lateral connection.
_WARP_CHANCE = 0.25
# Real Eagle's Tower-style structural puzzle: solving it doesn't just
# open one new door, it also SEALS a previously-open one elsewhere on
# the same floor -- "the floor's structure shifts," same spirit as the
# reference's pillar-triggered floor collapse, without needing a
# genuinely separate before/after floor copy.
_COLLAPSE_PUZZLE_CHANCE = 0.2

# Real, more literal Eagle's Tower carry-and-collapse puzzle -- see this
# constant's own usage site in generate_floor for the full research and
# design writeup. Mutually exclusive with _COLLAPSE_PUZZLE_CHANCE above
# (never both on one floor).
_CARRY_PUZZLE_CHANCE = 0.15

# Real "gated encounter" room (2026-09-04, per Coffee: after asking
# whether EVERY room with monsters should block movement onward until
# cleared, and being told that would turn ordinary exploration into a
# forced fight-every-room gauntlet -- "def number 1 [miniboss/boss
# rooms only for the real movement gate] but you can use number two
# [gate-on-any-monster] with the rng and the seeds to create ur
# dungeons and labyrinths with advanced pathways and gating"). Rather
# than a blanket rule, this is the SAME real movement-block mechanic
# (see bot._do_labyrinth_move's own is_miniboss_room/is_boss_room/
# is_gated_encounter check) applied sparingly and deliberately, like
# every other special mechanic here -- an occasional, flagged ordinary
# monster room the party genuinely cannot bypass, narrated honestly so
# it reads as a real design choice, not a random room that happened to
# trap them.
_GATED_ENCOUNTER_CHANCE = 0.3

# Real, more foundational research follow-up (2026-09-03, per Coffee:
# "research dungeons of infinity and all other research to achieve
# what our editor needs" -- see [[project_zelda_dungeon_algorithm_
# research]]'s "Follow-up research" section): both "Cyclic Dungeon
# Generation" and the standard Delaunay-triangulation/MST-plus-added-
# edges technique confirm the same real gap -- v1.27.484's branching
# was a pure TREE (exactly one path to every room), while every real
# reference map (Level 0/6/7) is a denser GRID with genuine loops. Loop-
# back connections close that gap: after the branching tree is fully
# built and grid-placed, add a small number of EXTRA edges between
# rooms that are grid-ADJACENT but not yet connected. Safe by a simple
# invariant -- adding an edge to an already-fully-reachable graph can
# never break reachability, only ever add a redundant path -- so this
# never needs its own solvability check, unlike a gate/switch. Excludes
# the hub/connector (kept exactly as branch-rooted, matching every
# other mechanic's own "never trivialize the main path" discipline) and
# any room that's only reachable through a still-real lock (a branch
# gate) or that a collapse-puzzle can later seal off -- a bare loop-back
# edge into either would silently bypass that gate/trigger forever.
_LOOP_BACK_CHANCE = 0.5
# Real live request (2026-09-05, per Coffee: "make longer
# interconnectable pathways like the samples i gave the other day").
# A flat cap of 3 real loop-back edges made sense when floors were
# small (5 or so rooms, per the OLD _BRANCH_DEPTH_WEIGHTS), but the
# longer branches above roughly triple a floor's real room count --
# the same fixed 3 would read as proportionally sparser, not denser,
# interconnection on a bigger floor. Scales the same way side_room_max
# already does (deeper floors, more headroom), so "interconnectable"
# keeps meaning something as floors grow.
_LOOP_BACK_MAX_EDGES = 5

# L3: one segment = this many real, interconnected floors. Reuses the
# exact number Phase L2c's milestone interval already used, per
# Coffee's own "5 levels of honeycombing" framing -- the OLD milestone
# vault on every 5th floor IS this segment's own checkpoint floor now,
# just upgraded (see _build_checkpoint_room) rather than a separate concept.
SEGMENT_SIZE = 5
MILESTONE_FLOOR_INTERVAL = SEGMENT_SIZE  # kept as an alias -- older code/tests reference this name

# L3, real theme variety (2026-09-02, per Coffee: "we need lots of
# themes and area types or scenarios playable in the labarythn...
# make sure it is something like 'an alternate universe' or 'another
# dimension'... make sure the generator can handle theme and
# descriptions and the rng can handle that"). Deliberately code-driven
# (a small, real, hand-authored pool the RNG picks from and templates
# real room names/descriptions out of), NOT an AI-narrated invention
# per room -- this generator has to run in well under a second, and
# this game's own grounding rule is "never invent lore the AI layer
# doesn't already have real facts for." Per Coffee's explicit
# direction, none of these reuse "Pandora's Box" or any other real
# main-story lore -- the Labyrinth is framed as its own genuine
# fracture between worlds, a real "alternate universe/another
# dimension" anomaly, distinct from the main campaign's own cosmology.
# One theme is chosen per SEGMENT (not per floor) via RNG, so a whole
# 5-floor honeycomb feels like one coherent place rather than five
# unrelated fragments.
LABYRINTH_THEMES = [
    {
        "id": "shattered_mirror",
        "name": "The Shattered Mirror",
        "intro": "The way ahead cracks like glass and reassembles wrong -- everything here is a reflection of somewhere real, just slightly, deliberately, off.",
        "hub_description": "Fractured light bends through the air here, throwing a dozen almost-right versions of the room across every wall.",
        "checkpoint_description": "The cracks stop spreading here -- a single unbroken pane, calm at the center of all this wrongness.",
        "room_names": [
            "A Cracked Reflection", "The Wrong Angle", "A Doubled Hallway", "The Silvered Room", "A Recursive Corner",
            "The Unlit Glass", "A Second Vantage", "The Fracture Line",
        ],
        "room_descriptions": [
            "A tall pane of broken glass stands freestanding in the middle of the floor, showing you a version of this room that isn't quite the one you're standing in.",
            "The corners here don't meet the way corners should -- every angle is a few degrees off from what your eyes expect.",
            "The same stretch of hallway repeats twice, mirrored, with no visible seam where one copy ends and the other begins.",
            "Every wall is silvered like an old mirror's backing, and your own reflection lags a half-second behind your real movements.",
            "The room curls back on itself at the far end, so that walking straight eventually brings you back to where you started.",
            "A sheet of glass leans against the far wall here, dark and unreflective no matter how the light shifts -- it shows nothing back at all.",
            "Two identical vantage points look out over the same stretch of nowhere, a few feet apart, each one certain it's the original.",
            "A jagged crack runs floor to ceiling here, and whatever's on the other side of it doesn't quite line up with this room anymore.",
        ],
        "signature_hazard": "drowning",
        # Real polish found via the hourly self-improvement monitoring
        # pass (2026-09-03): 4 of 5 themes had no secondary_hazard at
        # all -- every hazard roll in that theme produced the exact
        # same type forever, unlike Ashen Verge's own real lava/
        # overheating variety. No new mechanic needed (freezing already
        # has real code/resistance-checking) -- just real, thematically
        # fitting content: shattered ice, frozen reflections.
        "secondary_hazard": "freezing",
    },
    {
        "id": "hollow_between",
        "name": "The Hollow Between",
        "intro": "Sound dies a step behind you as you cross over -- this place sits in the gap between where you were and where you're going, and it doesn't want visitors noticed.",
        "hub_description": "A wide, grey nowhere, lit by no source anyone can point to.",
        "checkpoint_description": "The grey finally holds still here, just long enough to feel like an actual room again.",
        "room_names": [
            "A Room That Shouldn't Fit", "The Quiet Gap", "An Unfinished Space", "The In-Between Landing", "A Forgotten Threshold",
            "The Held Breath", "An Unmapped Pocket", "The Second Silence",
        ],
        "room_descriptions": [
            "The proportions here are wrong in a way you can't quite name -- this room is bigger on the inside than the space it sits in from outside.",
            "A narrow gap of nothing splits the room in two; crossing it takes a step too long, like the distance quietly grows as you walk it.",
            "Half the walls here trail off into flat grey nothing before they ever reach a real corner.",
            "This landing exists between two places that no longer connect to it -- neither one remembers building it.",
            "A threshold stands with no door in it and nothing obvious on either side, like something meant to be walked through was forgotten.",
            "The air here holds still the way a held breath does, like the room itself is waiting to see if you'll notice before it lets go.",
            "This space doesn't appear to connect to anything on either side of it -- just a pocket of somewhere, sealed off from whatever it used to belong to.",
            "Even your own footsteps land without a sound here, twice over -- once for real, once a beat later, from somewhere just behind you.",
        ],
        "signature_hazard": "freezing",
        "secondary_hazard": "acid",
    },
    {
        "id": "clockwork_fold",
        "name": "The Clockwork Fold",
        "intro": "Gears the size of houses turn somewhere out of sight, and the whole place ticks forward around you like it's keeping its own private time.",
        "hub_description": "Brass housing and slow-turning gearwork line every surface, all of it moving to a rhythm no one asked for.",
        "checkpoint_description": "The gears here have wound down to a stop -- whatever this place used to measure, it isn't measuring it anymore.",
        "room_names": [
            "A Gear-Locked Chamber", "The Ticking Vault", "A Stalled Mechanism", "The Brass Landing", "A Wound Spring Room",
            "The Counted Hour", "A Sealed Escapement", "The Overwound Room",
        ],
        "room_descriptions": [
            "A massive interlocking gear fills most of the far wall, its teeth taller than a person, turning so slowly you can't quite tell it's moving until you look away and back.",
            "Rows of brass-plated lockers line this vault, each one ticking faintly, like something inside is still being wound.",
            "A huge mechanism sits frozen mid-motion here, one gear jammed against another -- whatever it was building toward, it never finished.",
            "Pipework and pressure gauges cover every surface of this landing, needles twitching toward numbers that mean nothing to you.",
            "A tightly coiled spring, thick as a tree trunk, strains visibly against its housing in the corner, tensioned and never released.",
            "A single brass clock face dominates the far wall here, hands frozen at an hour that doesn't match anything, still audibly counting underneath.",
            "A small glass-fronted mechanism ticks away behind sealed brass, its gears visible but its purpose long since lost to whoever built it.",
            "Every spring and coil in this room looks tensioned past what it was ever meant to hold, straining toward a release that keeps not coming.",
        ],
        "signature_hazard": "arcing_current",
        "secondary_hazard": "overheating",
    },
    {
        "id": "ashen_verge",
        "name": "The Ashen Verge",
        "intro": "The air goes warm and grey the moment you cross in -- everything here looks like it's already burned, and burns a little more every time you look away.",
        "hub_description": "Ash drifts in from nowhere, settling over floors that were never actually on fire.",
        "checkpoint_description": "The ash doesn't fall here -- the only still, clean air anywhere in this stretch.",
        "room_names": [
            "An Ember-Lit Hollow", "The Smoldering Passage", "A Grey Ash Room", "The Cinder Landing", "An Ashen Threshold",
            "The Banked Hearth", "A Blackened Vault", "The Long Exhale",
        ],
        "room_descriptions": [
            "Embers glow faintly in the cracks of the floor here, throwing just enough orange light to see the shape of the room by.",
            "A narrow passage stretches ahead, smoke curling along the ceiling with nowhere obvious to vent to.",
            "Fine grey ash coats every surface in here, undisturbed until your own footprints cut through it.",
            "Charred beams cross overhead on this landing, blackened but still somehow holding the weight above them.",
            "The threshold here is scorched black on both sides, as if something passed through it burning, more than once.",
            "A wide hearth of blackened stone dominates one wall, banked low but unmistakably still warm to stand near.",
            "Every surface in this vault has been scorched to the same flat black, like whatever burned here did it all at once, then stopped.",
            "A slow breath of hot air moves through this room and never seems to run out, drifting from somewhere deeper in that never shows itself.",
        ],
        "signature_hazard": "lava",
        "secondary_hazard": "overheating",
    },
    {
        "id": "verdant_undoing",
        "name": "The Verdant Undoing",
        "intro": "Roots have already grown through walls that, by every sign, were built after them -- whatever this place is, growth here runs backward.",
        "hub_description": "Vines thick as rope hold up stonework that looks like it should have collapsed centuries ago.",
        "checkpoint_description": "The green here has gone still and orderly, almost tended, almost deliberate.",
        "room_names": [
            "A Root-Bound Chamber", "The Overgrown Landing", "A Reclaimed Hall", "The Living Threshold", "An Unpruned Room",
            "The Backward Grove", "A Split Foundation", "The Quiet Reclaiming",
        ],
        "room_descriptions": [
            "Thick roots have pushed up through the floor here, bracing the ceiling like they grew that way on purpose.",
            "Moss and creeping vine cover this landing so completely that the original stonework barely shows through anymore.",
            "Whatever this hall was built for, the green growing through every seam has clearly made other plans for it.",
            "Living vines form the actual doorframe of this threshold now, thick enough that stone underneath is only a guess.",
            "Nothing in this room has been pruned or tended in what feels like a very long time -- growth here answers to no one.",
            "A cluster of trees grows straight through the middle of this room, roots and all, in a pattern too deliberate to be an accident of nature.",
            "The floor has split clean in two where a root pushed up from beneath, and neither half looks in any hurry to settle back down.",
            "Green has crept over nearly everything in here, slow and patient, like it's simply waiting for the room to finish being a room.",
        ],
        "signature_hazard": "acid",
        "secondary_hazard": "drowning",
    },
]


def _pick_theme(rng: random.Random) -> dict:
    return rng.choice(LABYRINTH_THEMES)


# L3, real in-fiction framing for WHY the Labyrinth keeps generating
# new alternate-dimension segments (2026-09-02, per Coffee: "the
# labyrinth generator can use a[n] AI to make themes and storylines
# for the levels... but don't use characters that are in the
# storyline, make sure they are separate from the actual game"). A
# brand-new, Labyrinth-exclusive entity -- never a real campaign.json
# NPC/boss, never referenced by any main-story quest -- so it can
# never be confused with (or spoil) the actual game's own antagonists.
# Purely narrative/flavor (bot.py's narrate_labyrinth_segment_flavor,
# a real but OPTIONAL, fire-and-forget Ollama call fired once per new
# segment, never blocking the deterministic entry message) -- not a
# mechanical boss fight.
LABYRINTH_ANTAGONIST_NAME = "the Errant Cartographer"
LABYRINTH_ANTAGONIST_LORE = (
    "Something down here keeps redrawing the map, badly, endlessly -- each new stretch a fresh, "
    "malfunctioning attempt at a whole world, never quite finished before the next one starts."
)

# L2b. Floor modifiers -- a real, announced, floor-wide condition, never
# silent (Coffee's "state it if it's visible" discipline, same as the
# overworld's pits/breakables). 60% of floors have none at all. Now
# stored PER ROOM (every room on a given floor carries the same
# "modifier" value) rather than once per run, since a live segment
# holds 5 real floors at once and each rolls its own independently.
FLOOR_MODIFIERS = ("frenzied", "lightless", "bountiful", "dangerous")
_MODIFIER_CHANCE = 0.4
MODIFIER_ANNOUNCEMENT = {
    "frenzied": "The air here feels frenzied -- whatever's alive moves faster than it should.",
    "lightless": "No natural light reaches this deep. You'll need your own.",
    "bountiful": "Something about this floor feels generous.",
    "dangerous": "This floor feels wrong -- dangerous in a way the last one didn't.",
}

_MILESTONE_ITEM_BANDS = (
    (1, 10, "greater_healing_potion"),
    (11, 25, "superior_healing_potion"),
    (26, 1_000_000, "scroll_magic_missile"),
)

# L2d. Environmental hazards -- a visible, telegraphed obstacle, same
# "state it honestly" rule as the overworld's own pits/breakables.
_HAZARD_CHANCE = 0.15
HAZARD_KINDS = (
    "collapsing_floor", "gas_vent", "spike_pit", "bottomless_pit",
    "acid", "freezing", "overheating", "arcing_current", "lava", "drowning",
)
HAZARD_FLAVOR = {
    "collapsing_floor": "The floor here looks half-rotted through -- one wrong step and it's a long way down.",
    "gas_vent": "A faint hiss comes from a crack in the wall -- something in the air here isn't right.",
    "spike_pit": "A row of corroded spikes juts up through cracks in the floor -- landing on those wrong would knock anyone flat.",
    "bottomless_pit": "The floor gives way to a real pit here, deep enough that a dropped stone never seems to land.",
    # L3, real theme-signature hazards (2026-09-02, per Coffee: "themes
    # shud have hazards for them, lava, acid, drowning..., freezing...,
    # overheating..."). Each one honestly telegraphed, same discipline
    # as every other hazard here -- see HAZARD_DAMAGE_TYPE/
    # _LETHAL_HAZARD_KINDS below for exactly how each resolves.
    "acid": "A film of corrosive sap coats everything within reach, hissing faintly wherever it touches bare stone.",
    "freezing": "The cold here isn't ordinary cold -- it settles into the joints, deeper with every breath.",
    "overheating": "The heat here climbs steadily, well past anything a real fire alone would explain.",
    "arcing_current": "Current jumps between exposed brass fittings, arcing wherever something metal gets too close.",
    "lava": "The floor gives way to real, slow-moving lava here -- bright, silent, and very obviously final.",
    "drowning": "A still, black pool fills the low half of this room -- deep enough, the way it sits, that going under here doesn't feel like it would end the normal way.",
}
# Elemental hazards check the SAME real resistance/vulnerability system
# combat damage already uses (rules.combat.apply_damage_type_modifier)
# -- a character with real fire resistance shrugs off "overheating"
# far more than one without, never a flat, un-mitigatable number.
HAZARD_DAMAGE_TYPE = {
    "acid": "acid", "freezing": "cold", "overheating": "fire", "arcing_current": "lightning", "lava": "fire",
}
# L3 (2026-09-02, per Coffee: "add pits with holes we can plunge to our
# death, and spikes we can land on, if we fall or jump on them, KO").
# Real, distinct severity tiers, both still a single honest DC13 DEX
# save like every other hazard here -- never a silent gotcha:
# - spike_pit: a failed save is a real KO (hp_current -> 0, the exact
#   same "downed, unconscious, recoverable by healing/rest" state
#   combat already uses) -- never permanent on its own.
# - bottomless_pit: a failed save runs the party member through the
#   EXACT same real death-save sequence (rules.combat.resolve_death_
#   save) a downed combatant already faces turn-by-turn in combat,
#   just resolved all at once since there's no combat round to spread
#   it across -- genuinely real odds of dying (is_dead=1, revivable
#   only by the same Revivify path any other real death already uses),
#   not an unavoidable instant kill, and small but real odds of
#   catching a ledge and walking away basically fine (a natural 20).
#   Rare, and only ever appears from floor _BOTTOMLESS_PIT_MIN_FLOOR
#   onward -- brand-new Labyrinth-goers on floor 1 don't instantly meet
#   a real permadeath trap.
_MUNDANE_HAZARD_KINDS = ("collapsing_floor", "gas_vent")
_LETHAL_HAZARD_KINDS = ("spike_pit", "bottomless_pit")
_THEME_LETHAL_HAZARDS = ("lava", "drowning")  # theme-specific -- see HAZARD_DAMAGE_TYPE/_new_side_room
_BOTTOMLESS_PIT_MIN_FLOOR = 10
_LETHAL_HAZARD_CHANCE = 0.05

# L3, theme-signature hazards (2026-09-02, per Coffee: "themes shud
# have hazards for them"). A SEPARATE, independent roll from the
# generic hazard pool above -- checked first, per room, using the
# segment's own theme.  "lava" resolves through the SAME real
# death-save path bottomless_pit/drowning use, but ONLY for a
# character with no real fire resistance/immunity (rules.combat.
# apply_damage_type_modifier's own resistance set) -- a fire-resistant
# character instead just takes real, reduced fire damage and walks
# away, exactly matching Coffee's own "freezing... without frost or
# ice resistences, over heating... with out fire resistences" framing
# (the hazard checks the SAME real resistance system combat already
# has, never a new one invented for this).
_THEME_HAZARD_CHANCE = 0.12

# L2e. Multi-switch puzzle -- a second, independent switch-gated reward
# room on the SAME floor, requiring EVERY switch active at once
# (bot.py's own `multi_switch_gate` kind, shared with the overworld
# evolve pass).
_MULTI_SWITCH_CHANCE = 0.25
_SWITCH_ELEMENTS = ("fire", "cold", "lightning", "force", "radiant", "psychic", "poison", "necrotic", "earth", "physical")

# L2f. Mirror pairs -- a real, Labyrinth-scoped reading of ALTTP's
# Light/Dark World idea: two rooms on the SAME floor, same connection
# shape, deliberately inverted contents.
_MIRROR_PAIR_CHANCE = 0.2

# L3: a real CROSS-floor puzzle -- per Coffee: "intertwined with
# puzzles in all levels... lets players work with their party to get
# to the next level." Two switches, each on a DIFFERENT one of the
# segment's first 4 floors, jointly gate one bonus room on the
# checkpoint (5th) floor. Uses the exact same multi_switch_gate/
# _SWITCH_STATE plumbing L2e already proved chat-scoped, not
# room-scoped -- a switch flipped on floor 2 is visible to a gate on
# floor 5 with no new mechanism at all.
_CROSS_FLOOR_PUZZLE_CHANCE = 0.6

# L3: "each level in the honeycomb being harder and each RNG being
# harder" (Coffee, live) -- raw monster stats already strictly
# increase with floor via bot.py's labyrinth_depth_multiplier, but the
# GENERATION odds themselves stay flat at floor 1's values forever
# without this: a floor 80 side room shouldn't roll hazards/monster
# counts/modifiers at the exact same rate floor 1 does. Every rate
# below grows linearly with floor and caps well short of certainty, so
# early floors keep their original, already-tuned feel.
def _scaled_chance(base: float, floor: int, per_floor: float, cap: float) -> float:
    return min(base + per_floor * (floor - 1), cap)


def _depth_weighted_modifier(floor: int, rng: random.Random) -> str | None:
    if rng.random() >= _scaled_chance(_MODIFIER_CHANCE, floor, 0.004, 0.75):
        return None
    # Weights shift from flat (floor 1) toward "dangerous"/"frenzied"
    # as depth grows -- deeper floors are more often the harsher
    # modifiers, never exclusively (still real variety at any depth).
    lean = min(0.01 * (floor - 1), 3.0)
    weights = [1.0 + lean, 1.0, 1.0, 1.0 + lean]  # frenzied, lightless, bountiful, dangerous
    return rng.choices(FLOOR_MODIFIERS, weights=weights, k=1)[0]


def _labyrinth_monster_pool(campaign: dict) -> list[str]:
    """Wide, catalog-agnostic pool -- floor depth is unbounded, so monster CHOICE never depends on a level band (that's what bot.py's depth-multiplier is for); only real exclusions (Remnants, level-less headline bosses) apply, reusing dungeon_evolve's own filtering with the widest possible band."""
    return _candidate_monsters(campaign, (0, 10_000), boss=False)


def _labyrinth_boss_pool(campaign: dict) -> list[str]:
    """
    Real, reusable, leveled bosses only (same _candidate_monsters(boss=
    True) exclusion dungeon_evolve.py's own boss branches already rely
    on -- the level-less headline story climaxes never show up here,
    same as they never show up as evolved-dungeon bosses). Level itself
    doesn't gate eligibility, same reasoning as _labyrinth_monster_pool
    above: bot._build_labyrinth_enemy's own depth multiplier is what
    actually scales the fight, not the template's level field.
    """
    return _candidate_monsters(campaign, (0, 10_000), boss=True)


def segment_number_for_floor(floor: int) -> int:
    return (floor - 1) // SEGMENT_SIZE + 1


def segment_start_floor(segment: int) -> int:
    return (segment - 1) * SEGMENT_SIZE + 1


def is_checkpoint_floor(floor: int) -> bool:
    """The last floor of its segment -- the real safe waypoint (see _build_checkpoint_room)."""
    return floor % SEGMENT_SIZE == 0


def _hub_id(floor: int) -> str:
    return f"f{floor}_hub"


def _stairs_id(floor: int) -> str:
    return f"f{floor}_stairs"


def _checkpoint_id(floor: int) -> str:
    return f"f{floor}_checkpoint"


def _spiral_cells():
    """
    Yields (0, 0), then every integer cell at Chebyshev distance 1, then
    distance 2, and so on, each ring ordered by angle -- a simple,
    deterministic, collision-free way to place an arbitrary number of
    rooms around a hub with no risk of ever running out of cells. Each
    floor gets its OWN independent spiral (its own hub at its own
    local (0, 0)) -- floors are real, separately-drawn maps (see
    map_render.render_labyrinth_map's floor switcher), not one shared
    coordinate space.
    """
    yield (0, 0)
    radius = 1
    while True:
        ring = [
            (x, y)
            for x in range(-radius, radius + 1)
            for y in range(-radius, radius + 1)
            if max(abs(x), abs(y)) == radius
        ]
        ring.sort(key=lambda c: math.atan2(c[1], c[0]))
        yield from ring
        radius += 1


def _assign_grid_positions(rooms: dict, hub_room_id: str) -> None:
    """
    Hub at the origin; every other room placed by a real BFS walk over
    its ACTUAL connections/locked_connections graph, one cardinal step
    (N/S/E/W) from its real parent whenever a free cell is available.

    Real fix (2026-09-03, alongside the branching-topology algorithm
    above): the old version placed rooms in a blind Chebyshev spiral,
    completely ignorant of which rooms actually connect to which --
    fine for the OLD pure hub-and-spoke shape (every room was hub-
    adjacent anyway, so the exact cell barely mattered), but wrong now
    that real branches run several rooms deep. A child room could land
    anywhere on the spiral regardless of its real parent, so map_render.
    render_labyrinth_map's new per-edge door/wall drawing (a gap where
    two rooms are ACTUALLY connected) would show doors leading nowhere
    sensible. BFS placement keeps a branch visually contiguous, the way
    every real reference map studied actually reads.
    """
    rooms[hub_room_id]["grid_position"] = {"x": 0, "y": 0}
    occupied = {(0, 0): hub_room_id}
    visited = {hub_room_id}
    queue = [hub_room_id]
    directions = [(0, 1), (0, -1), (1, 0), (-1, 0)]
    while queue:
        current_id = queue.pop(0)
        cx, cy = rooms[current_id]["grid_position"]["x"], rooms[current_id]["grid_position"]["y"]
        neighbor_ids = list(rooms[current_id].get("connections", [])) + list(rooms[current_id].get("locked_connections", {}).keys())
        for neighbor_id in neighbor_ids:
            if neighbor_id in visited or neighbor_id not in rooms:
                continue
            placed = False
            for dx, dy in directions:
                if (cx + dx, cy + dy) not in occupied:
                    rooms[neighbor_id]["grid_position"] = {"x": cx + dx, "y": cy + dy}
                    occupied[(cx + dx, cy + dy)] = neighbor_id
                    placed = True
                    break
            if not placed:
                # Every cardinal neighbor of the real parent is already
                # taken (a tight loop back near the hub) -- fall back to
                # the nearest free cell outward from the parent, same
                # spiral search the old placement used globally, just
                # centered on this specific room instead of the origin.
                for dx, dy in _spiral_cells():
                    if (cx + dx, cy + dy) not in occupied:
                        rooms[neighbor_id]["grid_position"] = {"x": cx + dx, "y": cy + dy}
                        occupied[(cx + dx, cy + dy)] = neighbor_id
                        break
            visited.add(neighbor_id)
            queue.append(neighbor_id)

    # Defensive: any room genuinely unreachable from the hub via a real
    # connection (should never happen by construction, but a floor with
    # a real bug elsewhere must still render instead of crashing) gets
    # placed on the outer spiral rather than silently missing a
    # grid_position (map_render.render_labyrinth_map would KeyError).
    remaining = [rid for rid in rooms if rid not in visited]
    if remaining:
        spiral = _spiral_cells()
        for x, y in spiral:
            if not remaining:
                break
            if (x, y) not in occupied:
                rid = remaining.pop(0)
                rooms[rid]["grid_position"] = {"x": x, "y": y}
                occupied[(x, y)] = rid


def _add_loop_back_connections(rooms: dict, hub_id: str, connector_id: str, rng: random.Random, floor: int) -> None:
    """
    Real, more foundational research follow-up (2026-09-03) -- see
    `_LOOP_BACK_CHANCE`'s own module-level docstring for the full
    research this closes: densifies the pure branching TREE
    `_assign_grid_positions` just placed into a real GRID with a few
    genuine loops, matching every reference map studied. Must run
    AFTER grid positions are assigned (it needs real {x, y} adjacency)
    and adds at most `_LOOP_BACK_MAX_EDGES` real bidirectional
    `connections` entries between rooms that are grid-adjacent but not
    yet linked -- safe by construction (every room here is already
    reachable; a new edge can only add a redundant path, never remove
    one), so this never needs its own solvability check.

    Excludes the hub and connector (never trivialize the main path,
    same discipline warps already follow) and any room that's only
    reachable through a real branch gate, or that a collapse-puzzle
    trigger can later seal off -- a bare loop-back edge into either
    would silently let a player bypass that gate/trigger forever.
    """
    excluded = {hub_id, connector_id}
    for room in rooms.values():
        excluded.update(room.get("collapsing_connections", {}).keys())

    gated_all = {dest for room in rooms.values() for dest in room.get("locked_connections", {})}
    frontier = list(gated_all)
    while frontier:
        cur = frontier.pop()
        for nb in rooms[cur].get("connections", []):
            if nb not in gated_all:
                gated_all.add(nb)
                frontier.append(nb)
    excluded |= gated_all

    by_cell = {(r["grid_position"]["x"], r["grid_position"]["y"]): rid for rid, r in rooms.items()}
    candidates = []
    seen_pairs = set()
    for rid, room in rooms.items():
        if rid in excluded:
            continue
        x, y = room["grid_position"]["x"], room["grid_position"]["y"]
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            neighbor_id = by_cell.get((x + dx, y + dy))
            if neighbor_id is None or neighbor_id in excluded:
                continue
            if neighbor_id in room.get("connections", []) or neighbor_id in room.get("warps", []):
                continue
            pair = frozenset((rid, neighbor_id))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            candidates.append((rid, neighbor_id))

    rng.shuffle(candidates)
    added = 0
    chance = _scaled_chance(_LOOP_BACK_CHANCE, floor, 0.05, 0.9)
    max_edges = _LOOP_BACK_MAX_EDGES + min(floor // 10, 5)  # same depth-scaled-headroom shape as side_room_max above
    for a, b in candidates:
        if added >= max_edges:
            break
        if rng.random() < chance:
            rooms[a].setdefault("connections", []).append(b)
            rooms[b].setdefault("connections", []).append(a)
            added += 1


def _milestone_item_for_floor(floor: int) -> str:
    for lo, hi, item_id in _MILESTONE_ITEM_BANDS:
        if lo <= floor <= hi:
            return item_id
    return _MILESTONE_ITEM_BANDS[-1][2]


def _new_side_room(floor: int, hub_id: str, index: int, pool: list[str], modifier: str | None, rng: random.Random, theme: dict) -> dict:
    room_id = f"f{floor}_r{index}"
    monster_cap = min(2 + floor // 10, 5)  # depth-scaled RNG: floor 1-9 rolls 0-2, floor 40+ rolls 0-5
    monsters = rng.sample(pool, k=min(rng.randint(0, monster_cap), len(pool))) if pool else []
    # Real theme variety (2026-09-02, per Coffee): the room's own name
    # and base description come from the segment's chosen theme's own
    # real pool, cycled deterministically by index -- always suffixed
    # with a real room number so names stay genuinely unique even once
    # a deep floor's side-room count exceeds the theme's own name pool
    # (real bug precedent: two identically-named mirror-pair rooms once
    # made a typed destination genuinely ambiguous -- never repeat that).
    # room_descriptions is the SAME LENGTH as room_names and indexed
    # identically (2026-09-02, real live report, Coffee: descriptions
    # weren't "good enough" -- root cause was every side room in a theme
    # sharing one single flavor sentence regardless of its own distinct
    # name; each name now has its own matching, distinct description).
    name_index = index % len(theme["room_names"])
    room = {
        "id": room_id, "floor": floor, "name": f"{theme['room_names'][name_index]} {index + 1}",
        "description": theme["room_descriptions"][name_index],
        "connections": [hub_id], "monsters": monsters, "modifier": modifier,
    }
    # L2d/L3: a visible hazard, honestly stated in the description
    # itself. Checked in a fixed priority order, mutually exclusive
    # (one hazard per room keeps the room text simple and keeps any
    # single lethal trap always genuinely rare):
    # 1. the segment's own theme-signature hazard (lava/acid/freezing/
    #    overheating/arcing_current/drowning) -- real per-theme flavor.
    # 2. the generic lethal pool (spike_pit/bottomless_pit).
    # 3. the generic mundane pool (collapsing_floor/gas_vent).
    theme_hazard = theme.get("signature_hazard") if rng.random() < 0.6 else theme.get("secondary_hazard", theme.get("signature_hazard"))
    if theme_hazard and rng.random() < _scaled_chance(_THEME_HAZARD_CHANCE, floor, 0.0015, 0.35):
        if theme_hazard != "drowning" or floor >= _BOTTOMLESS_PIT_MIN_FLOOR:
            room["hazard"] = theme_hazard
            room["description"] += " " + HAZARD_FLAVOR[theme_hazard]
    if "hazard" not in room and rng.random() < _scaled_chance(_LETHAL_HAZARD_CHANCE, floor, 0.001, 0.18):
        lethal_pool = [k for k in _LETHAL_HAZARD_KINDS if k != "bottomless_pit" or floor >= _BOTTOMLESS_PIT_MIN_FLOOR]
        hazard = rng.choice(lethal_pool)
        room["hazard"] = hazard
        room["description"] += " " + HAZARD_FLAVOR[hazard]
    elif "hazard" not in room and rng.random() < _scaled_chance(_HAZARD_CHANCE, floor, 0.003, 0.45):
        hazard = rng.choice(_MUNDANE_HAZARD_KINDS)
        room["hazard"] = hazard
        room["description"] += " " + HAZARD_FLAVOR[hazard]
    # Real chest, doubled loot/gold under a "bountiful" floor. Grants a
    # Greater Healing Potion, not a plain one (2026-09-05, per Coffee,
    # dev-bridge: "Instead of healing potions, can you give greater
    # healing potions?") -- every ordinary Labyrinth/dungeon chest
    # below was still handing out the base tier regardless of how deep
    # a real party has already fought to reach it.
    if rng.random() < 0.3:
        bountiful = modifier == "bountiful"
        loot = {"greater_healing_potion": rng.randint(2, 4) if bountiful else rng.randint(1, 2)}
        dungeon_audit.maybe_add_spell_tonic_to_loot(loot, rng)
        room.setdefault("lockables", []).append({
            "id": f"f{floor}_cache_{index}", "kind": "chest", "name": "a real, hastily-buried cache",
            "loot": loot,
            "gold": (rng.randint(20, 80) * floor) * (2 if bountiful else 1),
        })
    return room, room_id


def _build_checkpoint_room(floor: int, hub_id: str, rng: random.Random, theme: dict) -> dict:
    """
    L3: the segment's own real safe waypoint (Coffee: "make this a safe
    spot with a waypoint to fast travel to later on"). bot.py's
    _resolve_labyrinth_checkpoint fires the actual one-time rewards
    (full heal, full spell-slot refill, an ASI point, an achievement)
    the first time a party arrives; this room's own data just marks it
    `is_checkpoint` and gives it a real wandering-trader shop (the
    exact same reusable NPC/shop dungeon_evolve.py's own Part B4
    already authored for large overworld dungeons).
    """
    item_id = _milestone_item_for_floor(floor)
    return {
        "id": _checkpoint_id(floor), "floor": floor, "is_checkpoint": True,
        "name": f"{theme['name']} -- A Waystation (Floor {floor})",
        "description": theme["checkpoint_description"],
        "connections": [hub_id], "monsters": [],
        "npcs": ["wandering_dungeon_trader"], "shop": "wandering_traders_pack",
        "lockables": [{
            "id": f"f{floor}_checkpoint_cache", "kind": "chest", "name": "a real, heavily reinforced vault",
            "loot": {item_id: 1}, "gold": rng.randint(100, 300) * floor,
        }],
    }


def generate_floor(campaign: dict, floor: int, rng: random.Random, pool: list[str] | None = None, theme: dict | None = None) -> dict:
    """
    Builds ONE floor's own rooms (hub + a stairs-or-checkpoint room +
    side rooms + this floor's own single-floor extras: L2c's vault no
    longer lives here -- see _build_checkpoint_room -- but L2e's
    same-floor multi-switch and L2f's mirror pair still do). Returns
    {"rooms": {...}, "hub_room_id": ..., "connector_room_id": ...,
    "modifier": ..., "is_checkpoint": bool}. `connector_room_id` is a
    plain "Stair Down" room for a non-checkpoint floor, or the real
    checkpoint room for the segment's 5th floor -- generate_segment
    below is what actually links `connector_room_id` onward to the
    next floor (or, for a checkpoint, leaves it for bot.py's own
    "go deeper" action to break the segment and build the next one).

    `theme` (2026-09-02, Phase L3, per Coffee: "lots of themes and area
    types... an alternate universe/another dimension"): one of
    LABYRINTH_THEMES, normally chosen ONCE per segment by generate_
    segment below and passed down to every one of its floors so a
    whole 5-floor honeycomb reads as one coherent place. Defaults to a
    real random pick only for direct/standalone calls (e.g. tests) --
    generate_segment never relies on this default itself.
    """
    theme = theme or _pick_theme(rng)
    pool = pool if pool is not None else _labyrinth_monster_pool(campaign)
    rooms: dict[str, dict] = {}
    modifier = _depth_weighted_modifier(floor, rng)
    checkpoint = is_checkpoint_floor(floor)

    hub_id = _hub_id(floor)
    hub = {
        "id": hub_id, "floor": floor, "name": f"{theme['name']} -- Floor {floor}",
        "description": theme["hub_description"],
        "connections": [], "monsters": [], "modifier": modifier,
    }
    rooms[hub_id] = hub

    # Real branching (2026-09-03, see _BRANCH_DEPTH_WEIGHTS' own module-
    # level docstring for the full research this replaces): each branch
    # ROOT still connects straight to the hub (unchanged shape, so the
    # existing L2e multi-switch/L2f mirror-pair features below -- which
    # both key off `room_ids`, the flat hub-adjacent set -- keep working
    # exactly as before), but a branch can now run 0-2 rooms DEEPER, a
    # real path rather than a single dead-end hop.
    side_room_max = _SIDE_ROOM_COUNT_RANGE[1] + min(floor // 20, 2)  # depth-scaled RNG: more chambers per floor, deeper in -- gentler growth now that the base range itself is smaller (2026-09-05 room-count reduction)
    num_branch_roots = rng.randint(_SIDE_ROOM_COUNT_RANGE[0], side_room_max)
    room_ids = []
    branch_chains = []
    next_index = 0
    for _ in range(num_branch_roots):
        room, room_id = _new_side_room(floor, hub_id, next_index, pool, modifier, rng, theme)
        next_index += 1
        hub["connections"].append(room_id)
        rooms[room_id] = room
        room_ids.append(room_id)
        chain = [room_id]
        depth = rng.choices(*_BRANCH_DEPTH_WEIGHTS)[0]
        parent_id = room_id
        for _ in range(depth):
            # _new_side_room's own 2nd positional param is only ever used
            # to set "connections": [that_id] -- passing the real parent
            # (not hub_id) is what actually builds a genuine deeper path.
            deep_room, deep_room_id = _new_side_room(floor, parent_id, next_index, pool, modifier, rng, theme)
            next_index += 1
            rooms[parent_id]["connections"].append(deep_room_id)
            rooms[deep_room_id] = deep_room
            chain.append(deep_room_id)
            parent_id = deep_room_id
        branch_chains.append(chain)

    # The main path: the single longest branch leads to the real stairs
    # (Metazelda/every reference map's own shape -- real content sits
    # BETWEEN the entrance and the exit, never a direct hop). Ties break
    # on the first-generated branch, so the same seed always reproduces
    # the same layout.
    main_chain = max(branch_chains, key=len)
    main_path_end = main_chain[-1]

    if checkpoint:
        connector = _build_checkpoint_room(floor, main_path_end, rng, theme)
    else:
        connector = {
            "id": _stairs_id(floor), "floor": floor, "name": "A Stair Down",
            "description": "A real stairway, always open -- however far you've come, the way deeper never asks permission.",
            "connections": [main_path_end], "monsters": [], "modifier": modifier,
        }
    rooms[main_path_end]["connections"].append(connector["id"])
    rooms[connector["id"]] = connector

    # A real end-of-segment BOSS (2026-09-05, per Coffee: "we should
    # have...something that we would deem a boss, something
    # significantly larger, this is the goal" -- the miniboss below was
    # never that; it's just the toughest ordinary trash monster on the
    # floor, with real bosses explicitly excluded from `pool` entirely.
    # Guaranteed (not chance-rolled) on every checkpoint floor -- the
    # segment's own natural "this is what you were working toward"
    # beat -- placed on `main_path_end`, the room right before the
    # checkpoint/stairs connector, so reaching the checkpoint genuinely
    # means beating it first (same is_boss_room movement-gate bot.py's
    # map-render/_is_gated_combat_room already honor for the miniboss
    # case, now finally set by a real generator for once). Draws from
    # _labyrinth_boss_pool -- real, reusable, non-headline bosses only,
    # same exclusion dungeon_evolve.py's own evolved-dungeon bosses
    # rely on -- and needs bot._build_labyrinth_enemy's own is_boss/
    # signature-flag copying fix (same date) to actually fight like one.
    boss_pool = _labyrinth_boss_pool(campaign) if checkpoint else []
    if boss_pool:
        boss_key = rng.choice(boss_pool)
        rooms[main_path_end]["monsters"] = [boss_key]
        rooms[main_path_end]["is_boss_room"] = True
        rooms[main_path_end]["description"] += " Something with real weight is waiting here -- the true end of this stretch of the Labyrinth."
    else:
        # A real mini-boss guarding the way to the stairs -- always on
        # the MAIN path, never a side branch, so reaching the exit
        # means facing it (every reference map studied placed at least
        # one mini-boss partway along the real critical path, not
        # tucked in an optional room). Still a real catalog monster
        # from this floor's own pool, just alone in its room and
        # flagged for narration/map-icon use -- never an invented
        # enemy. Only rolled on non-checkpoint floors now that
        # checkpoint floors always get the real boss above instead.
        miniboss_candidates = [rid for rid in main_chain if pool]
        if miniboss_candidates and rng.random() < _scaled_chance(_MINIBOSS_CHANCE, floor, 0.002, 0.6):
            miniboss_room_id = rng.choice(miniboss_candidates)
            strongest = max(pool, key=lambda mk: (campaign["monsters"].get(mk) or {}).get("hp_max", 0))
            rooms[miniboss_room_id]["monsters"] = [strongest]
            rooms[miniboss_room_id]["is_miniboss_room"] = True
            rooms[miniboss_room_id]["description"] += " Something far stronger than the rest of this floor is waiting here."

    # Real branch-gating, guaranteed solvable BY CONSTRUCTION (Metazelda's
    # own real technique, see the module-level research note above): a
    # non-main branch can be sealed behind a switch placed in a
    # DIFFERENT, already-generated, unlocked branch. Reuses the exact
    # `multi_switch_gate` mechanism L2e already ships (a single required
    # switch here, not two) rather than a bare `switch` referenced
    # directly by `locked_connections` -- confirmed by direct code read
    # of bot.py's own `_lockable_is_open` that a bare switch is only
    # ever checked against the SAME room's own `lockables`, so a truly
    # remote single-switch gate would silently never open; multi_switch_
    # gate's own `requires` list is the one real mechanism that checks
    # `_SWITCH_STATE` globally by id regardless of which room the switch
    # itself lives in, and it's already proven safe by L2e/generate_
    # segment's own cross-floor puzzle.
    other_chains = [c for c in branch_chains if c is not main_chain]

    # Real MANDATORY main-path gate (2026-09-04, per Coffee, dev-bridge:
    # "we should not be able to walk from one floor to the next we
    # should be having to clear the paths and the different gates...
    # It should feel like a maze or a labyrinth" -- backed by a real
    # "Zelda Dungeon Path & Gateway System" reference he shared: the
    # player's own critical path should run through real gateways, not
    # just optional side rooms). Every OTHER gate below only ever
    # targets `other_chains` -- main_chain (the one guaranteed path to
    # the stairs) was structurally NEVER gated at all, so a floor could
    # roll zero of the optional gates below and read as a bare
    # corridor, with any switch that DID spawn only ever wired to an
    # optional detour, never the path actually being walked. This is
    # unconditional (no chance roll) -- randomly picked from the SAME 4
    # real mechanics every optional gate below already uses (per
    # Coffee: "random from the full existing set" -- no new mechanic,
    # just a guaranteed application of what's already built).
    # _SIDE_ROOM_COUNT_RANGE's own floor of 2 branch roots (2026-09-05,
    # reduced for "fewer, more purposeful rooms") guarantees `other_
    # chains` has >= 1 entry in practice, not always >= 2 anymore --
    # every mechanic below that draws from `other_chains` already
    # degrades gracefully with just one (a single switch/key source, or
    # rune's own `min(..., len(other_chains))` clamp), and the handful
    # that explicitly require `len(other_chains) >= 2` (branch-gating,
    # for one) simply roll less often now, which is the intended
    # tradeoff of fewer branches per floor. The `if other_chains else
    # "plate"` fallback still covers the theoretical zero-side-branch
    # case, since pressure_plate is the one mechanic that's entirely
    # self-contained in the hub.
    mandatory_kind = rng.choice(("switch", "plate", "key", "rune")) if other_chains else "plate"
    mandatory_gate_room_id = main_chain[0]
    mandatory_source_chains: list = []
    if mandatory_kind == "switch":
        mandatory_source_chains = [rng.choice(other_chains)]
        switch_room_id = mandatory_source_chains[0][0]
        element = rng.choice(_SWITCH_ELEMENTS)
        switch_id = f"f{floor}_mainswitch"
        rooms[switch_room_id].setdefault("lockables", []).append({
            "id": switch_id, "kind": "switch", "name": f"a {element} crystal", "element": element,
        })
        hub["connections"].remove(mandatory_gate_room_id)
        hub.setdefault("locked_connections", {})[mandatory_gate_room_id] = f"f{floor}_maingate"
        hub.setdefault("lockables", []).append({
            "id": f"f{floor}_maingate", "kind": "multi_switch_gate", "name": "a real sealed archway",
            "requires": [switch_id],
        })
        rooms[mandatory_gate_room_id]["description"] += " A real elemental ward seals the only way onward -- something elsewhere on this floor must open it."
    elif mandatory_kind == "plate":
        plate_id = f"f{floor}_mainplate"
        hub.setdefault("lockables", []).append({"id": plate_id, "kind": "pressure_plate", "name": "a stone pressure plate"})
        hub.setdefault("movable_objects", []).append({"id": f"f{floor}_maincrate", "name": "a heavy crate"})
        hub["connections"].remove(mandatory_gate_room_id)
        hub.setdefault("locked_connections", {})[mandatory_gate_room_id] = plate_id
        rooms[mandatory_gate_room_id]["description"] += " A weight-locked mechanism seals the only way onward -- something heavy, placed just right, would hold it open."
    elif mandatory_kind == "key":
        mandatory_source_chains = [rng.choice(other_chains)]
        key_room_id = mandatory_source_chains[0][0]
        door_id = f"f{floor}_maingate"
        hub["connections"].remove(mandatory_gate_room_id)
        hub.setdefault("locked_connections", {})[mandatory_gate_room_id] = door_id
        # Real live bug found and fixed (2026-09-05, exposed by an RNG-
        # stream shift from an unrelated change elsewhere in this same
        # function): this mandatory gate and the SEPARATE optional key
        # gate below used to share the exact literal name "a real,
        # heavily-barred door" -- harmless while they could never both
        # exist on the same floor, but a genuine, real _find_lockable
        # ambiguity ("which one?") the moment both actually fire
        # together, which they always could in principle (independent
        # rolls, no mutual exclusion). Named distinctly now -- this one
        # is always the single guaranteed main-path gate.
        hub.setdefault("lockables", []).append({
            "id": door_id, "kind": "door", "name": "a real, heavily-barred main door",
            "requires_key_item": "labyrinth_floor_key", "consume_key": True,
        })
        rooms[mandatory_gate_room_id]["description"] += " A real, heavily-barred main door seals the only way onward -- it looks like it needs an actual key, not brute force or a steady hand."
        if rng.random() < 0.5 and rooms[key_room_id].get("monsters"):
            rooms[key_room_id]["guaranteed_key_drop"] = True
            rooms[key_room_id]["description"] += " Something here looks like it might be carrying something worth taking."
        else:
            rooms[key_room_id].setdefault("lockables", []).append({
                "id": f"f{floor}_maingate_key_cache", "kind": "chest", "name": "a small, dusty lockbox",
                "loot": {"labyrinth_floor_key": 1}, "gold": rng.randint(20, 60),
            })
    else:  # "rune"
        rune_count = min(rng.randint(2, 4), len(other_chains))
        mandatory_source_chains = rng.sample(other_chains, rune_count)
        door_id = f"f{floor}_maingate"
        hub["connections"].remove(mandatory_gate_room_id)
        hub.setdefault("locked_connections", {})[mandatory_gate_room_id] = door_id
        hub.setdefault("lockables", []).append({
            "id": door_id, "kind": "door", "name": "a locked rune doorway",
            "requires_rune_item": "labyrinth_rune", "rune_count": rune_count,
        })
        rooms[mandatory_gate_room_id]["description"] += f" A locked rune doorway seals the only way onward -- it looks like it needs {rune_count} real Labyrinth Runes, not brute force or a steady hand."
        for i, src_chain in enumerate(mandatory_source_chains):
            rune_room_id = src_chain[0]
            if rng.random() < 0.5 and rooms[rune_room_id].get("monsters"):
                rooms[rune_room_id]["guaranteed_rune_drop"] = True
                rooms[rune_room_id]["description"] += " Something here looks like it might be carrying something worth taking."
            else:
                rooms[rune_room_id].setdefault("lockables", []).append({
                    "id": f"f{floor}_maingate_rune_cache_{i}", "kind": "chest", "name": "a small, rune-etched cache",
                    "loot": {"labyrinth_rune": 1}, "gold": rng.randint(20, 60),
                })
    room_ids = [
        rid for rid in room_ids
        if rid != mandatory_gate_room_id and rid not in {c[0] for c in mandatory_source_chains}
    ]

    gated_chain = None
    if len(other_chains) >= 2 and rng.random() < _scaled_chance(_BRANCH_GATE_CHANCE, floor, 0.002, 0.55):
        _branch_gate_eligible = [c for c in other_chains if c not in mandatory_source_chains]
        if len(_branch_gate_eligible) < 2:
            _branch_gate_eligible = other_chains
        gated_chain, switch_chain = rng.sample(_branch_gate_eligible, 2)
        gate_room_id = gated_chain[0]
        switch_room_id = switch_chain[0]
        element = rng.choice(_SWITCH_ELEMENTS)
        switch_id = f"f{floor}_branchswitch"
        rooms[switch_room_id].setdefault("lockables", []).append({
            "id": switch_id, "kind": "switch", "name": f"a {element} crystal", "element": element,
        })
        hub["connections"].remove(gate_room_id)
        hub.setdefault("locked_connections", {})[gate_room_id] = f"f{floor}_branchgate"
        hub.setdefault("lockables", []).append({
            "id": f"f{floor}_branchgate", "kind": "multi_switch_gate", "name": "a real sealed archway",
            "requires": [switch_id],
        })
        rooms[gate_room_id]["description"] += " A real elemental ward seals this path -- something elsewhere on this floor must open it."
        # L2e/L2f below both pick freely from `room_ids` -- excluding the
        # room this pass just locked keeps them from compounding an
        # unrelated puzzle onto a room the party can't reach yet.
        room_ids = [rid for rid in room_ids if rid != gate_room_id]

    # Real pressure-plate gate (2026-09-03, per Coffee: "look at the
    # dead ends... a grid style map with multiple paths" -- confirmed
    # via follow-up research to be Bottle Grotto's own real signature
    # mechanic: "grab a nearby pot and step on the lift to make it
    # fall"). A second, independent kind of branch gate -- deliberately
    # never the same branch as the elemental-switch gate above, so both
    # can coexist or neither can. Mirrors rules/dungeon_evolve.py's own
    # identical mechanic for the overworld generator exactly: the plate
    # AND its movable object live in the SAME room as the gate itself
    # (the hub) -- NOT a remote puzzle. Confirmed by direct code read of
    # `_lockable_is_open`: a bare `pressure_plate`/`switch` kind is only
    # ever looked up against the SAME room passed in, so placing it
    # remotely (the way a `switch` gets wrapped in `multi_switch_gate`
    # to work around that) would silently never open -- dungeon_evolve.
    # py's own version already gets this right by keeping everything
    # local, reused here as-is.
    plate_eligible = [c for c in other_chains if c is not gated_chain and c not in mandatory_source_chains]
    plated_chain = None
    if plate_eligible and rng.random() < _scaled_chance(_PRESSURE_PLATE_CHANCE, floor, 0.002, 0.5):
        plated_chain = rng.choice(plate_eligible)
        plate_room_id = plated_chain[0]
        plate_id = f"f{floor}_plate"
        crate_id = f"f{floor}_crate"
        hub.setdefault("lockables", []).append(
            {"id": plate_id, "kind": "pressure_plate", "name": "a stone pressure plate"}
        )
        hub.setdefault("movable_objects", []).append(
            {"id": crate_id, "name": "a heavy crate"}
        )
        hub["connections"].remove(plate_room_id)
        hub.setdefault("locked_connections", {})[plate_room_id] = plate_id
        rooms[plate_room_id]["description"] += " A weight-locked mechanism seals this path -- something heavy, placed just right, would hold it open."
        room_ids = [rid for rid in room_ids if rid != plate_room_id]

    # Real key-gated door (2026-09-04, per Coffee: "you can have locked
    # doors requireing 'keys'... keys can be dropped by enemies or
    # found in another room" -- Phase B of the combat-gating work). A
    # third, independent kind of branch gate -- never the same branch
    # as the switch or plate gate above. Unlike those two, a key-gated
    # door is NEVER pickable at all (bot._do_lockpick's requires_key_
    # item branch returns unconditionally before the generic DC13 roll
    # further down that function ever runs) -- the key, always placed
    # in a real, DIFFERENT branch than the one it unlocks (guaranteed
    # solvable by construction, same discipline as every other gate
    # here), is the only way through.
    key_gate_eligible = [c for c in other_chains if c is not gated_chain and c is not plated_chain and c not in mandatory_source_chains]
    key_gated_chain = None
    if key_gate_eligible and rng.random() < _scaled_chance(_KEY_GATE_CHANCE, floor, 0.002, 0.5):
        key_gated_chain = rng.choice(key_gate_eligible)
        key_gate_room_id = key_gated_chain[0]
        # Real bug found via the end-to-end key-gate test (2026-09-04):
        # this must also exclude gated_chain/plated_chain -- otherwise
        # the key can land in a branch that's ITSELF already sealed
        # behind the unrelated elemental-switch or pressure-plate gate
        # above, silently requiring an unrelated puzzle be solved first
        # to reach a "guaranteed" find. Same discipline plate_eligible/
        # key_gate_eligible already apply to their own candidate lists.
        key_source_candidates = [c for c in other_chains if c is not key_gated_chain and c is not gated_chain and c is not plated_chain and c not in mandatory_source_chains]
        if key_source_candidates:
            key_source_chain = rng.choice(key_source_candidates)
            key_room_id = key_source_chain[0]
            door_id = f"f{floor}_key_gate"
            hub["connections"].remove(key_gate_room_id)
            hub.setdefault("locked_connections", {})[key_gate_room_id] = door_id
            # Named distinctly from the mandatory main-path gate above
            # (2026-09-05 fix) -- this is always the optional, side-
            # branch key gate, never the guaranteed one.
            hub.setdefault("lockables", []).append({
                "id": door_id, "kind": "door", "name": "a real, heavily-barred side door",
                "requires_key_item": "labyrinth_floor_key", "consume_key": True,
            })
            rooms[key_gate_room_id]["description"] += " A real, heavily-barred side door seals this path -- it looks like it needs an actual key, not brute force or a steady hand."
            # Coffee's own "dropped by enemies OR found in another
            # room" -- chosen randomly each time real content allows
            # it, always a guaranteed real find either way (never a
            # coin-flip that could leave the key genuinely absent).
            if rng.random() < 0.5 and rooms[key_room_id].get("monsters"):
                rooms[key_room_id]["guaranteed_key_drop"] = True
                rooms[key_room_id]["description"] += " Something here looks like it might be carrying something worth taking."
            else:
                rooms[key_room_id].setdefault("lockables", []).append({
                    "id": f"f{floor}_key_cache", "kind": "chest", "name": "a small, dusty lockbox",
                    "loot": {"labyrinth_floor_key": 1}, "gold": rng.randint(20, 60),
                })
            # Real bug found testing this fix: the mirror-pair mechanic
            # further down picks freely from `room_ids` and OVERWRITES
            # whichever room it calls chest_room_id's own monsters/
            # lockables outright -- if that happened to land on this
            # same key room, it would silently wipe the guaranteed
            # drop's own monsters (or the real key chest just placed)
            # after the fact. Same "don't compound an unrelated puzzle
            # onto a room already used" exclusion key_gate_room_id
            # already gets, just for the room hosting the key itself.
            room_ids = [rid for rid in room_ids if rid != key_gate_room_id and rid != key_room_id]

    # Real Locked Rune Doorway (2026-09-04, per Coffee: "have the mini
    # boss, and boss and scatter them around, have it collected by
    # battle and by chests" -- "use it in variations with the other
    # mechanics", explicitly not the main one). A fourth, independent
    # branch gate -- never a branch already claimed by the switch,
    # plate, or key gate above. Unlike the key (one unique item), this
    # needs a variable COUNT of the fungible "labyrinth_rune" item --
    # "if a gateway needs 3 the player will need 3 runes" (Coffee's own
    # words) -- so the guaranteed find is scattered across that many
    # separate sources instead of just one.
    rune_gate_eligible = [c for c in other_chains if c is not gated_chain and c is not plated_chain and c is not key_gated_chain and c not in mandatory_source_chains]
    if rune_gate_eligible and rng.random() < _scaled_chance(_RUNE_GATE_CHANCE, floor, 0.002, 0.5):
        rune_gated_chain = rng.choice(rune_gate_eligible)
        rune_gate_room_id = rune_gated_chain[0]
        rune_source_candidates = [c for c in other_chains if c is not rune_gated_chain and c is not gated_chain and c is not plated_chain and c is not key_gated_chain and c not in mandatory_source_chains]
        if rune_source_candidates:
            rune_count = min(rng.randint(2, 4), len(rune_source_candidates))
            rune_source_chains = rng.sample(rune_source_candidates, rune_count)
            door_id = f"f{floor}_rune_gate"
            hub["connections"].remove(rune_gate_room_id)
            hub.setdefault("locked_connections", {})[rune_gate_room_id] = door_id
            hub.setdefault("lockables", []).append({
                "id": door_id, "kind": "door", "name": "a locked rune doorway",
                "requires_rune_item": "labyrinth_rune", "rune_count": rune_count,
            })
            rooms[rune_gate_room_id]["description"] += f" A locked rune doorway seals this path -- it looks like it needs {rune_count} real Labyrinth Runes, not brute force or a steady hand."
            rune_room_ids = [rune_source_chain[0] for rune_source_chain in rune_source_chains]
            for i, rune_room_id in enumerate(rune_room_ids):
                # Same "dropped by enemies OR found in another room"
                # 50/50 split the key gate uses, one guaranteed real
                # rune per chosen source room.
                if rng.random() < 0.5 and rooms[rune_room_id].get("monsters"):
                    rooms[rune_room_id]["guaranteed_rune_drop"] = True
                    rooms[rune_room_id]["description"] += " Something here looks like it might be carrying something worth taking."
                else:
                    rooms[rune_room_id].setdefault("lockables", []).append({
                        "id": f"f{floor}_rune_cache_{i}", "kind": "chest", "name": "a small, rune-etched cache",
                        "loot": {"labyrinth_rune": 1}, "gold": rng.randint(20, 60),
                    })
            # Same exclusion discipline as the key gate just above --
            # keep later mechanics (mirror-pair, collapse puzzle) from
            # compounding an unrelated puzzle onto any room this gate
            # already claimed.
            room_ids = [rid for rid in room_ids if rid != rune_gate_room_id and rid not in rune_room_ids]

    # Real breakable-wall/floor secret (2026-09-03, per Coffee's same
    # research request): a genuine ALTTP/Bottle-Grotto-style optional
    # secret off an already-reachable branch LEAF -- never gates the
    # critical path, purely adds a bonus room, so it needs no
    # solvability check at all. Reuses bot.py's already-generic,
    # already-shipped `_do_break_obstacle` dispatch (a plain hit always
    # works; a fire/force spell also works) -- this had real dispatch
    # code since v1.27.450 but was never actually PLACED anywhere in
    # the live game (not by dungeon_evolve.py, not by hand in campaign.
    # json) until now.
    breakable_leaf_candidates = [c[-1] for c in branch_chains if c is not main_chain]
    if breakable_leaf_candidates and rng.random() < _scaled_chance(_BREAKABLE_CHANCE, floor, 0.002, 0.5):
        leaf_id = rng.choice(breakable_leaf_candidates)
        kind = rng.choice(("breakable_wall", "breakable_floor"))
        noun = "wall" if kind == "breakable_wall" else "floor"
        bonus_id = f"f{floor}_secret"
        lockable_id = f"f{floor}_breakable"
        secret_loot = {"greater_healing_potion": rng.randint(1, 2)}
        dungeon_audit.maybe_add_spell_tonic_to_loot(secret_loot, rng)
        rooms[bonus_id] = {
            "id": bonus_id, "floor": floor, "name": "Labyrinth -- A Hidden Cache", "monsters": [],
            "description": f"A room that shouldn't be here, sealed off until the {noun} that hid it finally gave way.",
            "connections": [leaf_id], "modifier": modifier,
            "lockables": [{
                "id": f"f{floor}_secret_cache", "kind": "chest", "name": "a real cache no one else has found",
                "loot": secret_loot, "gold": rng.randint(80, 200) * floor,
            }],
        }
        rooms[leaf_id].setdefault("locked_connections", {})[bonus_id] = lockable_id
        rooms[leaf_id].setdefault("lockables", []).append({
            "id": lockable_id, "kind": kind,
            "name": f"a real, visibly cracked {noun}",
        })
        rooms[leaf_id]["description"] += f" A real crack splits the {noun} here -- something about it looks ready to give way."

    # L2e: a second, independent switch-gated reward, requiring EVERY
    # switch active at once -- deliberately placed in two side rooms
    # that already exist, never the hub/connector.
    if room_ids and len(room_ids) >= 2 and rng.random() < _scaled_chance(_MULTI_SWITCH_CHANCE, floor, 0.003, 0.6):
        switch_room_ids = rng.sample(room_ids, 2)
        switch_lockable_ids = []
        for j, rid in enumerate(switch_room_ids):
            element = rng.choice(_SWITCH_ELEMENTS)
            switch_id = f"f{floor}_switch_{j}"
            rooms[rid].setdefault("lockables", []).append({
                "id": switch_id, "kind": "switch", "name": f"a {element} crystal", "element": element,
            })
            switch_lockable_ids.append(switch_id)
        gate_room_id = f"f{floor}_gate_reward"
        gate_loot = {"greater_healing_potion": rng.randint(1, 2)}
        dungeon_audit.maybe_add_spell_tonic_to_loot(gate_loot, rng)
        gate_room = {
            "id": gate_room_id, "floor": floor, "name": "Labyrinth -- A Sealed Archway",
            "description": "The archway won't budge -- whatever opens it isn't here in this room.",
            "connections": [hub_id], "monsters": [], "modifier": modifier,
            "lockables": [{
                "id": f"f{floor}_gate_cache", "kind": "chest", "name": "a real cache behind the archway",
                "loot": gate_loot, "gold": rng.randint(60, 150) * floor,
            }],
        }
        rooms[gate_room_id] = gate_room
        hub.setdefault("locked_connections", {})[gate_room_id] = f"f{floor}_multi_gate"
        hub.setdefault("lockables", []).append({
            "id": f"f{floor}_multi_gate", "kind": "multi_switch_gate", "name": "a real sealed archway",
            "requires": switch_lockable_ids,
        })

    # L2f: a real mirror pair -- same connection shape (both are plain
    # hub-adjacent side rooms already), deliberately inverted contents.
    if pool and len(room_ids) >= 2 and rng.random() < _MIRROR_PAIR_CHANCE:
        monster_room_id, chest_room_id = rng.sample(room_ids, 2)
        strongest = max(pool, key=lambda mk: (campaign["monsters"].get(mk) or {}).get("hp_max", 0))
        # Numbered so both stay thematically identical but are always
        # individually nameable -- two rooms sharing the exact same
        # name made a typed "go to A Mirrored Chamber" genuinely
        # ambiguous (real bug, found live 2026-09-02).
        monster_room = rooms[monster_room_id]
        monster_room["monsters"] = [strongest]
        monster_room["mirror_twin"] = chest_room_id
        monster_room["name"] = "Labyrinth -- A Mirrored Chamber (I)"
        monster_room["description"] = "This room feels like it's happening twice, somewhere else on this same floor."
        chest_room = rooms[chest_room_id]
        chest_room["monsters"] = []
        chest_room["mirror_twin"] = monster_room_id
        chest_room["name"] = "Labyrinth -- A Mirrored Chamber (II)"
        chest_room["description"] = "This room feels like it's happening twice, somewhere else on this same floor."
        mirror_loot = {"greater_healing_potion": rng.randint(1, 2)}
        dungeon_audit.maybe_add_spell_tonic_to_loot(mirror_loot, rng)
        chest_room["lockables"] = [{
            "id": f"f{floor}_mirror_cache", "kind": "chest", "name": "an unguarded, matching cache",
            "loot": mirror_loot, "gold": rng.randint(60, 150) * floor,
        }]

    # Real warp shortcut (2026-09-03, per Coffee: "do all of them" --
    # warps, a real Zelda reference mechanic, after Level 6/Level 7's
    # own maps both showed one linking two distant rooms directly).
    # Picked from two DIFFERENT branches' own deepest room (the most
    # "interesting" real content to shortcut between) -- never the hub
    # or connector, so a warp is always a genuine bonus link, never a
    # way to skip the real critical path outright. Stored separately
    # from `connections` (a warp's own map line is drawn differently --
    # straight across the floor, not a doorway between grid-adjacent
    # cells) rather than pretending it's an ordinary lateral exit.
    #
    # Real live bug found and fixed (2026-09-04, Coffee, dev-bridge: "I
    # don't understand what the point of this warp was because we were
    # already able to access this room. The point of warps are to bring
    # us to an area of the dungeon that we weren't able to get to
    # before"): `len(c) >= 1` let a warp connect two branch ROOTS --
    # each only ONE hop from the hub -- meaning the "shortcut" (1 hop)
    # was never shorter than the real path it replaced (hub -> A, 1 hop,
    # or hub -> B, 1 hop; A and B are each already just 2 hops apart via
    # the hub). Requiring real depth (>= 2, i.e. root + at least one
    # deeper room) on BOTH ends guarantees hub->leaf is itself >= 2 hops,
    # so warping between two such leaves always saves real distance
    # (>= 4 hops the long way vs. 1 hop through the warp).
    warp_candidates = [c[-1] for c in branch_chains if c is not main_chain and len(c) >= 2]
    warp_endpoint_ids: set = set()
    if len(warp_candidates) >= 2 and rng.random() < _scaled_chance(_WARP_CHANCE, floor, 0.002, 0.5):
        warp_a, warp_b = rng.sample(warp_candidates, 2)
        rooms[warp_a].setdefault("warps", []).append(warp_b)
        rooms[warp_b].setdefault("warps", []).append(warp_a)
        rooms[warp_a]["description"] += " A warp shimmers faintly in the corner -- it leads somewhere else on this floor."
        rooms[warp_b]["description"] += " A warp shimmers faintly in the corner -- it leads somewhere else on this floor."
        warp_endpoint_ids = {warp_a, warp_b}

    # Real Eagle's Tower-style structural puzzle (2026-09-03): solving
    # it doesn't just open one new door, it ALSO seals a previously-open
    # dead-end branch elsewhere on the floor -- "the floor's structure
    # shifts beneath you." Only ever targets a genuine LEAF of a non-
    # main, non-gated branch (never anything hosting a switch another
    # gate depends on), so this can never break the real solvable-by-
    # construction guarantee the rest of this generator relies on.
    # Real bug found via scripts/preview_labyrinth_floor.py's own first
    # real use (2026-09-03): a branch already gated by the pressure-
    # plate mechanic above (removed from hub["connections"] entirely)
    # could still get PICKED as a seal target here, producing a real,
    # generated `collapsing_connections` entry that's permanently
    # inert -- the room's actual reachability is already fully decided
    # by its own `locked_connections` gate, so this trigger would never
    # visibly do anything. `c[0] in hub["connections"]` requires the
    # branch root to still be a genuine, ungated, plain hub connection.
    sealable_candidates = [
        c for c in branch_chains
        if c is not main_chain and len(c) >= 1 and c[0] in hub["connections"]
        and not any(lk.get("kind") == "switch" for lk in rooms[c[0]].get("lockables", []))
    ]
    if len(sealable_candidates) >= 1 and other_chains and rng.random() < _scaled_chance(_COLLAPSE_PUZZLE_CHANCE, floor, 0.001, 0.4):
        seal_chain = rng.choice(sealable_candidates)
        seal_leaf = seal_chain[-1]
        seal_parent = hub_id if len(seal_chain) == 1 else seal_chain[-2]
        # Switches for THIS trigger live in a real, unrelated, already-
        # reachable branch -- never inside the branch being sealed.
        switch_source_chains = [c for c in other_chains if c is not seal_chain]
        if switch_source_chains:
            switch_room_id = rng.choice(switch_source_chains)[0]
            element = rng.choice(_SWITCH_ELEMENTS)
            trigger_switch_id = f"f{floor}_collapse_switch"
            rooms[switch_room_id].setdefault("lockables", []).append({
                "id": trigger_switch_id, "kind": "switch", "name": f"a shuddering {element} crystal", "element": element,
            })
            trigger_id = f"f{floor}_collapse_trigger"
            rooms[seal_parent].setdefault("lockables", []).append({
                "id": trigger_id, "kind": "multi_switch_gate", "name": "a real structural trigger",
                "requires": [trigger_switch_id],
            })
            rooms[seal_parent].setdefault("collapsing_connections", {})[seal_leaf] = trigger_id
            rooms[seal_parent]["description"] += " Something here feels structurally unstable -- like it's rigged to give way."
            # The real reward: a genuine new shortcut opens elsewhere on
            # the floor the instant this same trigger fires, so solving
            # it is a real trade, not just a loss.
            # Excludes real warp endpoints too (2026-09-05, real
            # regression found and fixed while shipping "longer
            # interconnectable pathways"): an echo shortcut landing on
            # the SAME tail a warp already connects to silently
            # shortens that warp's own real distance-saved guarantee
            # (already computed and relied on above, before this
            # puzzle's own placement runs).
            shortcut_pair = [c[-1] for c in other_chains if c is not seal_chain and c[-1] not in warp_endpoint_ids]
            if len(shortcut_pair) >= 2:
                shortcut_a, shortcut_b = rng.sample(shortcut_pair, 2)
                rooms[shortcut_a].setdefault("locked_connections", {})[shortcut_b] = trigger_id
                rooms[shortcut_a].setdefault("lockables", []).append({
                    "id": f"{trigger_id}_echo_{shortcut_a}", "kind": "multi_switch_gate",
                    "name": "a newly-opened shortcut", "requires": [trigger_switch_id],
                })
                # locked_connections looks up its OWN lockable id, not the
                # shared trigger's -- point it at the per-room echo just
                # added so _lockable_is_open finds it locally, same real
                # constraint the branch-gate fix above already worked
                # around once.
                rooms[shortcut_a]["locked_connections"][shortcut_b] = f"{trigger_id}_echo_{shortcut_a}"

    # Real Eagle's Tower-style carry-and-collapse puzzle (2026-09-03,
    # per Coffee's own live screenshot re-ask, comparing his actual
    # in-game floor to a reference map: "multiple levels floors and
    # basements to get to the other end of the dungeon" -- confirmed
    # via real research (zeldadungeon.net's own level-design writeup)
    # to be Eagle's Tower's literal mechanic: carry a heavy object
    # between rooms, strike 2+ real pillars scattered across the floor
    # with it (one trip at a time -- the object is dropped/consumed the
    # instant it strikes a pillar, forcing a genuine return trip for
    # the second), and the floor's structure shifts once every pillar
    # is struck. Reuses the exact same `collapsing_connections`
    # mechanism the simpler single-switch collapse puzzle above uses --
    # mutually exclusive with it (never both on the same floor, keeps
    # the puzzle count sane), same "solvable by construction"
    # discipline (the pillars are never inside the branch being sealed,
    # never a dependency any OTHER gate relies on -- multi_switch_
    # gate's own `requires` list is checked globally by id, same proven
    # mechanism L2e/the collapse puzzle already use).
    already_has_collapse = any(r.get("collapsing_connections") for r in rooms.values())
    # Same real fix as sealable_candidates above -- a branch already
    # gated by the pressure-plate mechanic (removed from hub[
    # "connections"]) must never be picked as a seal target here either.
    carry_sealable = [
        c for c in branch_chains
        if c is not main_chain and len(c) >= 1 and c[0] in hub["connections"]
        and not any(lk.get("kind") == "switch" for lk in rooms[c[0]].get("lockables", []))
    ]
    if not already_has_collapse and len(carry_sealable) >= 1 and rng.random() < _scaled_chance(_CARRY_PUZZLE_CHANCE, floor, 0.001, 0.35):
        seal_chain = rng.choice(carry_sealable)
        seal_leaf = seal_chain[-1]
        seal_parent = hub_id if len(seal_chain) == 1 else seal_chain[-2]
        pillar_source_chains = [c for c in other_chains if c is not seal_chain]
        if len(pillar_source_chains) >= 2:
            pillar_chain_a, pillar_chain_b = rng.sample(pillar_source_chains, 2)
            puzzle_id = f"f{floor}_carry"
            carry_id = f"{puzzle_id}_object"
            pillar_ids = [f"{puzzle_id}_pillar_0", f"{puzzle_id}_pillar_1"]
            # The real, carriable object always starts in the hub --
            # found immediately, same as Eagle's Tower's own wrecking
            # ball -- so it never depends on a branch a player hasn't
            # reached yet.
            hub.setdefault("lockables", []).append({
                "id": carry_id, "kind": "carry_object", "name": "a real, half-buried stone weight", "puzzle_id": puzzle_id,
            })
            rooms[pillar_chain_a[0]].setdefault("lockables", []).append({
                "id": pillar_ids[0], "kind": "pillar", "name": "a real weathered support pillar", "puzzle_id": puzzle_id,
            })
            rooms[pillar_chain_b[0]].setdefault("lockables", []).append({
                "id": pillar_ids[1], "kind": "pillar", "name": "a real weathered support pillar", "puzzle_id": puzzle_id,
            })
            rooms[pillar_chain_a[0]]["description"] += " A real, weathered stone pillar stands here, cracked with age."
            rooms[pillar_chain_b[0]]["description"] += " A real, weathered stone pillar stands here, cracked with age."
            trigger_id = f"{puzzle_id}_trigger"
            rooms[seal_parent].setdefault("lockables", []).append({
                "id": trigger_id, "kind": "multi_switch_gate", "name": "a real structural trigger",
                "requires": pillar_ids,
            })
            rooms[seal_parent].setdefault("collapsing_connections", {})[seal_leaf] = trigger_id
            rooms[seal_parent]["description"] += " Something here feels structurally unstable -- like a well-placed blow, twice over, could bring it down."
            # Excludes real warp endpoints too (2026-09-05, real
            # regression found and fixed while shipping "longer
            # interconnectable pathways"): an echo shortcut landing on
            # the SAME tail a warp already connects to silently
            # shortens that warp's own real distance-saved guarantee
            # (already computed and relied on above, before this
            # puzzle's own placement runs).
            shortcut_pair = [c[-1] for c in other_chains if c is not seal_chain and c[-1] not in warp_endpoint_ids]
            if len(shortcut_pair) >= 2:
                shortcut_a, shortcut_b = rng.sample(shortcut_pair, 2)
                rooms[shortcut_a].setdefault("locked_connections", {})[shortcut_b] = trigger_id
                rooms[shortcut_a].setdefault("lockables", []).append({
                    "id": f"{trigger_id}_echo_{shortcut_a}", "kind": "multi_switch_gate",
                    "name": "a newly-opened shortcut", "requires": pillar_ids,
                })
                rooms[shortcut_a]["locked_connections"][shortcut_b] = f"{trigger_id}_echo_{shortcut_a}"

    # Real gated encounter (2026-09-04) -- see _GATED_ENCOUNTER_CHANCE's
    # own comment for the full reasoning. Never the hub (the hub's own
    # exits already have plenty of other gates competing for it), never
    # a room already flagged is_miniboss_room (that one already gates
    # movement on its own, no need to double up).
    gated_encounter_candidates = [
        r for r in rooms.values() if r.get("monsters") and r is not hub and not r.get("is_miniboss_room")
    ]
    if gated_encounter_candidates and rng.random() < _scaled_chance(_GATED_ENCOUNTER_CHANCE, floor, 0.002, 0.6):
        gated_room = rng.choice(gated_encounter_candidates)
        gated_room["is_gated_encounter"] = True
        gated_room["description"] += " Something about this space feels sealed -- like the way beyond won't truly open until whatever's here is dealt with."

    # Real owl-statue-style hint (2026-09-03, Link's Awakening research
    # -- an optional, non-spoiler ambient warning before committing to
    # a room). Strictly honest and vague, matching this project's own
    # hard `feedback_never_spoil_puzzle_answers` rule: real facts about
    # what's somewhere on this floor, never a room name or exact
    # position. Only placed when there's something real to hint at.
    hint_lines = []
    if any(r.get("is_miniboss_room") for r in rooms.values()):
        hint_lines.append("Something stronger than the rest of this floor waits along the way down.")
    if any(r.get("locked_connections") for r in rooms.values()):
        hint_lines.append("A passage further in stays sealed until something elsewhere on this floor is answered.")
    if any(r.get("collapsing_connections") for r in rooms.values()):
        hint_lines.append("A passage further in feels unstable, like a well-placed blow could change its shape.")
    if any(r.get("warps") for r in rooms.values()):
        hint_lines.append("A shimmer somewhere on this floor doesn't belong here -- it leads somewhere else entirely.")
    if any(r.get("is_gated_encounter") for r in rooms.values()):
        hint_lines.append("Somewhere here, a real fight is standing between you and the rest of this floor.")
    if hint_lines:
        hub.setdefault("lockables", []).append({
            "id": f"f{floor}_hint_statue", "kind": "hint_statue", "name": "a worn statue, one eye missing",
            "hint_lines": hint_lines,
        })

    # Real shortcut levers (2026-09-05, per Coffee: "make longer
    # interconnectable pathways like the samples i gave the other day").
    # _add_loop_back_connections just below only ever connects rooms
    # that happen to land grid-ADJACENT after placement -- with branches
    # radiating outward from the hub in mostly-separate directions, that
    # rarely fires in practice (confirmed empirically: under 1 real loop
    # edge per floor on average even with today's widened chance/cap).
    # A deliberate shortcut lever is the same real, proven mechanic
    # dungeon_evolve.py's own evolved dungeons already use for exactly
    # this ("the genuine Zelda beat: pull the lever at the FAR end of
    # the branch and gain a quick way straight back to the hub -- not
    # the hub reaching into the branch") -- ported here so a branch that
    # got genuinely LONG under the widened _BRANCH_DEPTH_WEIGHTS above
    # also gets real interconnection, not just extra length. Kind
    # "lever" always auto-succeeds with no roll (_do_lockpick), and the
    # lockable + locked_connections entry live ONLY on the tail room
    # pointing back at the hub -- same one-way-findable shape
    # add_lever_shortcut already establishes, so it can never be
    # triggered from the hub side and never bypasses anything (the
    # branch was already freely reachable; this only adds a faster way
    # back out of it).
    #
    # Excludes any chain whose tail is a real warp endpoint (2026-09-05,
    # real regression found and fixed while shipping this): a warp's
    # own "genuine shortcut" guarantee is that its two ends are
    # otherwise far apart (see _WARP_CHANCE's own live-bug history
    # above) -- a lever placed on that SAME tail would shorten the real
    # distance back to the hub to just 1 hop, silently undermining a
    # guarantee the warp placement above already computed and relied
    # on before this lever mechanic existed.
    long_chains = [c for c in other_chains if len(c) >= 3 and c[-1] not in warp_endpoint_ids]
    for i, chain in enumerate(long_chains):
        if rng.random() < _scaled_chance(_LOOP_BACK_CHANCE, floor, 0.05, 0.9):
            tail_id = chain[-1]
            lever_id = f"f{floor}_shortcut_lever_{i}"
            rooms[tail_id].setdefault("lockables", []).append({
                "id": lever_id, "kind": "lever", "name": "a weathered lever",
            })
            rooms[tail_id].setdefault("locked_connections", {})[hub_id] = lever_id

    _assign_grid_positions(rooms, hub_id)
    _add_loop_back_connections(rooms, hub_id, connector["id"], rng, floor)
    return {
        "rooms": rooms, "hub_room_id": hub_id, "connector_room_id": connector["id"],
        "modifier": modifier, "is_checkpoint": checkpoint,
    }


def generate_segment(campaign: dict, segment: int, rng: random.Random) -> dict:
    """
    L3: builds SEGMENT_SIZE (5) real, interconnected floors at once --
    the actual unit of persistence (db.labyrinth_runs' rooms_json holds
    every room from every floor of the CURRENT live segment, all at
    once, discarded together only when the party breaks the waypoint
    at the checkpoint floor and moves on). Floor k's connector room
    (a plain stairs room, floors 1-4) `descends_to` floor k+1's hub,
    and floor k+1's hub `ascends_to` back to it -- real, walkable,
    reusing the exact same field names the overworld's own multi-story
    buildings already use, so bot.py's movement code barely has to
    change to support it.

    Returns {"rooms": {...every room, every floor...}, "entry_room_id":
    <floor start's hub>, "checkpoint_room_id": <floor end's checkpoint>,
    "segment": segment, "theme": <the one LABYRINTH_THEMES dict chosen
    for every floor in this segment>}.
    """
    pool = _labyrinth_monster_pool(campaign)
    start_floor = segment_start_floor(segment)
    floors = list(range(start_floor, start_floor + SEGMENT_SIZE))
    theme = _pick_theme(rng)

    all_rooms: dict[str, dict] = {}
    floor_data_by_floor: dict[int, dict] = {}
    for floor in floors:
        floor_data = generate_floor(campaign, floor, rng, pool=pool, theme=theme)
        all_rooms.update(floor_data["rooms"])
        floor_data_by_floor[floor] = floor_data

    for floor in floors[:-1]:
        this_connector = all_rooms[floor_data_by_floor[floor]["connector_room_id"]]
        next_hub = all_rooms[floor_data_by_floor[floor + 1]["hub_room_id"]]
        this_connector["descends_to"] = next_hub["id"]
        next_hub["ascends_to"] = this_connector["id"]

    # L3: a real cross-floor puzzle -- two switches, each on a
    # DIFFERENT one of this segment's first 4 floors, jointly gate one
    # bonus room on the checkpoint floor. Reuses the exact same
    # multi_switch_gate kind/plumbing L2e already proved chat-scoped.
    if len(floors) >= 3 and rng.random() < _CROSS_FLOOR_PUZZLE_CHANCE:
        source_floors = rng.sample(floors[:-1], 2)
        switch_lockable_ids = []
        for j, floor in enumerate(source_floors):
            side_room_ids = [
                rid for rid, room in floor_data_by_floor[floor]["rooms"].items()
                if room is not all_rooms[floor_data_by_floor[floor]["hub_room_id"]]
                and room["id"] != floor_data_by_floor[floor]["connector_room_id"]
            ]
            if not side_room_ids:
                continue
            target_room = all_rooms[rng.choice(side_room_ids)]
            element = rng.choice(_SWITCH_ELEMENTS)
            switch_id = f"seg{segment}_switch_{j}"
            target_room.setdefault("lockables", []).append({
                "id": switch_id, "kind": "switch", "name": f"a distant {element} crystal, humming faintly", "element": element,
            })
            switch_lockable_ids.append(switch_id)
        if len(switch_lockable_ids) == 2:
            checkpoint_hub = all_rooms[floor_data_by_floor[floors[-1]]["hub_room_id"]]
            gate_room_id = f"seg{segment}_gate_reward"
            gate_room = {
                "id": gate_room_id, "floor": floors[-1], "name": "Labyrinth -- A Waystation Vault",
                "description": (
                    "A second vault, sealed tight -- whatever opens it is scattered across the floors "
                    "above, not in this room."
                ),
                "connections": [checkpoint_hub["id"]], "monsters": [],
                "lockables": [{
                    "id": f"seg{segment}_gate_cache", "kind": "chest", "name": "a real cache, shared by the whole party's effort",
                    "loot": {"superior_healing_potion": rng.randint(1, 2)}, "gold": rng.randint(200, 500) * floors[-1],
                }],
            }
            # This room was added AFTER _assign_grid_positions already
            # ran once per floor inside generate_floor -- give it a
            # real, collision-free cell on the checkpoint floor's own
            # local grid too, or map_render.render_labyrinth_map's
            # {x, y} lookup crashes on this one room (real bug, caught
            # by this file's own map test).
            occupied = {
                (r["grid_position"]["x"], r["grid_position"]["y"])
                for r in floor_data_by_floor[floors[-1]]["rooms"].values()
            }
            spiral = _spiral_cells()
            next(spiral)
            for x, y in spiral:
                if (x, y) not in occupied:
                    gate_room["grid_position"] = {"x": x, "y": y}
                    break
            all_rooms[gate_room_id] = gate_room
            checkpoint_hub.setdefault("locked_connections", {})[gate_room_id] = f"seg{segment}_multi_gate"
            checkpoint_hub.setdefault("lockables", []).append({
                "id": f"seg{segment}_multi_gate", "kind": "multi_switch_gate", "name": "a real, second sealed archway",
                "requires": switch_lockable_ids,
            })

    return {
        "rooms": all_rooms,
        "entry_room_id": floor_data_by_floor[start_floor]["hub_room_id"],
        "checkpoint_room_id": floor_data_by_floor[floors[-1]]["connector_room_id"],
        "segment": segment,
        "theme": theme,
    }
