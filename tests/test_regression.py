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
