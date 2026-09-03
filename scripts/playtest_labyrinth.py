#!/usr/bin/env python3
"""
scripts/playtest_labyrinth.py

Phase L4 follow-up (2026-09-03, per Coffee: "generate some dungeons/
labyrinths and have your Test environment play it with AI characters
so you can make levels like the ones in my example... and generate new
creative ones... use that info to self improve the generators... and
then repeat... update each segment as we go... and when i ask").

Drives a real, throwaway AI-controlled character through a live
Labyrinth run using the EXACT SAME real pipeline a live AI companion
uses: ai.autonomous_player.choose_next_action (a real Ollama call,
grounded via bot._build_labyrinth_ai_situation_facts -- the same real
room text a human player sees) -> ai.intent_parser.parse_intents ->
bot._dispatch_intent. Not a synthetic simulation -- if the real bot
would misfire on some action, this will misfire on it too, which is
the point.

Real cost: 2 Ollama calls per turn (one to choose an action, one to
classify it), each tens of seconds on this CPU-only box -- a 15-turn
playtest is genuinely 10-20+ minutes. Never run this while a real
player might be active (same single-slot-contention rule every other
Ollama-touching test/tool in this repo already follows).

Uses a fresh, throwaway SQLite file (never the live pandora_mmo.db) --
tests.helpers.use_test_db, the same safe pattern every test in this
repo already uses, which also isolates sessions.py's own snapshot file
so this can never clobber a live player's real combat state.

Usage:
    python3 scripts/playtest_labyrinth.py --turns 15
    python3 scripts/playtest_labyrinth.py --turns 20 --verbose
"""
import argparse
import asyncio
import random
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tests.helpers import DummyContext, FakeUpdate, use_test_db  # noqa: E402


async def run_playtest(turns: int, verbose: bool) -> dict:
    db_path = f"tests/tmp/playtest_labyrinth_{random.randint(0, 10**9)}.db"
    use_test_db(db_path)

    import bot
    import db
    from ai.autonomous_player import choose_next_action
    from ai.intent_parser import parse_intents

    chat_id = -777000 - random.randint(0, 999)
    companion = db.create_ai_companion(
        chat_id, "PlaytestScout", "Human", "Fighter",
        ability_scores={"strength": 14, "dexterity": 14, "constitution": 14,
                         "intelligence": 10, "wisdom": 12, "charisma": 10},
        hp_max=30, armor_class=14, gold=0, inventory={},
    )
    ai_user_id = companion["telegram_user_id"]
    db.update_character(ai_user_id, chat_id, current_location="the_colosseum", defeated_monsters=["colosseum_champion"])
    await bot._do_enter_labyrinth(FakeUpdate(ai_user_id, "", [], chat_id=chat_id))

    rooms_visited: set[str] = set()
    mechanics_seen: set[str] = set()
    stuck_room_streak = 0
    last_room_id = None
    last_action = None
    turn_log = []

    for turn in range(1, turns + 1):
        # Real live finding (2026-09-03, this tool's own first real run):
        # every choose_next_action call timed out (200s) under real
        # Ollama contention, silently falling back to a safe default
        # action every turn -- the harness never crashed, but produced
        # zero meaningful playtest data (stuck in the hub for all 5
        # turns). Checking bot._ollama_congested() (the same real load-
        # average proxy the hourly world-tick already uses to skip
        # purely discretionary Ollama work) before each turn stops the
        # run honestly instead of burning through every remaining turn
        # on guaranteed-fallback noise.
        if bot._ollama_congested():
            turn_log.append(f"[{turn}] Ollama is under real load right now -- stopping early rather than producing fallback-only noise")
            break
        character = db.get_character(ai_user_id, chat_id)
        if character is None:
            turn_log.append(f"[{turn}] character vanished -- stopping")
            break
        character["telegram_user_id"] = ai_user_id
        character["chat_id"] = chat_id

        run = db.get_labyrinth_run(chat_id, f"solo:{ai_user_id}")
        if run is None:
            turn_log.append(f"[{turn}] no active run (left/died?) -- stopping")
            break
        room = run["rooms"].get(run["current_room_id"])
        if room is not None:
            rooms_visited.add(room["id"])
            if room.get("is_miniboss_room"):
                mechanics_seen.add("miniboss")
            if room.get("hazard"):
                mechanics_seen.add("hazard")
            if room.get("warps"):
                mechanics_seen.add("warp")
            if room.get("collapsing_connections"):
                mechanics_seen.add("collapse_or_carry_puzzle")
            for lk in room.get("lockables", []):
                if lk.get("kind") in ("chest",):
                    mechanics_seen.add("chest")
                elif lk.get("kind") in ("switch", "multi_switch_gate"):
                    mechanics_seen.add("switch")
                elif lk.get("kind") == "pressure_plate":
                    mechanics_seen.add("pressure_plate")
                elif lk.get("kind") == "pillar":
                    mechanics_seen.add("carry_puzzle")
                elif lk.get("kind") == "hint_statue":
                    mechanics_seen.add("hint_statue")
            if room["id"] == last_room_id:
                stuck_room_streak += 1
            else:
                stuck_room_streak = 0
            last_room_id = room["id"]
            if room.get("is_checkpoint"):
                turn_log.append(f"[{turn}] reached the checkpoint after {turn} turns")
                break

        situation_facts = bot._build_labyrinth_ai_situation_facts(character, chat_id)
        action_text = await asyncio.to_thread(choose_next_action, character, "a cautious, thorough explorer", situation_facts, last_action)
        action_text = action_text.replace("[", "").replace("]", "").strip()
        last_action = action_text

        sink: list[str] = []
        update = FakeUpdate(ai_user_id, action_text, sink, chat_id=chat_id)
        intents = parse_intents(action_text)
        for intent in intents:
            await bot._dispatch_intent(update, DummyContext(), intent, action_text)

        line = f"[{turn}] room={room['id'] if room else '?'} action={action_text!r} -> {' | '.join(sink) or '(silent)'}"
        turn_log.append(line)
        if verbose:
            print(line)

        if stuck_room_streak >= 4:
            turn_log.append(f"[{turn}] STUCK: same room {stuck_room_streak + 1}x in a row with no progress")
            break
        refreshed = db.get_character(ai_user_id, chat_id)
        if refreshed and (refreshed.get("hp_current", 1) <= 0 or refreshed.get("is_dead")):
            turn_log.append(f"[{turn}] the playtest character went down")
            break

    return {
        "db_path": db_path,
        "turns_run": turn,
        "rooms_visited": len(rooms_visited),
        "mechanics_seen": sorted(mechanics_seen),
        "reached_checkpoint": any("reached the checkpoint" in l for l in turn_log),
        "got_stuck": any("STUCK" in l for l in turn_log),
        "died": any("went down" in l for l in turn_log),
        "log": turn_log,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--turns", type=int, default=15)
    parser.add_argument("--verbose", action="store_true", help="print each turn as it happens, not just the final summary")
    args = parser.parse_args()

    result = asyncio.run(run_playtest(args.turns, args.verbose))

    print("=" * 70)
    print(f"Playtest summary ({result['turns_run']} turns run):")
    print(f"  Rooms visited: {result['rooms_visited']}")
    print(f"  Mechanics encountered: {', '.join(result['mechanics_seen']) or '(none)'}")
    print(f"  Reached checkpoint: {result['reached_checkpoint']}")
    print(f"  Got stuck: {result['got_stuck']}")
    print(f"  Died: {result['died']}")
    print(f"  Throwaway DB: {result['db_path']} (safe to delete)")
    if not args.verbose:
        print("-" * 70)
        for line in result["log"]:
            print(line)


if __name__ == "__main__":
    main()
