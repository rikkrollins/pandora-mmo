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
from ai.intent_parser import _keyword_fallback
from ai.support_agent import _deterministic_inventory_answer
from rules.combat import resolve_attack
from rules.crafting import RECIPES
from tests.helpers import DummyContext, FakeUpdate, make_basic_character, use_test_db


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
        known_npcs = ["Grimsby", "Old Maren", "Sera", "Theron", "Kess",
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

    # -- Stuck-location bug (v1.7.4) -----------------------------------
    def test_move_covers_generic_leave_phrasing(self):
        for text in ("Go downstairs", "Leave this area", "Leave this room", "Head upstairs"):
            self.assertEqual(_keyword_fallback(text, [])["action"], "move", text)

    def test_leave_the_party_not_hijacked_by_move(self):
        self.assertEqual(_keyword_fallback("Leave the party", [])["action"], "leave_party")

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

    # -- Combat scoped to the fight's real location (post-1.8.3) -------
    async def test_combat_excludes_characters_at_a_different_location(self):
        import sessions
        sessions.end_session(-999)  # tests share chat_id -999 -- don't inherit another test's session
        tavern_id, wood_id = 444444, 555555
        make_basic_character(tavern_id, "Elduinn", current_location="crossroads_tavern")
        make_basic_character(wood_id, "Roric", current_location="whispering_wood")

        sink = []
        await bot.adventure_master_handler(
            FakeUpdate(wood_id, "Let's start a fight", sink), DummyContext())

        session = sessions.get_session(-999)
        self.assertIsNotNone(session)
        participant_ids = {p["telegram_user_id"] for p in session.participants}
        self.assertIn(wood_id, participant_ids)
        self.assertNotIn(tavern_id, participant_ids)
        sessions.end_session(-999)

    async def test_combat_excludes_resting_characters(self):
        import sessions
        sessions.end_session(-999)
        wood_id, resting_id = 666666, 777777
        make_basic_character(wood_id, "Roric2", current_location="whispering_wood")
        make_basic_character(resting_id, "Snorri", current_location="whispering_wood")
        db.update_character(resting_id, is_inactive=1)

        sink = []
        await bot.adventure_master_handler(
            FakeUpdate(wood_id, "Let's start a fight", sink), DummyContext())

        session = sessions.get_session(-999)
        self.assertIsNotNone(session)
        participant_ids = {p["telegram_user_id"] for p in session.participants}
        self.assertNotIn(resting_id, participant_ids)
        sessions.end_session(-999)

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
            sera_id, "Sera", "Elf", "Ranger",
            {"strength": 12, "dexterity": 17, "constitution": 13,
             "intelligence": 11, "wisdom": 15, "charisma": 10},
            hp_max=11, armor_class=14, gold=50, inventory={}, known_spells=[], is_ai=True,
        )
        sink = []
        update = FakeUpdate(coffee_id, "Show me my character sheet and Sera's character sheet",
                             sink, thread_id=bot.config.TOPIC_SUPPORT_ID)
        await bot.support_topic_handler(update, DummyContext())
        reply = sink[-1]
        self.assertIn("Elduinn", reply)
        self.assertIn("AC 14", reply)   # Sera's REAL AC, not invented
        self.assertIn("HP 11/11", reply)  # Sera's REAL HP, not invented
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
        known_npcs = ["Sera"]
        self.assertEqual(_keyword_fallback("Recruit sera to my party", known_npcs)["action"], "recruit_npc")
        self.assertEqual(_keyword_fallback("Invite Sera to my party", known_npcs)["action"], "recruit_npc")

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
            # 10+ seconds while Ollama holds the CPU) a flat 2.5s sleep
            # occasionally loses the race against the cleanup task's own
            # 2s internal sleep, flaking a test that isn't actually
            # broken. Polling up to 15s gives real headroom either way.
            asyncio = __import__("asyncio")
            for _ in range(30):
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

    # -- Real live bug (2026-07-15): Sera (sera_wanderer) is both
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
        self.assertEqual(len(found_at), 1, "Sera should be listed at exactly one real location")
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
        db.update_character(user_id, level=3)
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
        result = _keyword_fallback("give my healing potion to Sera", [])
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
        for _ in range(20):
            result = resolve_attack(dict(attacker), defender, weapon, round_number=1)
            if result["hit"] and not result["critical_hit"]:
                self.assertTrue(result["uncanny_dodge_triggered"])
                self.assertEqual(result["damage_dealt"], 4)
                return
        self.fail("no plain hit landed in 20 tries at AC 1")

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
        accepting Sera's companion quest instead, because that shortcut
        ran unconditionally. Needs a real board quest generated (a live
        Ollama call for its branching setup_narration), hence SlowLiveTests.
        """
        import board_quests as board_quests_module

        bot.setup_default_npcs()
        user_id = 111111
        make_basic_character(user_id, "Elduinn", current_location="crossroads_tavern")

        sink = []
        await bot.adventure_master_handler(
            FakeUpdate(user_id, "Recruit Sera to my party", sink), DummyContext())

        db.update_character(user_id, current_location="whispering_wood")
        board_quests = board_quests_module.get_or_generate_board_quests(bot.CAMPAIGN, "whispering_wood")
        self.assertTrue(board_quests, "expected a real board quest to generate here")
        named_quest = board_quests[0]

        sink.clear()
        text = f"Accept the quest, {named_quest['title'].lower()}"
        await bot.adventure_master_handler(FakeUpdate(user_id, text, sink), DummyContext())
        combined = " ".join(sink)
        self.assertIn(named_quest["title"], combined)
        self.assertNotIn("Sera's Safer Crossing", combined)

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


if __name__ == "__main__":
    unittest.main()
