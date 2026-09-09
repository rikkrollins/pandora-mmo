"""
rules/dungeon_evolve.py

Phase 3 of the dungeon toolkit (rules/dungeon_audit.py was Phases 1-2):
an offline, RNG-driven "evolve this dungeon" pass, per Coffee (2026-09-01,
"start phase 3" -- following his own original ask, 2026-08-31: "i have
plans after chapter 8 to use this to rework dungeons to evolve them or
get harder... you can even add some RNG to the generator, that would
be awesome for evolving maps!").

Design, confirmed directly with Coffee before any code was written:
  - An evolve produces a NEW variant dungeon (new location_ids)
    alongside the untouched original -- never an in-place rewrite.
  - Generation is offline and authored (this module, run by Claude
    Code via scripts/evolve_dungeon.py), never a live per-attempt
    reshuffle -- campaign.json is one global dict loaded once at
    process start and never rewritten at runtime (confirmed via code
    audit), so there is no live-instancing architecture to hook into,
    and building one would be a far bigger change than "start phase 3."
  - The evolved dungeon's entrance connects from the SOURCE dungeon's
    own hub room, gated by requires_rebirth_count -- the exact same
    location-gate field/mechanism _meets_rebirth_requirement (bot.py)
    already enforces for every other rebirth-gated location. No new
    bot.py code needed.
  - No permanent key-item locks: only a lever shortcut back to the new
    hub (add_lever_shortcut) and a plain DC13 door gating the boss
    branch. Evolved dungeons carry no story quest of their own, so
    there's no NPC to hand out a key -- and check_no_self_referential_
    lock only ever fires for a requires_key_item lock tied to a real
    quest reward, so avoiding key items sidesteps that whole class of
    risk cleanly.
  - Monster/boss picks are drawn from the EXISTING monster catalog
    (campaign["monsters"]), filtered to a computed "harder target
    band" -- never new hand-authored stat blocks.

Real Python `random` (module-level import, matching rules/dice.py's own
convention of calling the stdlib module directly rather than a seeded
singleton); the top-level function accepts an optional `rng` for
deterministic tests, the same spirit as dice.py's `forced_roll`.
"""
import copy
import random

import remnants
from rules import dungeon_audit
from rules.dungeon_audit import (
    CHAPTER_LEVEL_BANDS,
    _dungeon_rooms,
    _find_room,
    _owning_arc_id,
    add_key_gate,
    add_lever_shortcut,
    add_room,
    add_rune_gate,
    connect,
)
from scripts import build_location_grid

# The hub's own real, plain-`connections` lateral neighbor budget: one
# edge back to the entrance (from the source dungeon) plus this many
# generated branches. Real live bug (2026-09-01, found right after
# shipping Wrathflame Vault Evolved): a room can only ever carry 4
# real cardinal (N/S/E/W) neighbors on this game's actual minimap
# (scripts/build_location_grid.py's own hard compass-grid limit) --
# the shipped dungeon's hub fanned out to 6 non-boss branches at once
# (7 total, the boss branch excluded since its own hub edge is
# locked_connections-only and never counts as a lateral neighbor),
# blowing that budget so several branches were reachable by text but
# invisible on the map. 3 non-boss branches + 1 entrance edge = 4,
# exactly the real limit -- matches real a classic action-adventure game hub design too, which
# rarely fans out past 3-4 real branches. Room-count growth now comes
# from LONGER branches, not more of them (see _generate_once below).
_HUB_NON_BOSS_BRANCHES = 3

# Real "gated encounter" room, ported from rules/labyrinth.py's own
# identical mechanic (2026-09-04, per Coffee: after being told that
# gating movement on ANY monster room would turn ordinary evolved-
# dungeon exploration into a forced fight-every-room gauntlet -- every
# _new_room call below already gives most rooms 1-2 trash monsters --
# "def number 1 [miniboss/boss rooms only for the real movement gate]
# but you can use number two [gate-on-any-monster] with the rng and
# the seeds to create ur dungeons and labyrinths with advanced
# pathways and gating"). Applied sparingly, like every other special
# mechanic here, not as a blanket rule.
_GATED_ENCOUNTER_CHANCE = 0.3

# Real Phase L5 "Advanced Dungeons" mid-branch gate chance (2026-09-06),
# ported from rules/labyrinth.py's identical mechanic -- same "roll
# for it" discipline every other optional gate below already uses
# (switch 0.5, plate 0.35, key 0.3, rune 0.2), never "always fires
# whenever a branch happens to be long enough." A real regression this
# exact omission caused, found running the full test suite: with no
# chance gate this fired on nearly every real generation, so a
# statistical test expecting the ORIGINAL switch-gate mechanic to be
# absent on SOME real seeds saw 20/20 seeds carrying a switch instead
# (this mechanic's own switch, not that one's) -- both use the same
# `kind: "switch"` lockable shape, so an unconditional mid-branch gate
# silently made that other test's own "not every time" assumption false.
_MID_BRANCH_GATE_CHANCE = 0.4

# Real Phase L5 "Advanced Dungeons" key-mesh redundant key mesh
# chance (2026-09-06), ported from rules/labyrinth.py's identical
# mechanic -- same "roll for it" discipline as every sibling gate.
_KEY_MESH_CHANCE = 0.3

# Real Phase L5 "Advanced Dungeons" repeated mini-boss gate chance
# (2026-09-06), ported from rules/labyrinth.py's identical mechanic
# (a reference repeat-encounter dungeon's own real pattern). Independent of the mini-boss's
# own placement above -- only rolled once a mini-boss already exists.
_MINIBOSS_REPEAT_CHANCE = 0.25

# Loop-back connections (rules/labyrinth.py's own "densify the tree
# into a real grid" mechanic) were tried here too (2026-09-03) and
# deliberately NOT shipped: real testing across 3 source dungeons x 40
# seeds each found it fires ZERO times, because every branch here
# extends in ONE fixed compass direction in a straight line for its
# whole length (see the branch-extension loop below) -- branches
# radiate outward from the hub and never curve back into grid-adjacency
# with each other, unlike the Labyrinth's own organic, BFS-placed
# branches. Shipping a mechanic confirmed dead on real output would
# violate this project's own "never claim something works without
# running it" rule. Revisit if this generator's branch-extension logic
# ever grows real direction changes (jogs/turns) mid-branch.


def _harder_target_band(campaign: dict, source_dungeon_id: str, explicit_band: tuple[int, int] | None = None) -> tuple[int, int]:
    """
    The evolved variant's target monster-level band: the NEXT chapter's
    band after whichever one owns the source dungeon. Raises rather
    than guessing for a frontier dungeon (the last banded chapter, or
    either bonus vault with no owning arc at all) -- those need an
    explicit --target-band, same "never silently guessed" discipline
    check_level_band itself already applies to the ceiling-only rule.
    """
    if explicit_band is not None:
        return explicit_band
    if source_dungeon_id in dungeon_audit._BONUS_VAULT_DUNGEON_IDS:
        raise ValueError(
            f"{source_dungeon_id!r} is a bonus vault with no owning arc at all "
            f"-- pass an explicit target_band"
        )
    rooms = _dungeon_rooms(campaign, source_dungeon_id)
    # _owning_arc_id's own seeded-BFS-flood fallback has no concept of
    # "this dungeon is deliberately outside the arc system" -- it would
    # happily hand a bonus vault whatever arc floods in from outside,
    # which is exactly why check_level_band checks _BONUS_VAULT_DUNGEON_
    # IDS FIRST, before ever calling _owning_arc_id. Same order here.
    arc_id = _owning_arc_id(campaign, set(rooms.keys()))
    band_keys = list(CHAPTER_LEVEL_BANDS.keys())
    if arc_id is None or arc_id not in band_keys:
        raise ValueError(
            f"{source_dungeon_id!r} has no owning arc in CHAPTER_LEVEL_BANDS "
            f"(arc_id={arc_id!r}) -- pass an explicit target_band"
        )
    idx = band_keys.index(arc_id)
    if idx + 1 >= len(band_keys):
        raise ValueError(
            f"{arc_id!r} is the last banded chapter -- pass an explicit target_band"
        )
    return CHAPTER_LEVEL_BANDS[band_keys[idx + 1]]


def _candidate_monsters(campaign: dict, target_band: tuple[int, int], boss: bool, exclude: set[str] | None = None) -> list[str]:
    """
    Real catalog monster_ids fit for this band.

    For TRASH (boss=False), a monster with no real `level` field is
    safe filler -- it's dynamically scaled at combat time (rules/
    leveling.py's undertuned/overtuned/rebirth multipliers) regardless
    of where it's placed, so band-appropriateness was never about its
    level field in the first place.

    For BOSS (boss=True), a level-less monster is deliberately EXCLUDED
    rather than treated as filler: real audit of this catalog shows the
    handful of is_boss entries with no level field at all (the_unbegun,
    the_unasked, the_undertone, the_verge_warden) are exactly the
    headline, hand-tuned, one-of-a-kind story climaxes CLAUDE.md itself
    documents as having "its own other real signature mechanic" --
    every ordinary, reusable boss in this catalog carries a real level.
    Randomly handing one of those to a side "evolved" replay dungeon
    would be a real narrative mistake (an unearned spoiler), not a
    harmless filler pick.
    """
    lo, hi = target_band
    exclude = exclude or set()
    out = []
    for key, template in campaign.get("monsters", {}).items():
        if key in exclude or remnants.remnant_for_monster_key(key) is not None:
            continue
        is_boss = bool(template.get("is_boss"))
        if is_boss != boss:
            continue
        level = template.get("level")
        if level is None:
            if not boss:
                out.append(key)
            continue
        if boss:
            if level >= lo:
                out.append(key)
        elif lo <= level <= hi:
            out.append(key)
    return out


def _generate_once(campaign: dict, source_hub_id: str, source_layer: str, new_dungeon_id: str, layer: str,
                    rebirth_gate: int, target_band: tuple[int, int], min_rooms: int, rng: random.Random,
                    display_name: str) -> dict:
    room_ids: list[str] = []

    entrance_id = f"{new_dungeon_id}_entrance"
    add_room(
        campaign, layer, new_dungeon_id, entrance_id, f"{display_name} -- The Threshold",
        "A newly opened way, harder and stranger than what came before.",
        requires_rebirth_count=rebirth_gate,
    )
    room_ids.append(entrance_id)
    # Deliberately UNHINTED -- the source dungeon's own hub can already
    # have real, pre-existing neighbors (including vertical up/down
    # delves, which also compete for the same 4 lateral compass slots;
    # a real live case found testing this fix: Wrathflame Vault's own
    # Ember Hall already has two "up"/"down" delves plus one more plain
    # neighbor, filling 3 of its 4 slots before this new edge is even
    # considered). Forcing a specific direction here fought the
    # existing algorithm's own fallback instead of using it -- letting
    # it fall into whichever real slot is actually free (or none, which
    # is handled gracefully as a "no compass label" note, not a
    # failure) is simpler and correct either way.
    connect(campaign, source_hub_id, entrance_id)

    # Real, confirmed root cause found testing this fix: with the hub
    # only 1 hop past the entrance (2 hops from the SOURCE dungeon's
    # own hub), it can land close enough to the source hub's own
    # nearby structure -- including vertical up/down delves, which
    # also claim lateral compass cells -- that a completely unrelated,
    # pre-existing room ends up occupying a cell this new hub actually
    # needs. When that happens, an earlier-processed branch can then
    # cascade into "stealing" a later branch's own intended slot via
    # its own fallback search, leaving the last branch processed with
    # zero real slots left at all -- not a rare edge case, reproduced
    # on every seed tried against Wrathflame Vault. A short buffer
    # corridor between entrance and hub pushes the hub's own 4-way
    # fan-out far enough from the source dungeon's own local geometry
    # that this becomes vanishingly unlikely -- each extra hop moves
    # another real cell further away in whatever direction the BFS
    # picks, and dungeon_audit's own checks don't care how many plain
    # corridor rooms sit between two real locations.
    # Random per-attempt length (not fixed) -- a busier source hub
    # (e.g. Goblin Warrens', already at its own real 4-neighbor cap)
    # can still need more distance than a quieter one before this
    # dungeon's own fan-out clears its local geometry entirely; varying
    # the buffer length gives evolve_dungeon's own retry loop a
    # genuinely different attempt to try each time instead of
    # deterministically failing the same way every retry.
    buffer_tail = entrance_id
    for i in range(rng.randint(4, 8)):
        buffer_id = f"{new_dungeon_id}_approach_{i}"
        add_room(
            campaign, layer, new_dungeon_id, buffer_id, f"{display_name} -- Approach {i + 1}",
            "The passage narrows here, still a long way from wherever it's actually going.",
        )
        room_ids.append(buffer_id)
        connect(campaign, buffer_tail, buffer_id)
        buffer_tail = buffer_id

    hub_id = f"{new_dungeon_id}_hub"
    add_room(
        campaign, layer, new_dungeon_id, hub_id, f"{display_name} -- Deep Hub",
        "The paths beyond here fork in every direction at once.",
        dungeon_hub=True,
    )
    room_ids.append(hub_id)
    connect(campaign, buffer_tail, hub_id)

    trash_candidates = _candidate_monsters(campaign, target_band, boss=False)
    boss_candidates = _candidate_monsters(campaign, target_band, boss=True)
    if not boss_candidates:
        # Real content-scarcity finding (2026-09-01): this catalog only
        # has ~28 is_boss entries total, and most above level ~35 are
        # already Remnants (a deliberately separate ladder, excluded
        # above) -- there usually isn't a "spare" boss sitting exactly
        # in a higher target_band. Rather than accept an arbitrary
        # weak one, fall back to the STRONGEST real, leveled,
        # non-Remnant boss the catalog has at all (reusing an existing
        # dungeon's own boss type here is fine -- trash monsters are
        # already freely reused across locations campaign-wide with no
        # exclusivity rule; a boss is no different, only the two real
        # story-critical exclusions above -- Remnants and the level-less
        # headline climaxes -- actually matter).
        leveled_bosses = [
            (key, t["level"]) for key, t in campaign.get("monsters", {}).items()
            if t.get("is_boss") and t.get("level") is not None and remnants.remnant_for_monster_key(key) is None
        ]
        if not leveled_bosses:
            raise RuntimeError("no real, leveled, non-Remnant boss monster exists in this catalog at all")
        best_level = max(level for _, level in leveled_bosses)
        boss_candidates = [key for key, level in leveled_bosses if level == best_level]

    def _new_room(branch_idx: int, step: int) -> str:
        room_id = f"{new_dungeon_id}_b{branch_idx}_r{step}"
        add_room(
            campaign, layer, new_dungeon_id, room_id, f"{display_name} -- Chamber {branch_idx}-{step}",
            "One more room this deep in, same as the last, except for what's waiting in it.",
            monsters=rng.sample(trash_candidates, k=min(rng.randint(1, 2), len(trash_candidates))) if trash_candidates else [],
        )
        room_ids.append(room_id)
        return room_id

    # Fixed branch count (_HUB_NON_BOSS_BRANCHES + 1 boss branch) to
    # stay inside the hub's real grid budget -- see that constant's own
    # comment. Each non-boss branch also gets its OWN compass direction,
    # held consistently for every room it's ever extended to -- a real
    # second grid-conflict bug found testing this fix: with NO
    # direction hint at all, build_location_grid's own BFS tries the
    # same "north first" default for every brand-new room with no
    # distinguishing hint, so a long, hint-less branch can wander and
    # collide with a completely different branch's own cells several
    # rooms out, even though the hub's own slot budget was already
    # fixed. Radiating each branch outward on its own axis makes two
    # branches colliding with EACH OTHER geometrically impossible.
    #
    # The 4 directions are RANDOMLY assigned per attempt (not a fixed
    # order) rather than reserving one direction for the entrance edge
    # up front -- a real live case found testing this fix: the source
    # dungeon's own hub can already have pre-existing neighbors
    # (including vertical up/down delves, which also compete for the
    # same 4 lateral slots) landing the entrance-to-hub edge in an
    # unpredictable direction relative to the source hub, which a FIXED
    # branch layout could then collide with several rooms later once
    # everything's placed relative to that same unpredictable origin.
    # Random reshuffling means a failed attempt (caught by the
    # grid-placement check in evolve_dungeon's own retry loop) tries a
    # genuinely different geometry next time, instead of failing
    # identically on every retry.
    # Real, confirmed bug found testing this fix: the boss branch (the
    # 4th) used to get a hardcoded "north" regardless of what the other
    # 3 branches sampled -- since connect() blindly overwrites a
    # direction rather than checking for a conflict, whichever branch
    # ended up ALSO sampling "north" had its own hub-side hint silently
    # clobbered by the boss branch's later connect() call (the boss
    # branch is always generated last), corrupting that branch's own
    # placement data before build_location_grid ever saw it. All 4
    # directions are shuffled together so every branch (boss included)
    # gets a real, distinct one -- the boss branch's hub edge still
    # gets removed from `connections` below regardless, so this is only
    # about keeping the intermediate `directions` data honest, not
    # about the boss branch's own final grid placement (it becomes a
    # separate island either way).
    num_branches = _HUB_NON_BOSS_BRANCHES + 1
    branch_directions = rng.sample(["north", "south", "east", "west"], num_branches)
    total_rooms = 2  # entrance + hub
    branches = []  # [{"idx", "tail_id", "next_step", "direction"}, ...] -- tracks each branch's own growing tail
    for branch_idx in range(1, num_branches + 1):
        direction = branch_directions[branch_idx - 1]
        branch_len = rng.randint(2, 4)
        tail_id = hub_id
        for step in range(branch_len):
            new_room_id = _new_room(branch_idx, step)
            connect(campaign, tail_id, new_room_id, direction=direction)
            tail_id = new_room_id
            total_rooms += 1
        branches.append({"idx": branch_idx, "tail_id": tail_id, "next_step": branch_len, "direction": direction})

    # Room-count growth now comes from EXTENDING existing branches'
    # own tails further, not from adding more branches (that's exactly
    # what caused the grid-overflow bug this fix addresses) -- each
    # extension keeps walking the SAME fixed direction as the rest of
    # its own branch, for the same collision-avoidance reason above.
    # Always extends the CURRENTLY SHORTEST branch (not a uniform
    # random pick) -- keeps growth spread evenly across all 4 rather
    # than risking one branch getting unluckily long. A real, separate
    # grid-conflict case found testing this fix: a long enough straight
    # branch can wander far enough from the hub to cross back into the
    # SOURCE dungeon's own pre-existing room layout (Wrathflame Vault's
    # real 13 rooms occupy real cells too, in whatever shape it was
    # originally authored in) -- even distribution keeps every branch
    # shorter and closer to the hub, minimizing that reach.
    while total_rooms < min_rooms:
        b = min(branches, key=lambda x: x["next_step"])
        new_tail = _new_room(b["idx"], b["next_step"])
        connect(campaign, b["tail_id"], new_tail, direction=b["direction"])
        b["tail_id"] = new_tail
        b["next_step"] += 1
        total_rooms += 1

    branch_idx = num_branches  # the last branch generated is always the boss branch, same convention as before
    branch_leaf_ids = [b["tail_id"] for b in branches]

    # Elemental switch gate (2026-09-01, per Coffee's own classic-action-adventure-inspired
    # ask). Roughly half the time, one non-boss branch's own hub edge
    # gets gated behind a real elemental switch instead of staying a
    # freely open corridor -- real variety beyond every branch always
    # being a plain walk from the hub. Same "convert a plain connection
    # into a locked_connections + lockables entry" shape the boss
    # branch already uses below, just with kind="switch" -- it becomes
    # its own separate grid island the same way the boss branch does
    # (confirmed safe by extensive real testing, see evolve_dungeon's
    # own grid-placement retry step).
    switch_branch_idx = None
    if rng.random() < 0.5:
        switch_branch = rng.choice([b for b in branches if b["idx"] != branch_idx])
        switch_branch_idx = switch_branch["idx"]
        switch_branch_first_id = f"{new_dungeon_id}_b{switch_branch['idx']}_r0"
        _, hub_room_for_switch = _find_room(campaign, hub_id)
        _, switch_gate_far_room = _find_room(campaign, switch_branch_first_id)
        hub_room_for_switch["connections"].remove(switch_branch_first_id)
        switch_gate_far_room["connections"].remove(hub_id)
        switch_gate_far_room.setdefault("connections", []).append(hub_id)
        switch_id = f"{new_dungeon_id}_switch_{switch_branch['idx']}"
        # Torch/brazier flavor is its OWN real 50/50 roll, not a rare
        # "fire AND also a coin flip" combination -- a real bug found
        # testing this fix: the original "element==fire (10%) AND a
        # second 50% roll" chain made torches (and therefore remote
        # gates, scoped to torches below) so rare that 40 real seeds
        # could easily -- and did -- produce zero, not because the
        # mechanic was broken, just because the odds of ever observing
        # it were far lower than intended.
        is_torch = rng.random() < 0.5
        element = "fire" if is_torch else rng.choice(["cold", "lightning", "force", "radiant", "psychic", "poison", "necrotic", "earth", "physical"])
        switch_name = "a lit brazier" if is_torch else f"a {element} crystal"
        switch_lockable = {"id": switch_id, "kind": "switch", "name": switch_name, "element": element}

        # Real remote gate (2026-09-01, per Coffee: "lighting a torch
        # here opens a gate somewhere else"). Scoped to torches
        # specifically, matching how he described it -- ~40% of the
        # time a torch is placed at all, its own definition goes in a
        # DIFFERENT room entirely (any room along a different,
        # unrelated non-boss/non-gated branch), while the
        # locked_connections entry gating the actual path stays on the
        # hub, same as always. Already supported by the existing data
        # model (a locked_connections entry only ever needs the
        # lockable's OWN id, never requires it live in the same room)
        # and confirmed harmless to dungeon_audit.py's own checks:
        # check_no_self_referential_lock only ever looks up a SAME-room
        # lockable for a requires_key_item gate, which a switch never
        # has, so a remote placement is simply invisible to it, not a
        # violation.
        remote_candidates = [
            rid for b in branches if b["idx"] not in (branch_idx, switch_branch["idx"])
            for rid in [f"{new_dungeon_id}_b{b['idx']}_r{step}" for step in range(b["next_step"])]
        ]
        if is_torch and remote_candidates and rng.random() < 0.4:
            host_room_id = rng.choice(remote_candidates)
            _, host_room = _find_room(campaign, host_room_id)
            host_room.setdefault("lockables", []).append(switch_lockable)
        else:
            hub_room_for_switch.setdefault("lockables", []).append(switch_lockable)
        hub_room_for_switch.setdefault("locked_connections", {})[switch_branch_first_id] = switch_id

    # Pressure-plate gate (2026-09-01, per Coffee: "pushing things down
    # holes"/"filling urns" mechanic -- the first ask was specifically
    # for a movable-object puzzle distinct from the elemental switch
    # above). A second, independent kind of branch gate, deliberately
    # rolled on a DIFFERENT branch than the switch above (never the
    # same one) so it can't compete with or replace the switch odds the
    # existing statistical test already measures -- both gates can
    # coexist on the same generated dungeon, or neither can. The real
    # movable object (a crate) is placed in the SAME room as the plate
    # itself, matching the reversible bot.py mechanic
    # (_do_activate_pressure_plate/_SWITCH_STATE) -- push it onto the
    # plate to open the gate, exactly the "something heavy" pattern
    # Coffee described.
    plate_branch_idx = None
    plate_eligible = [b for b in branches if b["idx"] != branch_idx and b["idx"] != switch_branch_idx]
    if plate_eligible and rng.random() < 0.35:
        plate_branch = rng.choice(plate_eligible)
        plate_branch_idx = plate_branch["idx"]
        plate_branch_first_id = f"{new_dungeon_id}_b{plate_branch['idx']}_r0"
        _, hub_room_for_plate = _find_room(campaign, hub_id)
        _, plate_gate_far_room = _find_room(campaign, plate_branch_first_id)
        hub_room_for_plate["connections"].remove(plate_branch_first_id)
        plate_gate_far_room["connections"].remove(hub_id)
        plate_gate_far_room.setdefault("connections", []).append(hub_id)
        plate_id = f"{new_dungeon_id}_plate_{plate_branch['idx']}"
        crate_id = f"{new_dungeon_id}_crate_{plate_branch['idx']}"
        hub_room_for_plate.setdefault("lockables", []).append(
            {"id": plate_id, "kind": "pressure_plate", "name": "a stone pressure plate"}
        )
        hub_room_for_plate.setdefault("movable_objects", []).append(
            {"id": crate_id, "name": "a heavy crate"}
        )
        hub_room_for_plate.setdefault("locked_connections", {})[plate_branch_first_id] = plate_id

    # Real key-gated door (2026-09-04, Phase B port of rules/labyrinth.
    # py's identical mechanic -- per Coffee: "you can have locked doors
    # requireing 'keys'... keys can be dropped by enemies or found in
    # another room"). A third, independent kind of branch gate, never
    # the same branch as the switch or plate gate above. The key itself
    # is placed in a DIFFERENT branch than the one it gates, and that
    # source branch also excludes the switch/plate-gated branches --
    # real bug already found and fixed in the Labyrinth's own port of
    # this exact mechanic: a "guaranteed" find behind an unrelated,
    # already-existing gate isn't actually guaranteed without solving
    # that other gate first.
    key_branch_idx = None
    key_eligible = [b for b in branches if b["idx"] not in (branch_idx, switch_branch_idx, plate_branch_idx)]
    if key_eligible and rng.random() < 0.3:
        key_branch = rng.choice(key_eligible)
        key_branch_idx = key_branch["idx"]
        key_branch_first_id = f"{new_dungeon_id}_b{key_branch['idx']}_r0"
        _, hub_room_for_key = _find_room(campaign, hub_id)
        _, key_gate_far_room = _find_room(campaign, key_branch_first_id)
        hub_room_for_key["connections"].remove(key_branch_first_id)
        key_gate_far_room["connections"].remove(hub_id)
        key_gate_far_room.setdefault("connections", []).append(hub_id)
        door_id = f"{new_dungeon_id}_key_gate"
        add_key_gate(campaign, hub_id, key_branch_first_id, door_id, "a real, heavily-barred door", "labyrinth_floor_key")
        for lk in hub_room_for_key.get("lockables", []):
            if lk["id"] == door_id:
                lk["consume_key"] = True
        key_gate_far_room["description"] += " A real, heavily-barred door seals this path -- it looks like it needs an actual key, not brute force or a steady hand."

        # Coffee's own "dropped by enemies OR found in another room" --
        # chosen randomly each time, always a guaranteed real find
        # either way (never a coin-flip that could leave the key
        # genuinely absent). The source room is the gated branch's own
        # analogue -- a DIFFERENT branch's first room, guaranteed to
        # still be a plain, reachable hub connection.
        key_source_eligible = [b for b in branches if b["idx"] not in (branch_idx, switch_branch_idx, plate_branch_idx, key_branch_idx)]
        if key_source_eligible:
            key_source_branch = rng.choice(key_source_eligible)
            key_source_room_id = f"{new_dungeon_id}_b{key_source_branch['idx']}_r0"
            _, key_source_room = _find_room(campaign, key_source_room_id)
            if rng.random() < 0.5 and key_source_room.get("monsters"):
                key_source_room["guaranteed_key_drop"] = True
                key_source_room["description"] += " Something here looks like it might be carrying something worth taking."
            else:
                key_source_room.setdefault("lockables", []).append({
                    "id": f"{new_dungeon_id}_key_cache", "kind": "chest", "name": "a small, dusty lockbox",
                    "loot": {"labyrinth_floor_key": 1}, "gold": rng.randint(20, 60),
                })

    # Real Locked Rune Doorway (2026-09-04, Phase B port of rules/
    # labyrinth.py's identical mechanic -- per Coffee: "have the mini
    # boss, and boss and scatter them around, have it collected by
    # battle and by chests" -- "use it in variations with the other
    # mechanics", explicitly not the main one). A fourth, independent
    # branch gate, never the same branch as the switch/plate/key gate
    # above. Unlike the key (one unique item), this needs a variable
    # COUNT of the fungible "labyrinth_rune" item -- "if a gateway
    # needs 3 the player will need 3 runes" (Coffee's own words) -- so
    # the guaranteed find is scattered across that many separate
    # branches instead of just one.
    rune_branch_idx = None
    rune_eligible = [b for b in branches if b["idx"] not in (branch_idx, switch_branch_idx, plate_branch_idx, key_branch_idx)]
    if rune_eligible and rng.random() < 0.2:
        rune_branch = rng.choice(rune_eligible)
        rune_branch_idx = rune_branch["idx"]
        rune_branch_first_id = f"{new_dungeon_id}_b{rune_branch['idx']}_r0"
        _, hub_room_for_rune = _find_room(campaign, hub_id)
        _, rune_gate_far_room = _find_room(campaign, rune_branch_first_id)
        hub_room_for_rune["connections"].remove(rune_branch_first_id)
        rune_gate_far_room["connections"].remove(hub_id)
        rune_gate_far_room.setdefault("connections", []).append(hub_id)

        rune_source_eligible = [b for b in branches if b["idx"] not in (branch_idx, switch_branch_idx, plate_branch_idx, rune_branch_idx)]
        rune_count = min(rng.randint(2, 4), len(rune_source_eligible))
        if rune_count > 0:
            door_id = f"{new_dungeon_id}_rune_gate"
            add_rune_gate(campaign, hub_id, rune_branch_first_id, door_id, "a locked rune doorway", "labyrinth_rune", rune_count)
            rune_gate_far_room["description"] += f" A locked rune doorway seals this path -- it looks like it needs {rune_count} real Labyrinth Runes, not brute force or a steady hand."

            rune_source_branches = rng.sample(rune_source_eligible, rune_count)
            for i, rune_source_branch in enumerate(rune_source_branches):
                rune_source_room_id = f"{new_dungeon_id}_b{rune_source_branch['idx']}_r0"
                _, rune_source_room = _find_room(campaign, rune_source_room_id)
                if rng.random() < 0.5 and rune_source_room.get("monsters"):
                    rune_source_room["guaranteed_rune_drop"] = True
                    rune_source_room["description"] += " Something here looks like it might be carrying something worth taking."
                else:
                    rune_source_room.setdefault("lockables", []).append({
                        "id": f"{new_dungeon_id}_rune_cache_{i}", "kind": "chest", "name": "a small, rune-etched cache",
                        "loot": {"labyrinth_rune": 1}, "gold": rng.randint(20, 60),
                    })

    # Real Phase L5 "Advanced Dungeons" mid-branch gate, ported from
    # rules/labyrinth.py's identical mechanic (2026-09-06, per Coffee:
    # "work on the lower-priority and unscheduled stuff" -- see
    # [[project_advanced_interconnected_dungeons_research]]'s own real
    # caution that a Labyrinth-shaped fix does NOT automatically port
    # cleanly here, e.g. loop-back was tried and abandoned). This one
    # DOES port safely: unlike loop-back (which needed grid-adjacency
    # between branches that never happens here, since every branch
    # radiates outward in one straight compass line), a mid-branch gate
    # is a pure DATA operation -- lock one internal edge, place a
    # switch elsewhere -- with no dependency on branch geometry at all.
    # Deliberately allowed to target the SAME branch a switch/plate/
    # key/rune gate above already claimed: those only ever lock a
    # branch's own ROOT edge (hub -> branch's first room), this locks a
    # genuinely different, INTERIOR edge one or more rooms further in,
    # so there's no real collision -- same tolerance this generator's
    # own rune-gate block already extends to the mandatory/switch/plate
    # gates before it (never re-excluding every earlier pick).
    # Real regression found and fixed shipping the mesh mechanic below
    # (2026-09-06): a branch root can ALREADY hold a switch-kind
    # lockable from an earlier, independent mechanic (the collapse-
    # puzzle's own switch, confirmed via real testing to land on a
    # branch root too) -- placing a SECOND one in that same room makes
    # `_find_lockable`'s real "same-kind ambiguous, refuse" rule
    # (v1.27.513) silently swallow whichever action a player takes
    # there, breaking BOTH mechanics at once. `dungeon_audit.py`'s own
    # checks never catch this (an ambiguous lockable is a gameplay
    # problem, not a graph-structure one), so both new mechanics below
    # check for it directly before ever placing a switch.
    def _room_already_has_a_switch(room_id: str) -> bool:
        _, room = _find_room(campaign, room_id)
        return any(lk.get("kind") == "switch" for lk in room.get("lockables", []))

    mid_branch = None
    mid_branch_candidates = [b for b in branches if b["idx"] != branch_idx and b["next_step"] >= 3]
    if mid_branch_candidates and rng.random() < _MID_BRANCH_GATE_CHANCE:
        mid_branch = rng.choice(mid_branch_candidates)
        mid_branch_source_candidates = [
            b for b in branches if b["idx"] != mid_branch["idx"] and not _room_already_has_a_switch(f"{new_dungeon_id}_b{b['idx']}_r0")
        ]
        if mid_branch_source_candidates:
            mid_branch_source = rng.choice(mid_branch_source_candidates)
            source_room_id = f"{new_dungeon_id}_b{mid_branch_source['idx']}_r0"
            edge_index = rng.randint(0, mid_branch["next_step"] - 2)
            from_room_id = f"{new_dungeon_id}_b{mid_branch['idx']}_r{edge_index}"
            to_room_id = f"{new_dungeon_id}_b{mid_branch['idx']}_r{edge_index + 1}"
            _, source_room = _find_room(campaign, source_room_id)
            _, from_room = _find_room(campaign, from_room_id)
            element = rng.choice(["fire", "cold", "lightning", "force", "radiant", "psychic", "poison", "necrotic", "earth", "physical"])
            switch_id = f"{new_dungeon_id}_midbranch_switch"
            source_room.setdefault("lockables", []).append({
                "id": switch_id, "kind": "switch", "name": f"a {element} crystal", "element": element,
            })
            door_id = f"{new_dungeon_id}_midbranch_gate"
            from_room["connections"].remove(to_room_id)
            from_room.setdefault("locked_connections", {})[to_room_id] = door_id
            from_room.setdefault("lockables", []).append({
                "id": door_id, "kind": "multi_switch_gate", "name": "a real inner seal", "requires": [switch_id],
            })
            from_room["description"] += " A real inner seal blocks the way deeper in -- something elsewhere in this dungeon must open it."

    # Real Phase L5 "Advanced Dungeons" key-mesh redundant key mesh,
    # ported from rules/labyrinth.py's identical mechanic (2026-09-06,
    # per Coffee: "work on the lower-priority and unscheduled stuff").
    # Same pure-data shape as the mid-branch gate above, so it ports
    # the same clean way. Two DIFFERENT branches each gate the OTHER's
    # own interior, with each branch's own switch sitting in the OTHER
    # branch's root room (always open straight off the hub) -- no
    # forced first branch, unlike the mid-branch gate's own strict
    # dependency. Excludes `mid_branch` (not just this block's own
    # picks) so the two mechanics never target the identical branch,
    # matching the Labyrinth's own `mesh_used` exclusion discipline.
    mesh_used_idx = {mid_branch["idx"]} if mid_branch else set()
    mesh_candidates = [
        b for b in branches if b["idx"] != branch_idx and b["idx"] not in mesh_used_idx and b["next_step"] >= 2
        and not _room_already_has_a_switch(f"{new_dungeon_id}_b{b['idx']}_r0")
    ]
    if len(mesh_candidates) >= 2 and rng.random() < _KEY_MESH_CHANCE:
        mesh_branches = rng.sample(mesh_candidates, 2)
        mesh_switch_ids = []
        for i, branch in enumerate(mesh_branches):
            switch_room_id = f"{new_dungeon_id}_b{branch['idx']}_r0"
            _, switch_room = _find_room(campaign, switch_room_id)
            element = rng.choice(["fire", "cold", "lightning", "force", "radiant", "psychic", "poison", "necrotic", "earth", "physical"])
            switch_id = f"{new_dungeon_id}_mesh_switch_{i}"
            switch_room.setdefault("lockables", []).append({
                "id": switch_id, "kind": "switch", "name": f"a {element} crystal", "element": element,
            })
            mesh_switch_ids.append(switch_id)
        for i, branch in enumerate(mesh_branches):
            other_switch_id = mesh_switch_ids[1 - i]
            edge_index = rng.randint(0, branch["next_step"] - 2)
            from_room_id = f"{new_dungeon_id}_b{branch['idx']}_r{edge_index}"
            to_room_id = f"{new_dungeon_id}_b{branch['idx']}_r{edge_index + 1}"
            _, mesh_from_room = _find_room(campaign, from_room_id)
            door_id = f"{new_dungeon_id}_mesh_gate_{i}"
            mesh_from_room["connections"].remove(to_room_id)
            mesh_from_room.setdefault("locked_connections", {})[to_room_id] = door_id
            mesh_from_room.setdefault("lockables", []).append({
                "id": door_id, "kind": "multi_switch_gate", "name": "a real, answering seal", "requires": [other_switch_id],
            })
            mesh_from_room["description"] += " A real, answering seal blocks the way deeper in -- something in a different part of this dungeon answers it."

    # Extra chest lockables -- comfortable lock_density padding, real
    # loot from items already known-good elsewhere in this same
    # catalog. Real bug found testing this fix: a fixed 1-2 chests was
    # only ever enough for a Wrathflame-Vault-sized dungeon (~20 rooms,
    # needing 2-3 real lockables); a larger source like Goblin Warrens
    # can push the generated room count past 40, where
    # check_lock_density's own room_count // 8 floor genuinely needs
    # more than a fixed small range can provide. Scales with the real
    # final room count instead, sampling from every non-hub/entrance/
    # buffer room (not just the 4 branch leaves, which can't supply
    # more than 4 unique picks on their own once a larger dungeon needs
    # more chests than that).
    # len(room_ids), not total_rooms -- total_rooms only tracks
    # entrance+hub+branches, but check_lock_density's own room_count
    # counts every room tagged with this dungeon_id, buffer corridor
    # rooms included.
    lock_floor = max(1, len(room_ids) // 8)
    chests_needed = max(rng.randint(1, 2), lock_floor - 1)  # -1 for the guaranteed lever; the boss door is added below
    chest_pool = [rid for rid in room_ids if rid not in (entrance_id, hub_id) and "_approach_" not in rid]
    chest_leaves = rng.sample(chest_pool, k=min(chests_needed, len(chest_pool)))
    for i, leaf_id in enumerate(chest_leaves):
        _, leaf_room = _find_room(campaign, leaf_id)
        # Greater, not plain (2026-09-05, per Coffee, dev-bridge:
        # "Instead of healing potions, can you give greater healing
        # potions?" -- same fix as rules/labyrinth.py's own chests).
        # Real independent rare spell-tonic roll (2026-09-05, per
        # Coffee: "add ethers to the item list of things that can be
        # won from a chest" -- see dungeon_audit.maybe_add_spell_
        # tonic_to_loot's own docstring for the full reasoning).
        cache_loot = {"greater_healing_potion": rng.randint(1, 2)}
        dungeon_audit.maybe_add_spell_tonic_to_loot(cache_loot, rng)
        leaf_room.setdefault("lockables", []).append({
            "id": f"{new_dungeon_id}_cache_{i}", "kind": "chest", "name": "a real, half-buried cache",
            "loot": cache_loot, "gold": rng.randint(40, 220),
        })

    # Lever shortcut: from a non-boss branch leaf back to the new hub.
    lever_source = rng.choice(branch_leaf_ids[:-1]) if len(branch_leaf_ids) > 1 else branch_leaf_ids[0]
    add_lever_shortcut(campaign, lever_source, hub_id, f"{new_dungeon_id}_shortcut_lever", "a weathered lever")

    # Boss branch: the LAST branch generated. Gate its first room behind
    # a plain DC13 door from the hub (no key item) -- the far room keeps
    # a plain connection back so players can freely walk out once past
    # it, same asymmetric shape every key/door gate in this game uses.
    boss_branch_first_id = f"{new_dungeon_id}_b{branch_idx}_r0"
    _, hub_room = _find_room(campaign, hub_id)
    _, gate_room = _find_room(campaign, boss_branch_first_id)
    hub_room["connections"].remove(boss_branch_first_id)
    gate_room["connections"].remove(hub_id)
    gate_room.setdefault("connections", []).append(hub_id)
    door_id = f"{new_dungeon_id}_boss_gate"
    hub_room.setdefault("lockables", []).append({"id": door_id, "kind": "door", "name": "a sealed stone door"})
    hub_room.setdefault("locked_connections", {})[boss_branch_first_id] = door_id

    boss_room_id = branch_leaf_ids[-1]
    boss_monster = rng.choice(boss_candidates)
    _, boss_room = _find_room(campaign, boss_room_id)
    boss_room.setdefault("monsters", [])
    if boss_monster not in boss_room["monsters"]:
        boss_room["monsters"].append(boss_monster)
    boss_room["is_boss_room"] = True

    # Real event-gated live edge, ported to evolved overworld dungeons
    # (2026-09-06, per Coffee: "keep going" -- a reference water dungeon's own real
    # "After Hinox defeat, a portal between this room and entrance is
    # now usable, creating a shortcut," already shipped for the
    # Labyrinth in v1.27.529). Two earlier approaches were rejected for
    # real reasons (see [[project_advanced_interconnected_dungeons_
    # research]]): a live campaign.json mutation would violate this
    # module's own explicit "no live-instancing architecture" design,
    # and a bidirectional `warps` pair would need to be ungated in the
    # hub->boss direction (warps must be real bidirectional pairs, see
    # check_warps_reference_real_rooms), letting a player skip the
    # whole branch by naming the boss room directly. This is the real
    # third option: the plain connection is written ONCE, at
    # generation time, but `story_gates`/requires_cleared_location
    # (already a real, per-CHARACTER gate -- see check_story_gate)
    # keeps it genuinely closed in the hub-ward direction until this
    # specific character has actually beaten the boss, exactly
    # mirroring the Labyrinth's own "reveals a brand-new connection"
    # feel without ever needing a live campaign mutation or an
    # ungated backdoor. check_reciprocity's own real exemption for
    # gated one-way edges now covers story_gates pairs too (see its
    # own docstring).
    if boss_room_id not in hub_room.get("connections", []):
        boss_room.setdefault("connections", []).append(hub_id)
        boss_room.setdefault("story_gates", {})[hub_id] = {"requires_cleared_location": boss_room_id}
        boss_room["description"] += " Something here looks like it could open a much shorter way back, if it were ever truly dealt with."

    # Miniboss (2026-09-01, per the a classic action-adventure game research: dungeons almost
    # always have one "you're not ready yet" encounter partway through,
    # distinct from both trash and the real climax). A non-boss
    # branch's own leaf gets one monster from the UPPER half of
    # target_band -- stronger than ordinary trash, still meaningfully
    # below the real boss, since it draws from the same pool bounded by
    # the same target_band ceiling rather than reaching above it.
    non_boss_leaves = branch_leaf_ids[:-1]
    miniboss_room_id = None
    if non_boss_leaves:
        band_lo, band_hi = target_band
        mid_lo = band_lo + (band_hi - band_lo) // 2
        miniboss_candidates = _candidate_monsters(campaign, (mid_lo, band_hi), boss=False) or trash_candidates
        if miniboss_candidates:
            miniboss_room_id = rng.choice(non_boss_leaves)
            miniboss = rng.choice(miniboss_candidates)
            _, miniboss_room = _find_room(campaign, miniboss_room_id)
            miniboss_room.setdefault("monsters", [])
            if miniboss not in miniboss_room["monsters"]:
                miniboss_room["monsters"].append(miniboss)
            miniboss_room["is_miniboss_room"] = True
            # Real "Advanced Dungeons" repeated gate (2026-09-06, per
            # Coffee: "keep going" -- a reference repeat-encounter dungeon's own real pattern,
            # already shipped for the Labyrinth in v1.27.534). A rare,
            # independent roll on top of an already-placed mini-boss.
            # No monster-respawn logic is needed here (unlike the
            # Labyrinth's own port): this room's `monsters` list is
            # shared, campaign-wide state that's never cleared on a
            # real victory in the first place, so the exact same
            # encounter is already still there for a real rematch --
            # `bot._mark_location_cleared_for_party` is the one real
            # gate that needs to know to hold off on `cleared_locations`
            # until the required count is actually met.
            if rng.random() < _MINIBOSS_REPEAT_CHANCE:
                miniboss_room["repeat_required"] = rng.randint(2, 3)

    # Real gated encounter (2026-09-04) -- see _GATED_ENCOUNTER_CHANCE's
    # own comment for the full reasoning. Never the entrance/hub/buffer
    # rooms, never a room already flagged is_miniboss_room/is_boss_room
    # (those already gate movement on their own).
    gated_encounter_candidates = []
    for rid in room_ids:
        if rid in (entrance_id, hub_id) or "_approach_" in rid:
            continue
        _, candidate_room = _find_room(campaign, rid)
        if candidate_room.get("monsters") and not candidate_room.get("is_miniboss_room") and not candidate_room.get("is_boss_room"):
            gated_encounter_candidates.append(rid)
    if gated_encounter_candidates and rng.random() < _GATED_ENCOUNTER_CHANCE:
        gated_room_id = rng.choice(gated_encounter_candidates)
        _, gated_room = _find_room(campaign, gated_room_id)
        gated_room["is_gated_encounter"] = True
        gated_room["description"] += " Something about this space feels sealed -- like the way beyond won't truly open until whatever's here is dealt with."

    # Real warp shortcut (2026-09-04, ported from rules/labyrinth.py's
    # identical mechanic -- per Coffee's own correction that this was
    # never meant to be deferred to the final chapter: "the warp and
    # other things was a misclassification -- they are supposed to be
    # in the generator, not in the final chapter also the final chapter
    # does use them, i want them in the generator too"). Picked from
    # two DIFFERENT non-boss branches' own tails -- never the hub or
    # the boss branch, so a warp is always a genuine bonus link, never
    # a way to skip the real climax gate. Stored separately from
    # `connections` (same reasoning as the Labyrinth: a warp's own map
    # line is drawn differently -- straight across the map, not a
    # doorway between grid-adjacent cells) -- confirmed harmless to
    # every existing dungeon_audit.py check; a new
    # check_warps_reference_real_rooms check verifies the new field
    # itself is well-formed.
    # Real live bug found and fixed (2026-09-04, building the Locked
    # Rune Doorway feature -- exposed by a real end-to-end test, a
    # pre-existing gap unrelated to that feature's own logic): a
    # branch's tail room can ALSO be flagged is_miniboss_room/
    # is_boss_room/is_gated_encounter by the placements just above,
    # each of which genuinely blocks all movement out of that room
    # (bot._is_gated_combat_room) until its real monsters are
    # defeated -- including through a warp planted on that same room,
    # making the warp itself unusable from that side until then.
    # Excluded from candidacy entirely, same "don't compound one
    # mechanic onto a room another already claimed" discipline every
    # other gate in this function already follows.
    def _tail_room_is_gated(tail_id: str) -> bool:
        _, tail_room = _find_room(campaign, tail_id)
        return bool(tail_room.get("monsters") and (tail_room.get("is_miniboss_room") or tail_room.get("is_boss_room") or tail_room.get("is_gated_encounter")))

    non_boss_branches = [b for b in branches if b["idx"] != branch_idx]
    warp_eligible_branches = [b for b in non_boss_branches if not _tail_room_is_gated(b["tail_id"])]
    if len(warp_eligible_branches) >= 2 and rng.random() < 0.35:
        warp_a_branch, warp_b_branch = rng.sample(warp_eligible_branches, 2)
        warp_a, warp_b = warp_a_branch["tail_id"], warp_b_branch["tail_id"]
        _, warp_a_room = _find_room(campaign, warp_a)
        _, warp_b_room = _find_room(campaign, warp_b)
        warp_a_room.setdefault("warps", []).append(warp_b)
        warp_b_room.setdefault("warps", []).append(warp_a)
        warp_a_room["description"] += " A warp shimmers faintly in the corner -- it leads somewhere else in this dungeon."
        warp_b_room["description"] += " A warp shimmers faintly in the corner -- it leads somewhere else in this dungeon."

    # Real carry-and-collapse-style single-switch collapse puzzle (2026-09-04,
    # same port). Solving it seals a previously-open dead-end branch
    # (the inverse of locked_connections -- starts open, becomes
    # blocked once its own multi_switch_gate trigger is thrown) while
    # opening a genuine new shortcut elsewhere -- "the floor's
    # structure shifts beneath you," never a silent loss with no real
    # trade. Never targets the switch- or plate-gated branch (those
    # branches' own hub edge is already a locked_connections gate, not
    # a plain `connections` entry -- there'd be nothing plain left to
    # seal), matching the exact same exclusion rules/labyrinth.py's own
    # sealable_candidates already applies. A new
    # check_collapse_never_orphans_a_room audit check (wired into
    # evolve_dungeon's existing retry loop) catches the one real risk
    # this introduces: sealing a branch that happens to host a switch
    # some OTHER gate still depends on, or the boss room itself -- if
    # that happens, this whole generation attempt is simply retried
    # with a fresh RNG draw, same as any other check failure.
    seal_eligible = [b for b in branches if b["idx"] not in (branch_idx, switch_branch_idx, plate_branch_idx, key_branch_idx)]
    if len(seal_eligible) >= 1 and len(non_boss_branches) >= 3 and rng.random() < 0.3:
        seal_branch = rng.choice(seal_eligible)
        seal_leaf = seal_branch["tail_id"]
        seal_parent = hub_id if seal_branch["next_step"] <= 1 else f"{new_dungeon_id}_b{seal_branch['idx']}_r{seal_branch['next_step'] - 2}"
        # Real regression found and fixed shipping the mid-branch gate/
        # key-mesh mechanics (2026-09-06): those can ALSO place a real
        # switch at a branch root, and this block runs after them --
        # without this check, two same-kind "switch" lockables could
        # land in the identical room, and `_find_lockable`'s own real
        # "same-kind ambiguous, refuse" rule (v1.27.513) would silently
        # swallow whichever action a player takes there, breaking BOTH
        # mechanics at once. dungeon_audit.py's own checks never catch
        # this (an ambiguous lockable is a gameplay problem, not a
        # graph-structure one).
        other_branches = [
            b for b in non_boss_branches
            if b["idx"] != seal_branch["idx"] and not _room_already_has_a_switch(f"{new_dungeon_id}_b{b['idx']}_r0")
        ]
        if len(other_branches) >= 2:
            switch_host_branch = rng.choice(other_branches)
            switch_host_id = f"{new_dungeon_id}_b{switch_host_branch['idx']}_r0"
            _, switch_host_room = _find_room(campaign, switch_host_id)
            element = rng.choice(["fire", "cold", "lightning", "force", "radiant", "psychic", "poison", "necrotic", "earth", "physical"])
            trigger_switch_id = f"{new_dungeon_id}_collapse_switch"
            switch_host_room.setdefault("lockables", []).append({
                "id": trigger_switch_id, "kind": "switch", "name": f"a shuddering {element} crystal", "element": element,
            })
            trigger_id = f"{new_dungeon_id}_collapse_trigger"
            _, seal_parent_room = _find_room(campaign, seal_parent)
            seal_parent_room.setdefault("lockables", []).append({
                "id": trigger_id, "kind": "multi_switch_gate", "name": "a real structural trigger",
                "requires": [trigger_switch_id],
            })
            seal_parent_room.setdefault("collapsing_connections", {})[seal_leaf] = trigger_id
            seal_parent_room["description"] += " Something here feels structurally unstable -- like it's rigged to give way."
            # The switch's own branch is a perfectly valid shortcut
            # endpoint (matches rules/labyrinth.py's own identical
            # mechanic exactly -- its shortcut_pair is drawn from
            # `other_chains`, with no exclusion for whichever chain
            # hosts the switch). Excluding it here was a real bug in
            # this port found via testing: with the overworld's fixed
            # 3 non-boss branches, excluding both the sealed branch AND
            # the switch's own branch leaves only 1 candidate, so the
            # 2-sample shortcut/reward could never actually fire --
            # sealing something with no compensating shortcut ever
            # created, which contradicts the whole "solving it is a
            # real trade, never just a loss" design.
            if len(other_branches) >= 2:
                shortcut_a_branch, shortcut_b_branch = rng.sample(other_branches, 2)
                shortcut_a, shortcut_b = shortcut_a_branch["tail_id"], shortcut_b_branch["tail_id"]
                _, shortcut_a_room = _find_room(campaign, shortcut_a)
                echo_id = f"{trigger_id}_echo_{shortcut_a}"
                shortcut_a_room.setdefault("lockables", []).append({
                    "id": echo_id, "kind": "multi_switch_gate", "name": "a newly-opened shortcut",
                    "requires": [trigger_switch_id],
                })
                shortcut_a_room.setdefault("locked_connections", {})[shortcut_b] = echo_id

    # Bonus/optional room (2026-09-01, a classic action-adventure game research: reward exploration
    # off the critical path). Extends a random NON-BOSS branch one room
    # further, in that branch's own already-established direction (so
    # it can't introduce a new cross-branch grid collision) -- no gate
    # at all, so it's automatically exempt from check_boss_gated/
    # reachability requirements, just a free chest for wandering off
    # the main line.
    bonus_branch = rng.choice([b for b in branches if b["idx"] != branch_idx])
    bonus_room_id = _new_room(bonus_branch["idx"], bonus_branch["next_step"])
    connect(campaign, bonus_branch["tail_id"], bonus_room_id, direction=bonus_branch["direction"])
    _, bonus_room = _find_room(campaign, bonus_room_id)
    bonus_room["name"] = f"{display_name} -- A Quiet Alcove"
    bonus_room["description"] = "Off the main path, easy to miss -- exactly the kind of place worth a second look."
    bonus_room["monsters"] = []
    bonus_loot = {"greater_healing_potion": 1}
    dungeon_audit.maybe_add_spell_tonic_to_loot(bonus_loot, rng)
    bonus_room.setdefault("lockables", []).append({
        "id": f"{new_dungeon_id}_bonus_cache", "kind": "chest", "name": "an unguarded stash",
        "loot": bonus_loot, "gold": rng.randint(60, 180),
    })

    # Dungeon shop for larger dungeons only (2026-09-01) -- the shop
    # system already exists and works end-to-end; the only real gap was
    # that every existing shop is tied to a specific named overworld
    # NPC, none of which fit turning up inside a random dungeon. Reuses
    # the one reusable "wandering trader" NPC/shop authored for exactly
    # this, placed in the hub of any evolve past a real size threshold.
    if len(room_ids) >= 18:
        hub_room.setdefault("npcs", []).append("wandering_dungeon_trader")
        hub_room["shop"] = "wandering_traders_pack"

    # Real, explicit opt-out from check_level_band's arc-ownership
    # lookup -- see that check's own docstring for exactly why a
    # quest-less generated dungeon needs this (the same principle as
    # the hardcoded bonus-vault set, applied to a dungeon that didn't
    # exist when that set was written). Stamped on the SAME campaign
    # object being validated (the `trial` copy during generation, the
    # real campaign after a successful commit), so the exemption is
    # live for both the retry-loop's own validation and any later
    # standalone audit of the shipped result.
    if new_dungeon_id not in campaign.setdefault("evolved_dungeon_ids", []):
        campaign["evolved_dungeon_ids"].append(new_dungeon_id)

    # Real owl-statue-style hint (2026-09-03, Phase L4, item 4 -- ported
    # from rules/labyrinth.py's own identical mechanic, per Coffee's own
    # repeated "dungeons and labyrinth"/"dungeon or labyrinth
    # generation" phrasing that this scope was never Labyrinth-only).
    # Directly portable as-is: same `_find_lockable`/`_lockable_callout_
    # lines`/examine machinery, same non-spoiler discipline. Only ever
    # hints at a miniboss or a switch/pressure-plate-gated branch --
    # never the boss gate itself, since every evolved dungeon has one
    # by definition (not a real discovery, unlike the Labyrinth's own
    # probabilistic features).
    hint_lines = []
    if miniboss_room_id is not None:
        hint_lines.append("Something stronger than the rest of this place waits along the way, off the main path.")
    if switch_branch_idx is not None:
        hint_lines.append("A passage further in stays sealed until something elsewhere here is answered.")
    if plate_branch_idx is not None:
        hint_lines.append("A passage further in waits on something heavy, placed just right.")
    if gated_encounter_candidates and any(_find_room(campaign, rid)[1].get("is_gated_encounter") for rid in gated_encounter_candidates):
        hint_lines.append("Somewhere here, a real fight is standing between you and the rest of this place.")
    if hint_lines:
        hub_room.setdefault("lockables", []).append({
            "id": f"{new_dungeon_id}_hint_statue", "kind": "hint_statue", "name": "a worn statue, one eye missing",
            "hint_lines": hint_lines,
        })

    return {
        "new_dungeon_id": new_dungeon_id,
        "room_ids": room_ids,
        "room_count": len(room_ids),
        "target_band": target_band,
        "boss_room_id": boss_room_id,
        "boss_monster": boss_monster,
        "rebirth_gate": rebirth_gate,
        "source_hub_id": source_hub_id,
        "bonus_room_id": bonus_room_id,
        "has_shop": len(room_ids) >= 18,
        "switch_branch_idx": switch_branch_idx,
        "plate_branch_idx": plate_branch_idx,
        "boss_gate_room_id": boss_branch_first_id,
    }


def evolve_dungeon(
    campaign: dict, source_dungeon_id: str, new_dungeon_id: str, layer: str, rebirth_gate: int,
    target_band: tuple[int, int] | None = None, rng: random.Random | None = None, max_retries: int = 20,
    display_name: str | None = None,
) -> dict:
    """
    Generates a new, harder variant dungeon (new_dungeon_id) alongside
    source_dungeon_id, mutating `campaign` in place only once a full
    attempt has passed real validation. Retries (fresh RNG draws) up to
    max_retries on any check failure; raises RuntimeError if it never
    converges.

    Validated against every real Phase 1 check, completely unchanged --
    including level_band, which self-exempts a generated dungeon via
    `campaign["evolved_dungeon_ids"]` (stamped by _generate_once; see
    check_level_band's own docstring for exactly why an evolved
    dungeon needs the same opt-out the two hardcoded bonus vaults
    already get).
    """
    rng = rng or random.Random()
    target_band = target_band or _harder_target_band(campaign, source_dungeon_id)
    source_rooms = _dungeon_rooms(campaign, source_dungeon_id)
    if not source_rooms:
        raise ValueError(f"no rooms found tagged dungeon_id={source_dungeon_id!r}")
    source_hub_id = next(
        (rid for rid, r in source_rooms.items() if r.get("dungeon_hub")),
        max(source_rooms, key=lambda rid: len(source_rooms[rid].get("connections", []))),
    )
    source_layer, _ = _find_room(campaign, source_hub_id)
    min_rooms = len(source_rooms) + 1 + rng.randint(2, 6)
    display_name = display_name or new_dungeon_id.replace("_", " ").title()

    last_failures = None
    for _attempt in range(max_retries):
        trial = copy.deepcopy(campaign)
        summary = _generate_once(
            trial, source_hub_id, source_layer, new_dungeon_id, layer, rebirth_gate, target_band, min_rooms, rng, display_name,
        )

        # Real grid coordinates, not add_room's own {"x":0,"y":0} default
        # (the actual root cause of the map-conflict bug this fix
        # addresses) -- reuses the same authoritative, already-tested
        # BFS-over-connections logic scripts/build_location_grid.py's
        # own --apply uses campaign-wide, so a generated room's
        # placement is computed with full awareness of its real
        # neighbors, not invented separately here. Only WRITES BACK the
        # entries for this dungeon's own new rooms (plus the source
        # hub, whose own connections list gained the new entrance edge)
        # -- deliberately does NOT bundle in any of this layer's other
        # pre-existing direction/position fixes the same BFS would also
        # compute, to keep an evolve's diff scoped to what it actually
        # generated. If any new room genuinely can't be grid-placed
        # (the compass-grid equivalent of "no free cardinal slot" /
        # "genuinely disconnected"), that's treated exactly like any
        # other check failure below -- discarded and retried.
        grid_results, _grid_notes = build_location_grid.build_layer(layer, trial["locations"][layer])
        grid_ok = all("grid_position" in grid_results.get(rid, {}) for rid in summary["room_ids"])
        if grid_ok:
            for room_id in summary["room_ids"]:
                trial["locations"][layer][room_id]["grid_position"] = grid_results[room_id]["grid_position"]
                trial["locations"][layer][room_id]["directions"] = grid_results[room_id]["directions"]
            if source_layer == layer:
                trial["locations"][layer][source_hub_id]["directions"] = grid_results[source_hub_id]["directions"]

        failures = dungeon_audit.audit_dungeon(trial, new_dungeon_id)
        if grid_ok and all(not f for f in failures.values()):
            for room_id in summary["room_ids"]:
                campaign["locations"][layer][room_id] = trial["locations"][layer][room_id]
            campaign["locations"][source_layer][source_hub_id] = trial["locations"][source_layer][source_hub_id]
            if new_dungeon_id not in campaign.setdefault("evolved_dungeon_ids", []):
                campaign["evolved_dungeon_ids"].append(new_dungeon_id)
            return summary
        last_failures = dict(failures)
        if not grid_ok:
            unplaced = [rid for rid in summary["room_ids"] if "grid_position" not in grid_results.get(rid, {})]
            last_failures["grid_placement"] = [f"{rid!r} could not be placed on the compass grid" for rid in unplaced]
    raise RuntimeError(
        f"evolve_dungeon: could not generate a valid {new_dungeon_id!r} in {max_retries} attempts; "
        f"last failures: {last_failures}"
    )
