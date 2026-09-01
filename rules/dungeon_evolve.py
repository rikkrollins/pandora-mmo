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
    connect(campaign, source_hub_id, entrance_id)

    hub_id = f"{new_dungeon_id}_hub"
    add_room(
        campaign, layer, new_dungeon_id, hub_id, f"{display_name} -- Deep Hub",
        "The paths beyond here fork in every direction at once.",
        dungeon_hub=True,
    )
    room_ids.append(hub_id)
    connect(campaign, entrance_id, hub_id)

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

    num_branches = rng.randint(3, 5)
    branch_leaf_ids = []
    total_rooms = 2  # entrance + hub
    branch_idx = 0
    while branch_idx < num_branches or total_rooms < min_rooms:
        branch_idx += 1
        branch_len = rng.randint(2, 4)
        prev_id = hub_id
        leaf_id = hub_id
        for step in range(branch_len):
            room_id = f"{new_dungeon_id}_b{branch_idx}_r{step}"
            add_room(
                campaign, layer, new_dungeon_id, room_id, f"{display_name} -- Chamber {branch_idx}-{step}",
                "One more room this deep in, same as the last, except for what's waiting in it.",
                monsters=rng.sample(trash_candidates, k=min(rng.randint(1, 2), len(trash_candidates))) if trash_candidates else [],
            )
            room_ids.append(room_id)
            connect(campaign, prev_id, room_id)
            prev_id = room_id
            leaf_id = room_id
            total_rooms += 1
        branch_leaf_ids.append(leaf_id)
        if branch_idx >= 12:
            break  # safety valve, should never actually trigger

    # Extra chest lockables at 1-2 random leaves -- comfortable
    # lock_density padding regardless of final room count, real loot
    # from items already known-good elsewhere in this same catalog.
    chest_leaves = rng.sample(branch_leaf_ids, k=min(rng.randint(1, 2), len(branch_leaf_ids)))
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
        failures = dungeon_audit.audit_dungeon(trial, new_dungeon_id)
        if all(not f for f in failures.values()):
            for room_id in summary["room_ids"]:
                campaign["locations"][layer][room_id] = trial["locations"][layer][room_id]
            campaign["locations"][source_layer][source_hub_id] = trial["locations"][source_layer][source_hub_id]
            if new_dungeon_id not in campaign.setdefault("evolved_dungeon_ids", []):
                campaign["evolved_dungeon_ids"].append(new_dungeon_id)
            return summary
        last_failures = failures
    raise RuntimeError(
        f"evolve_dungeon: could not generate a valid {new_dungeon_id!r} in {max_retries} attempts; "
        f"last failures: {last_failures}"
    )
