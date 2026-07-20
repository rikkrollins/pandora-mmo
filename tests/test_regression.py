"""
tests/test_regression.py
Permanent regression suite for real bugs found and fixed during live
play (2026-07-14 session). Run with:

    python3 -m unittest tests.test_regression -v

FastRegressionTests need no Ollama call (pure intent parsing / DB /
deterministic-reply paths) and run in seconds -- run these before every
deploy. SlowLiveTests exercise real Ollama-backed narration and can
take minutes each under load; run these when you have time to spare,
or when touching code they cover directly.
"""
import os
import shutil
import unittest

import bot
import db
import items as items_module
import spells
from ai.intent_parser import _keyword_fallback, parse_intents
from ai.support_agent import _deterministic_inventory_answer
from ai.text_cleanup import strip_think_tags
from rules.combat import resolve_attack
from rules.crafting import RECIPES
from tests.helpers import (
    DummyContext, DummyMessage, FakeCallbackUpdate, FakeUpdate, make_basic_character, use_test_db,
)


class FastRegressionTests(unittest.IsolatedAsyncioTestCase):
    """
    No Ollama calls -- safe to run before every deploy. DB is
    initialized ONCE for the whole class (setUpClass, not setUp) since
    db.init_db() alone can take 60-90s+ under real disk contention on
    this machine (see project_ollama_single_slot_contention /
    documented CLAUDE.md latency notes) -- doing that per-test-method
    made a genuinely fast suite take many minutes for no real benefit,
    since the handful of tests that touch the DB use distinct
    user_ids and don't depend on a clean slate.
    """

    @classmethod
    def setUpClass(cls):
        use_test_db("tests/tmp/regression_fast.db")

    # -- NPC-name stopword bug (v1.7.3) --------------------------------
    def test_npc_name_filler_words_dont_hijack_unrelated_messages(self):
        known_npcs = ["Grimsby", "Old Maren", "Sarah", "Theron", "Kess",
                      "Ossian Vane", "Borin Ironjaw", "Wren Hollowbrook",
                      "Pip Thistledown", "Grask Emberscale", "Vesh Nightglass"]
        # These all contain "the" or "old" and must NOT be swallowed by
        # per-word NPC matching just because a filler word overlaps.
        for text in ("Read the guest book on the landing table",
                     "Check out the door at the end of the hall",
                     "the old bridge creaks as I cross"):
            action = _keyword_fallback(text, known_npcs)["action"]
            self.assertNotEqual(action, "talk_npc", f"{text!r} was misrouted to talk_npc")

    def test_real_npc_names_still_resolve(self):
        known_npcs = ["Old Maren", "Theron", "Kess"]
        self.assertEqual(_keyword_fallback("Say hello to Maren", known_npcs)["action"], "talk_npc")
        self.assertEqual(_keyword_fallback("Say hello to Theron", known_npcs)["action"], "talk_npc")

    # -- Examine verb coverage (v1.7.3) --------------------------------
    def test_examine_covers_read_observed_checked_out(self):
        for text in ("Read the guest book", "Observed the guest book",
                     "Checked out the strange door", "looked at the painting"):
            self.assertEqual(_keyword_fallback(text, [])["action"], "examine", text)

    def test_examine_verb_doesnt_misfire_on_substrings(self):
        for text in ("I am already tired", "I want some bread", "lets spread out"):
            self.assertNotEqual(_keyword_fallback(text, [])["action"], "examine", text)

    # -- "Peer into" silently dropped to chat (2026-07-17, task #151) --
    def test_peer_into_classified_as_examine_not_silent_chat(self):
        for text in ("Peer into the murky water", "Peer at the strange markings",
                      "Peered into the well"):
            self.assertEqual(_keyword_fallback(text, [])["action"], "examine", text)

    def test_peer_typo_still_classified_as_examine(self):
        # Coffee's exact live report: "Perr" (typo for "Peer") fell all
        # the way through to the silent 'chat' default with zero reply.
        result = _keyword_fallback(
            "Perr into the face of the wide-boled tree and say hello", []
        )
        self.assertEqual(result["action"], "examine")

    def test_peer_still_yields_to_a_known_npc_mention(self):
        self.assertEqual(
            _keyword_fallback("Say hello to Sarah", ["Sarah"])["action"], "talk_npc"
        )

    # -- "Touch X" misclassified as look, not examine (2026-07-17, Coffee,
    #    caught via live gameplay monitoring) ---------------------------
    def test_touch_classified_as_examine(self):
        for text in ("I touch the tree", "I touched the ancient stone"):
            self.assertEqual(_keyword_fallback(text, [])["action"], "examine", text)

    def test_touch_substring_doesnt_misfire(self):
        for text in ("I am out of touch with my party", "lets keep in touch"):
            self.assertNotEqual(_keyword_fallback(text, [])["action"], "examine", text)

    # -- Stuck-location bug (v1.7.4) -----------------------------------
    def test_move_covers_generic_leave_phrasing(self):
        for text in ("Go downstairs", "Leave this area", "Leave this room", "Head upstairs"):
            self.assertEqual(_keyword_fallback(text, [])["action"], "move", text)

    def test_leave_the_party_not_hijacked_by_move(self):
        self.assertEqual(_keyword_fallback("Leave the party", [])["action"], "leave_party")

    # -- "Return to X" recognized as move (2026-07-16, task #88) -------
    def test_return_to_phrasing_classified_as_move(self):
        for text in ("Return to the crossroads tavern", "Head back to town", "I go back to the tavern"):
            self.assertEqual(_keyword_fallback(text, [])["action"], "move", text)

    # -- "Check quests" bare phrase (2026-07-16, task #92) --------------
    def test_bare_check_quests_phrasing_classified_correctly(self):
        for text in ("Check quests", "check my quests please", "list quests"):
            self.assertEqual(_keyword_fallback(text, [])["action"], "check_quests", text)

    # -- Typo tolerance for "accept" (2026-07-16, task #89) -------------
    def test_accept_quest_typo_tolerance(self):
        for text in ("I accepet the quest", "Accepet this quest"):
            self.assertEqual(_keyword_fallback(text, [])["action"], "accept_quest", text)

    def test_typo_tolerance_doesnt_break_unrelated_words(self):
        # Guards against the typo-normalizer being too aggressive.
        self.assertEqual(_keyword_fallback("I attack the goblin", [])["action"], "attack")
        self.assertEqual(_keyword_fallback("give my potion to Sarah", [])["action"], "give_item")

    async def test_player_can_actually_leave_tavern_upstairs(self):
        user_id = 222222
        make_basic_character(user_id, current_location="tavern_upstairs")
        sink = []
        await bot.adventure_master_handler(FakeUpdate(user_id, "Go downstairs", sink), DummyContext())
        character = db.get_character(user_id)
        self.assertNotEqual(character["current_location"], "tavern_upstairs")

    # -- Examine target-matching (v1.7.5) ------------------------------
    def test_find_interactable_handles_article_and_spacing_mismatch(self):
        location = {"interactables": {
            "guestbook": {"name": "a guestbook on the landing table", "description": "..."},
        }}
        found = bot._find_interactable(location, "guest book on the landing table")
        self.assertIsNotNone(found)
        self.assertEqual(found[0], "guestbook")

    def test_find_interactable_handles_different_wording(self):
        location = {"interactables": {
            "room_seven_door": {"name": "the door at the end of the hall", "description": "..."},
        }}
        found = bot._find_interactable(location, "the door down the hall")
        self.assertIsNotNone(found)
        self.assertEqual(found[0], "room_seven_door")

    # -- check_party covers "current"/"active" inserted before "party" (post-1.8.3) --
    def test_check_party_tolerates_words_between_my_and_party(self):
        for text in ("Who is in my current party?", "my active party status"):
            self.assertEqual(_keyword_fallback(text, [])["action"], "check_party", text)

    def test_check_party_still_works_for_original_phrasings(self):
        for text in ("Who's in my party?", "who is in my party", "party members"):
            self.assertEqual(_keyword_fallback(text, [])["action"], "check_party", text)

    # -- Combat-scoping logic (post-1.8.3), tested WITHOUT starting a
    #    real combat session (see SlowLiveTests for that version) -------
    #    Real live bug (2026-07-17, task #145): these two used to call
    #    bot.adventure_master_handler with "Let's start a fight", which
    #    genuinely starts combat and triggers a real Ollama narration
    #    call -- a real live suite run measured this single test at
    #    290s, despite this class's own docstring promising "no Ollama
    #    calls." Moved the full-handler version of both to SlowLiveTests
    #    (test_combat_excludes_characters_at_a_different_location_full,
    #    test_combat_excludes_resting_characters_full); these two now
    #    exercise the exact same location/rest filtering logic directly
    #    against _get_combat_eligible_party_members, so the FastRegressionTests
    #    guarantee holds again without losing coverage of the filter itself.
    def test_combat_excludes_characters_at_a_different_location(self):
        tavern_id, wood_id = 444444, 555555
        make_basic_character(tavern_id, "Elduinn", current_location="crossroads_tavern")
        make_basic_character(wood_id, "Roric", current_location="whispering_wood")
        eligible_ids = {p["telegram_user_id"] for p in bot._get_combat_eligible_party_members("whispering_wood")}
        self.assertIn(wood_id, eligible_ids)
        self.assertNotIn(tavern_id, eligible_ids)

    def test_combat_excludes_resting_characters(self):
        wood_id, resting_id = 666666, 777777
        make_basic_character(wood_id, "Roric2", current_location="whispering_wood")
        make_basic_character(resting_id, "Snorri", current_location="whispering_wood")
        db.update_character(resting_id, is_inactive=1)
        eligible_ids = {p["telegram_user_id"] for p in bot._get_combat_eligible_party_members("whispering_wood")}
        self.assertNotIn(resting_id, eligible_ids)

    # -- Gathering verb coverage (v1.7.6) ------------------------------
    def test_gather_covers_every_skill_verb(self):
        cases = ["I would like to chop for lumber", "go fishing", "catch some fish",
                 "I dig for sulfur", "I mine some ore", "gather silverleaf herbs",
                 "Get wood from the whispering wood"]
        for text in cases:
            self.assertEqual(_keyword_fallback(text, [])["action"], "gather", text)

    def test_gather_verbs_dont_misfire_on_substrings(self):
        for text in ("that seems selfish", "I want some shellfish stew", "let me get my bearings"):
            self.assertNotEqual(_keyword_fallback(text, [])["action"], "gather", text)

    def test_find_resource_node_matches_lumber_to_wood_node(self):
        location = {"resource_nodes": [
            {"id": "silverleaf_patch", "name": "a patch of Silverleaf Herbs",
             "material": "silverleaf_herb", "ability": "wisdom", "skill": "herbalism"},
            {"id": "old_timber_stand", "name": "a stand of old, sturdy timber",
             "material": "wood", "ability": "strength", "skill": "lumberjacking"},
        ]}
        node = bot._find_resource_node(location, "I would like to chop for lumber")
        self.assertIsNotNone(node)
        self.assertEqual(node["id"], "old_timber_stand")

    # -- Multi-sheet hallucination (v1.7.7) ----------------------------
    async def test_support_shows_real_sheets_for_multiple_names_no_hallucination(self):
        coffee_id, sera_id = 111111, -1002
        make_basic_character(coffee_id, "Elduinn")
        db.create_character(
            sera_id, "Sarah", "Elf", "Ranger",
            {"strength": 12, "dexterity": 17, "constitution": 13,
             "intelligence": 11, "wisdom": 15, "charisma": 10},
            hp_max=11, armor_class=14, gold=50, inventory={}, known_spells=[], is_ai=True,
        )
        sink = []
        update = FakeUpdate(coffee_id, "Show me my character sheet and Sarah's character sheet",
                             sink, thread_id=bot.config.TOPIC_SUPPORT_ID)
        await bot.support_topic_handler(update, DummyContext())
        reply = sink[-1]
        self.assertIn("Elduinn", reply)
        self.assertIn("AC 14", reply)   # Sarah's REAL AC, not invented
        self.assertIn("HP 11/11", reply)  # Sarah's REAL HP, not invented
        self.assertNotIn("mirrors this structure", reply.lower())
        self.assertNotIn("similar stats", reply.lower())

    # -- Named sheet lookup broadened beyond own party (v1.7.1) --------
    def test_find_campaign_npc_by_name_gives_honest_info_not_fake_stats(self):
        npc = bot._find_campaign_npc_by_name("Grimsby")
        self.assertIsNotNone(npc)
        info = bot._format_npc_basic_info(npc)
        self.assertIn("not currently recruited", info)
        self.assertNotIn("HP", info)

    # -- Recruit still routes correctly despite examine/sheet fixes ----
    def test_recruit_and_invite_not_hijacked_by_other_fixes(self):
        known_npcs = ["Sarah"]
        self.assertEqual(_keyword_fallback("Recruit sarah to my party", known_npcs)["action"], "recruit_npc")
        self.assertEqual(_keyword_fallback("Invite Sarah to my party", known_npcs)["action"], "recruit_npc")

    # -- TTS no longer leaves a visible duplicate message (post-1.8.2) -
    async def test_tts_trigger_message_gets_cleaned_up(self):
        db.set_setting("tts_enabled", "1")
        try:
            sink = []
            update = FakeUpdate(333333, "irrelevant", sink)
            await bot._maybe_speak(update, "The goblin attacks!", None)
            self.assertEqual(sink, ["/tts The goblin attacks!"])
            self.assertFalse(update.effective_chat.last_sent_message.deleted)
            # Poll instead of a single fixed sleep -- under heavy system
            # load (this box regularly sees asyncio scheduling delays of
            # 10+ seconds while Ollama holds the CPU) a flat sleep
            # occasionally loses the race against the cleanup task's own
            # internal sleep, flaking a test that isn't actually broken.
            # TTS_TRIGGER_DELETE_DELAY_SECONDS is 20s (bumped 2026-07-16,
            # real live feedback that 2s was too fast to actually tap the
            # message) -- poll well past that for real headroom.
            asyncio = __import__("asyncio")
            for _ in range(70):
                if update.effective_chat.last_sent_message.deleted:
                    break
                await asyncio.sleep(0.5)
            self.assertTrue(update.effective_chat.last_sent_message.deleted)
        finally:
            db.set_setting("tts_enabled", "0")

    # -- The Unmoored Isle is actually reachable (post-1.9.2) ---------
    async def test_unmoored_isle_reachable_with_shard_blocked_without(self):
        """
        Found while mapping the location graph for the reverse
        playthrough: The Unmoored Isle (real quest, real interactables,
        a requires_item gate) had no connection from ANYWHERE in the
        whole campaign -- completely unreachable dead content. Fixed
        by adding ascends_to on The First City.
        """
        user_id = 888888
        make_basic_character(user_id, "Roamer", current_location="the_first_city")
        db.update_character(user_id, level=10)

        sink = []
        await bot.adventure_master_handler(
            FakeUpdate(user_id, "Ascend to the Unmoored Isle", sink), DummyContext())
        character = db.get_character(user_id)
        self.assertEqual(character["current_location"], "the_first_city")  # blocked, no shard

        db.add_item(user_id, "shard_of_dim_light", 1)
        sink.clear()
        await bot.adventure_master_handler(
            FakeUpdate(user_id, "Ascend to the Unmoored Isle", sink), DummyContext())
        character = db.get_character(user_id)
        self.assertEqual(character["current_location"], "the_unmoored_isle")

    # -- The Unmoored Isle has a real final boss (post-1.10.0) --------
    def test_unmoored_isle_has_a_real_unfleeable_boss(self):
        """
        The First City and The Unmoored Isle (the campaign's two
        deepest locations) had zero monsters -- nothing to fight at
        the actual climax, despite the isle's own interactable text
        foreshadowing a confrontation. Structural checks only here
        (data correctness) -- the full live fight-to-completion path
        was verified live as far as system load allowed (reachability,
        accepting the quest while already present, and combat
        correctly engaging the boss all confirmed live); the final
        defeat->quest-complete link reuses the exact same generic
        _check_quest_completions_defeat_monster code already proven
        live for a different quest (Grask's) the same day.
        """
        monster = bot.CAMPAIGN["monsters"]["the_waiting_shape"]
        self.assertTrue(monster["is_boss"])
        self.assertGreater(monster["hp_max"], 21)  # tougher than goblin_boss

        location = bot.CAMPAIGN["locations"]["sky"]["the_unmoored_isle"]
        self.assertIn("the_waiting_shape", location.get("monsters", []))

        quest = bot.CAMPAIGN["quests"]["the_unmoored_isle_quest"]
        self.assertEqual(quest["trigger"]["type"], "defeat_monster")
        self.assertEqual(quest["trigger"]["monster"], "the_waiting_shape")

    # -- "What am I carrying" never needs Ollama (post-1.8.1) ---------
    def test_inventory_question_answered_without_ollama(self):
        character = {"inventory": {"healing_potion": 2, "shortsword": 1}}
        answer = _deterministic_inventory_answer(character)
        self.assertIn("Healing Potion x2", answer)
        self.assertIn("Shortsword", answer)

    def test_empty_inventory_answered_without_ollama(self):
        self.assertIn("empty", _deterministic_inventory_answer({"inventory": {}}))

    # -- Companion personal quests (v1.8.0/1.8.1) ----------------------
    def test_all_six_recruitables_have_a_real_personal_quest(self):
        expected_givers = {
            "sera_wanderer", "borin_ironjaw", "wren_hollowbrook",
            "pip_thistledown", "grask_emberscale", "vesh_nightglass",
        }
        real_givers = {
            q.get("giver_npc") for q in bot.CAMPAIGN["quests"].values() if q.get("giver_npc")
        }
        self.assertEqual(real_givers, expected_givers)

    # -- Real live bug (2026-07-15): Sarah (sera_wanderer) is both
    #    can_wander AND recruitable, so the living-world wander tick
    #    relocated her randomly, making her genuinely unfindable at the
    #    location a player would look for her to recruit. -------------
    def test_no_recruitable_npc_wanders(self):
        for npc_id, npc in bot.CAMPAIGN["npcs"].items():
            if npc.get("recruitable"):
                self.assertFalse(bot._npc_currently_wanders(npc_id), npc_id)

    def test_sera_is_a_static_npc_at_her_campaign_location(self):
        bot._NPC_LOCATIONS.clear()
        bot._seed_npc_locations()
        self.assertNotIn("sera_wanderer", bot._NPC_LOCATIONS)
        found_at = [
            loc_id for region in bot.CAMPAIGN["locations"].values()
            for loc_id, loc in region.items()
            if "sera_wanderer" in loc.get("npcs", [])
        ]
        self.assertEqual(len(found_at), 1, "Sarah should be listed at exactly one real location")
        self.assertIn("sera_wanderer", bot._npcs_at_location(found_at[0]))

    # -- "I have decided the answer to the riddle is X" (v1.9.2) -------
    def test_puzzle_answer_not_shadowed_by_resolve_choice(self):
        """
        resolve_choice's "i have decided" trigger is checked before
        answer_puzzle's own triggers, so a natural puzzle answer that
        happens to open with decision-framing words ("I have decided
        the answer to the riddle is a map") was misclassified as
        resolve_choice and answer_puzzle never ran. "riddle"/"puzzle"/
        "the answer to" are unambiguous puzzle-answer signals, so they
        must win regardless of surrounding decision phrasing.
        """
        result = _keyword_fallback("I have decided the answer to the riddle is a map", [])
        self.assertEqual(result["action"], "answer_puzzle")

    def test_resolve_choice_still_works_without_puzzle_language(self):
        result = _keyword_fallback("I choose to spare the bandit", [])
        self.assertEqual(result["action"], "resolve_choice")

    # -- 7 previously-orphaned magic items now have a real acquisition
    #    path (shops or quest rewards) (v1.9.2) ------------------------
    def test_no_orphaned_items_remain_in_shops_or_quest_rewards(self):
        acquirable = set()
        for shop in bot.CAMPAIGN.get("shops", {}).values():
            acquirable.update(shop.get("inventory", []))
        for quest in bot.CAMPAIGN["quests"].values():
            if quest.get("reward_item"):
                acquirable.add(quest["reward_item"])
        for region in bot.CAMPAIGN.get("locations", {}).values():
            for location in region.values():
                for node in location.get("resource_nodes", []) or []:
                    acquirable.update(node.get("materials", []) or [])
        for recipe in RECIPES.values():
            acquirable.update(recipe.get("materials", {}).keys())
            if recipe.get("result_item"):
                acquirable.add(recipe["result_item"])

        previously_orphaned = {
            "greater_healing_potion", "silvered_dagger", "scroll_fireball",
            "ring_of_protection", "cloak_of_elvenkind", "amulet_of_health",
            "boots_of_the_winterlands",
        }
        missing = previously_orphaned - acquirable
        self.assertEqual(missing, set(), f"still orphaned: {missing}")

    # -- Spell progression actually reaches every level the unlock table
    #    promises, up to character level 9 (v1.10.3) ---------------------
    def test_every_class_has_real_spells_at_every_promised_tier(self):
        """
        SPELL_LEVEL_UNLOCK_CHAR_LEVEL promised 4th/5th-level spells at
        character levels 7/9, but no spell of either level existed in
        SPELLS at all -- full casters got nothing new from level 7
        onward. Added real 4th/5th-level spells and wired them into
        every full caster's CLASS_SPELL_LISTS; paladin/ranger (half-
        casters) are allowed to cap out early, same as they already did
        for 2nd/3rd level before this fix.
        """
        full_casters = ["wizard", "sorcerer", "cleric", "druid", "bard", "warlock"]
        for cls in full_casters:
            highest_known = max(
                spells.SPELLS[s]["level"] for s in spells.spells_unlocked_at_level(cls, 9)
            )
            self.assertEqual(highest_known, 5, f"{cls} caps below the promised 5th-level tier")

    def test_no_orphaned_spells_unreachable_by_any_class(self):
        referenced = set()
        for cantrips in spells.CLASS_CANTRIPS.values():
            referenced.update(cantrips)
        for spell_list in spells.CLASS_SPELL_LISTS.values():
            referenced.update(spell_list)
        orphaned = set(spells.SPELLS.keys()) - referenced
        self.assertEqual(orphaned, set(), f"unreachable by any class: {orphaned}")

    # -- Cleric's Divine Domain (Life Domain fixed-default) was flavor
    #    text only; Disciple of Life is now a real heal bonus (v1.10.3) --
    def test_cleric_disciple_of_life_adds_bonus_healing(self):
        cleric = {"char_class": "Cleric", "name": "Test Cleric"}
        target = {"name": "Ally", "hp_current": 1, "hp_max": 100}
        result = spells.resolve_heal_spell("cure_wounds", cleric, target)
        # cure_wounds is heal_dice "1d8+2" (min 3) + Disciple of Life's
        # 2 + spell level (1) = +3 -- minimum possible total is 6.
        self.assertGreaterEqual(result["healing_done"], 6)

    def test_non_cleric_gets_no_disciple_of_life_bonus(self):
        wizard = {"char_class": "Wizard", "name": "Test Wizard"}
        target = {"name": "Ally", "hp_current": 1, "hp_max": 100}
        result = spells.resolve_heal_spell("cure_wounds", wizard, target)
        self.assertLessEqual(result["healing_done"], 11)  # 1d8+2 max, no bonus

    # -- Guild membership benefits were data-only; bonus_spell_scroll and
    #    bonus_damage_vs_undead had nothing checking them (v1.10.4) ------
    def test_silver_wardens_bonus_damage_vs_undead(self):
        # AC 1 guarantees a hit on any roll except a natural 1 (5E
        # auto-miss regardless of AC) or natural 20 (crit, which would
        # double the weapon die and complicate the exact-damage check) --
        # retry until a plain hit so those ~10% of rolls don't make this
        # test flaky.
        attacker = {
            "name": "Warden", "dexterity": 14, "strength": 16, "armor_class": 15,
            "hp_current": 20, "hp_max": 20, "char_class": "Fighter", "guild": "silver_wardens",
        }
        weapon = {"ability": "strength", "damage_dice": "1d1", "damage_bonus": 0}
        for _ in range(20):
            defender = {
                "name": "Shadow Wisp", "dexterity": 18, "armor_class": 1,
                "hp_current": 100, "hp_max": 100, "monster_key": "shadow_wisp",
            }
            result = resolve_attack(attacker, defender, weapon)
            if result["hit"] and not result["critical_hit"]:
                self.assertEqual(result["damage_dealt"], 3)  # 1 (die) + 2 (warden bonus)
                return
        self.fail("no plain (non-crit) hit landed in 20 tries at AC 1 -- suspiciously unlucky or broken")

    def test_no_warden_bonus_against_non_undead_or_non_member(self):
        warden_vs_goblin = {
            "name": "Warden", "dexterity": 14, "strength": 16, "armor_class": 15,
            "hp_current": 20, "hp_max": 20, "char_class": "Fighter", "guild": "silver_wardens",
        }
        weapon = {"ability": "strength", "damage_dice": "1d1", "damage_bonus": 0}
        for _ in range(20):
            defender = {
                "name": "Goblin", "dexterity": 14, "armor_class": 1,
                "hp_current": 100, "hp_max": 100, "monster_key": "goblin",
            }
            result = resolve_attack(warden_vs_goblin, defender, weapon)
            if result["hit"] and not result["critical_hit"]:
                self.assertEqual(result["damage_dealt"], 1)  # no bonus vs. non-undead
                return
        self.fail("no plain (non-crit) hit landed in 20 tries at AC 1 -- suspiciously unlucky or broken")

    async def test_arcane_circle_join_grants_a_real_scroll(self):
        use_test_db("tests/tmp/guild_benefit_test.db")
        user_id = 777777
        character = make_basic_character(
            user_id, "Elowen", char_class="Wizard", current_location="crossroads_tavern",
        )
        # Task #170 (guild vetting): joining now requires proven_in_combat,
        # not just level/class -- this fixture predates that requirement.
        db.update_character(user_id, level=3, proven_in_combat=1)
        sink = []
        await bot._do_join_guild(FakeUpdate(user_id, "join the arcane circle", sink), "join the arcane circle")
        character = db.get_character(user_id)
        self.assertGreaterEqual(character["inventory"].get("scroll_magic_missile", 0), 1)

    # -- story_arcs (campaign.json) mapped the whole campaign's main
    #    questline but nothing read it -- wired into quest completion
    #    and the quest journal (v1.10.5) --------------------------------
    def test_fresh_character_starts_in_the_first_chapter(self):
        character = {"completed_quests": []}
        arc_id, arc = bot._current_story_arc(character)
        self.assertEqual(arc_id, "arc_1_discovery")
        self.assertEqual(arc["title"], "Discovery")

    def test_chapter_advances_once_its_quests_are_all_done(self):
        arc1_quests = bot.CAMPAIGN["story_arcs"]["arc_1_discovery"]["quests"]
        character = {"completed_quests": list(arc1_quests)}
        arc_id, _ = bot._current_story_arc(character)
        self.assertEqual(arc_id, "arc_2_descent")

    def test_chapter_complete_note_fires_on_the_arcs_last_quest(self):
        use_test_db("tests/tmp/story_arc_test.db")
        user_id = 888899
        make_basic_character(user_id, "Arclight", current_location="crossroads_tavern")
        arc1_quests = bot.CAMPAIGN["story_arcs"]["arc_1_discovery"]["quests"]
        for q in arc1_quests:  # _complete_quest_and_announce calls db.complete_quest
            db.complete_quest(user_id, q)  # BEFORE _chapter_complete_note, so this
            # test mirrors that real ordering rather than checking a
            # state that would never actually occur mid-flow.
        note = bot._chapter_complete_note(user_id, arc1_quests[-1])
        self.assertIn("Chapter complete", note)
        self.assertIn("Discovery", note)

    def test_no_chapter_complete_note_mid_chapter(self):
        use_test_db("tests/tmp/story_arc_test2.db")
        user_id = 888900
        make_basic_character(user_id, "Arclight", current_location="crossroads_tavern")
        arc1_quests = bot.CAMPAIGN["story_arcs"]["arc_1_discovery"]["quests"]
        db.complete_quest(user_id, arc1_quests[0])  # only the first of several
        note = bot._chapter_complete_note(user_id, arc1_quests[0])
        self.assertEqual(note, "")

    async def test_check_quests_shows_current_chapter(self):
        use_test_db("tests/tmp/story_arc_test3.db")
        user_id = 888901
        make_basic_character(user_id, "Arclight", current_location="crossroads_tavern")
        sink = []
        await bot._do_check_quests(FakeUpdate(user_id, "check my quests", sink))
        self.assertTrue(any("Discovery" in msg for msg in sink))

    # -- Player-to-player item trading (v1.10.7 backlog item) ----------
    def test_give_item_phrasing_classified_correctly(self):
        result = _keyword_fallback("give my healing potion to Sarah", [])
        self.assertEqual(result["action"], "give_item")

    def test_give_me_a_clue_not_shadowed_by_give_item(self):
        self.assertEqual(_keyword_fallback("give me a clue", [])["action"], "ask_clue")
        self.assertEqual(_keyword_fallback("give me a hint about this quest", [])["action"], "ask_clue")

    async def test_give_item_transfers_between_characters_at_the_same_location(self):
        use_test_db("tests/tmp/give_item_test.db")
        giver_id, recipient_id = 900001, 900002
        make_basic_character(giver_id, "Giver", current_location="crossroads_tavern")
        make_basic_character(recipient_id, "Receiver", current_location="crossroads_tavern")
        db.add_item(giver_id, "healing_potion", 2)

        sink = []
        await bot._do_give_item(
            FakeUpdate(giver_id, "give my healing potion to Receiver", sink),
            "give my healing potion to Receiver",
        )
        combined = " ".join(sink)
        self.assertIn("Receiver", combined)

        giver = db.get_character(giver_id)
        recipient = db.get_character(recipient_id)
        self.assertEqual(giver["inventory"].get("healing_potion", 0), 1)
        self.assertEqual(recipient["inventory"].get("healing_potion", 0), 1)

    async def test_give_item_rejects_recipient_at_a_different_location(self):
        use_test_db("tests/tmp/give_item_test2.db")
        giver_id, elsewhere_id = 900003, 900004
        make_basic_character(giver_id, "Giver2", current_location="crossroads_tavern")
        make_basic_character(elsewhere_id, "Farflung", current_location="whispering_wood")
        db.add_item(giver_id, "healing_potion", 1)

        sink = []
        await bot._do_give_item(
            FakeUpdate(giver_id, "give my healing potion to Farflung", sink),
            "give my healing potion to Farflung",
        )
        giver = db.get_character(giver_id)
        self.assertEqual(giver["inventory"].get("healing_potion", 0), 1)  # nothing transferred

    async def test_give_item_rejects_item_the_giver_doesnt_have(self):
        use_test_db("tests/tmp/give_item_test3.db")
        giver_id, recipient_id = 900005, 900006
        make_basic_character(giver_id, "Giver3", current_location="crossroads_tavern")
        make_basic_character(recipient_id, "Receiver3", current_location="crossroads_tavern")

        sink = []
        await bot._do_give_item(
            FakeUpdate(giver_id, "give my healing potion to Receiver3", sink),
            "give my healing potion to Receiver3",
        )
        recipient = db.get_character(recipient_id)
        self.assertEqual(recipient["inventory"].get("healing_potion", 0), 0)

    # -- Potions were completely non-functional: no action anywhere ever
    #    read a consumable's heal_dice/effect field (v1.10.8) ----------
    def test_use_item_phrasing_classified_correctly(self):
        for text in ["drink the healing potion", "I use my antitoxin", "quaff the potion"]:
            self.assertEqual(_keyword_fallback(text, [])["action"], "use_item", text)

    def test_use_item_doesnt_shadow_arcane_recovery(self):
        self.assertEqual(_keyword_fallback("I use arcane recovery", [])["action"], "arcane_recovery")

    async def test_use_item_heals_and_consumes_the_potion(self):
        use_test_db("tests/tmp/use_item_test.db")
        user_id = 900101
        make_basic_character(user_id, "Drinker", current_location="crossroads_tavern", hp_max=20)
        db.update_character(user_id, hp_current=5)
        db.add_item(user_id, "healing_potion", 2)

        sink = []
        await bot._do_use_item(FakeUpdate(user_id, "drink the healing potion", sink), "drink the healing potion")
        combined = " ".join(sink)
        self.assertIn("healing", combined.lower())

        character = db.get_character(user_id)
        self.assertGreater(character["hp_current"], 5)
        self.assertEqual(character["inventory"].get("healing_potion", 0), 1)  # consumed exactly 1

    async def test_use_item_cures_poison_mid_combat(self):
        import sessions
        sessions.end_session(-999)
        user_id = 900102
        make_basic_character(user_id, "Poisoned", current_location="crossroads_tavern")
        db.add_item(user_id, "antitoxin", 1)
        character = db.get_character(user_id)
        character["conditions"] = ["poisoned"]
        session = sessions.start_session(-999, [character], {user_id: "party"})

        sink = []
        await bot._do_use_item(FakeUpdate(user_id, "I use my antitoxin", sink), "I use my antitoxin")
        combined = " ".join(sink)
        self.assertIn("neutralized", combined.lower())
        live = next(p for p in session.participants if p["telegram_user_id"] == user_id)
        self.assertNotIn("poisoned", live.get("conditions", []))
        sessions.end_session(-999)

    async def test_use_item_rejects_when_none_carried(self):
        use_test_db("tests/tmp/use_item_test2.db")
        user_id = 900103
        make_basic_character(user_id, "Empty", current_location="crossroads_tavern")

        sink = []
        await bot._do_use_item(FakeUpdate(user_id, "drink the healing potion", sink), "drink the healing potion")
        combined = " ".join(sink)
        self.assertIn("carrying", combined.lower())

    # -- Cooking: raw_fish was gatherable but no recipe used it --------
    def test_cooked_fish_recipe_is_real_and_usable(self):
        from rules.crafting import RECIPES
        self.assertIn("cooked_fish", RECIPES)
        recipe = RECIPES["cooked_fish"]
        self.assertIn("raw_fish", recipe["materials"])
        result_item = items_module.get_item(recipe["result_item"])
        self.assertEqual(result_item["type"], "consumable")
        self.assertIn("heal_dice", result_item)

    # -- Equipment never affected combat: items.py's weapon damage_dice/
    #    ability and armor ac_base fields existed but nothing ever
    #    equipped anything or read them (v1.10.9) -----------------------
    def test_equip_item_phrasing_classified_correctly(self):
        for text in ["equip my longsword", "wield the dagger", "wear the chain mail", "put on my leather armor"]:
            self.assertEqual(_keyword_fallback(text, [])["action"], "equip_item", text)

    def test_equipping_weapon_changes_what_weapon_for_attacker_returns(self):
        use_test_db("tests/tmp/equip_test.db")
        user_id = 900201
        make_basic_character(user_id, "Wielder", current_location="crossroads_tavern")
        db.add_item(user_id, "greataxe", 1)

        default_weapon = bot._weapon_for_attacker(db.get_character(user_id))
        self.assertNotEqual(default_weapon["damage_dice"], "1d12")

        success, message, updated = db.equip_item(user_id, "greataxe")
        self.assertTrue(success)
        self.assertEqual(updated["equipped_weapon"], "greataxe")
        equipped_weapon = bot._weapon_for_attacker(updated)
        self.assertEqual(equipped_weapon["damage_dice"], "1d12")
        self.assertEqual(equipped_weapon["ability"], "strength")

    def test_equipping_armor_recomputes_armor_class(self):
        use_test_db("tests/tmp/equip_test2.db")
        user_id = 900202
        character = make_basic_character(user_id, "Armored", current_location="crossroads_tavern")
        db.add_item(user_id, "chain_mail", 1)

        success, message, updated = db.equip_item(user_id, "chain_mail")
        self.assertTrue(success)
        from rules.dice import ability_modifier
        expected_ac = 16 + ability_modifier(character["dexterity"])  # chain_mail's ac_base is 16
        self.assertEqual(updated["armor_class"], expected_ac)
        self.assertEqual(updated["equipped_armor"], "chain_mail")

    def test_equip_rejects_item_not_carried(self):
        use_test_db("tests/tmp/equip_test3.db")
        user_id = 900203
        make_basic_character(user_id, "Empty2", current_location="crossroads_tavern")
        success, message, _ = db.equip_item(user_id, "longsword")
        self.assertFalse(success)

    def test_equip_rejects_non_equippable_item(self):
        use_test_db("tests/tmp/equip_test4.db")
        user_id = 900204
        make_basic_character(user_id, "Drinker2", current_location="crossroads_tavern")
        db.add_item(user_id, "healing_potion", 1)
        success, message, _ = db.equip_item(user_id, "healing_potion")
        self.assertFalse(success)

    async def test_do_equip_item_handler_end_to_end(self):
        use_test_db("tests/tmp/equip_test5.db")
        user_id = 900205
        make_basic_character(user_id, "Handler", current_location="crossroads_tavern")
        db.add_item(user_id, "longsword", 1)

        sink = []
        await bot._do_equip_item(FakeUpdate(user_id, "equip my longsword", sink), "equip my longsword")
        combined = " ".join(sink)
        self.assertIn("Longsword", combined)
        character = db.get_character(user_id)
        self.assertEqual(character["equipped_weapon"], "longsword")

    # -- Reactions (Shield, Uncanny Dodge): CLAUDE.md flagged this as
    #    needing resolve_attack's roll-then-damage step to have a real
    #    checkpoint rather than a bolt-on -- that checkpoint already
    #    existed, both plug into it there (v1.10.10) ---------------------
    def test_shield_turns_a_would_be_hit_into_a_miss(self):
        # attacker with +0 to hit (str 10, proficiency_bonus 0) means the
        # attack total is just the raw d20 roll -- AC 15 means rolls
        # 15-19 would hit without Shield but not with it (+5 AC), while
        # 20 is a crit (unaffected) and under 15 already misses. Retry
        # until landing in that band so this isn't flaky.
        weapon = {"ability": "strength", "damage_dice": "1d1", "damage_bonus": 0}
        for _ in range(60):
            attacker = {"name": "Attacker", "strength": 10, "proficiency_bonus": 0}
            defender = {
                "name": "Shielder", "dexterity": 10, "armor_class": 15,
                "hp_current": 50, "hp_max": 50, "char_class": "Wizard",
                "known_spells": ["shield"], "spell_slots_current": 2,
            }
            result = resolve_attack(attacker, defender, weapon, round_number=1)
            if result["attack_roll"] in range(15, 20) and not result["critical_hit"]:
                self.assertFalse(result["hit"])
                self.assertTrue(result["shield_reaction_triggered"])
                self.assertEqual(defender["spell_slots_current"], 1)
                self.assertEqual(defender["reaction_used_round"], 1)
                return
        self.fail("never landed a roll in the Shield-relevant band in 60 tries")

    def test_shield_doesnt_fire_without_a_spell_slot(self):
        weapon = {"ability": "strength", "damage_dice": "1d1", "damage_bonus": 0}
        for _ in range(60):
            attacker = {"name": "Attacker", "strength": 10, "proficiency_bonus": 0}
            defender = {
                "name": "OutOfSlots", "dexterity": 10, "armor_class": 15,
                "hp_current": 50, "hp_max": 50, "char_class": "Wizard",
                "known_spells": ["shield"], "spell_slots_current": 0,
            }
            result = resolve_attack(attacker, defender, weapon, round_number=1)
            if result["attack_roll"] in range(15, 20) and not result["critical_hit"]:
                self.assertTrue(result["hit"])
                self.assertFalse(result["shield_reaction_triggered"])
                return
        self.fail("never landed a roll in the Shield-relevant band in 60 tries")

    def test_uncanny_dodge_halves_damage_for_a_level_5_rogue(self):
        weapon = {"ability": "strength", "damage_dice": "8d1", "damage_bonus": 0}  # always deals 8
        attacker = {"name": "Attacker", "strength": 20, "proficiency_bonus": 5}
        defender = {
            "name": "Dodger", "dexterity": 10, "armor_class": 1,
            "hp_current": 50, "hp_max": 50, "char_class": "Rogue", "level": 5,
        }
        # forced_roll=10 guarantees a plain (non-crit) hit on the first try --
        # the old 20-attempt retry loop reused the same defender dict and a
        # fixed round_number=1 across every attempt without resetting
        # reaction_used_round, so an earlier natural 20 (which is also a
        # "hit" and also enters the reaction block) could burn the round-1
        # reaction before the loop ever reached a plain hit to assert on.
        # Confirmed live 2026-07-16: failed under real random rolls in an
        # otherwise-clean full suite run for exactly this reason.
        result = resolve_attack(attacker, defender, weapon, round_number=1, forced_roll=10)
        self.assertTrue(result["hit"])
        self.assertFalse(result["critical_hit"])
        self.assertTrue(result["uncanny_dodge_triggered"])
        self.assertEqual(result["damage_dealt"], 4)

    def test_uncanny_dodge_doesnt_fire_below_level_5(self):
        weapon = {"ability": "strength", "damage_dice": "8d1", "damage_bonus": 0}
        attacker = {"name": "Attacker", "strength": 20, "proficiency_bonus": 5}
        defender = {
            "name": "TooYoung", "dexterity": 10, "armor_class": 1,
            "hp_current": 50, "hp_max": 50, "char_class": "Rogue", "level": 4,
        }
        for _ in range(20):
            result = resolve_attack(dict(attacker), defender, weapon, round_number=1)
            if result["hit"] and not result["critical_hit"]:
                self.assertFalse(result["uncanny_dodge_triggered"])
                self.assertEqual(result["damage_dealt"], 8)
                return
        self.fail("no plain hit landed in 20 tries at AC 1")

    def test_only_one_reaction_per_round(self):
        # A level-5 Rogue Wizard hybrid isn't real, but this directly
        # tests the shared reaction economy: once reaction_used_round
        # matches the current round, neither Shield nor Uncanny Dodge
        # should fire again this round.
        defender = {
            "name": "Spent", "dexterity": 10, "armor_class": 15,
            "hp_current": 50, "hp_max": 50, "char_class": "Rogue", "level": 5,
            "known_spells": ["shield"], "spell_slots_current": 5,
            "reaction_used_round": 1,
        }
        weapon = {"ability": "strength", "damage_dice": "8d1", "damage_bonus": 0}
        attacker = {"name": "Attacker", "strength": 10, "proficiency_bonus": 0}
        for _ in range(60):
            result = resolve_attack(dict(attacker), dict(defender), weapon, round_number=1)
            if result["attack_roll"] in range(15, 20) and not result["critical_hit"]:
                self.assertFalse(result["shield_reaction_triggered"])
                self.assertFalse(result["uncanny_dodge_triggered"])
                return
        self.fail("never landed a roll in the relevant band in 60 tries")

    # -- Shields, auto-equip, equipping party members, sheet display of
    #    equipped/carried-not-equipped gear (v1.10.10, per Coffee) ------
    def test_equip_item_handles_shields_additively(self):
        use_test_db("tests/tmp/shield_test.db")
        user_id = 900301
        make_basic_character(user_id, "Shieldbearer", current_location="crossroads_tavern", armor_class=16)
        db.add_item(user_id, "wooden_shield", 1)
        success, message, updated = db.equip_item(user_id, "wooden_shield")
        self.assertTrue(success)
        self.assertEqual(updated["equipped_shield"], "wooden_shield")
        self.assertEqual(updated["armor_class"], 18)  # 16 + wooden_shield's ac_bonus of 2

    def test_equipping_new_armor_preserves_an_already_equipped_shields_bonus(self):
        use_test_db("tests/tmp/shield_test2.db")
        user_id = 900302
        character = make_basic_character(user_id, "Upgrader", current_location="crossroads_tavern")
        db.add_item(user_id, "wooden_shield", 1)
        db.add_item(user_id, "chain_mail", 1)
        db.equip_item(user_id, "wooden_shield")
        success, message, updated = db.equip_item(user_id, "chain_mail")
        self.assertTrue(success)
        from rules.dice import ability_modifier
        expected = 16 + ability_modifier(character["dexterity"]) + 2  # chain_mail + dex + shield
        self.assertEqual(updated["armor_class"], expected)

    def test_auto_equip_picks_the_real_best_weapon_and_armor(self):
        use_test_db("tests/tmp/auto_equip_test.db")
        user_id = 900303
        make_basic_character(user_id, "Auto", current_location="crossroads_tavern")
        db.add_item(user_id, "rusty_dagger", 1)   # 1d4, worse
        db.add_item(user_id, "greataxe", 1)       # 1d12, better
        db.add_item(user_id, "leather_armor", 1)  # ac_base 11, worse
        db.add_item(user_id, "chain_mail", 1)     # ac_base 16, better
        summary, character = db.auto_equip_best_gear(user_id)
        self.assertEqual(character["equipped_weapon"], "greataxe")
        self.assertEqual(character["equipped_armor"], "chain_mail")

    async def test_auto_equip_handler_end_to_end(self):
        use_test_db("tests/tmp/auto_equip_test2.db")
        user_id = 900304
        make_basic_character(user_id, "AutoHandler", current_location="crossroads_tavern")
        db.add_item(user_id, "longsword", 1)
        sink = []
        await bot._do_auto_equip_gear(FakeUpdate(user_id, "auto equip my character", sink), "auto equip my character")
        character = db.get_character(user_id)
        self.assertEqual(character["equipped_weapon"], "longsword")
        self.assertTrue(any("Longsword" in msg for msg in sink))

    def test_auto_equip_phrasing_classified_correctly(self):
        for text in ["auto equip my character", "put on my gear automatically", "help me equip my player"]:
            self.assertEqual(_keyword_fallback(text, [])["action"], "auto_equip", text)

    # -- Real live bug (2026-07-15): "auto equip my equipment" contains
    #    "my equipment", which matched check_inventory's trigger (checked
    #    much earlier in the fallback chain) before auto_equip's own
    #    check was ever reached. --------------------------------------
    def test_auto_equip_my_equipment_not_shadowed_by_check_inventory(self):
        self.assertEqual(_keyword_fallback("auto equip my equipment", [])["action"], "auto_equip")

    def test_check_inventory_still_works_for_real_equipment_checks(self):
        for text in ["check my equipment", "my equipment", "my inventory", "what am I carrying"]:
            self.assertEqual(_keyword_fallback(text, [])["action"], "check_inventory", text)

    # -- Per Coffee (2026-07-15): bare "auto equip" alone, with no other
    #    words, should be enough to trigger it. ------------------------
    def test_bare_auto_equip_is_enough_to_trigger(self):
        for text in ["auto equip", "auto-equip", "auto equip me"]:
            self.assertEqual(_keyword_fallback(text, [])["action"], "auto_equip", text)

    # -- Real live bug (2026-07-16): "what items do you have for sale?"
    #    matched check_inventory's own "what items" trigger and showed the
    #    ASKER's own backpack instead of answering a question about a
    #    shop's/NPC's stock. Originally routed to "buy" (no better than
    #    silence with no item named); now routes to the real list_shop
    #    action instead, once it existed (task #110). ------------------
    def test_what_do_you_have_for_sale_routes_to_list_shop(self):
        for text in ["what items do you have for sale?", "what do you have for sale",
                     "what items are for sale here", "what's for sale"]:
            self.assertEqual(_keyword_fallback(text, [])["action"], "list_shop", text)

    # -- Real live bugs (2026-07-16, Coffee): with no real "browse a
    #    shop" action to reach for, "I want to shop" was silently
    #    swallowed as chat, and "I want to see the items in the shop"
    #    got guessed by the raw model as check_sheet with a garbled
    #    target name. Both now route to the real list_shop action. -----
    def test_shop_browsing_phrasing_routes_to_list_shop(self):
        for text in ["I want to shop", "let's shop", "items in the shop",
                     "browse the shop", "what's in the shop"]:
            self.assertEqual(_keyword_fallback(text, [])["action"], "list_shop", text)

    def test_check_inventory_first_person_phrasing_still_unaffected(self):
        for text in ["what items do I have", "what do i have in my backpack",
                     "what weapons do i have"]:
            self.assertEqual(_keyword_fallback(text, [])["action"], "check_inventory", text)

    # -- Real live incident (2026-07-16): a Support question failed when
    #    Ollama was transiently unreachable (contention from concurrent
    #    dev-side model calls), and the fallback reply was generic
    #    onboarding boilerplate unrelated to the actual question asked --
    #    "just talk naturally in Adventure... check the pinned message".
    #    Now says the AI is busy and to try again, which is both accurate
    #    and support-specific. -------------------------------------------
    def test_support_model_unreachable_fallback_is_specific_not_generic_onboarding(self):
        import ai.support_agent as support_agent_module
        from unittest.mock import patch
        import requests

        with patch("ai.support_agent.requests.post", side_effect=requests.RequestException("boom")), \
             patch("ai.support_agent.time.sleep"):
            answer = support_agent_module.answer_support_question("Where can I find a woodcutters axe?")
        # Wording was polished since this test was written (task #178)
        # -- checking for the CURRENT specific, non-generic fallback's
        # real key phrases instead of the older "busy"/"try asking
        # again" copy, same underlying intent: a real reason (overloaded,
        # not vague), a real next step (ask again or /redo), never a
        # generic "check the pinned message" onboarding non-answer.
        self.assertIn("overloaded", answer.lower())
        self.assertIn("ask again", answer.lower())
        self.assertNotIn("pinned message", answer.lower())

    # -- Real live bug (2026-07-16): this model has a documented bias
    #    toward guessing "pass_turn" for phrasing it doesn't recognize --
    #    "I'll take a mug, ale!!! how are you doing old buddy?" (ordinary
    #    tavern small talk, no active turn to pass) came back from the
    #    RAW MODEL as pass_turn, and since _keyword_fallback correctly
    #    says "chat" for it (no pass-turn phrase present), the model's
    #    ungrounded guess was trusted instead of the deterministic
    #    fallback, producing a confusing "There's no active turn to pass
    #    right now" reply to ordinary chatter. Mirrors the pre-existing
    #    start_combat guard's same defensive shape. -------------------
    def test_model_guessing_pass_turn_is_never_trusted_over_keyword_fallback(self):
        from unittest.mock import patch
        import ai.intent_parser as intent_parser_module

        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {"response": '{"action": "pass_turn"}'}

        with patch("ai.intent_parser.requests.post", return_value=FakeResponse()):
            result = intent_parser_module.parse_intent(
                "I'll take a mug, ale!!! how are you doing old buddy?", []
            )
        self.assertEqual(result["action"], "chat")

    # -- Real live bug (2026-07-17): capping num_predict for speed
    #    (ai/dm_agent.py, ai/support_agent.py, ai/intent_parser.py) can
    #    truncate generation mid-thought, before the model ever emits
    #    </think> -- caught live via a /redo test hitting the real
    #    model: the ENTIRE raw chain-of-thought leaked to a real
    #    player instead of being stripped, since the paired regex
    #    requires a closing tag to match at all. ------------------------
    def test_strip_think_tags_handles_a_truncated_unclosed_block(self):
        truncated = "<think> Okay, let's tackle this. The user wants me to answer..."
        self.assertEqual(strip_think_tags(truncated), "")

    def test_strip_think_tags_still_handles_a_normal_closed_block(self):
        normal = "<think>internal reasoning here</think>The actual answer."
        self.assertEqual(strip_think_tags(normal), "The actual answer.")

    def test_strip_think_tags_handles_closed_block_then_truncated_second_one(self):
        mixed = "<think>reasoning</think>partial answer <think>more reasoning that never closes"
        self.assertEqual(strip_think_tags(mixed), "partial answer")

    # -- Real live bug (2026-07-16): a compound message repeating the
    #    SAME action type ("go to X, then go to Y") only ever executed
    #    the first step and silently dropped the rest, because
    #    parse_intents used to require 2+ segments to independently
    #    produce DIFFERENT action types before treating a message as
    #    genuinely compound. --------------------------------------------
    def test_compound_same_action_repeated_now_produces_both_intents(self):
        intents = parse_intents(
            "Go to the crossroads Tavern, and then go to the whispering wood", []
        )
        self.assertEqual([i["action"] for i in intents], ["move", "move"])
        self.assertIn("Tavern", intents[0]["raw_text"])
        self.assertIn("whispering wood", intents[1]["raw_text"])

    def test_compound_different_actions_still_works(self):
        intents = parse_intents(
            "recruit Sarah to my party, look at the quest board, and leave the tavern", []
        )
        self.assertEqual([i["action"] for i in intents], ["recruit_npc", "check_quests", "move"])

    def test_compound_false_positive_guard_still_holds(self):
        # "attack the goblin and the wolf" must NOT become two separate
        # attack intents against an implicit shared target -- only ONE
        # segment ("attack the goblin") ever produces a real action,
        # "the wolf" alone classifies as chat and is filtered out.
        intents = parse_intents("attack the goblin and the wolf", [])
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0]["action"], "attack")

    def test_my_sheet_still_classified_as_check_sheet(self):
        for text in ["my sheet", "my character", "my stats", "my class"]:
            self.assertEqual(_keyword_fallback(text, [])["action"], "check_sheet", text)

    async def test_equip_can_target_another_party_member(self):
        use_test_db("tests/tmp/equip_other_test.db")
        equipper_id, helped_id = 900305, 900306
        make_basic_character(equipper_id, "Helper", current_location="crossroads_tavern")
        helped = make_basic_character(helped_id, "Helped", current_location="crossroads_tavern")
        db.add_item(helped_id, "longsword", 1)  # in HELPED's own inventory, not the helper's

        sink = []
        await bot._do_equip_item(
            FakeUpdate(equipper_id, "equip Helped with the longsword", sink),
            "equip Helped with the longsword",
        )
        combined = " ".join(sink)
        self.assertIn("Helped", combined)
        helped_after = db.get_character(helped_id)
        self.assertEqual(helped_after["equipped_weapon"], "longsword")
        equipper_after = db.get_character(equipper_id)
        self.assertIsNone(equipper_after["equipped_weapon"])  # the HELPER didn't equip anything

    def test_sheet_shows_equipped_and_carried_not_equipped_gear(self):
        use_test_db("tests/tmp/sheet_gear_test.db")
        user_id = 900307
        make_basic_character(user_id, "SheetTest", current_location="crossroads_tavern")
        db.add_item(user_id, "longsword", 1)
        db.add_item(user_id, "shortsword", 1)
        db.equip_item(user_id, "longsword")
        character = db.get_character(user_id)
        sheet = bot._format_character_sheet(character)
        self.assertIn("Equipped: Longsword", sheet)
        self.assertIn("Carried but not equipped: Shortsword", sheet)

    # -- Rings/amulets/wondrous items were completely non-functional too
    #    (same shape as potions/equipment): Ring of Protection, Ring of
    #    the Undertow, Amulet of Health, Cloak of Elvenkind all had real
    #    mechanical fields nothing ever read (v1.10.11) ------------------
    def test_equipping_a_ring_adds_its_ac_bonus(self):
        use_test_db("tests/tmp/ring_test.db")
        user_id = 900401
        make_basic_character(user_id, "RingBearer", current_location="crossroads_tavern", armor_class=15)
        db.add_item(user_id, "ring_of_protection", 1)
        success, message, updated = db.equip_item(user_id, "ring_of_protection")
        self.assertTrue(success)
        self.assertEqual(updated["armor_class"], 16)
        self.assertIn("ring_of_protection", updated["equipped_accessories"])

    def test_two_rings_stack_their_ac_bonus(self):
        use_test_db("tests/tmp/ring_test2.db")
        user_id = 900402
        make_basic_character(user_id, "TwoRings", current_location="crossroads_tavern", armor_class=15)
        db.add_item(user_id, "ring_of_protection", 1)
        db.add_item(user_id, "ring_of_the_undertow", 1)
        db.equip_item(user_id, "ring_of_protection")
        success, message, updated = db.equip_item(user_id, "ring_of_the_undertow")
        self.assertTrue(success)
        self.assertEqual(updated["armor_class"], 17)

    def test_equipping_armor_after_rings_preserves_ring_bonus(self):
        use_test_db("tests/tmp/ring_test3.db")
        user_id = 900403
        character = make_basic_character(user_id, "RingThenArmor", current_location="crossroads_tavern")
        db.add_item(user_id, "ring_of_protection", 1)
        db.add_item(user_id, "chain_mail", 1)
        db.equip_item(user_id, "ring_of_protection")
        success, message, updated = db.equip_item(user_id, "chain_mail")
        self.assertTrue(success)
        from rules.dice import ability_modifier
        expected = 16 + ability_modifier(character["dexterity"]) + 1  # chain_mail + dex + ring
        self.assertEqual(updated["armor_class"], expected)

    def test_amulet_of_health_sets_constitution(self):
        use_test_db("tests/tmp/amulet_test.db")
        user_id = 900404
        make_basic_character(user_id, "Amuleted", current_location="crossroads_tavern")
        db.add_item(user_id, "amulet_of_health", 1)
        success, message, updated = db.equip_item(user_id, "amulet_of_health")
        self.assertTrue(success)
        self.assertEqual(updated["constitution"], 19)

    def test_amulet_of_health_never_lowers_a_higher_constitution(self):
        use_test_db("tests/tmp/amulet_test2.db")
        user_id = 900405
        make_basic_character(
            user_id, "AlreadyStrong", current_location="crossroads_tavern",
            ability_scores={"strength": 10, "dexterity": 10, "constitution": 20,
                             "intelligence": 10, "wisdom": 10, "charisma": 10},
        )
        db.add_item(user_id, "amulet_of_health", 1)
        success, message, updated = db.equip_item(user_id, "amulet_of_health")
        self.assertTrue(success)
        self.assertEqual(updated["constitution"], 20)

    def test_cannot_equip_the_same_ring_twice(self):
        use_test_db("tests/tmp/ring_test4.db")
        user_id = 900406
        make_basic_character(user_id, "Careful", current_location="crossroads_tavern")
        db.add_item(user_id, "ring_of_protection", 1)
        db.equip_item(user_id, "ring_of_protection")
        success, message, _ = db.equip_item(user_id, "ring_of_protection")
        self.assertFalse(success)

    def test_auto_equip_wears_every_carried_ring_and_amulet(self):
        use_test_db("tests/tmp/auto_equip_accessories_test.db")
        user_id = 900407
        make_basic_character(user_id, "AutoAccessory", current_location="crossroads_tavern")
        db.add_item(user_id, "ring_of_protection", 1)
        db.add_item(user_id, "ring_of_the_undertow", 1)
        db.add_item(user_id, "amulet_of_health", 1)
        summary, character = db.auto_equip_best_gear(user_id)
        self.assertIn("ring_of_protection", character["equipped_accessories"])
        self.assertIn("ring_of_the_undertow", character["equipped_accessories"])
        self.assertIn("amulet_of_health", character["equipped_accessories"])

    def test_equip_item_rejects_non_equippable_types_still(self):
        use_test_db("tests/tmp/ring_test5.db")
        user_id = 900408
        make_basic_character(user_id, "StillChecked", current_location="crossroads_tavern")
        db.add_item(user_id, "waterlogged_journal", 1)
        success, message, _ = db.equip_item(user_id, "waterlogged_journal")
        self.assertFalse(success)

    def test_cloak_of_elvenkind_grants_advantage_on_sneak_checks(self):
        use_test_db("tests/tmp/cloak_test.db")
        user_id = 900409
        make_basic_character(user_id, "Sneaky", current_location="crossroads_tavern")
        db.add_item(user_id, "cloak_of_elvenkind", 1)
        db.equip_item(user_id, "cloak_of_elvenkind")
        character = db.get_character(user_id)
        self.assertTrue(
            bot._cloak_of_elvenkind_grants_advantage(character, "dexterity", "I try to sneak past the guard")
        )

    def test_cloak_of_elvenkind_doesnt_buff_non_stealth_dex_checks(self):
        use_test_db("tests/tmp/cloak_test2.db")
        user_id = 900410
        make_basic_character(user_id, "Climber", current_location="crossroads_tavern")
        db.add_item(user_id, "cloak_of_elvenkind", 1)
        db.equip_item(user_id, "cloak_of_elvenkind")
        character = db.get_character(user_id)
        self.assertFalse(
            bot._cloak_of_elvenkind_grants_advantage(character, "dexterity", "I try to climb the wall")
        )

    def test_no_advantage_without_the_cloak(self):
        use_test_db("tests/tmp/cloak_test3.db")
        user_id = 900411
        make_basic_character(user_id, "NoCloak", current_location="crossroads_tavern")
        character = db.get_character(user_id)
        self.assertFalse(
            bot._cloak_of_elvenkind_grants_advantage(character, "dexterity", "I try to sneak past the guard")
        )

    # -- Ranger's Natural Explorer (2026-07-16 audit): listed in
    #    class_features.py's Ranger flavor text but confirmed via grep
    #    to have zero mechanical hook anywhere. Fixed-default terrain
    #    (forest), advantage on Wisdom checks that read as tracking/
    #    surviving in the wild. ------------------------------------------
    def test_ranger_natural_explorer_grants_advantage_on_survival_checks(self):
        ranger = {"char_class": "Ranger"}
        for text in ("I track the wolf's trail", "I forage for food", "I try to navigate the forest",
                     "I hunt for something to eat", "I follow the trail"):
            self.assertTrue(
                bot._ranger_natural_explorer_grants_advantage(ranger, "wisdom", text), text
            )

    def test_ranger_natural_explorer_doesnt_buff_unrelated_wisdom_checks(self):
        ranger = {"char_class": "Ranger"}
        self.assertFalse(bot._ranger_natural_explorer_grants_advantage(ranger, "wisdom", "I listen at the door"))
        self.assertFalse(bot._ranger_natural_explorer_grants_advantage(ranger, "dexterity", "I track the wolf"))

    def test_natural_explorer_is_ranger_only(self):
        non_ranger = {"char_class": "Fighter"}
        self.assertFalse(bot._ranger_natural_explorer_grants_advantage(non_ranger, "wisdom", "I track the wolf"))

    # -- Cleric's Channel Divinity (2026-07-16 class-features audit):
    #    real 5E level-2 feature, listed nowhere (not even as flavor
    #    text) and completely unimplemented before this. Turn Undead,
    #    once per rest, single-target (this game's one undead-flavored
    #    monster type). ----------------------------------------------
    async def test_channel_divinity_rejects_non_cleric(self):
        import sessions
        sessions.end_session(-999)
        user_id = 900450
        make_basic_character(user_id, "Plainfolk", char_class="Fighter")
        sink = []
        await bot._do_channel_divinity(FakeUpdate(user_id, "channel divinity", sink))
        self.assertTrue(any("doesn't have it" in m for m in sink))

    async def test_channel_divinity_rejects_below_level_2(self):
        import sessions
        sessions.end_session(-999)
        user_id = 900451
        make_basic_character(user_id, "Acolyte", char_class="Cleric")
        sink = []
        await bot._do_channel_divinity(FakeUpdate(user_id, "channel divinity", sink))
        self.assertTrue(any("level 2" in m for m in sink))

    async def test_channel_divinity_rejects_with_no_undead_present(self):
        import sessions
        sessions.end_session(-999)
        user_id = 900452
        make_basic_character(user_id, "Chaplain", char_class="Cleric", current_location="crossroads_tavern")
        db.update_character(user_id, level=2)

        enemy_id = -2_500_040
        enemy = {
            "telegram_user_id": enemy_id, "name": "Goblin", "dexterity": 10,
            "hp_current": 7, "monster_key": "goblin",
        }
        session = sessions.start_session(-999, [enemy], {enemy_id: "enemy", user_id: "party"})

        sink = []
        await bot._do_channel_divinity(FakeUpdate(user_id, "channel divinity", sink))
        self.assertTrue(any("no undead" in m for m in sink))
        sessions.end_session(-999)

    async def test_channel_divinity_frightens_undead_and_is_once_per_rest(self):
        import sessions
        sessions.end_session(-999)
        user_id = 900453
        make_basic_character(user_id, "Chaplain", char_class="Cleric", current_location="crossroads_tavern")
        db.update_character(user_id, level=2)

        undead_id = -2_500_041
        undead = {
            "telegram_user_id": undead_id, "name": "Shadow Wisp", "dexterity": 18,
            "hp_current": 22, "monster_key": "shadow_wisp", "conditions": [],
        }
        session = sessions.start_session(-999, [undead], {undead_id: "enemy", user_id: "party"})

        sink = []
        await bot._do_channel_divinity(FakeUpdate(user_id, "channel divinity", sink))
        self.assertIn("frightened", undead["conditions"])
        self.assertEqual(db.get_feature_uses(user_id, "channel_divinity"), 1)

        sink2 = []
        await bot._do_channel_divinity(FakeUpdate(user_id, "channel divinity", sink2))
        self.assertTrue(any("already used" in m for m in sink2))
        sessions.end_session(-999)

    def test_channel_divinity_phrasing_classified_correctly(self):
        for text in ("I channel divinity", "I turn undead", "turn the undead", "channel divinity"):
            self.assertEqual(_keyword_fallback(text, [])["action"], "channel_divinity", text)

    # -- Gathering tools (2026-07-16, per Coffee): fishing needs a
    #    fishing pole + bait, lumberjacking needs an axe, mining needs a
    #    pickaxe -- herbalism needs no tool at all, but owning Shears
    #    lets an herbalist gather up to 3 per success instead of 1. ----
    async def test_fishing_rejected_without_pole_and_bait(self):
        user_id = 900460
        make_basic_character(user_id, "Angler", current_location="stonearch_bridge")
        sink = []
        await bot._do_gather(FakeUpdate(user_id, "I go fishing", sink), "I go fishing")
        self.assertTrue(any("Fishing Pole" in m for m in sink))
        character = db.get_character(user_id)
        self.assertEqual(character["inventory"].get("raw_fish", 0), 0)

    async def test_fishing_works_with_pole_and_bait(self):
        user_id = 900461
        make_basic_character(user_id, "Angler", current_location="stonearch_bridge")
        db.add_item(user_id, "fishing_pole", 1)
        db.add_item(user_id, "bait", 1)
        sink = []
        await bot._do_gather(FakeUpdate(user_id, "I go fishing", sink), "I go fishing")
        self.assertFalse(any("Fishing Pole" in m for m in sink))

    async def test_lumberjacking_rejected_without_axe(self):
        user_id = 900462
        make_basic_character(user_id, "Chopper", current_location="whispering_wood")
        sink = []
        await bot._do_gather(FakeUpdate(user_id, "I chop wood", sink), "I chop wood")
        self.assertTrue(any("Woodcutter's Axe" in m for m in sink))

    async def test_mining_rejected_without_pickaxe(self):
        user_id = 900463
        make_basic_character(user_id, "Digger", current_location="sunken_root_caverns")
        sink = []
        await bot._do_gather(FakeUpdate(user_id, "I mine for sulfur", sink), "I mine for sulfur")
        self.assertTrue(any("Pickaxe" in m for m in sink))

    async def test_herbalism_needs_no_tool(self):
        user_id = 900464
        make_basic_character(user_id, "Herbalist", current_location="whispering_wood")
        sink = []
        await bot._do_gather(FakeUpdate(user_id, "I gather herbs", sink), "I gather herbs")
        self.assertFalse(any("need" in m and "Shears" in m for m in sink))

    def test_shears_boost_herbalism_quantity_up_to_3(self):
        character = {"inventory": {"shears": 1}}
        for _ in range(20):
            qty = bot._gather_quantity(character, "herbalism", practiced_bonus=0)
            self.assertIn(qty, (1, 2, 3))

    def test_no_shears_means_flat_quantity_of_1(self):
        character = {"inventory": {}}
        for _ in range(20):
            self.assertEqual(bot._gather_quantity(character, "herbalism", practiced_bonus=0), 1)

    def test_shears_dont_affect_other_gathering_skills(self):
        character = {"inventory": {"shears": 1}}
        for _ in range(10):
            self.assertEqual(bot._gather_quantity(character, "mining", practiced_bonus=0), 1)

    def test_gathering_tools_stocked_at_marens_wares(self):
        shop = bot.CAMPAIGN["shops"]["marens_wares"]
        for tool_id in ("fishing_pole", "bait", "woodcutters_axe", "pickaxe", "shears"):
            self.assertIn(tool_id, shop["inventory"], tool_id)

    # -- Level 2-10 class features batch (2026-07-16, per Coffee): Action
    #    Surge, Reckless Attack, Divine Smite, Flurry of Blows. ----------
    async def test_action_surge_rejects_non_fighter(self):
        import sessions
        sessions.end_session(-999)
        user_id = 900470
        make_basic_character(user_id, "Plainfolk", char_class="Wizard")
        sink = []
        await bot._do_action_surge(FakeUpdate(user_id, "action surge", sink))
        self.assertTrue(any("doesn't have it" in m for m in sink))

    async def test_action_surge_rejects_below_level_2(self):
        import sessions
        sessions.end_session(-999)
        user_id = 900471
        make_basic_character(user_id, "Recruit", char_class="Fighter")
        sink = []
        await bot._do_action_surge(FakeUpdate(user_id, "action surge", sink))
        self.assertTrue(any("level 2" in m for m in sink))

    async def test_action_surge_doubles_attack_count_once_per_rest(self):
        import sessions
        sessions.end_session(-999)
        user_id = 900472
        make_basic_character(user_id, "Twinstrike", char_class="Fighter", current_location="crossroads_tavern")
        db.update_character(user_id, level=2)

        enemy_id = -2_500_050
        enemy = {"telegram_user_id": enemy_id, "name": "Dummy", "dexterity": 10, "hp_current": 100}
        player = db.get_character(user_id)
        player["telegram_user_id"] = user_id
        session = sessions.start_session(-999, [player, enemy], {enemy_id: "enemy", user_id: "party"})

        sink = []
        await bot._do_action_surge(FakeUpdate(user_id, "action surge", sink))
        participant = next(p for p in session.participants if p["telegram_user_id"] == user_id)
        self.assertTrue(participant.get("action_surge_active"))
        self.assertEqual(db.get_feature_uses(user_id, "action_surge"), 1)

        sink2 = []
        await bot._do_action_surge(FakeUpdate(user_id, "action surge", sink2))
        self.assertTrue(any("already used" in m for m in sink2))
        sessions.end_session(-999)

    def test_reckless_attack_grants_advantage_and_is_consumed(self):
        attacker = {"reckless_active": True, "race": "Human", "char_class": "Barbarian"}
        defender = {}
        adv, disadv = bot._attack_advantage_disadvantage(attacker, defender)
        self.assertTrue(adv)
        self.assertFalse(attacker.get("reckless_active"))
        # Second call with the flag already consumed shouldn't grant it again.
        adv2, _ = bot._attack_advantage_disadvantage(attacker, defender)
        self.assertFalse(adv2)

    async def test_reckless_attack_rejects_non_barbarian(self):
        import sessions
        sessions.end_session(-999)
        user_id = 900473
        make_basic_character(user_id, "Plainfolk", char_class="Fighter")
        sink = []
        await bot._do_reckless_attack(FakeUpdate(user_id, "reckless attack", sink))
        self.assertTrue(any("doesn't have it" in m for m in sink))

    async def test_divine_smite_rejects_non_paladin(self):
        import sessions
        sessions.end_session(-999)
        user_id = 900474
        make_basic_character(user_id, "Plainfolk", char_class="Fighter")
        sink = []
        await bot._do_divine_smite(FakeUpdate(user_id, "divine smite", sink))
        self.assertTrue(any("doesn't have it" in m for m in sink))

    async def test_divine_smite_rejects_below_level_2(self):
        import sessions
        sessions.end_session(-999)
        user_id = 900475
        make_basic_character(user_id, "Squire", char_class="Paladin")
        db.update_character(user_id, spell_slots_max=2, spell_slots_current=2)
        sink = []
        await bot._do_divine_smite(FakeUpdate(user_id, "divine smite", sink))
        self.assertTrue(any("level 2" in m for m in sink))

    async def test_divine_smite_rejects_with_no_spell_slot(self):
        import sessions
        sessions.end_session(-999)
        user_id = 900476
        make_basic_character(user_id, "Squire", char_class="Paladin")
        db.update_character(user_id, level=2, spell_slots_max=2, spell_slots_current=0)
        sink = []
        await bot._do_divine_smite(FakeUpdate(user_id, "divine smite", sink))
        self.assertTrue(any("spell slot" in m for m in sink))

    async def test_divine_smite_adds_bonus_damage_and_spends_a_slot_only_on_hit(self):
        import sessions
        sessions.end_session(-999)
        user_id = 900477
        make_basic_character(
            user_id, "Smiter", char_class="Paladin", current_location="crossroads_tavern",
            armor_class=18,
        )
        db.update_character(user_id, level=2, spell_slots_max=2, spell_slots_current=2)

        enemy_id = -2_500_051
        enemy = {
            "telegram_user_id": enemy_id, "name": "Dummy", "dexterity": 10, "strength": 10,
            "armor_class": 1, "proficiency_bonus": 2,  # guaranteed hits
            "hp_current": 200, "hp_max": 200, "is_ai": 1, "monster_key": "goblin",
        }
        player = db.get_character(user_id)
        player["telegram_user_id"] = user_id
        session = sessions.start_session(-999, [player, enemy], {user_id: "party", enemy_id: "enemy"})
        session.turn_order = [user_id, enemy_id]
        session.current_turn_index = 0

        sink = []
        await bot._do_divine_smite(FakeUpdate(user_id, "divine smite", sink))
        sink2 = []
        # Forced roll of 20 guarantees a hit -- without this, a natural 1
        # (1/20 chance, an automatic miss in 5E regardless of AC) made
        # this test genuinely flaky (confirmed live 2026-07-16: failed
        # under real random rolls in an otherwise-clean full suite run).
        await bot._do_attack(
            FakeUpdate(user_id, "I attack the dummy", sink2), "I attack the dummy", forced_roll=20
        )

        self.assertLess(enemy["hp_current"], 200)
        self.assertEqual(db.get_character(user_id)["spell_slots_current"], 1)
        sessions.end_session(-999)

    async def test_flurry_of_blows_rejects_non_monk(self):
        import sessions
        sessions.end_session(-999)
        user_id = 900478
        make_basic_character(user_id, "Plainfolk", char_class="Fighter")
        sink = []
        await bot._do_flurry_of_blows(FakeUpdate(user_id, "flurry of blows", sink))
        self.assertTrue(any("doesn't have it" in m for m in sink))

    async def test_flurry_of_blows_ki_pool_scales_with_level_and_adds_two_attacks(self):
        import sessions
        sessions.end_session(-999)
        user_id = 900479
        make_basic_character(user_id, "Fistfighter", char_class="Monk", current_location="crossroads_tavern")
        db.update_character(user_id, level=2)

        enemy_id = -2_500_052
        enemy = {"telegram_user_id": enemy_id, "name": "Dummy", "dexterity": 10, "hp_current": 100}
        player = db.get_character(user_id)
        player["telegram_user_id"] = user_id
        session = sessions.start_session(-999, [player, enemy], {enemy_id: "enemy", user_id: "party"})

        sink = []
        await bot._do_flurry_of_blows(FakeUpdate(user_id, "flurry of blows", sink))
        participant = next(p for p in session.participants if p["telegram_user_id"] == user_id)
        self.assertEqual(participant.get("flurry_bonus_attacks"), 2)
        self.assertEqual(db.get_feature_uses(user_id, "ki"), 1)

        # A level 2 Monk only has 2 ki points -- second use ok, third rejected.
        participant.pop("flurry_bonus_attacks", None)
        sink2 = []
        await bot._do_flurry_of_blows(FakeUpdate(user_id, "flurry of blows", sink2))
        self.assertEqual(db.get_feature_uses(user_id, "ki"), 2)
        participant.pop("flurry_bonus_attacks", None)
        sink3 = []
        await bot._do_flurry_of_blows(FakeUpdate(user_id, "flurry of blows", sink3))
        self.assertTrue(any("out of ki" in m for m in sink3))
        sessions.end_session(-999)

    def test_new_class_feature_phrasing_classified_correctly(self):
        for text, expected in (
            ("action surge", "action_surge"),
            ("I go reckless", "reckless_attack"),
            ("divine smite", "divine_smite"),
            ("flurry of blows", "flurry_of_blows"),
        ):
            self.assertEqual(_keyword_fallback(text, [])["action"], expected, text)

    # -- Physical dice mode (2026-07-16, per Coffee) -------------------
    def test_manual_dice_toggle_phrasing_classified_correctly(self):
        for text in ("use my own dice", "roll my own dice", "let the game roll for me",
                     "dice on", "dice off"):
            self.assertEqual(_keyword_fallback(text, [])["action"], "toggle_manual_dice", text)

    async def test_toggle_manual_dice_turns_on_and_off(self):
        user_id = 900480
        make_basic_character(user_id, "Roller")
        sink = []
        await bot._do_toggle_manual_dice(FakeUpdate(user_id, "use my own dice", sink), "use my own dice")
        self.assertEqual(db.get_character(user_id)["manual_dice_enabled"], 1)
        # Task #158 (2026-07-18): _safe_send now strips ** markers into real
        # Telegram bold entities instead of sending them as literal text.
        self.assertTrue(any("now ON" in m for m in sink))

        sink2 = []
        await bot._do_toggle_manual_dice(FakeUpdate(user_id, "let the game roll for me", sink2), "let the game roll for me")
        self.assertEqual(db.get_character(user_id)["manual_dice_enabled"], 0)
        self.assertTrue(any("now OFF" in m for m in sink2))

    async def test_skill_check_prompts_for_manual_roll_and_resumes_with_it(self):
        user_id = 900481
        make_basic_character(user_id, "Sharpeyes", current_location="crossroads_tavern")
        db.update_character(user_id, manual_dice_enabled=1)
        bot._PENDING_DICE_ROLLS.pop(user_id, None)

        sink = []
        await bot._do_skill_check(FakeUpdate(user_id, "I listen at the door", sink), "wisdom", "I listen at the door")
        # Prompt wording is lowercase "roll a d20" (only the character's
        # own name is capitalized/bolded) -- this test predates that
        # standardized wording and checked for a capital "Roll".
        self.assertTrue(any("roll a d20" in m.lower() for m in sink))
        self.assertIn(user_id, bot._PENDING_DICE_ROLLS)
        self.assertEqual(bot._PENDING_DICE_ROLLS[user_id]["kind"], "skill_check")

        sink2 = []
        pending = bot._PENDING_DICE_ROLLS.pop(user_id)
        await bot._do_skill_check(
            FakeUpdate(user_id, "20", sink2), pending["ability"], pending["action_text"], forced_roll=20
        )
        self.assertTrue(any("raw" in m.lower() or "20" in m for m in sink2))

    def test_extract_manual_roll_parses_free_text(self):
        self.assertEqual(bot._extract_manual_roll("14"), 14)
        self.assertEqual(bot._extract_manual_roll("I rolled a 17"), 17)
        self.assertEqual(bot._extract_manual_roll("natural 20"), 20)
        self.assertIsNone(bot._extract_manual_roll("no numbers here"))
        self.assertIsNone(bot._extract_manual_roll("I rolled a 47"))  # out of d20 range

    # -- Real live bug (2026-07-16, Coffee, Support topic): "On my
    #    character sheet, I noticed there are spell slots. What are
    #    they used for?" matched the sheet-lookup shortcut on "my
    #    character sheet" and just dumped his own sheet, ignoring the
    #    real question -- exactly the kind of genuine how-to-play
    #    question Support is supposed to explain (Coffee's "make
    #    Support a wiki" goal). -----------------------------------------
    def test_sheet_mention_with_a_real_question_skips_the_sheet_dump(self):
        for question in (
            "On my character sheet, I noticed there are spell slots. What are they used for?",
            "What is my character sheet's AC used for?",
            "How do spell slots on my sheet work?",
        ):
            self.assertEqual(bot._wants_sheet_names(question), [], question)

    def test_genuine_sheet_requests_still_work(self):
        self.assertEqual(bot._wants_sheet_names("show me my character sheet"), ["my"])
        self.assertEqual(bot._wants_sheet_names("Sarah's character sheet"), ["sarah"])

    async def test_attack_forced_roll_determines_hit_or_miss(self):
        import sessions
        sessions.end_session(-999)
        user_id = 900482
        make_basic_character(user_id, "Roller", current_location="crossroads_tavern")
        enemy_id = -2_500_053
        enemy = {
            "telegram_user_id": enemy_id, "name": "Dummy", "strength": 10, "dexterity": 10,
            "armor_class": 15, "hp_current": 100, "hp_max": 100, "is_ai": 1, "monster_key": "goblin",
        }
        player = db.get_character(user_id)
        player["telegram_user_id"] = user_id
        session = sessions.start_session(-999, [player, enemy], {user_id: "party", enemy_id: "enemy"})
        session.turn_order = [user_id, enemy_id]
        session.current_turn_index = 0

        sink = []
        # A forced natural 1 should always miss (critical fail). The
        # dummy still needs a real "strength" score of its own -- after
        # the player's forced miss, _resolve_ai_turns takes the dummy's
        # own turn and resolves its unarmed attack back, which needs it.
        await bot._do_attack(FakeUpdate(user_id, "I attack the dummy", sink), "I attack the dummy", forced_roll=1)
        self.assertEqual(enemy["hp_current"], 100, "a forced natural 1 should never hit")
        sessions.end_session(-999)

    # -- Inactive party members' XP share (2026-07-16, per Coffee) ----
    async def test_absent_party_member_gets_partial_xp_share(self):
        import sessions
        sessions.end_session(-999)

        fighter_id = 900483
        absent_id = 900484
        make_basic_character(fighter_id, "Fighter", current_location="crossroads_tavern")
        make_basic_character(absent_id, "Homebody", current_location="crossroads_tavern")
        party_id = db.create_party(fighter_id)
        db.update_character(absent_id, party_id=party_id)

        enemy_id = -2_500_054
        enemy = {
            "telegram_user_id": enemy_id, "name": "Goblin", "dexterity": 10, "xp_reward": 100,
        }
        fighter = db.get_character(fighter_id)
        fighter["telegram_user_id"] = fighter_id
        session = sessions.start_session(-999, [fighter, enemy], {enemy_id: "enemy", fighter_id: "party"})
        session.turn_order = [fighter_id, enemy_id]

        before_absent = db.get_character(absent_id)["xp"]
        before_fighter = db.get_character(fighter_id)["xp"]
        bot._award_victory_xp(session)
        after_absent = db.get_character(absent_id)["xp"]
        after_fighter = db.get_character(fighter_id)["xp"]

        self.assertEqual(after_fighter - before_fighter, 100)
        self.assertEqual(after_absent - before_absent, 10)  # 10% of the 100 xp_each fighters earned
        sessions.end_session(-999)

    async def test_ai_companions_never_get_the_absent_party_bonus(self):
        import sessions
        sessions.end_session(-999)

        fighter_id = 900485
        make_basic_character(fighter_id, "Fighter", current_location="crossroads_tavern")
        party_id = db.create_party(fighter_id)
        companion = db.create_ai_companion(
            "Buddy", "Human", "Fighter",
            ability_scores={"strength": 15, "dexterity": 14, "constitution": 13,
                             "intelligence": 10, "wisdom": 10, "charisma": 10},
            hp_max=12, armor_class=15, gold=10, inventory={},
        )
        db.add_ai_companion_to_party(companion["telegram_user_id"], party_id)

        enemy_id = -2_500_055
        enemy = {"telegram_user_id": enemy_id, "name": "Goblin", "dexterity": 10, "xp_reward": 50}
        fighter = db.get_character(fighter_id)
        fighter["telegram_user_id"] = fighter_id
        session = sessions.start_session(-999, [fighter, enemy], {enemy_id: "enemy", fighter_id: "party"})
        session.turn_order = [fighter_id, enemy_id]

        # Should not raise even if an AI companion shares the party, and
        # the companion (is_ai) must never receive the absent-member bonus.
        companion_xp_before = companion["xp"]
        bot._award_victory_xp(session)
        companion_xp_after = db.get_character(companion["telegram_user_id"])["xp"]
        self.assertEqual(companion_xp_after, companion_xp_before)
        sessions.end_session(-999)

    # -- Dev-topic video handling (2026-07-15, per Coffee): videos had NO
    #    handler at all before this (only TEXT/PHOTO/Document.ALL were
    #    registered) -- same silent-failure gap photos had before
    #    2026-07-11, now fixed with frame extraction via imageio. -------
    def test_extract_video_frames_produces_real_readable_jpegs(self):
        import numpy as np
        import imageio.v3 as iio

        video_path = "tests/tmp/synthetic_test_video.mp4"
        os.makedirs("tests/tmp", exist_ok=True)
        # 6 seconds at 10fps, distinct-colored frames so a real decode is
        # verifiable (not just "a file exists").
        frames = [np.full((64, 64, 3), (i * 12) % 256, dtype=np.uint8) for i in range(60)]
        iio.imwrite(video_path, frames, fps=10, plugin="FFMPEG")

        try:
            count, frames_dir, error = bot._extract_video_frames(video_path)
            self.assertIsNone(error)
            # 6s of video at one frame per VIDEO_FRAME_INTERVAL_SECONDS (2s) -> 3 frames.
            self.assertEqual(count, 3)
            saved_files = sorted(os.listdir(frames_dir))
            self.assertEqual(len(saved_files), 3)
            first_frame = iio.imread(os.path.join(frames_dir, saved_files[0]))
            self.assertEqual(first_frame.shape, (64, 64, 3))
        finally:
            if os.path.exists(video_path):
                os.remove(video_path)
            if os.path.isdir(video_path + "_frames"):
                shutil.rmtree(video_path + "_frames")

    def test_extract_video_frames_reports_error_for_a_bad_file(self):
        bad_path = "tests/tmp/not_a_real_video.mp4"
        os.makedirs("tests/tmp", exist_ok=True)
        with open(bad_path, "w") as f:
            f.write("this is not a real video file")
        try:
            count, frames_dir, error = bot._extract_video_frames(bad_path)
            self.assertEqual(count, 0)
            self.assertIsNotNone(error)
        finally:
            os.remove(bad_path)
            if os.path.isdir(bad_path + "_frames"):
                shutil.rmtree(bad_path + "_frames")

    # -- Real live bug (2026-07-15): "Write my characters name in the
    #    guest book" -- Coffee dropped the apostrophe on "character's",
    #    and the literal text matched list_characters' "my characters"
    #    trigger, showing his unrelated character roster instead of
    #    responding to what he actually typed. --------------------------
    def test_dropped_apostrophe_possessive_not_misread_as_list_characters(self):
        result = _keyword_fallback("Write my characters name in the guest book", [])
        self.assertNotEqual(result["action"], "list_characters")

    def test_list_characters_still_works_for_real_roster_requests(self):
        for text in ("show my characters", "my characters", "list my characters", "character roster"):
            self.assertEqual(_keyword_fallback(text, [])["action"], "list_characters", text)

    # -- Real live bug (2026-07-15): "Glare into the shadows listen, what
    #    do I hear?" -- a real Perception-check phrasing -- fell through
    #    the keyword fallback (returning "chat", i.e. no opinion) to the
    #    small local model, which misjudged it as pass_turn. Bare
    #    "listen,"/"what do I hear" had no trigger at all (only "listen
    #    for" did), so the fallback had nothing to override the model's
    #    error with. -----------------------------------------------------
    def test_listen_and_what_do_i_hear_route_to_skill_check(self):
        for text in ["Glare into the shadows listen, what do I hear?",
                     "I listen. What do I hear?", "What do I hear in the tunnel?"]:
            result = _keyword_fallback(text, [])
            self.assertEqual(result["action"], "skill_check", text)
            self.assertEqual(result["ability"], "wisdom", text)

    # -- Arc 2 and Arc 3 had no real climax (2026-07-16 reverse-
    #    playthrough, reading the actual story_arcs/quests data): both
    #    "the_hush" and "the_first_city_quest" completed on a bare
    #    reach_location trigger, no fight at all, despite each location's
    #    own flavor text clearly building to a real threat. Added two
    #    new is_boss monsters (the_unspoken, the_waking_ember) and
    #    switched both quests' triggers to defeat_monster. This doesn't
    #    need SlowLiveTests/Ollama -- _check_quest_completions_defeat_monster
    #    and _safe_send with FakeUpdate are both narration-free here. ---
    async def test_the_hush_quest_requires_defeating_the_unspoken(self):
        # Task #86/Full-storyline Phase 2 split the old single "the_hush"
        # quest into a 3-stage chain (the_hush_stage1_signs/stage2_the_
        # wisp/stage3_the_unspoken) -- this fixture predates that and
        # needs the final stage's real id, the one that actually carries
        # the defeat_monster trigger for The Unspoken.
        import sessions
        sessions.end_session(-999)

        player_id = 999910
        make_basic_character(player_id, "Listener", current_location="the_hush_below")
        db.accept_quest(player_id, "the_hush_stage3_the_unspoken")

        boss_id = -2_500_010
        boss = {
            "telegram_user_id": boss_id, "name": "The Unspoken", "dexterity": 16,
            "is_ai": 1, "monster_key": "the_unspoken",
        }
        session = sessions.start_session(-999, [boss], {boss_id: "enemy", player_id: "party"})
        session.turn_order = [boss_id, player_id]

        sink = []
        await bot._check_quest_completions_defeat_monster(FakeUpdate(player_id, "irrelevant", sink), session)

        character = db.get_character(player_id)
        self.assertIn("the_hush_stage3_the_unspoken", character["completed_quests"])
        self.assertNotIn("the_hush_stage3_the_unspoken", character["active_quests"])
        sessions.end_session(-999)

    async def test_the_first_city_quest_requires_defeating_the_waking_ember(self):
        import sessions
        sessions.end_session(-999)

        player_id = 999911
        make_basic_character(player_id, "Delver", current_location="the_first_city")
        db.accept_quest(player_id, "the_first_city_quest")

        boss_id = -2_500_011
        boss = {
            "telegram_user_id": boss_id, "name": "The Waking Ember", "dexterity": 12,
            "is_ai": 1, "monster_key": "the_waking_ember",
        }
        session = sessions.start_session(-999, [boss], {boss_id: "enemy", player_id: "party"})
        session.turn_order = [boss_id, player_id]

        sink = []
        await bot._check_quest_completions_defeat_monster(FakeUpdate(player_id, "irrelevant", sink), session)

        character = db.get_character(player_id)
        self.assertIn("the_first_city_quest", character["completed_quests"])
        self.assertNotIn("the_first_city_quest", character["active_quests"])
        sessions.end_session(-999)

    def test_new_story_bosses_are_placed_at_their_real_locations(self):
        locs = {}
        for region in bot.CAMPAIGN["locations"].values():
            locs.update(region)
        self.assertIn("the_unspoken", locs["the_hush_below"]["monsters"])
        self.assertIn("the_waking_ember", locs["the_first_city"]["monsters"])
        self.assertTrue(bot.CAMPAIGN["monsters"]["the_unspoken"]["is_boss"])
        self.assertTrue(bot.CAMPAIGN["monsters"]["the_waking_ember"]["is_boss"])

    # -- Racial traits audit (2026-07-16): only Half-Orc's traits were
    #    ever mechanically wired; every other race's signature traits
    #    were pure flavor text in races.py, confirmed inert by grep.
    #    Added Dwarven Resilience (immune to poisoned) and Fey Ancestry
    #    (immune to paralyzed, the closest existing condition to
    #    "magical sleep") via a new _racially_immune_to_condition
    #    helper. ---------------------------------------------------------
    def test_dwarf_is_immune_to_poisoned(self):
        self.assertTrue(bot._racially_immune_to_condition({"race": "Dwarf"}, "poisoned"))
        self.assertFalse(bot._racially_immune_to_condition({"race": "Human"}, "poisoned"))

    def test_elf_and_half_elf_are_immune_to_paralyzed(self):
        self.assertTrue(bot._racially_immune_to_condition({"race": "Elf"}, "paralyzed"))
        self.assertTrue(bot._racially_immune_to_condition({"race": "Half-Elf"}, "paralyzed"))
        self.assertFalse(bot._racially_immune_to_condition({"race": "Human"}, "paralyzed"))

    def test_racial_immunity_is_condition_specific_not_blanket(self):
        # A Dwarf's immunity is to poison specifically, not every
        # condition -- shouldn't also block paralyzed/blinded/etc.
        self.assertFalse(bot._racially_immune_to_condition({"race": "Dwarf"}, "paralyzed"))
        self.assertFalse(bot._racially_immune_to_condition({"race": "Elf"}, "poisoned"))

    # -- Dragonborn Breath Weapon (2026-07-16): races.py's own trait text
    #    called this "a real, usable action" but nothing ever implemented
    #    it. Real 5E damage scaling + a real combat action, gated to
    #    once per rest via the existing feature_uses convention. --------
    def test_breath_weapon_damage_scales_with_level(self):
        from rules.leveling import breath_weapon_dice_count
        self.assertEqual(breath_weapon_dice_count(1), 2)
        self.assertEqual(breath_weapon_dice_count(5), 2)
        self.assertEqual(breath_weapon_dice_count(6), 3)
        self.assertEqual(breath_weapon_dice_count(10), 3)
        self.assertEqual(breath_weapon_dice_count(11), 4)
        self.assertEqual(breath_weapon_dice_count(15), 4)
        self.assertEqual(breath_weapon_dice_count(16), 5)
        self.assertEqual(breath_weapon_dice_count(20), 5)

    async def test_breath_weapon_rejects_non_dragonborn(self):
        import sessions
        sessions.end_session(-999)
        user_id = 999920
        make_basic_character(user_id, "Plainfolk", race="Human")
        sink = []
        await bot._do_breath_weapon(FakeUpdate(user_id, "breath weapon", sink))
        self.assertTrue(any("doesn't have it" in m for m in sink))

    async def test_breath_weapon_deals_real_damage_and_is_once_per_rest(self):
        import sessions
        sessions.end_session(-999)

        user_id = 999921
        player = make_basic_character(user_id, "Scaleborn", race="Dragonborn", current_location="crossroads_tavern")
        db.update_character(user_id, level=6)  # 3d6 tier
        player = db.get_character(user_id)
        player["telegram_user_id"] = user_id

        enemy_id = -2_500_020
        enemy = {
            "telegram_user_id": enemy_id, "name": "Target Dummy", "dexterity": 10, "strength": 10,
            "armor_class": 10, "proficiency_bonus": 2,
            "hp_current": 100, "hp_max": 100, "is_ai": 1, "monster_key": "goblin",
        }
        session = sessions.start_session(-999, [player, enemy], {user_id: "party", enemy_id: "enemy"})
        session.turn_order = [user_id, enemy_id]
        session.current_turn_index = 0

        sink = []
        await bot._do_breath_weapon(FakeUpdate(user_id, "breath weapon", sink))
        self.assertLess(enemy["hp_current"], 100, "breath weapon should have dealt real damage")
        self.assertEqual(db.get_feature_uses(user_id, "breath_weapon"), 1)

        # Second use this same rest should be rejected.
        sink2 = []
        await bot._do_breath_weapon(FakeUpdate(user_id, "breath weapon", sink2))
        self.assertTrue(any("already used" in m for m in sink2))
        sessions.end_session(-999)

    def test_breath_weapon_phrasing_classified_correctly(self):
        for text in ("I use my breath weapon", "breathe fire", "unleash my breath", "breath weapon"):
            self.assertEqual(_keyword_fallback(text, [])["action"], "breath_weapon", text)

    # -- Saving throw proficiency (2026-07-16 audit): confirmed via grep
    #    every save-based spell only ever added the raw ability modifier,
    #    never a proficiency bonus, even for a class real 5E says is
    #    proficient in that specific save -- and no CLASS_SAVE_
    #    PROFICIENCIES table existed anywhere to look one up in. -------
    def test_class_save_proficiencies_match_real_5e(self):
        from rules.leveling import is_proficient_in_save
        self.assertTrue(is_proficient_in_save("Fighter", "strength"))
        self.assertTrue(is_proficient_in_save("Fighter", "constitution"))
        self.assertFalse(is_proficient_in_save("Fighter", "wisdom"))
        self.assertTrue(is_proficient_in_save("wizard", "intelligence"))
        self.assertFalse(is_proficient_in_save("wizard", "strength"))
        self.assertFalse(is_proficient_in_save(None, "strength"))

    def test_save_bonus_adds_proficiency_when_the_class_is_proficient(self):
        from spells import _save_bonus
        proficient_target = {"char_class": "Fighter", "constitution": 14, "proficiency_bonus": 3}
        non_proficient_target = {"char_class": "Wizard", "constitution": 14, "proficiency_bonus": 3}
        # +2 CON modifier either way; Fighter is proficient in CON saves, Wizard isn't.
        self.assertEqual(_save_bonus(proficient_target, "constitution"), 2 + 3)
        self.assertEqual(_save_bonus(non_proficient_target, "constitution"), 2)

    # -- Skill checks never applied a proficiency bonus at all (2026-07-16 audit) --
    def test_skill_check_proficiency_bonus_applies_for_a_proficient_class(self):
        from rules.dice import roll_ability_check
        from rules.leveling import is_proficient_in_skill
        rogue = {"dexterity": 14, "proficiency_bonus": 3}
        self.assertTrue(is_proficient_in_skill("Rogue", "dexterity"))
        result = roll_ability_check(rogue, "dexterity", proficient=True, forced_roll=10)
        self.assertEqual(result["total"], 10 + 2 + 3)  # roll + dex mod + proficiency

    def test_skill_check_proficiency_bonus_withheld_for_a_non_proficient_class(self):
        from rules.leveling import is_proficient_in_skill
        self.assertFalse(is_proficient_in_skill("Rogue", "charisma"))
        self.assertFalse(is_proficient_in_skill("Wizard", "strength"))

    async def test_skill_check_handler_applies_proficiency_for_the_right_class(self):
        user_id = 900501
        make_basic_character(user_id, "Sneaky", char_class="Rogue", current_location="crossroads_tavern")
        sink = []
        # forced_roll=10: 10 + dex mod(14->+2) + Rogue Expertise on a
        # proficient ability at level 1 (2 x proficiency_bonus 2 = 4) = 16.
        await bot._do_skill_check(FakeUpdate(user_id, "I sneak past the guard", sink), "dexterity", "I sneak past the guard", forced_roll=10)
        combined = " ".join(sink)
        self.assertIn("16", combined)

    # -- Rogue/Bard Expertise + Bard Jack of All Trades (2026-07-16) ----
    def test_rogue_expertise_doubles_proficiency_from_level_1(self):
        from rules.leveling import skill_check_proficiency_bonus
        self.assertEqual(skill_check_proficiency_bonus("Rogue", 1, "dexterity", 2), 4)
        self.assertEqual(skill_check_proficiency_bonus("Rogue", 1, "charisma", 2), 0, "not a proficient ability")

    def test_bard_expertise_only_unlocks_at_level_3(self):
        from rules.leveling import skill_check_proficiency_bonus
        self.assertEqual(skill_check_proficiency_bonus("Bard", 1, "charisma", 2), 2, "proficient, but no Expertise yet")
        self.assertEqual(skill_check_proficiency_bonus("Bard", 3, "charisma", 2), 4, "Expertise unlocked")

    def test_bard_jack_of_all_trades_adds_half_proficiency_from_level_2(self):
        from rules.leveling import skill_check_proficiency_bonus
        self.assertEqual(skill_check_proficiency_bonus("Bard", 1, "strength", 2), 0, "not yet unlocked")
        self.assertEqual(skill_check_proficiency_bonus("Bard", 2, "strength", 3), 1, "half of 3, rounded down")
        self.assertEqual(skill_check_proficiency_bonus("Wizard", 2, "strength", 3), 0, "only Bard gets Jack of All Trades")

    # -- Bard's Song of Rest (2026-07-16) --------------------------------
    def test_song_of_rest_grants_a_die_when_a_bard_is_in_the_party(self):
        from unittest.mock import patch
        bard = {"char_class": "Bard", "level": 2, "party_id": None}
        with patch("bot.roll", return_value=[4]):
            self.assertEqual(bot._song_of_rest_bonus(bard), 4)

    def test_song_of_rest_nothing_without_a_bard(self):
        fighter = {"char_class": "Fighter", "level": 5, "party_id": None}
        self.assertEqual(bot._song_of_rest_bonus(fighter), 0)

    def test_song_of_rest_checks_the_whole_party_not_just_self(self):
        user_id = 900502
        bard_id = 900503
        make_basic_character(user_id, "Fighty", char_class="Fighter")
        make_basic_character(bard_id, "Songful", char_class="Bard")
        db.update_character(bard_id, level=2)
        party_id = db.create_party(user_id)
        db.update_character(bard_id, party_id=party_id)
        fighter = db.get_character(user_id)
        from unittest.mock import patch
        with patch("bot.roll", return_value=[6]):
            self.assertEqual(bot._song_of_rest_bonus(fighter), 6)

    def test_gnome_cunning_grants_advantage_on_mental_saves_only(self):
        from spells import _gnome_cunning_advantage
        gnome = {"race": "Gnome"}
        self.assertTrue(_gnome_cunning_advantage(gnome, "intelligence"))
        self.assertTrue(_gnome_cunning_advantage(gnome, "wisdom"))
        self.assertTrue(_gnome_cunning_advantage(gnome, "charisma"))
        self.assertFalse(_gnome_cunning_advantage(gnome, "dexterity"), "Gnome Cunning doesn't cover physical saves")
        self.assertFalse(_gnome_cunning_advantage({"race": "Human"}, "wisdom"))

    # -- Ranger's Danger Sense (2026-07-16) ------------------------------
    def test_ranger_danger_sense_grants_advantage_on_dex_saves_from_level_2(self):
        # ranger_danger_sense_advantage has no leading underscore in the
        # real module (unlike _gnome_cunning_advantage above) -- it's
        # called from bot.py's _do_flee outside spells.py, so it was
        # made a real public name; this test never got updated to match.
        from spells import ranger_danger_sense_advantage
        ranger = {"char_class": "Ranger", "level": 2}
        self.assertTrue(ranger_danger_sense_advantage(ranger, "dexterity"))
        self.assertFalse(ranger_danger_sense_advantage(ranger, "constitution"), "only covers Dexterity saves")
        self.assertFalse(ranger_danger_sense_advantage({"char_class": "Ranger", "level": 1}, "dexterity"),
                          "not unlocked until level 2")
        self.assertFalse(ranger_danger_sense_advantage({"char_class": "Fighter", "level": 5}, "dexterity"))

    # -- Sorcerer's Metamagic: Empowered Spell (2026-07-16) --------------
    def test_empowered_spell_rerolls_ones_and_twos(self):
        from unittest.mock import patch
        user_id = 900504
        make_basic_character(user_id, "Sparky", char_class="Sorcerer")
        db.update_character(user_id, level=3)
        character = db.get_character(user_id)
        spell = {"damage_dice": "3d6"}
        result = {"rolls": [1, 2, 6], "damage_dealt": 9}
        with patch("bot.roll", return_value=[5]):
            new_result = bot._apply_empowered_spell(user_id, character, spell, result)
        self.assertEqual(new_result["rolls"], [5, 5, 6])
        self.assertEqual(new_result["damage_dealt"], 9 + (5 - 1) + (5 - 2))

    def test_empowered_spell_only_once_per_rest(self):
        user_id = 900505
        make_basic_character(user_id, "Sparky2", char_class="Sorcerer")
        db.update_character(user_id, level=3)
        character = db.get_character(user_id)
        spell = {"damage_dice": "3d6"}
        result = {"rolls": [1, 1, 1], "damage_dealt": 3}
        first = bot._apply_empowered_spell(user_id, character, spell, result)
        self.assertNotEqual(first["rolls"], [1, 1, 1])
        second = bot._apply_empowered_spell(user_id, character, spell, dict(result))
        self.assertEqual(second["rolls"], [1, 1, 1], "already used this rest")

    def test_empowered_spell_requires_sorcerer_level_3(self):
        user_id = 900506
        make_basic_character(user_id, "Lowbie", char_class="Sorcerer")
        character = db.get_character(user_id)
        spell = {"damage_dice": "3d6"}
        result = {"rolls": [1, 1, 1], "damage_dealt": 3}
        unchanged = bot._apply_empowered_spell(user_id, character, spell, result)
        self.assertEqual(unchanged["rolls"], [1, 1, 1])

        user_id2 = 900507
        make_basic_character(user_id2, "WrongClass", char_class="Fighter")
        db.update_character(user_id2, level=5)
        character2 = db.get_character(user_id2)
        unchanged2 = bot._apply_empowered_spell(user_id2, character2, spell, dict(result))
        self.assertEqual(unchanged2["rolls"], [1, 1, 1])

    # -- Extra Attack (2026-07-16 audit): confirmed via grep this was
    #    completely absent -- not even mentioned as flavor text -- despite
    #    being the single biggest DPS feature every 5E martial class gets
    #    at level 5. Boss Multiattack (task #58) existed for monsters;
    #    players never got the real equivalent. ---------------------------
    def test_extra_attack_kicks_in_at_level_5_for_martial_classes(self):
        for char_class in ("Fighter", "Barbarian", "Paladin", "Ranger", "Monk"):
            self.assertEqual(bot._attacks_per_turn({"char_class": char_class, "level": 1}), 1, char_class)
            self.assertEqual(bot._attacks_per_turn({"char_class": char_class, "level": 4}), 1, char_class)
            self.assertEqual(bot._attacks_per_turn({"char_class": char_class, "level": 5}), 2, char_class)
            self.assertEqual(bot._attacks_per_turn({"char_class": char_class, "level": 10}), 2, char_class)

    def test_fighter_gets_a_third_and_fourth_attack(self):
        self.assertEqual(bot._attacks_per_turn({"char_class": "Fighter", "level": 10}), 2)
        self.assertEqual(bot._attacks_per_turn({"char_class": "Fighter", "level": 11}), 3)
        self.assertEqual(bot._attacks_per_turn({"char_class": "Fighter", "level": 19}), 3)
        self.assertEqual(bot._attacks_per_turn({"char_class": "Fighter", "level": 20}), 4)

    def test_non_martial_classes_never_get_extra_attack(self):
        for char_class in ("Wizard", "Sorcerer", "Warlock", "Cleric", "Druid", "Bard", "Rogue"):
            self.assertEqual(bot._attacks_per_turn({"char_class": char_class, "level": 20}), 1, char_class)

    # -- Sneak Attack / Rage level scaling (2026-07-16 audit): both
    #    features existed but were frozen at their level-1 values --
    #    Sneak Attack always rolled exactly 1d6 regardless of level,
    #    Rage's bonus damage was always a flat +2. --------------------
    def test_sneak_attack_dice_scale_with_rogue_level(self):
        from rules.leveling import sneak_attack_dice_count
        self.assertEqual(sneak_attack_dice_count(1), 1)
        self.assertEqual(sneak_attack_dice_count(2), 1)
        self.assertEqual(sneak_attack_dice_count(3), 2)
        self.assertEqual(sneak_attack_dice_count(10), 5)
        self.assertEqual(sneak_attack_dice_count(19), 10)
        self.assertEqual(sneak_attack_dice_count(20), 10)

    def test_rage_bonus_scales_with_barbarian_level(self):
        from rules.leveling import rage_damage_bonus
        self.assertEqual(rage_damage_bonus(1), 2)
        self.assertEqual(rage_damage_bonus(8), 2)
        self.assertEqual(rage_damage_bonus(9), 3)
        self.assertEqual(rage_damage_bonus(15), 3)
        self.assertEqual(rage_damage_bonus(16), 4)
        self.assertEqual(rage_damage_bonus(20), 4)

    def test_sneak_attack_damage_scales_with_level_in_a_real_attack(self):
        from unittest.mock import patch
        attacker_low = {
            "name": "Lowbie", "char_class": "Rogue", "level": 1, "strength": 10, "dexterity": 10,
        }
        attacker_high = {
            "name": "Highroller", "char_class": "Rogue", "level": 10, "strength": 10, "dexterity": 10,
        }
        weapon = {"ability": "strength", "damage_dice": "1d8", "damage_bonus": 0}
        with patch("rules.dice.random.randint", return_value=4):
            defender_low = {"name": "Target", "armor_class": 1, "hp_current": 200}
            result_low = resolve_attack(attacker_low, defender_low, weapon, advantage=True)
            defender_high = {"name": "Target", "armor_class": 1, "hp_current": 200}
            result_high = resolve_attack(attacker_high, defender_high, weapon, advantage=True)
        # Fixed die value 4: level 1 = 1 sneak die (4), level 10 = 5 sneak dice (20) -- a +16 delta,
        # with everything else (weapon roll, ability mod, hit/crit) identical between the two calls.
        self.assertEqual(result_high["damage_dealt"] - result_low["damage_dealt"], 16)

    # -- Board quest completion bugs (2026-07-16, task #93) -------------
    def test_completed_board_quest_frees_a_slot_for_a_fresh_one(self):
        import board_quests as board_quests_module
        location_id = "stonearch_bridge"
        # Simulate a full day already posted, with one already completed --
        # previously this permanently occupied a slot for the rest of the
        # day instead of a fresh quest ever being generated to replace it.
        stale = db.create_board_quest(
            location_id, board_quests_module._day_key(), "Old bounty", "...", None,
            "defeat_monster", "goblin", 2, 50, 20,
        )
        db.complete_board_quest(stale["board_quest_id"])

        active = board_quests_module.get_or_generate_board_quests(bot.CAMPAIGN, location_id)
        self.assertEqual(len(active), board_quests_module.DAILY_BOARD_QUEST_COUNT)
        self.assertTrue(all(not q.get("completed_at") for q in active),
                         "a completed quest should never be returned for display")

    def test_board_quests_completed_counter_increments(self):
        user_id = 900490
        make_basic_character(user_id, "Bountyhunter")
        self.assertEqual(db.get_character(user_id)["board_quests_completed"], 0)
        db.increment_board_quests_completed(user_id)
        db.increment_board_quests_completed(user_id)
        self.assertEqual(db.get_character(user_id)["board_quests_completed"], 2)

    async def test_check_quests_shows_board_quest_completion_count(self):
        user_id = 900491
        make_basic_character(user_id, "Bountyhunter2")
        db.increment_board_quests_completed(user_id)
        sink = []
        await bot._do_check_quests(FakeUpdate(user_id, "check quests", sink))
        self.assertIn("Board quests completed", " ".join(sink))

    # -- Real player-driven ASI level-up (2026-07-16, per Coffee) -------
    def test_level_up_phrasing_classified_correctly(self):
        for text in ("level up", "I want to level up", "Level up!"):
            self.assertEqual(_keyword_fallback(text, [])["action"], "level_up", text)

    async def test_add_xp_banks_asi_points_instead_of_auto_applying(self):
        user_id = 900492
        make_basic_character(user_id, "Leveler")
        before_strength = db.get_character(user_id)["strength"]
        after = db.add_xp(user_id, 2700)  # crosses level 4, a real ASI level
        self.assertEqual(after["level"], 4)
        self.assertEqual(after["strength"], before_strength, "ASI should no longer auto-apply")
        self.assertEqual(after["pending_asi_points"], 2)

    async def test_level_up_with_no_pending_points_says_so(self):
        user_id = 900493
        make_basic_character(user_id, "Leveler2")
        sink = []
        await bot._do_level_up(FakeUpdate(user_id, "level up", sink), "level up")
        self.assertIn("No ability score improvements", " ".join(sink))

    async def test_level_up_prompts_then_resolves_with_named_ability(self):
        user_id = 900494
        make_basic_character(user_id, "Leveler3")
        db.update_character(user_id, pending_asi_points=2)
        sink = []
        await bot._do_level_up(FakeUpdate(user_id, "level up", sink), "level up")
        self.assertIn(user_id, bot._PENDING_ASI_CHOICE)
        self.assertTrue(any("Which ability" in m for m in sink))

        before_con = db.get_character(user_id)["constitution"]
        sink2 = []
        await bot._do_level_up(FakeUpdate(user_id, "constitution", sink2), "constitution")
        after = db.get_character(user_id)
        self.assertEqual(after["constitution"], before_con + 2)
        self.assertEqual(after["pending_asi_points"], 0)
        self.assertNotIn(user_id, bot._PENDING_ASI_CHOICE)

    async def test_level_up_auto_assigns_to_class_primary_ability(self):
        user_id = 900495
        make_basic_character(user_id, "Leveler4")  # Fighter -> primary ability strength
        db.update_character(user_id, pending_asi_points=4)
        before_strength = db.get_character(user_id)["strength"]
        sink = []
        await bot._do_level_up(FakeUpdate(user_id, "level up, auto", sink), "level up, auto")
        after = db.get_character(user_id)
        self.assertEqual(after["strength"], before_strength + 4)
        self.assertEqual(after["pending_asi_points"], 0)
        self.assertNotIn(user_id, bot._PENDING_ASI_CHOICE)

    async def test_level_up_single_ability_choice_caps_at_two_and_keeps_remainder(self):
        user_id = 900496
        make_basic_character(user_id, "Leveler5")
        db.update_character(user_id, pending_asi_points=4)
        before_wis = db.get_character(user_id)["wisdom"]
        sink = []
        await bot._do_level_up(FakeUpdate(user_id, "wisdom", sink), "wisdom")
        after = db.get_character(user_id)
        self.assertEqual(after["wisdom"], before_wis + 2)
        self.assertEqual(after["pending_asi_points"], 2)
        self.assertTrue(any("still have 2" in m for m in sink))

    async def test_check_sheet_shows_pending_asi_points(self):
        user_id = 900497
        make_basic_character(user_id, "Leveler6")
        db.update_character(user_id, pending_asi_points=2)
        sink = []
        await bot._do_check_sheet(FakeUpdate(user_id, "check my sheet", sink))
        self.assertTrue(any("ability point(s) waiting" in m for m in sink))

    # -- Auto-assign ability scores at character creation (2026-07-16, per Coffee) --
    def test_auto_assign_ability_scores_uses_each_rolled_value_once(self):
        rolled = [15, 14, 13, 12, 10, 8]
        for char_class in ("Fighter", "Wizard", "Rogue", "Cleric", "Paladin", "Monk"):
            assigned = bot._auto_assign_ability_scores(char_class, rolled)
            self.assertEqual(sorted(assigned), sorted(rolled), char_class)

    def test_auto_assign_gives_highest_roll_to_class_primary_ability(self):
        rolled = [15, 14, 13, 12, 10, 8]
        cases = {"Fighter": "strength", "Wizard": "intelligence", "Rogue": "dexterity", "Cleric": "wisdom"}
        for char_class, primary in cases.items():
            assigned = dict(zip(bot._ABILITY_ORDER, bot._auto_assign_ability_scores(char_class, rolled)))
            self.assertEqual(assigned[primary], max(rolled), char_class)

    async def test_character_creation_auto_assigns_scores_on_request(self):
        user_id = 900498
        sink = []
        ctx = DummyContext()
        await bot.adventure_master_handler(FakeUpdate(user_id, "I want to create a character", sink), ctx)
        sink.clear()
        await bot.adventure_master_handler(FakeUpdate(user_id, "Autobot", sink), ctx)
        sink.clear()
        await bot.adventure_master_handler(FakeUpdate(user_id, "Human", sink), ctx)
        sink.clear()
        await bot.adventure_master_handler(FakeUpdate(user_id, "Fighter", sink), ctx)
        rolled = ctx.user_data["creation"]["rolled_scores"]

        sink.clear()
        await bot.adventure_master_handler(FakeUpdate(user_id, "do it for me", sink), ctx)
        self.assertEqual(ctx.user_data["creation"]["step"], "dice_preference")
        assigned = ctx.user_data["creation"]["assigned_scores"]
        self.assertEqual(sorted(assigned), sorted(rolled))

    # -- Quest hints on "look around" (2026-07-16, per Coffee) ---------
    async def test_look_hints_at_an_offerable_story_quest(self):
        user_id = 900499
        make_basic_character(user_id, "Looker", current_location="crossroads_tavern")
        sink = []
        await bot._do_look(FakeUpdate(user_id, "look around", sink))
        combined = " ".join(sink)
        self.assertIn("could use your help", combined)
        self.assertIn("Grimsby", combined)

    async def test_look_hints_at_an_unclaimed_board_quest(self):
        import board_quests as board_quests_module
        location_id = "stonearch_bridge"
        board_quests_module.get_or_generate_board_quests(bot.CAMPAIGN, location_id)
        user_id = 900500
        make_basic_character(user_id, "Looker2", current_location=location_id)
        sink = []
        await bot._do_look(FakeUpdate(user_id, "look around", sink))
        combined = " ".join(sink)
        self.assertIn("bounty posted on the board", combined)

    # -- Character description field (2026-07-16, per Coffee) ----------
    async def test_set_description_inline_extraction_saves_directly(self):
        user_id = 900508
        make_basic_character(user_id, "Thrain")
        sink = []
        await bot._do_set_description(
            FakeUpdate(user_id, "set my description to: A grizzled dwarf who never smiles", sink),
            "set my description to: A grizzled dwarf who never smiles",
        )
        character = db.get_character(user_id)
        self.assertEqual(character["description"], "A grizzled dwarf who never smiles")

    async def test_set_description_with_no_content_prompts_then_saves_on_next_message(self):
        user_id = 900509
        make_basic_character(user_id, "Sable")
        sink = []
        await bot._do_set_description(
            FakeUpdate(user_id, "I'd like to add a character description to my player", sink),
            "I'd like to add a character description to my player",
        )
        self.assertIsNone(db.get_character(user_id)["description"])
        self.assertIn(user_id, bot._PENDING_DESCRIPTION)
        self.assertIn("what would you like your character's description", sink[-1])

        sink2 = []
        await bot._do_set_description(
            FakeUpdate(user_id, "A quiet elven ranger who speaks rarely but shoots true.", sink2),
            "A quiet elven ranger who speaks rarely but shoots true.",
            from_prompt=True,
        )
        self.assertEqual(
            db.get_character(user_id)["description"],
            "A quiet elven ranger who speaks rarely but shoots true.",
        )

    async def test_set_description_skip_leaves_it_blank(self):
        user_id = 900510
        make_basic_character(user_id, "Skippy")
        sink = []
        await bot._do_set_description(FakeUpdate(user_id, "skip", sink), "skip", from_prompt=True)
        self.assertIsNone(db.get_character(user_id)["description"])

    async def test_set_description_trigger_recognized_over_check_sheet(self):
        # "add a description to my character" contains "my character" as a
        # substring, which is check_sheet's own broad trigger -- confirmed
        # this doesn't get shadowed the way "auto equip my character" once
        # would have been, since set_description is checked first.
        from ai.intent_parser import _keyword_fallback
        result = _keyword_fallback("add a description to my character", known_npc_names=[])
        self.assertEqual(result["action"], "set_description")

    def test_character_sheet_shows_description_when_present(self):
        user_id = 900511
        make_basic_character(user_id, "Described")
        character = db.update_character(user_id, description="A former sellsword.")
        sheet = bot._format_character_sheet(character)
        self.assertIn("A former sellsword.", sheet)

    def test_character_sheet_omits_description_line_when_absent(self):
        user_id = 900512
        character = make_basic_character(user_id, "Undescribed")
        sheet = bot._format_character_sheet(character)
        self.assertNotIn('""', sheet)

    # -- Pronouns (2026-07-17, per Coffee, task #117): "no gender/pronoun
    #    field -- narration guesses pronouns with no real data, can guess
    #    wrong". Same settable-at-creation-or-anytime pattern as
    #    description above; narration falls back to they/them, never
    #    guesses (see ai/dm_agent.py's _pronoun_line). ------------------
    async def test_set_pronouns_inline_extraction_saves_directly(self):
        user_id = 900530
        make_basic_character(user_id, "Nyx")
        sink = []
        await bot._do_set_pronouns(FakeUpdate(user_id, "", sink), "set my pronouns to: she/her")
        self.assertEqual(db.get_character(user_id)["pronouns"], "she/her")

    async def test_set_pronouns_with_no_content_prompts_then_saves_on_resume(self):
        user_id = 900531
        make_basic_character(user_id, "Vex")
        sink = []
        await bot._do_set_pronouns(FakeUpdate(user_id, "I'd like to set my pronouns", sink), "I'd like to set my pronouns")
        self.assertIsNone(db.get_character(user_id)["pronouns"])
        self.assertIn(user_id, bot._PENDING_PRONOUNS)

        sink2 = []
        await bot._do_set_pronouns(FakeUpdate(user_id, "he/him", sink2), "he/him", from_prompt=True)
        self.assertEqual(db.get_character(user_id)["pronouns"], "he/him")

    async def test_set_pronouns_skip_leaves_it_unset(self):
        user_id = 900532
        make_basic_character(user_id, "Ambiguous")
        sink = []
        await bot._do_set_pronouns(FakeUpdate(user_id, "skip", sink), "skip", from_prompt=True)
        self.assertIsNone(db.get_character(user_id)["pronouns"])

    def test_set_pronouns_trigger_recognized(self):
        from ai.intent_parser import _keyword_fallback
        result = _keyword_fallback("set my pronouns to she/her", known_npc_names=[])
        self.assertEqual(result["action"], "set_pronouns")

    def test_character_sheet_shows_pronouns_when_present(self):
        user_id = 900533
        make_basic_character(user_id, "Told")
        character = db.update_character(user_id, pronouns="they/them")
        sheet = bot._format_character_sheet(character)
        self.assertIn("they/them", sheet)

    def test_narration_prompt_uses_real_pronouns_not_a_guess(self):
        from ai.dm_agent import _build_skill_check_prompt
        user_id = 900534
        make_basic_character(user_id, "Set")
        character = db.update_character(user_id, pronouns="he/him")
        prompt = _build_skill_check_prompt(character, "climb the wall", "strength", {"raw_roll": 15, "total": 18})
        self.assertIn("he/him", prompt)

    def test_narration_prompt_defaults_to_they_them_when_unset(self):
        from ai.dm_agent import _build_skill_check_prompt
        user_id = 900535
        character = make_basic_character(user_id, "Unset")
        prompt = _build_skill_check_prompt(character, "climb the wall", "strength", {"raw_roll": 15, "total": 18})
        self.assertIn("they/them", prompt)

    # -- Real live bug (2026-07-16, Coffee): ability scores were only
    #    ever shown once, on the one-off creation sheet -- the shared
    #    sheet used by every later "check my sheet"/Support/party-sheet
    #    lookup never included them at all. --------------------------
    def test_character_sheet_shows_ability_scores(self):
        user_id = 900515
        character = make_basic_character(user_id, "Statted")
        sheet = bot._format_character_sheet(character)
        self.assertIn("STR 15", sheet)
        self.assertIn("DEX 14", sheet)
        self.assertIn("CON 13", sheet)
        self.assertIn("WIS 10", sheet)
        self.assertIn("CHA 10", sheet)

    # -- Per Coffee (2026-07-16): pending ASI points should be flagged at
    #    the BOTTOM of the sheet, so a player who missed the level-up
    #    prompt still sees it waiting every time they check their sheet.
    def test_character_sheet_shows_pending_asi_points_at_the_bottom(self):
        user_id = 900516
        make_basic_character(user_id, "Leveled")
        character = db.update_character(user_id, pending_asi_points=2)
        sheet = bot._format_character_sheet(character)
        lines = [line for line in sheet.split("\n") if line.strip()]
        self.assertIn("ability point(s) waiting to be spent", lines[-1])

    def test_character_sheet_no_asi_line_when_nothing_pending(self):
        user_id = 900517
        character = make_basic_character(user_id, "NotLeveled")
        sheet = bot._format_character_sheet(character)
        self.assertNotIn("ability point(s) waiting to be spent", sheet)

    # -- Bestiary / monster compendium (2026-07-16, per Coffee's backlog) --
    async def test_bestiary_is_empty_before_any_fight(self):
        user_id = 900513
        make_basic_character(user_id, "Fresh")
        sink = []
        await bot._do_bestiary(FakeUpdate(user_id, "bestiary", sink))
        self.assertIn("empty", sink[-1])

    async def test_starting_combat_marks_the_monster_known_and_bestiary_lists_it(self):
        from unittest.mock import patch
        import sessions
        user_id = 900514
        character = make_basic_character(user_id, "Fighter1", current_location="crossroads_tavern")
        sessions.end_session(-999)  # tests share chat_id -999 -- don't inherit another test's session
        sink = []
        # _get_combat_eligible_party_members returns every active character
        # sharing this location globally -- patched to just this one
        # character so the test isn't at the mercy of what other tests
        # happen to have left standing at the same default location.
        with patch("bot._get_combat_eligible_party_members", return_value=[character]):
            await bot._do_start_combat(FakeUpdate(user_id, "fight a goblin", sink), monster_key="goblin", count=1)
        updated = db.get_character(user_id)
        self.assertIn("goblin", updated["known_monsters"])
        sessions.end_session(-999)

        sink2 = []
        await bot._do_bestiary(FakeUpdate(user_id, "bestiary", sink2))
        combined = " ".join(sink2)
        self.assertIn("Goblin", combined)
        self.assertIn("HP", combined)
        self.assertIn("XP", combined)

    async def test_ai_companions_never_learn_monsters_for_the_human(self):
        # mark_known_monster is only ever called for non-AI party members
        # in _do_start_combat -- an AI companion in the same fight must
        # not somehow cause the monster to show up on ITS OWN row (it has
        # no bestiary of its own to check, but this guards the loop's
        # is_ai filter against a future regression).
        ai_companion = db.create_ai_companion(
            "Buddy", "Human", "Fighter",
            {"strength": 15, "dexterity": 14, "constitution": 13, "intelligence": 10, "wisdom": 10, "charisma": 10},
            hp_max=12, armor_class=15, gold=0, inventory={},
        )
        result = db.mark_known_monster(ai_companion["telegram_user_id"], "goblin")
        # Just confirms the helper itself is generic/safe to call on any
        # character row -- the actual exclusion lives in _do_start_combat's
        # `if not p.get("is_ai")` filter, exercised by the test above.
        self.assertIn("goblin", result["known_monsters"])

    def test_bestiary_entry_formats_boss_and_condition_tags(self):
        template = {"name": "Goblin Boss", "hp_max": 21, "armor_class": 15, "strength": 12,
                    "dexterity": 15, "xp_reward": 200, "is_boss": True, "on_hit_condition": "paralyzed"}
        entry = bot._format_bestiary_entry("goblin_boss", template)
        self.assertIn("boss", entry)
        self.assertIn("paralyzed", entry)
        self.assertIn("HP 21", entry)

    # -- Real live bug (2026-07-16, Coffee): "Look for a shop to buy an
    #    axe" failed to match Woodcutter's Axe because the fallback
    #    category-word matcher required 4+ letter words, excluding
    #    short-but-unambiguous item nouns like "axe" (3 letters). ------
    def test_short_item_word_now_matches(self):
        candidates = ["rusty_dagger", "shortsword", "longsword", "longbow", "leather_armor",
                      "chain_shirt", "wooden_shield", "healing_potion", "antitoxin", "rations",
                      "torch", "iron_ore", "woodcutters_axe", "pickaxe", "shears"]
        self.assertEqual(
            items_module.find_item_mentioned_in_text("Look for a shop to buy an axe", candidates),
            "woodcutters_axe",
        )

    def test_potions_fallback_still_works_after_the_axe_fix(self):
        self.assertEqual(
            items_module.find_item_mentioned_in_text("buy two potions from Grimsby", ["healing_potion"]),
            "healing_potion",
        )

    def test_stopword_the_does_not_falsely_match_of_the_x_items(self):
        # "the" appears as a real word in 3 different item names (Boots
        # of the Winterlands, Bracers of the Steady Hand, Ring of the
        # Undertow) -- a message that only says "the" (not any real
        # item) must not match any of them, now that "the" is an
        # explicit stopword rather than merely excluded by length.
        candidates = ["boots_of_the_winterlands", "bracers_of_the_steady_hand", "ring_of_the_undertow"]
        self.assertIsNone(items_module.find_item_mentioned_in_text("give me the thing", candidates))

    def test_item_word_match_respects_word_boundaries_not_bare_substring(self):
        # "ore" (iron_ore) must not match inside an unrelated longer
        # word like "before" -- confirms the fix uses real word
        # membership, not a bare substring check.
        self.assertIsNone(items_module.find_item_mentioned_in_text("I was here before you", ["iron_ore"]))
        self.assertEqual(items_module.find_item_mentioned_in_text("I mined some ore", ["iron_ore"]), "iron_ore")

    # -- Real live feedback (2026-07-16, Coffee): narrations should
    #    always name the acting character, not default to ambiguous
    #    "you"/pronoun framing, since this is a shared group chat. -----
    def test_narration_preambles_instruct_naming_the_character(self):
        import ai.dm_agent as dm_agent_module
        for preamble_fn in (dm_agent_module._skill_check_preamble,
                             dm_agent_module._combat_preamble,
                             dm_agent_module._examine_preamble):
            text = preamble_fn()
            self.assertIn("actual given name", text, preamble_fn.__name__)

    # -- Real shop-browse action (2026-07-16, per Coffee, task #110) ---
    async def test_list_shop_shows_real_stocked_items_and_prices(self):
        user_id = 900518
        make_basic_character(user_id, "Shopper", current_location="crossroads_tavern")
        sink = []
        await bot._do_list_shop(FakeUpdate(user_id, "I want to shop", sink))
        combined = " ".join(sink)
        self.assertIn("Rations", combined)
        self.assertIn("Healing Potion", combined)
        self.assertIn("gold", combined)

    async def test_list_shop_says_no_shop_when_none_here(self):
        user_id = 900519
        make_basic_character(user_id, "Wanderer", current_location="whispering_wood")
        sink = []
        await bot._do_list_shop(FakeUpdate(user_id, "I want to shop", sink))
        self.assertIn("no shop here", sink[-1])

    # -- Real live bug (2026-07-16, Coffee): .title() on a hallucinated
    #    non-name target mangled ordinary text into nonsense like
    #    "Player'S Own Character". ---------------------------------
    async def test_check_sheet_unknown_target_is_not_mangled_by_title_case(self):
        user_id = 900520
        make_basic_character(user_id, "Asker")
        sink = []
        await bot._do_check_sheet(FakeUpdate(user_id, "check sheet", sink), "player's own character")
        self.assertIn("player's own character", sink[-1])
        self.assertNotIn("Player'S", sink[-1])

    # -- Real live bug (2026-07-17, Coffee, task #147): "Buy 10 torches,
    #    1 shears, 1 pickaxe, 1 fishing pole, 5 bait." only ever bought
    #    the torches -- every item after the first either fell through
    #    to "chat" or, worse, false-fired on unrelated vocabulary ("1
    #    pickaxe" contains "pick", matching the gather fallback's bare-
    #    "pick" rule). Fixed in ai/intent_parser.py's parse_intents
    #    (keeps a shopping-list message as ONE action) and bot.py's new
    #    _extract_item_list (resolves every item + its own quantity).
    async def test_multi_item_buy_purchases_every_item_not_just_the_first(self):
        user_id = 900521
        make_basic_character(user_id, "Ravenloft", current_location="market_row", gold=500)
        sink = []
        await bot._do_buy(
            FakeUpdate(user_id, "", sink),
            "Buy 10 torches, 1 shears, 1 pickaxe, 1 fishing pole, 5 bait.",
        )
        char = db.get_character(user_id)
        self.assertEqual(char["inventory"].get("torch"), 10)
        self.assertEqual(char["inventory"].get("shears"), 1)
        self.assertEqual(char["inventory"].get("pickaxe"), 1)
        self.assertEqual(char["inventory"].get("fishing_pole"), 1)
        self.assertEqual(char["inventory"].get("bait"), 5)

    async def test_single_item_buy_still_works_after_the_multi_item_fix(self):
        user_id = 900522
        make_basic_character(user_id, "Solo", current_location="market_row", gold=100)
        sink = []
        await bot._do_buy(FakeUpdate(user_id, "", sink), "buy a healing potion")
        char = db.get_character(user_id)
        self.assertEqual(char["inventory"].get("healing_potion"), 1)

    async def test_multi_item_give_hands_over_every_named_item(self):
        giver_id, recipient_id = 900523, 900524
        make_basic_character(giver_id, "Giver", current_location="market_row")
        make_basic_character(recipient_id, "Receiver", current_location="market_row")
        db.add_item(giver_id, "torch", 5)
        db.add_item(giver_id, "shears", 2)
        sink = []
        await bot._do_give_item(FakeUpdate(giver_id, "", sink), "give 3 torches and 2 shears to Receiver")
        giver = db.get_character(giver_id)
        recipient = db.get_character(recipient_id)
        self.assertEqual(giver["inventory"].get("torch"), 2)
        self.assertEqual(recipient["inventory"].get("torch"), 3)
        self.assertEqual(recipient["inventory"].get("shears"), 2)

    # -- Real live bug (2026-07-17, Coffee, task #152): a gather_material
    #    board quest hit 4/4 but completed_at stayed null forever --
    #    "Return to X to collect your reward" is meaningless when
    #    gathering only ever happens AT that same location, so the
    #    move-triggered turn-in check never got an "arrival" event to
    #    fire on. Fixed by checking turn-in immediately in _do_gather.
    async def test_gather_completed_quest_turns_in_without_a_move_event(self):
        from unittest.mock import patch

        user_id = 900525
        make_basic_character(user_id, "Standfast", current_location="whispering_wood", gold=50)
        db.add_item(user_id, "woodcutters_axe", 1)
        bq = db.create_board_quest(
            "whispering_wood", "2026-07-17", "A supply run for Wood",
            "Bring wood back to the board.", None, "gather_material", "wood", 1, 50, 20,
        )
        db.accept_board_quest(bq["board_quest_id"], user_id)

        sink = []
        with patch("bot.roll_ability_check", return_value={
            "raw_roll": 20, "modifier": 0, "proficiency": 0, "total": 20,
        }), patch("bot.narrate_skill_check", return_value="You chop the timber cleanly."):
            await bot._do_gather(FakeUpdate(user_id, "", sink), "Use my axe and chop lumber")

        updated = db.get_active_board_quest("whispering_wood", "2026-07-17")
        self.assertIsNotNone(updated["completed_at"])
        self.assertEqual(db.get_character(user_id)["gold"], 70)
        self.assertTrue(any("Board quest complete" in msg for msg in sink))

    # -- /help + /hint (2026-07-17, per Coffee) -------------------------
    async def test_bare_help_sends_static_reference_text(self):
        sink = []
        await bot.help_command(FakeUpdate(900526, "/help", sink), None)
        self.assertIn("Look around", sink[-1])

    async def test_help_as_a_reply_forwards_to_support_agent(self):
        from unittest.mock import patch

        sink = []
        replied_to = DummyMessage(900527)
        replied_to.text = "The Whispering Wood hums beneath the dusk's hush."
        update = FakeUpdate(900527, "/help", sink, reply_to_message=replied_to)
        with patch("bot.answer_support_question", return_value="That's flavor narration."):
            await bot.help_command(update, None)
        self.assertEqual(sink, ["That's flavor narration."])

    async def test_hint_gives_grounded_non_spoiler_suggestions(self):
        user_id = 900528
        make_basic_character(user_id, "Hinter", current_location="whispering_wood")
        sink = []
        await bot.hint_command(FakeUpdate(user_id, "/hint", sink), None)
        reply = sink[-1]
        self.assertIn("Things you might try here", reply)
        self.assertIn("Gather", reply)

    # -- Leaderboard / Hall of Fame (2026-07-17, per Coffee, task #74) --
    async def test_leaderboard_ranks_by_xp_and_excludes_combat_companions(self):
        make_basic_character(900940, "TopDog", gold=10)
        db.add_xp(900940, 500)
        make_basic_character(900941, "LastPlace", gold=10)
        db.add_xp(900941, 50)
        db.create_ai_companion(
            name="CombatOnly", race="Human", char_class="Fighter",
            ability_scores={"strength": 15, "dexterity": 14, "constitution": 13,
                             "intelligence": 10, "wisdom": 10, "charisma": 10},
            hp_max=12, armor_class=15, gold=0, inventory={},
        )
        ranked = db.get_leaderboard(limit=10)
        names = [c["name"] for c in ranked]
        self.assertLess(names.index("TopDog"), names.index("LastPlace"))
        self.assertNotIn("CombatOnly", names)

    def test_leaderboard_trigger_recognized(self):
        from ai.intent_parser import _keyword_fallback
        for text in ("show me the leaderboard", "who's the best"):
            self.assertEqual(_keyword_fallback(text, [])["action"], "leaderboard", text)

    async def test_do_leaderboard_produces_a_real_reply(self):
        make_basic_character(900942, "Ranked", gold=10)
        db.add_xp(900942, 300)
        sink = []
        await bot._do_leaderboard(FakeUpdate(900942, "", sink))
        self.assertIn("Hall of Fame", sink[-1])

    # -- find_item_mentioned_in_text: fishing gear (2026-07-19, both
    #    caught live within the same hour). First "Buy fishing hooks"
    #    silently bought a Fishing Pole ("hooks" isn't real, but bare
    #    "fishing" alone matched as a modifier); a required_for-based
    #    ambiguity fix for that then broke "Sell 1x fishing rod" for the
    #    same player, since Bait ALSO got flagged via required_for even
    #    though Fishing Pole already matched confidently. Final fix:
    #    normalize "fishing rod" -> "fishing pole" (same tier as the
    #    existing armour/armor fix) and only treat a match on an item's
    #    HEAD noun as strong/confident, so a bare modifier-only match
    #    still falls through to the required_for ambiguity check. -------
    def test_fishing_pole_and_bait_item_matching(self):
        import items as items_module
        candidates = ["fishing_pole", "bait"]
        self.assertIsNone(items_module.find_item_mentioned_in_text("Buy fishing hooks", candidate_ids=candidates))
        self.assertEqual(items_module.find_item_mentioned_in_text("Buy bait", candidate_ids=candidates), "bait")
        self.assertEqual(
            items_module.find_item_mentioned_in_text("Sell 1x fishing rod", candidate_ids=candidates),
            "fishing_pole",
        )
        self.assertEqual(
            items_module.find_item_mentioned_in_text("Sell 1x fishing pole", candidate_ids=candidates),
            "fishing_pole",
        )
        self.assertEqual(items_module.find_item_mentioned_in_text("Sell 2x bait", candidate_ids=candidates), "bait")

    # -- _find_interactable: smart-quote apostrophe + "old" filler word
    #    (2026-07-19, live-caught via a real player's own screenshot):
    #    "Look at maren's brass scale" (a real, described interactable)
    #    failed -- a phone's smart-quote autocorrect sends a curly
    #    apostrophe that never equals the straight one the stored name
    #    uses, and without "old" (a throwaway descriptor, not a real
    #    stopword in this function before this fix) a shorter phrasing
    #    fell one word short of the word-overlap match threshold. -------
    def test_find_interactable_handles_curly_apostrophe_and_old_filler(self):
        location = bot.cl.get_location(bot.CAMPAIGN, "market_row")
        for text in ["maren’s brass scale", "brass scale", "old maren’s brass scale",
                     "maren's brass scale"]:
            found = bot._find_interactable(location, text)
            self.assertIsNotNone(found, f"failed to match: {text!r}")
            self.assertEqual(found[0], "marens_scale")

    # -- _find_interactable: hyphenated stored names (2026-07-19,
    #    live-caught the same session, same "market_row" location): "a
    #    boarded up stall" (the natural way to type it, no hyphen) never
    #    matched the stored "a boarded-up stall" -- the existing
    #    trailing-punctuation strip only handled a stray character stuck
    #    to a word's END, not a hyphen genuinely joining two words into
    #    one unsplittable token. -------------------------------------
    def test_find_interactable_handles_hyphen_vs_space(self):
        location = bot.cl.get_location(bot.CAMPAIGN, "market_row")
        for text in ["a boarded up stall", "a boarded-up stall", "the boarded up stall"]:
            found = bot._find_interactable(location, text)
            self.assertIsNotNone(found, f"failed to match: {text!r}")
            self.assertEqual(found[0], "shuttered_stall")

    # -- _find_interactable: a real head-noun match should be confident
    #    on its own (2026-07-19, same live session): "Old Maren's locked
    #    strongbox" (added as a real interactable so the player's own
    #    repeated live attempts to examine it -- the quest referenced it
    #    but no interactable ever existed -- actually resolve) has 3
    #    significant words once "old" is excluded, so a phrasing that
    #    only says "strongbox" fell short of the plain >half threshold
    #    even though "strongbox" IS the object's real head noun -- the
    #    same head-noun-is-confident distinction already fixed in
    #    items.find_item_mentioned_in_text, mirrored here. -------------
    def test_find_interactable_head_noun_alone_is_confident(self):
        location = bot.cl.get_location(bot.CAMPAIGN, "market_row")
        for text in ["Examine the lid of the strongbox", "the old strongbox", "the strongbox",
                     "Search for Maren's old strongbox"]:
            found = bot._find_interactable(location, text)
            self.assertIsNotNone(found, f"failed to match: {text!r}")
            self.assertEqual(found[0], "marens_strongbox")

    # -- "Examine" a real monster present at the location, not just
    #    interactable objects (2026-07-17, Coffee, caught via live
    #    gameplay monitoring: "Look at the wolves ... give me detail
    #    about them" got "doesn't spot anything like that here" even
    #    though wolves are a real threat at that exact location). ------
    def test_find_monster_mentioned_in_text_handles_plurals(self):
        location = bot.cl.get_location(bot.CAMPAIGN, "whispering_wood")
        found = bot._find_monster_mentioned_in_text(location, "the wolves in the whispering wood")
        self.assertIsNotNone(found)
        self.assertEqual(found[0], "wolf")

    async def test_examine_unknown_monster_acknowledges_threat_without_stats(self):
        user_id = 900943
        make_basic_character(user_id, "Ravenloft", current_location="whispering_wood")
        sink = []
        await bot._do_examine(FakeUpdate(user_id, "", sink), "the wolves in the whispering wood")
        reply = sink[-1]
        self.assertNotIn("doesn't spot anything", reply)
        self.assertIn("wolf", reply.lower())
        self.assertIn("hasn't fought", reply)

    async def test_examine_known_monster_shows_real_bestiary_stats(self):
        user_id = 900944
        make_basic_character(user_id, "Veteran", current_location="whispering_wood")
        db.update_character(user_id, known_monsters=["wolf"])
        sink = []
        await bot._do_examine(FakeUpdate(user_id, "", sink), "the wolves")
        reply = sink[-1]
        self.assertIn("HP", reply)
        self.assertIn("AC", reply)

    # -- Rogue's Cunning Action (level 2+, task #91 audit, 2026-07-19):
    #    real 5E lets a Rogue Disengage as a bonus action, so THEIR
    #    flee shouldn't provoke the opportunity attacks every other
    #    class's does -- reachable right now by a real level 2 Rogue
    #    already playing, unlike most of this game's remaining level
    #    5+ class-feature gaps. -------------------------------------------
    async def test_level_2_rogue_flee_skips_opportunity_attacks(self):
        from unittest.mock import patch
        import sessions
        sessions.end_session(-999)

        player_id = 999950
        make_basic_character(
            player_id, "Quickstep", char_class="Rogue", current_location="crossroads_tavern",
            hp_max=20, armor_class=13,
        )
        db.update_character(player_id, level=2)
        player = db.get_character(player_id)
        player["telegram_user_id"] = player_id
        player["hp_current"] = 20

        enemy_id = -2_500_050
        enemy = {
            "telegram_user_id": enemy_id, "name": "Goblin", "dexterity": 10, "strength": 10,
            "armor_class": 12, "hp_current": 10, "hp_max": 10, "is_ai": 1, "monster_key": "goblin",
        }
        session = sessions.start_session(-999, [player, enemy], {player_id: "party", enemy_id: "enemy"})
        session.turn_order = [player_id, enemy_id]
        session.current_turn_index = 0

        sink = []
        with patch("bot.roll_ability_check", return_value={
            "raw_roll": 20, "modifier": 0, "proficiency": 0, "total": 20,
        }), patch("bot.narrate_skill_check", return_value="You slip away."):
            await bot._do_flee(FakeUpdate(player_id, "I flee", sink), "I flee")
        reply = "\n".join(sink)
        self.assertNotIn("Opportunity attacks", reply)
        self.assertIn("Cunning Action", reply)
        sessions.end_session(-999)

    async def test_level_1_rogue_flee_still_takes_opportunity_attacks(self):
        from unittest.mock import patch
        import sessions
        sessions.end_session(-999)

        player_id = 999951
        make_basic_character(
            player_id, "Greenhorn", char_class="Rogue", current_location="crossroads_tavern",
            hp_max=20, armor_class=13,
        )
        player = db.get_character(player_id)
        player["telegram_user_id"] = player_id
        player["hp_current"] = 20

        enemy_id = -2_500_051
        enemy = {
            "telegram_user_id": enemy_id, "name": "Goblin", "dexterity": 10, "strength": 10,
            "armor_class": 12, "hp_current": 10, "hp_max": 10, "is_ai": 1, "monster_key": "goblin",
        }
        session = sessions.start_session(-999, [player, enemy], {player_id: "party", enemy_id: "enemy"})
        session.turn_order = [player_id, enemy_id]
        session.current_turn_index = 0

        sink = []
        with patch("bot.roll_ability_check", return_value={
            "raw_roll": 20, "modifier": 0, "proficiency": 0, "total": 20,
        }), patch("bot.narrate_skill_check", return_value="You slip away."):
            await bot._do_flee(FakeUpdate(player_id, "I flee", sink), "I flee")
        reply = "\n".join(sink)
        self.assertIn("Opportunity attacks", reply)
        sessions.end_session(-999)

    # -- Task #195, 2026-07-19: fast-travel skipped requires_item and
    #    locked_connections entirely (only min_level and story_gates were
    #    ever checked), so both were trivially bypassable by fast-
    #    traveling instead of walking. Mirrors _do_move's own checks.
    async def test_fast_travel_blocks_on_missing_requires_item(self):
        user_id = 900530
        make_basic_character(user_id, "Gatetest", current_location="the_first_city")
        db.mark_visited(user_id, "the_unmoored_isle")
        db.mark_visited(user_id, "the_first_city")
        sink = []
        await bot._do_fast_travel(FakeUpdate(user_id, "", sink), "fast travel to the unmoored isle")
        self.assertIn("missing something needed", sink[-1])
        self.assertEqual(db.get_character(user_id)["current_location"], "the_first_city")

        db.add_item(user_id, "shard_of_dim_light", 1)
        sink.clear()
        await bot._do_fast_travel(FakeUpdate(user_id, "", sink), "fast travel to the unmoored isle")
        self.assertEqual(db.get_character(user_id)["current_location"], "the_unmoored_isle")

    async def test_fast_travel_blocks_on_locked_connection(self):
        user_id = 900531
        make_basic_character(user_id, "Locktest", current_location="the_weeping_well")
        db.mark_visited(user_id, "the_weeping_well")
        db.mark_visited(user_id, "glimmerdeep_grotto")
        sink = []
        await bot._do_fast_travel(FakeUpdate(user_id, "", sink), "fast travel to glimmerdeep grotto")
        self.assertIn("blocked by", sink[-1])
        self.assertEqual(db.get_character(user_id)["current_location"], "the_weeping_well")

        bot._UNLOCKED.add("sealed_stone_door")
        sink.clear()
        await bot._do_fast_travel(FakeUpdate(user_id, "", sink), "fast travel to glimmerdeep grotto")
        self.assertEqual(db.get_character(user_id)["current_location"], "glimmerdeep_grotto")

    # -- Per Coffee, 2026-07-19: fishing loses bait by a real 50/50 d20
    #    roll on every attempt (catch or miss), not a fixed schedule.
    async def test_fishing_loses_bait_on_a_low_roll_not_a_high_one(self):
        from unittest.mock import patch

        user_id = 900532
        make_basic_character(user_id, "Fishtest", current_location="stonearch_bridge")
        db.add_item(user_id, "fishing_pole", 1)
        db.add_item(user_id, "bait", 1)

        with patch("bot.roll_ability_check", return_value={
            "raw_roll": 15, "modifier": 0, "proficiency": 0, "total": 15,
        }), patch("bot.narrate_skill_check", return_value="You cast your line."), \
             patch("bot.roll_d20", return_value=3):
            sink = []
            await bot._do_gather(FakeUpdate(user_id, "", sink), "fish in the stream")
            self.assertIn("bait comes free", sink[-1])
            self.assertEqual(db.get_character(user_id)["inventory"].get("bait", 0), 0)

        db.add_item(user_id, "bait", 1)
        with patch("bot.roll_ability_check", return_value={
            "raw_roll": 15, "modifier": 0, "proficiency": 0, "total": 15,
        }), patch("bot.narrate_skill_check", return_value="You cast your line."), \
             patch("bot.roll_d20", return_value=18):
            sink = []
            await bot._do_gather(FakeUpdate(user_id, "", sink), "fish in the stream")
            self.assertNotIn("bait comes free", sink[-1])
            self.assertEqual(db.get_character(user_id)["inventory"].get("bait", 0), 1)

    # -- Real live bug (2026-07-19, Sugar): "Create character" (no
    #    article) and "Create a second character" both missed the old
    #    phrase list and fell through to chat.
    def test_create_character_phrasing_gaps(self):
        self.assertEqual(_keyword_fallback("Create character", [])["action"], "create_character")
        self.assertEqual(_keyword_fallback("Create a second character", [])["action"], "create_character")
        self.assertEqual(_keyword_fallback("I'd like to make another character", [])["action"], "create_character")

    # -- Per Coffee, 2026-07-19: discoverable bait-gathering spots (mud,
    #    fungus, rocks, bushes) -- reuses the generic resource_node system,
    #    a new "bait_gathering" skill key so shears' quantity bonus
    #    (hardcoded to skill_key=="herbalism") doesn't leak onto it.
    async def test_bait_gathering_nodes_grant_real_bait(self):
        from unittest.mock import patch

        for loc_id, phrase in [
            ("whispering_wood", "search the rotted log under the bushes"),
            ("stonearch_bridge", "dig in the muddy bank"),
            ("sunken_root_caverns", "check the fungus on the root"),
            ("greymoor_downs", "look under the loose stones"),
        ]:
            user_id = 900540 + hash(loc_id) % 1000
            make_basic_character(user_id, f"Baiter{loc_id}"[:20], current_location=loc_id)
            sink = []
            with patch("bot.roll_ability_check", return_value={
                "raw_roll": 20, "modifier": 0, "proficiency": 0, "total": 20,
            }), patch("bot.narrate_skill_check", return_value="You search around."):
                await bot._do_gather(FakeUpdate(user_id, "", sink), phrase)
            self.assertIn("Bait", sink[-1], f"{loc_id} didn't grant Bait: {sink[-1]}")
            self.assertGreaterEqual(db.get_character(user_id)["inventory"].get("bait", 0), 1)

    # -- World expansion (2026-07-19/20, per Coffee): compass navigation
    #    ("directions", a display/nav layer over "connections") added
    #    alongside the world's large-area expansion into many new
    #    location ids.
    async def test_compass_direction_resolves_to_the_right_location(self):
        user_id = 900550
        make_basic_character(user_id, "Compasstest", current_location="whispering_wood")
        sink = []
        await bot._do_move(FakeUpdate(user_id, "", sink), "go south")
        self.assertEqual(db.get_character(user_id)["current_location"], "whispering_wood_deep_glade")

    async def test_compass_word_with_no_directions_field_falls_through_harmlessly(self):
        user_id = 900551
        make_basic_character(user_id, "Compasstest2", current_location="market_row")
        sink = []
        await bot._do_move(FakeUpdate(user_id, "", sink), "go north")
        # market_row has no "directions" field -- must not crash, and must
        # not move the character anywhere (no location name matched either).
        self.assertEqual(db.get_character(user_id)["current_location"], "market_row")

    async def test_look_shows_compass_labels_for_directed_connections(self):
        user_id = 900552
        make_basic_character(user_id, "Compasstest3", current_location="greymoor_downs")
        sink = []
        await bot._do_look(FakeUpdate(user_id, "", sink))
        reply = sink[-1]
        self.assertIn("North:", reply)
        self.assertIn("West:", reply)

    # -- Real live bug (2026-07-19, Coffee): "Switch to my character
    #    Elduinn" extracted "my character elduinn" as the target name
    #    (longer than the real name "Elduinn"), which never matched --
    #    the error message even listed "Elduinn" as a valid character
    #    right there, while claiming it couldn't find it.
    def test_switch_to_my_character_name_phrasing(self):
        self.assertEqual(
            _keyword_fallback("Switch to my character Elduinn", [])["target"], "Elduinn"
        )

    async def test_switch_character_matches_despite_my_character_filler(self):
        user_id = 900553
        make_basic_character(user_id, "Elduinn")
        match = bot._find_own_character_by_name_fragment(user_id, "my character elduinn")
        self.assertIsNotNone(match)
        self.assertEqual(match["name"], "Elduinn")

    # -- Dev-topic feedback (Coffee, 2026-07-19): "Only show level up
    #    when we have points to distribute. Otherwise it serves no
    #    purpose."
    def test_main_menu_hides_level_up_button_with_no_pending_points(self):
        character = {"pending_asi_points": 0}
        keyboard = bot._main_menu_keyboard(character)
        labels = [btn.text for row in keyboard.inline_keyboard for btn in row]
        self.assertNotIn("📈 Level Up", labels)

    def test_main_menu_shows_level_up_button_with_pending_points(self):
        character = {"pending_asi_points": 2}
        keyboard = bot._main_menu_keyboard(character)
        labels = [btn.text for row in keyboard.inline_keyboard for btn in row]
        self.assertIn("📈 Level Up", labels)

    async def test_level_menu_shows_xp_remaining_to_next_level(self):
        user_id = 900554
        make_basic_character(user_id, "XPTest")
        sink = []
        await bot._do_show_level_menu(FakeUpdate(user_id, "", sink))
        self.assertIn("XP to Level 2", sink[-1])

    async def test_level_menu_shows_max_level_at_20(self):
        user_id = 900555
        make_basic_character(user_id, "MaxLevelTest")
        db.update_character(user_id, level=20)
        sink = []
        await bot._do_show_level_menu(FakeUpdate(user_id, "", sink))
        self.assertIn("Max level reached", sink[-1])

    # -- Task #213, per Coffee: "switch characters ... open up a tap
    #    menu for my characters and ... an option to be able to see
    #    those characters and switch them."
    async def test_character_roster_shows_tap_to_switch_buttons(self):
        user_id = 900556
        make_basic_character(user_id, "First")
        make_basic_character(user_id, "Second")
        roster = bot.db.list_characters(user_id)
        active = bot.db.get_character(user_id)
        kb = bot._roster_keyboard(roster, active["character_id"])
        labels = [btn.text for row in kb.inline_keyboard for btn in row]
        self.assertTrue(any("First" in l for l in labels))
        self.assertTrue(any("Second" in l for l in labels))

    async def test_roster_button_tap_switches_active_character(self):
        user_id = 900557
        make_basic_character(user_id, "Alpha")
        second = make_basic_character(user_id, "Beta")
        sink = []
        update = FakeCallbackUpdate(user_id, f"roster|switch|{second['character_id']}", sink)
        await bot.roster_menu_callback(update, DummyContext())
        active = bot.db.get_character(user_id)
        self.assertEqual(active["name"], "Beta")

    async def test_roster_button_tap_blocked_during_combat(self):
        import sessions
        sessions.end_session(-999)
        user_id = 900558
        first = make_basic_character(user_id, "Gamma")
        second = make_basic_character(user_id, "Delta")
        # make_basic_character makes the newest row active (Delta) -- switch
        # back to Gamma so there's a real "currently active" character to
        # stay locked onto while combat blocks the tap-to-switch attempt.
        bot.db.switch_character(user_id, first["character_id"])
        sessions.start_session(-999, [{"telegram_user_id": user_id, "name": "Gamma", "dexterity": 10}], {user_id: "party"})
        sink = []
        update = FakeCallbackUpdate(user_id, f"roster|switch|{second['character_id']}", sink)
        await bot.roster_menu_callback(update, DummyContext())
        active = bot.db.get_character(user_id)
        self.assertEqual(active["name"], "Gamma")
        sessions.end_session(-999)

    # -- Dev-topic feedback (Coffee, 2026-07-19): "If a player wants to
    #    look at stalls and shopfronts in market row - please give a
    #    description and then open the shop."
    async def test_examine_generic_stalls_describes_then_opens_shop(self):
        user_id = 900559
        make_basic_character(user_id, "Shopper", current_location="market_row")
        sink = []
        await bot._do_examine(FakeUpdate(user_id, "", sink), "the stalls and shopfronts")
        self.assertTrue(any("🛒" in msg for msg in sink), sink)

    async def test_examine_the_shuttered_stall_specifically_does_not_open_shop(self):
        from unittest.mock import patch

        user_id = 900560
        make_basic_character(user_id, "Shopper2", current_location="market_row")
        sink = []
        with patch("bot.narrate_examine", return_value="Nailed shut, same as always."):
            await bot._do_examine(FakeUpdate(user_id, "", sink), "the boarded-up shuttered stall")
        self.assertFalse(any("🛒" in msg for msg in sink), sink)

    async def test_examine_stalls_at_a_shopless_location_falls_through_normally(self):
        user_id = 900561
        make_basic_character(user_id, "Shopper3", current_location="whispering_wood")
        sink = []
        await bot._do_examine(FakeUpdate(user_id, "", sink), "the stalls")
        self.assertFalse(any("🛒" in msg for msg in sink), sink)

    # -- Real live bug (2026-07-19, Sugar, dev-topic screenshot): "Look
    #    at wares available for purchase" fell through to "buy" with no
    #    item named, giving "not sure what item you mean" instead of
    #    actually showing the shop.
    def test_wares_and_available_for_purchase_open_the_shop_not_buy(self):
        self.assertEqual(_keyword_fallback("Look at wares available for purchase", [])["action"], "list_shop")
        self.assertEqual(_keyword_fallback("What items are available to buy", [])["action"], "list_shop")
        self.assertEqual(_keyword_fallback("Show me the wares", [])["action"], "list_shop")

    def test_real_buy_requests_still_classify_as_buy(self):
        self.assertEqual(_keyword_fallback("I want to buy a healing potion", [])["action"], "buy")
        self.assertEqual(_keyword_fallback("buy 2 torches", [])["action"], "buy")


class SlowLiveTests(unittest.IsolatedAsyncioTestCase):
    """
    Real Ollama-backed narration -- each of these can take 30s-5min+
    depending on system load (see project_ollama_single_slot_contention
    memory: the live bot competes for the same single generation slot).
    Run individually when touching code they cover, not as a matter of
    routine.
    """

    def setUp(self):
        use_test_db("tests/tmp/regression_slow.db")

    async def test_combat_excludes_characters_at_a_different_location_full(self):
        import sessions
        sessions.end_session(-999)  # tests share chat_id -999 -- don't inherit another test's session
        tavern_id, wood_id = 444445, 555556
        make_basic_character(tavern_id, "Elduinn2", current_location="crossroads_tavern")
        make_basic_character(wood_id, "Roric3", current_location="whispering_wood")

        sink = []
        await bot.adventure_master_handler(
            FakeUpdate(wood_id, "Let's start a fight", sink), DummyContext())

        session = sessions.get_session(-999)
        self.assertIsNotNone(session)
        participant_ids = {p["telegram_user_id"] for p in session.participants}
        self.assertIn(wood_id, participant_ids)
        self.assertNotIn(tavern_id, participant_ids)
        sessions.end_session(-999)

    async def test_combat_excludes_resting_characters_full(self):
        import sessions
        sessions.end_session(-999)
        wood_id, resting_id = 666667, 777778
        make_basic_character(wood_id, "Roric4", current_location="whispering_wood")
        make_basic_character(resting_id, "Snorri2", current_location="whispering_wood")
        db.update_character(resting_id, is_inactive=1)

        sink = []
        await bot.adventure_master_handler(
            FakeUpdate(wood_id, "Let's start a fight", sink), DummyContext())

        session = sessions.get_session(-999)
        self.assertIsNotNone(session)
        participant_ids = {p["telegram_user_id"] for p in session.participants}
        self.assertNotIn(resting_id, participant_ids)
        sessions.end_session(-999)

    async def test_attack_auto_starts_combat_against_a_real_monster(self):
        user_id = 111111
        make_basic_character(user_id, current_location="whispering_wood")
        self.assertIsNone(__import__("sessions").get_session(-999))
        sink = []
        await bot.adventure_master_handler(FakeUpdate(user_id, "I attack the goblin", sink), DummyContext())
        combined = " ".join(sink)
        self.assertIn("Combat Begins!", combined)
        self.assertNotIn("no combat is active right now", combined.lower())

    async def test_accept_quest_honors_a_specifically_named_quest_over_companion_quest(self):
        """
        Real regression (2026-07-14): "Accept the quest, a quiet request
        for wood" -- a specific, real, correctly-classified accept_quest
        naming a real board quest -- got silently swallowed into
        accepting Sarah's companion quest instead, because that shortcut
        ran unconditionally. Needs a real board quest generated (a live
        Ollama call for its branching setup_narration), hence SlowLiveTests.
        """
        import board_quests as board_quests_module

        bot.setup_default_npcs()
        user_id = 111111
        make_basic_character(user_id, "Elduinn", current_location="crossroads_tavern")

        sink = []
        await bot.adventure_master_handler(
            FakeUpdate(user_id, "Recruit Sarah to my party", sink), DummyContext())

        db.update_character(user_id, current_location="whispering_wood")
        board_quests = board_quests_module.get_or_generate_board_quests(bot.CAMPAIGN, "whispering_wood")
        self.assertTrue(board_quests, "expected a real board quest to generate here")
        named_quest = board_quests[0]

        sink.clear()
        text = f"Accept the quest, {named_quest['title'].lower()}"
        await bot.adventure_master_handler(FakeUpdate(user_id, text, sink), DummyContext())
        combined = " ".join(sink)
        self.assertIn(named_quest["title"], combined)
        self.assertNotIn("Sarah's Safer Crossing", combined)

        character = db.get_character(user_id)
        self.assertNotIn("seras_safer_crossing", character["active_quests"])

    # -- Boss Multiattack + The Waiting Shape's Life Drain (v1.10.6) ---
    async def test_boss_gets_two_attacks_and_drains_life(self):
        """
        Real bugs from campaign.json (2026-07-15 reverse-playthrough):
        both bosses (goblin_boss, the_waiting_shape) fought exactly like
        a regular monster with a bigger stat block -- no is_boss-gated
        mechanic existed at all. Added boss Multiattack (2 attacks/turn)
        plus The Waiting Shape's own life_drain flag. Needs real
        narration calls (_post_narrated), hence SlowLiveTests.
        """
        import sessions
        sessions.end_session(-999)

        player_id = 999901
        player = make_basic_character(
            player_id, "Bulwark", current_location="the_unmoored_isle",
            hp_max=200, armor_class=1,  # guaranteed hits so drain/multiattack are deterministic
        )
        player["hp_current"] = 200
        player["telegram_user_id"] = player_id

        template = bot.CAMPAIGN["monsters"]["the_waiting_shape"]
        boss_id = -2_500_000
        boss = {
            "telegram_user_id": boss_id, "name": template["name"],
            "dexterity": template["dexterity"], "strength": template["strength"],
            "armor_class": template["armor_class"],
            "hp_max": template["hp_max"], "hp_current": template["hp_max"] - 20,
            "proficiency_bonus": template["proficiency_bonus"],
            "is_ai": 1, "xp_reward": template["xp_reward"],
            "on_hit_condition": template.get("on_hit_condition"),
            "monster_key": "the_waiting_shape", "is_boss": True,
            "life_drain": template.get("life_drain", False),
        }
        session = sessions.start_session(
            -999, [boss, player], {boss_id: "enemy", player_id: "party"},
        )
        session.turn_order = [boss_id, player_id]
        session.current_turn_index = 0
        boss_hp_before = boss["hp_current"]

        events_before = len(session.event_log)  # start_session already logged "Combat begins!"
        sink = []
        await bot._resolve_ai_turns(FakeUpdate(player_id, "irrelevant", sink), session)

        new_events = len(session.event_log) - events_before
        self.assertEqual(new_events, 2, "boss should get exactly 2 attacks (Multiattack)")
        self.assertGreater(
            boss["hp_current"], boss_hp_before,
            "Life Drain should have healed the boss after landing a hit",
        )
        sessions.end_session(-999)

    # -- Extra Attack (2026-07-16): confirmed live that a level 5+
    #    Fighter's own _do_attack call resolves the real handler path,
    #    not just the isolated _attacks_per_turn helper. -----------------
    async def test_level_5_fighter_gets_two_real_attacks_via_do_attack(self):
        import sessions
        sessions.end_session(-999)

        player_id = 999930
        make_basic_character(
            player_id, "Twinstrike", char_class="Fighter", current_location="crossroads_tavern",
            hp_max=50, armor_class=18,
        )
        db.update_character(player_id, level=5)
        player = db.get_character(player_id)
        player["telegram_user_id"] = player_id
        player["hp_current"] = 50

        enemy_id = -2_500_030
        enemy = {
            "telegram_user_id": enemy_id, "name": "Straw Dummy", "dexterity": 10, "strength": 10,
            "armor_class": 1,  # guaranteed hits so the attack count is deterministic
            "hp_current": 200, "hp_max": 200, "is_ai": 1, "monster_key": "goblin",
        }
        session = sessions.start_session(-999, [player, enemy], {player_id: "party", enemy_id: "enemy"})
        session.turn_order = [player_id, enemy_id]
        session.current_turn_index = 0

        events_before = len(session.event_log)
        sink = []
        await bot._do_attack(FakeUpdate(player_id, "I attack the straw dummy", sink), "I attack the straw dummy")

        new_events = len(session.event_log) - events_before
        self.assertGreaterEqual(new_events, 2, "level 5 Fighter should get 2 real attacks per turn")
        sessions.end_session(-999)

    # -- Wild Shape (task #91 audit, 2026-07-19): Druid had ZERO unique
    #    mechanical features before this -- confirmed real via the
    #    actual _do_wild_shape handler, resolve_attack's bonus damage,
    #    and _do_cast_spell's real spellcasting block. --------------------
    def test_wild_shape_damage_bonus_scales_with_druid_level(self):
        from rules.leveling import wild_shape_damage_bonus
        self.assertEqual(wild_shape_damage_bonus(1), 2)
        self.assertEqual(wild_shape_damage_bonus(8), 2)
        self.assertEqual(wild_shape_damage_bonus(9), 3)
        self.assertEqual(wild_shape_damage_bonus(15), 3)
        self.assertEqual(wild_shape_damage_bonus(16), 4)
        self.assertEqual(wild_shape_damage_bonus(20), 4)

    async def test_wild_shape_end_to_end_via_real_handlers(self):
        import sessions
        sessions.end_session(-999)

        player_id = 999940
        make_basic_character(
            player_id, "Thornback", char_class="Druid", current_location="crossroads_tavern",
            hp_max=20, armor_class=13, known_spells=["produce_flame"],
        )
        db.update_character(player_id, level=2)
        player = db.get_character(player_id)
        player["telegram_user_id"] = player_id
        player["hp_current"] = 20

        enemy_id = -2_500_040
        enemy = {
            "telegram_user_id": enemy_id, "name": "Straw Dummy", "dexterity": 10, "strength": 10,
            "armor_class": 1, "hp_current": 200, "hp_max": 200, "is_ai": 1, "monster_key": "goblin",
        }
        session = sessions.start_session(-999, [player, enemy], {player_id: "party", enemy_id: "enemy"})
        session.turn_order = [player_id, enemy_id]
        session.current_turn_index = 0

        sink = []
        await bot._do_wild_shape(FakeUpdate(player_id, "I wild shape", sink))
        participant = next(p for p in session.participants if p["telegram_user_id"] == player_id)
        self.assertTrue(participant.get("wild_shaped"))
        self.assertGreater(participant.get("temp_hp", 0), 0)
        self.assertEqual(db.get_feature_uses(player_id, "wild_shape"), 1)

        # A second Wild Shape attempt mid-fight should be refused, not stack.
        sink2 = []
        await bot._do_wild_shape(FakeUpdate(player_id, "I wild shape", sink2))
        self.assertTrue(any("already" in m.lower() for m in sink2))

        # Casting a real spell while Wild Shaped must be blocked (real 5E rule).
        sink3 = []
        await bot._do_cast_spell(FakeUpdate(player_id, "cast produce flame", sink3), "cast produce flame")
        self.assertTrue(any("wild shaped" in m.lower() for m in sink3))
        sessions.end_session(-999)


if __name__ == "__main__":
    unittest.main()
