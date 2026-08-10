"""
scripts/ai_party_simulation.py

Real-handler AI-party simulation for catching live gameplay bugs
(intent misclassification, narration failures, crashes) that only
show up when actions actually flow through bot.py's real handlers and
real Ollama narration -- the same category of bug CLAUDE.md's testing
convention says direct code reading will miss (HP not persisting, an
infinite loop, a narration mismatch were all only caught this way).

This is NOT part of the regression suite (tests/test_regression.py) --
those tests mock or avoid Ollama specifically so they're safe to run
any time. This script deliberately calls the real handlers with real
narration, which means it shares the single Ollama generation slot
with live player traffic. Per Coffee's explicit instruction
(2026-08-10) and a real live-overload incident the same day, it is
idle-gated: it refuses to start, and aborts mid-run, whenever a real
player has been active recently. See the reversed guidance in
memory file feedback_background_sim_runs_continuously.md.

Usage:
    cd ~/pandora_mmo
    python3 scripts/ai_party_simulation.py

Always uses a throwaway DB (tests/tmp/ai_party_sim.db), never the
live pandora_mmo.db. Prints a report at the end; does not commit,
push, or restart anything.
"""
import asyncio
import os
import re
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

IDLE_THRESHOLD_SECONDS = 300  # 5 minutes of real quiet before we'll touch Ollama
LOG_PATH = "bot_live_tmp.log"
TEST_DB_PATH = "tests/tmp/ai_party_sim.db"


def seconds_since_last_real_activity() -> float | None:
    """
    None means "no non-getUpdates activity found at all" (treat as
    idle). Otherwise seconds since the last real (non-getUpdates) log
    line -- a real player/owner action, not just polling.
    """
    if not os.path.exists(LOG_PATH):
        return None
    last_ts = None
    with open(LOG_PATH, "r", errors="ignore") as f:
        for line in f:
            if "getUpdates" in line:
                continue
            m = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3})", line)
            if m:
                last_ts = m.group(1)
    if last_ts is None:
        return None
    import datetime
    last_dt = datetime.datetime.strptime(last_ts, "%Y-%m-%d %H:%M:%S")
    now_dt = datetime.datetime.utcnow()
    return (now_dt - last_dt).total_seconds()


def require_idle(context: str) -> bool:
    idle_for = seconds_since_last_real_activity()
    if idle_for is not None and idle_for < IDLE_THRESHOLD_SECONDS:
        print(f"[ABORT] Real activity {idle_for:.0f}s ago (< {IDLE_THRESHOLD_SECONDS}s threshold) "
              f"at step: {context}. Stopping the simulation, not touching a shared Ollama slot "
              f"while a real player might be active.")
        return False
    return True


async def run_simulation():
    from tests.helpers import use_test_db, FakeUpdate, DummyContext
    use_test_db(TEST_DB_PATH)
    import bot
    import db
    import sessions

    report = {"steps": [], "exceptions": [], "suspicious": []}

    wizard_id, rogue_id = 910001, 910002
    db.create_character(
        telegram_user_id=wizard_id, chat_id=-999, name="SimWren", race="Human", char_class="Wizard",
        ability_scores={"strength": 8, "dexterity": 12, "constitution": 12,
                         "intelligence": 16, "wisdom": 10, "charisma": 10},
        hp_max=14, armor_class=12, gold=20, inventory={"rusty_dagger": 1}, known_spells=["fire_bolt"],
        current_location="crossroads_tavern",
    )
    db.create_character(
        telegram_user_id=rogue_id, chat_id=-999, name="SimBram", race="Human", char_class="Rogue",
        ability_scores={"strength": 10, "dexterity": 16, "constitution": 12,
                         "intelligence": 10, "wisdom": 10, "charisma": 10},
        hp_max=16, armor_class=13, gold=20, inventory={"shortsword": 1, "silvered_dagger": 1}, known_spells=[],
        current_location="crossroads_tavern",
    )
    db.update_character(rogue_id, -999, subclass="Assassin", level=10, backstab_proficiency_pct=100.0)
    db.equip_item(rogue_id, -999, "shortsword")
    # Real bug in THIS harness, caught by its own first run (2026-08-10):
    # without this, ai.npc_agent's module-level _NPCS registry is empty
    # in a fresh process, so "talk to Grimsby" silently fell through
    # talk_npc's "not npc_id in _NPCS" branch into the fast, no-Ollama
    # _do_examine fallback instead of exercising real NPC dialogue --
    # returned instantly (0.0s) and looked like a pass, not a bug, but
    # never actually tested the thing it claimed to test. bot.py itself
    # calls this once at real startup (line ~22400); tests that touch
    # talk_npc call it too (see tests/test_regression.py).
    bot.setup_default_npcs()

    async def step(label: str, coro_fn):
        if not require_idle(label):
            return False
        start = time.time()
        sink = []
        try:
            await coro_fn(sink)
            elapsed = time.time() - start
            text = "\n".join(sink)
            flags = []
            if not text.strip():
                flags.append("empty response")
            for bad in ("Traceback", "genuinely overloaded", "Exception", "KeyError", "AttributeError"):
                if bad in text:
                    flags.append(f"contains '{bad}'")
            report["steps"].append({"label": label, "elapsed_s": round(elapsed, 1), "flags": flags,
                                     "excerpt": text[:300]})
            if flags:
                report["suspicious"].append({"label": label, "flags": flags, "excerpt": text[:500]})
            print(f"[OK] {label} ({elapsed:.1f}s){' -- FLAGGED: ' + ','.join(flags) if flags else ''}")
        except Exception:
            tb = traceback.format_exc()
            report["exceptions"].append({"label": label, "traceback": tb})
            print(f"[EXCEPTION] {label}\n{tb}")
        return True

    async def do_adv(user_id, text):
        async def inner(sink):
            await bot.adventure_master_handler(FakeUpdate(user_id, text, sink), DummyContext())
        return inner

    # --- Cheap, deterministic steps first (no Ollama call at all) ---
    if not await step("look around", await do_adv(wizard_id, "look around")):
        return report
    if not await step("check inventory", await do_adv(wizard_id, "check my inventory")):
        return report

    # --- Combat sweep FIRST among the real-narration steps (2026-08-10,
    # after 3 real runs all aborted on real player activity before
    # reaching it): idle windows on a live bot are short and get
    # interrupted often, so the steps most likely to actually catch a
    # live bug (narration wording, targeting, damage math -- the exact
    # class of bug the last several real fixes this session were) go
    # first, while the already-recently-verified non-combat steps
    # (examine, NPC dialogue) move to the end where an abort costs less. ---
    if not require_idle("combat setup"):
        return report
    sessions.end_session(-999)
    wizard = db.get_character(wizard_id, -999)
    wizard["telegram_user_id"] = wizard_id
    rogue = db.get_character(rogue_id, -999)
    rogue["telegram_user_id"] = rogue_id
    enemy = {"telegram_user_id": -2_500_070, "name": "SimGoblin", "strength": 10, "dexterity": 10,
             "hp_current": 60, "hp_max": 60, "armor_class": 8, "is_ai": 1, "monster_key": "goblin"}
    session = sessions.start_session(-999, [wizard, rogue, enemy],
                                      {wizard_id: "party", rogue_id: "party", -2_500_070: "enemy"})
    session.turn_order = [wizard_id, rogue_id, -2_500_070]
    session.current_turn_index = 0

    async def cast_spell(sink):
        await bot._do_cast_spell(FakeUpdate(wizard_id, "cast fire bolt at SimGoblin", sink),
                                  "cast fire bolt at SimGoblin")
    if not await step("wizard casts fire bolt", cast_spell):
        return report

    async def rogue_attack(sink):
        session.current_turn_index = session.turn_order.index(rogue_id)
        await bot._do_attack(FakeUpdate(rogue_id, "I attack SimGoblin", sink), "I attack SimGoblin")
    if not await step("rogue (assassin) attacks -- Backstab narration check", rogue_attack):
        return report

    async def throw_weapon(sink):
        session.current_turn_index = session.turn_order.index(wizard_id)
        await bot._do_throw_weapon(FakeUpdate(wizard_id, "throw rusty dagger at SimGoblin", sink),
                                    "throw rusty dagger at SimGoblin")
    if not await step("wizard throws dagger", throw_weapon):
        return report

    sessions.end_session(-999)

    if not await step("examine item in inventory", await do_adv(wizard_id, "examine the rusty dagger in my inventory")):
        return report
    if not await step("talk to Grimsby", await do_adv(wizard_id, "talk to Grimsby")):
        return report
    if not await step("rest", await do_adv(wizard_id, "I rest")):
        return report

    return report


def print_report(report):
    print("\n" + "=" * 60)
    print(f"SIMULATION REPORT -- {len(report['steps'])} step(s) completed")
    print("=" * 60)
    for s in report["steps"]:
        flag_str = f" [FLAGGED: {', '.join(s['flags'])}]" if s["flags"] else ""
        print(f"  {s['label']}: {s['elapsed_s']}s{flag_str}")
    if report["exceptions"]:
        print(f"\n{len(report['exceptions'])} EXCEPTION(S):")
        for e in report["exceptions"]:
            print(f"  --- {e['label']} ---")
            print(e["traceback"])
    if report["suspicious"]:
        print(f"\n{len(report['suspicious'])} SUSPICIOUS OUTPUT(S):")
        for s in report["suspicious"]:
            print(f"  --- {s['label']} ({', '.join(s['flags'])}) ---")
            print(f"  {s['excerpt']}")
    if not report["exceptions"] and not report["suspicious"]:
        print("\nNothing flagged.")


def cleanup():
    for suffix in ("", "-journal", ".sessions_snapshot.json"):
        path = TEST_DB_PATH + suffix
        if os.path.exists(path):
            os.remove(path)


if __name__ == "__main__":
    if not require_idle("startup"):
        sys.exit(1)
    report = asyncio.run(run_simulation())
    print_report(report)
    cleanup()
