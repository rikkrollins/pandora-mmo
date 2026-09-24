#!/usr/bin/env python3
"""
scripts/generate_narration_cache.py

Pre-generates and caches static, party-addressed narration for chapter
cutscenes (arc openings) and generic climactic-quest endings, so real
gameplay never has to make a live, timeout-risking Ollama call for
content that reads identically for every player anyway.

Real live request (2026-09-24, Coffee: "find a way so we can still
have story line cutscenes" -> "pre-write each chapter's cutscene once,
reuse for everyone" -- the real per-character live narrate_arc_opening/
narrate_chapter_climax calls were measurably timing out under real
hypervisor CPU steal time on this VPS).

Run this OFFLINE, with no live-player time pressure -- each call can
genuinely take minutes under real load, same latency this whole fix is
working around. Writes results directly into campaigns/default/
campaign.json's own "narration_cache" key, saving after EVERY single
generation (not just at the end) so a real interruption partway
through never loses already-completed work. Safe to re-run any number
of times: skips any (category, key) already cached unless --force is
given.

Usage:
    python3 scripts/generate_narration_cache.py
    python3 scripts/generate_narration_cache.py --force
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import items as items_module
from ai.dm_agent import narrate_arc_opening_generic, narrate_chapter_climax

CAMPAIGN_PATH = "campaigns/default/campaign.json"

# Quests with their own fully hand-written, zero-Ollama-call ending
# already (see bot.py's own quest-completion elif chain, right after
# "climax_narration = \"\"") -- generating a cached entry for these
# would produce real narration nothing in the live game ever reads.
_HAND_WRITTEN_CLIMAX_QUEST_IDS = {
    "kess_first_reckoning", "kess_the_unbound_reckoning", "the_kept_shrines_vigil",
    "the_scouting_grounds_warning", "clear_the_warrens", "the_hush_stage3_the_unspoken",
    "the_original_spires_reckoning", "the_suns_thresholds_secret",
    "the_true_paymasters_reckoning", "the_sources_reckoning", "the_old_keeps_warden",
}


def build_reward_text(quest: dict) -> str:
    """Same real construction bot.py's own quest-completion handler uses -- see its own reward_parts block."""
    parts = []
    if quest.get("reward_xp"):
        parts.append(f"{quest['reward_xp']} XP")
    if quest.get("reward_gold"):
        parts.append(f"{quest['reward_gold']} gold")
    if quest.get("reward_item"):
        item = items_module.get_item(quest["reward_item"])
        if item:
            parts.append(item["name"])
    return ", ".join(parts) or "real progress, if nothing material"


def save(campaign: dict) -> None:
    with open(CAMPAIGN_PATH, "w", encoding="utf-8") as f:
        json.dump(campaign, f, indent=2)
        f.write("\n")


# Real live gap found generating the first real batch (2026-09-24): a
# real Ollama timeout under load falls back to a flat, honest-but-plain
# template line -- perfectly fine as a LIVE, one-off degradation, but
# catastrophic to cache permanently: it would become the PERMANENT
# result shown to every future player for that story beat, forever,
# defeating the entire point of pre-writing real narration. Retries a
# few times before giving up; if every attempt falls back, the entry is
# left UNCACHED (not written at all) so a later re-run of this same
# script tries it again fresh, rather than ever locking in degraded text.
MAX_ATTEMPTS = 4


def main() -> None:
    force = "--force" in sys.argv
    with open(CAMPAIGN_PATH, encoding="utf-8") as f:
        campaign = json.load(f)

    cache = campaign.setdefault("narration_cache", {})
    arc_cache = cache.setdefault("arc_opening", {})
    climax_cache = cache.setdefault("chapter_climax", {})

    for arc_id, arc in campaign["story_arcs"].items():
        if not force and arc_id in arc_cache:
            print(f"[skip] arc_opening/{arc_id} already cached")
            continue
        arc_quests = arc.get("quests", [])
        first_quest = campaign["quests"].get(arc_quests[0]) if arc_quests else None
        quest_title = first_quest["title"] if first_quest else arc["title"]
        fallback = f"A new chapter begins. {arc['description']}"
        text = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            print(f"[generate] arc_opening/{arc_id} (attempt {attempt}/{MAX_ATTEMPTS}) ...", flush=True)
            candidate = narrate_arc_opening_generic(arc["title"], arc["description"], quest_title)
            if candidate != fallback:
                text = candidate
                break
            print("  -> real call timed out/fell back, retrying" if attempt < MAX_ATTEMPTS else "  -> still falling back, giving up for now (uncached)", flush=True)
        if text is None:
            continue
        arc_cache[arc_id] = text
        print(f"  -> {text[:100]}", flush=True)
        save(campaign)

    for quest_id, quest in campaign["quests"].items():
        if quest.get("weight") != "climactic" or quest_id in _HAND_WRITTEN_CLIMAX_QUEST_IDS:
            continue
        if not force and quest_id in climax_cache:
            print(f"[skip] chapter_climax/{quest_id} already cached")
            continue
        reward_text = build_reward_text(quest)
        fallback = f"This was a turning point. {quest['description']}"
        text = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            print(f"[generate] chapter_climax/{quest_id} (attempt {attempt}/{MAX_ATTEMPTS}) ...", flush=True)
            candidate = narrate_chapter_climax(quest["title"], quest["description"], reward_text)
            if candidate != fallback:
                text = candidate
                break
            print("  -> real call timed out/fell back, retrying" if attempt < MAX_ATTEMPTS else "  -> still falling back, giving up for now (uncached)", flush=True)
        if text is None:
            continue
        climax_cache[quest_id] = text
        print(f"  -> {text[:100]}", flush=True)
        save(campaign)

    print("Done.")


if __name__ == "__main__":
    main()
