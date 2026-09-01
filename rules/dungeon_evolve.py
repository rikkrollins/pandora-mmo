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
    add_lever_shortcut,
    add_room,
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
# exactly the real limit -- matches real ALTTP hub design too, which
# rarely fans out past 3-4 real branches. Room-count growth now comes
# from LONGER branches, not more of them (see _generate_once below).
_HUB_NON_BOSS_BRANCHES = 3


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

    # Elemental switch gate (2026-09-01, per Coffee's own ALTTP-inspired
    # ask). Roughly half the time, one non-boss branch's own hub edge
    # gets gated behind a real elemental switch instead of staying a
    # freely open corridor -- real variety beyond every branch always
    # being a plain walk from the hub. Same "convert a plain connection
    # into a locked_connections + lockables entry" shape the boss
    # branch already uses below, just with kind="switch" -- it becomes
    # its own separate grid island the same way the boss branch does
    # (confirmed safe by extensive real testing, see evolve_dungeon's
    # own grid-placement retry step).
    if rng.random() < 0.5:
        switch_branch = rng.choice([b for b in branches if b["idx"] != branch_idx])
        switch_branch_first_id = f"{new_dungeon_id}_b{switch_branch['idx']}_r0"
        _, hub_room_for_switch = _find_room(campaign, hub_id)
        _, switch_gate_far_room = _find_room(campaign, switch_branch_first_id)
        hub_room_for_switch["connections"].remove(switch_branch_first_id)
        switch_gate_far_room["connections"].remove(hub_id)
        switch_gate_far_room.setdefault("connections", []).append(hub_id)
        element = rng.choice(["fire", "cold", "lightning", "force", "radiant", "psychic", "poison", "necrotic", "earth", "physical"])
        switch_id = f"{new_dungeon_id}_switch_{switch_branch['idx']}"
        # A fire switch is sometimes flavored as a torch/brazier instead
        # of a crystal -- same mechanic either way (kind="switch",
        # element="fire"), purely cosmetic naming.
        switch_name = "a lit brazier" if element == "fire" and rng.random() < 0.5 else f"a {element} crystal"
        hub_room_for_switch.setdefault("lockables", []).append({"id": switch_id, "kind": "switch", "name": switch_name, "element": element})
        hub_room_for_switch.setdefault("locked_connections", {})[switch_branch_first_id] = switch_id

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
        leaf_room.setdefault("lockables", []).append({
            "id": f"{new_dungeon_id}_cache_{i}", "kind": "chest", "name": "a real, half-buried cache",
            "loot": {"healing_potion": rng.randint(1, 2)}, "gold": rng.randint(40, 220),
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

    # Miniboss (2026-09-01, per the ALTTP research: dungeons almost
    # always have one "you're not ready yet" encounter partway through,
    # distinct from both trash and the real climax). A non-boss
    # branch's own leaf gets one monster from the UPPER half of
    # target_band -- stronger than ordinary trash, still meaningfully
    # below the real boss, since it draws from the same pool bounded by
    # the same target_band ceiling rather than reaching above it.
    non_boss_leaves = branch_leaf_ids[:-1]
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

    # Bonus/optional room (2026-09-01, ALTTP research: reward exploration
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
    bonus_room.setdefault("lockables", []).append({
        "id": f"{new_dungeon_id}_bonus_cache", "kind": "chest", "name": "an unguarded stash",
        "loot": {"greater_healing_potion": 1}, "gold": rng.randint(60, 180),
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
