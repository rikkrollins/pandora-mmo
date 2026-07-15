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
from ai.intent_parser import _keyword_fallback
from ai.support_agent import _deterministic_inventory_answer
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
            await __import__("asyncio").sleep(2.5)
            self.assertTrue(update.effective_chat.last_sent_message.deleted)
        finally:
            db.set_setting("tts_enabled", "0")

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


if __name__ == "__main__":
    unittest.main()
