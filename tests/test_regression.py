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
import time
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

import requests

import bot
import campaign_loader as cl
import config
import db
import guilds
import items as items_module
import spells
import topics
from ai.intent_parser import _keyword_fallback, parse_intents
from ai.support_agent import (
    _deterministic_inventory_answer, _deterministic_location_connections_answer, _deterministic_quest_task_answer,
)
from ai.text_cleanup import strip_think_tags
from rules.combat import resolve_attack
from rules.crafting import RECIPES
from tests.helpers import (
    DummyContext, DummyMessage, FakeBot, FakeCallbackUpdate, FakeChat, FakeUpdate, FakeUser,
    make_basic_character, use_test_db,
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

    # -- "Gaze at X" fell through to the low-confidence chat default,
    #    which the model then resolved as generic whole-area 'look'
    #    instead of 'examine' on the named object (2026-08-07, real
    #    player, caught via topic-activity monitoring) --
    def test_gaze_at_classified_as_examine(self):
        for text in ("Gaze at the pool of water", "I gazed at the ancient tree"):
            self.assertEqual(_keyword_fallback(text, [])["action"], "examine", text)

    # -- "Feel the X" misclassified as silent chat, not examine
    #    (2026-08-06, real player, caught via topic-activity monitoring) --
    def test_feel_the_x_classified_as_examine(self):
        for text in ("Feel the pulse on the weathered waystone", "I feel the tree", "She felt the wall"):
            self.assertEqual(_keyword_fallback(text, [])["action"], "examine", text)

    # -- "Take/grab X from Y" fell through to the silent chat default --
    #    no generic take/pick-up-from-environment mechanic exists, only
    #    real interactables you can examine (bot.py's _find_interactable
    #    already correctly matches this exact phrasing to the real
    #    interactable; only the classification was missing)
    #    (2026-08-09, real player, caught via topic-activity monitoring) --
    def test_take_x_from_y_classified_as_examine(self):
        for text in ("Take a key from the locksmith window", "Grab the letter from the desk",
                     "I took the coin from the fountain"):
            self.assertEqual(_keyword_fallback(text, [])["action"], "examine", text)

    def test_take_without_a_from_clause_stays_chat_so_the_model_can_still_call_use_item(self):
        """
        Deliberately narrower than touch/gaze/feel above: a bare "take/
        grab the X" with no "from" clause is far more often really about
        a carried, already-owned consumable ("take the healing potion" =
        drink it) than a location prop. This fallback must keep
        returning 'chat' (no opinion) for that bare phrasing so the real
        model's own -- typically correct -- use_item read passes through
        untouched, per the trust-priority rule (only a non-chat fallback
        opinion ever overrides the model).
        """
        for text in ("Take the healing potion", "I take the antitoxin", "grab my sword"):
            self.assertEqual(_keyword_fallback(text, [])["action"], "chat", text)

    # -- "Look under X" fell through to the low-confidence chat default,
    #    which the model then resolved as generic 'look' instead of
    #    'examine' on the named object (2026-08-06, real player, caught
    #    via topic-activity monitoring) --
    def test_look_under_classified_as_examine(self):
        result = _keyword_fallback("Look under the hollow stump shrine", [])
        self.assertEqual(result["action"], "examine")
        self.assertEqual(result["target"], "hollow stump shrine")

    # -- "Invite X ti my party" (typo for "to") fell through to talk_npc
    #    instead of recruit_npc/invite_to_party (2026-08-06, real player,
    #    caught via topic-activity monitoring) --
    def test_invite_npc_ti_my_party_typo_classified_as_recruit_npc(self):
        result = _keyword_fallback("Invite pip thistledown ti my party", ["Pip Thistledown"])
        self.assertEqual(result["action"], "recruit_npc")
        self.assertEqual(result["npc_name"], "Pip Thistledown")

    def test_invite_player_ti_my_party_typo_classified_as_invite_to_party(self):
        result = _keyword_fallback("Invite Sheri ti my party", ["Pip Thistledown"])
        self.assertEqual(result["action"], "invite_to_party")
        self.assertEqual(result["target"], "Sheri")

    def test_feel_as_an_emotion_verb_doesnt_misfire(self):
        # "feel" is overwhelmingly an EMOTION verb in ordinary English --
        # confirmed live that folding it into the shared examine-verb
        # regex (which has an OPTIONAL article) misfired on exactly this
        # phrasing. The fix requires a real article ("the"/"a"/"an")
        # right after the verb, which these phrases never have.
        for text in ("I am feeling great today", "I feel great", "I feel like resting"):
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

    # -- Real live incident (2026-08-06, a real player): "Look at quests"
    #    misclassified as examine, which would try to inspect a literal
    #    in-world object named "quests" instead of showing the quest log --
    #    the same recurring "bare quest phrasing with no qualifier word"
    #    gap this file has hit many times before (#92, #99, #109, #151,
    #    #154, #161, #185), just with "look at" as the filler this time.
    def test_look_at_quests_not_misclassified_as_examine(self):
        for text in ("Look at quests", "look at quest", "view my quests", "view quest", "see my quests"):
            self.assertEqual(_keyword_fallback(text, [])["action"], "check_quests", text)
        # Regression guard: a real object after "look at" must still
        # route to examine, not get swallowed by the new quest phrasing.
        self.assertEqual(_keyword_fallback("look at the strange amulet", [])["action"], "examine")

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
        character = db.get_character(user_id, -999)
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
        eligible_ids = {p["telegram_user_id"] for p in bot._get_combat_eligible_party_members("whispering_wood", -999)}
        self.assertIn(wood_id, eligible_ids)
        self.assertNotIn(tavern_id, eligible_ids)

    def test_combat_excludes_resting_characters(self):
        wood_id, resting_id = 666666, 777777
        make_basic_character(wood_id, "Roric2", current_location="whispering_wood")
        make_basic_character(resting_id, "Snorri", current_location="whispering_wood")
        db.update_character(resting_id, -999, is_inactive=1)
        eligible_ids = {p["telegram_user_id"] for p in bot._get_combat_eligible_party_members("whispering_wood", -999)}
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
            sera_id, -999, "Sarah", "Elf", "Ranger",
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
        db.update_character(user_id, -999, level=10)

        sink = []
        await bot.adventure_master_handler(
            FakeUpdate(user_id, "Ascend to the Unmoored Isle", sink), DummyContext())
        character = db.get_character(user_id, -999)
        self.assertEqual(character["current_location"], "the_first_city")  # blocked, no shard

        db.add_item(user_id, -999, "shard_of_dim_light", 1)
        sink.clear()
        await bot.adventure_master_handler(
            FakeUpdate(user_id, "Ascend to the Unmoored Isle", sink), DummyContext())
        character = db.get_character(user_id, -999)
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

    # -- Support game-wiki expansion (2026-08-08): location connections
    #    and quest task never need Ollama either, same "one correct
    #    answer" reasoning as inventory/XP/active-character above. Real
    #    live bug this replaces: the LLM path answered "What connects to
    #    Market Row?" with the nonsensical "Market Row connects directly
    #    to Market Row."
    def test_location_connections_question_answered_without_ollama(self):
        visited = cl.get_all_location_ids(bot.CAMPAIGN)[:3]
        character = {"visited_locations": visited}
        loc = cl.get_location(bot.CAMPAIGN, visited[0])
        answer = _deterministic_location_connections_answer(character, f"What connects to {loc['name']}?")
        self.assertIsNotNone(answer)
        self.assertNotIn(f"{loc['name']} connects directly to {loc['name']}", answer)

    def test_location_connections_question_ignores_unvisited_locations(self):
        all_ids = cl.get_all_location_ids(bot.CAMPAIGN)
        character = {"visited_locations": all_ids[:1]}
        unvisited_loc = cl.get_location(bot.CAMPAIGN, all_ids[5])
        answer = _deterministic_location_connections_answer(character, f"What connects to {unvisited_loc['name']}?")
        self.assertIsNone(answer)

    def test_quest_task_question_answered_without_ollama(self):
        quest_id = next(iter(bot.CAMPAIGN["quests"]))
        quest = bot.CAMPAIGN["quests"][quest_id]
        character = {"active_quests": {quest_id: True}}
        answer = _deterministic_quest_task_answer(character)
        self.assertIn(quest["title"], answer)
        self.assertIn(quest["description"], answer)

    def test_quest_task_question_with_no_active_quests(self):
        self.assertIn("don't have any active", _deterministic_quest_task_answer({"active_quests": {}}))

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
        self.assertNotIn("sera_wanderer", bot._NPC_LOCATIONS.get(bot._LAST_KNOWN_CHAT_ID, {}))
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
        # Arcane Circle exclusive spells (2026-07-25) are deliberately NOT
        # in any CLASS_SPELL_LISTS entry -- spells.py's own comment above
        # starfall_lance/voidcall says so -- reachable only via guild
        # membership + bot.py's _do_learn_guild_spell. This test predates
        # that real reachability path, same stale-assumption shape as the
        # 3 tests fixed in 822e728.
        referenced.update(guilds.ARCANE_CIRCLE_EXCLUSIVE_SPELLS)
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
        # Guild vetting was further extended (2026-07-25) to also require
        # a chosen subclass (eligible_for_guild) -- this fixture predates
        # that too, stale the same way the 3 tests fixed in 822e728 were.
        db.update_character(user_id, -999, level=3, proven_in_combat=1, subclass="evocation")
        sink = []
        await bot._do_join_guild(FakeUpdate(user_id, "join the arcane circle", sink), "join the arcane circle")
        character = db.get_character(user_id, -999)
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
            db.complete_quest(user_id, -999, q)  # BEFORE _chapter_complete_note, so this
            # test mirrors that real ordering rather than checking a
            # state that would never actually occur mid-flow.
        note = bot._chapter_complete_note(user_id, -999, arc1_quests[-1])
        self.assertIn("Chapter complete", note)
        self.assertIn("Discovery", note)

    def test_no_chapter_complete_note_mid_chapter(self):
        use_test_db("tests/tmp/story_arc_test2.db")
        user_id = 888900
        make_basic_character(user_id, "Arclight", current_location="crossroads_tavern")
        arc1_quests = bot.CAMPAIGN["story_arcs"]["arc_1_discovery"]["quests"]
        db.complete_quest(user_id, -999, arc1_quests[0])  # only the first of several
        note = bot._chapter_complete_note(user_id, -999, arc1_quests[0])
        self.assertEqual(note, "")

    async def test_arc_opening_cutscene_fires_on_a_chapters_first_quest(self):
        """
        Per Coffee's request for RPG-style cutscenes on story quests
        (2026-07-19/20): accepting the very first quest of a story arc
        (arc_1_discovery's welcome_to_the_crossroads) should get a real
        AI-narrated opening beat (_arc_opening_note), the opening bookend
        to the existing chapter-complete closing beat above.
        """
        from unittest.mock import patch
        use_test_db("tests/tmp/arc_opening_test.db")
        user_id = 888901
        make_basic_character(user_id, "Cutscenetester", current_location="crossroads_tavern")
        sink = []
        with patch("bot.narrate_arc_opening", return_value="A quiet dread settles over the crossroads.") as mock_narrate:
            await bot._do_accept_quest(FakeUpdate(user_id, "I accept the quest", sink), "I accept the quest")
        combined = " ".join(sink)
        self.assertTrue(mock_narrate.called)
        self.assertIn("🎬", combined)
        self.assertIn("Discovery", combined)

    async def test_arc_opening_cutscene_does_not_repeat_mid_chapter(self):
        """Sibling to the test above: a LATER quest in the same arc (not arc["quests"][0]) must not re-trigger the opening cutscene."""
        from unittest.mock import patch
        use_test_db("tests/tmp/arc_opening_test2.db")
        user_id = 888902
        make_basic_character(user_id, "Cutscenetester2", current_location="crossroads_tavern")
        db.complete_quest(user_id, -999, "welcome_to_the_crossroads")
        db.update_character(user_id, -999, current_location="hollow_stump_shrine")
        sink = []
        with patch("bot.narrate_arc_opening", return_value="SHOULD NOT APPEAR") as mock_narrate:
            await bot._do_accept_quest(FakeUpdate(user_id, "I accept the quest", sink), "I accept the quest")
        combined = " ".join(sink)
        self.assertFalse(mock_narrate.called)
        self.assertNotIn("🎬", combined)

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

    def test_give_item_dative_phrasing_without_an_article_is_classified_correctly(self):
        # Real live bug (2026-08-06, confirmed via topic-activity monitoring):
        # "Give Pan bracers of the steady hand" fell through to a plain "chat"
        # reply, and "Give Vesh ring of undertow" got hijacked to talk_npc by
        # the known-NPC-name loop -- both real multi-word item names handed
        # to a recipient with no article ("a"/"an"/"the"/"some") in between.
        result1 = _keyword_fallback("Give Pan bracers of the steady hand", [])
        self.assertEqual(result1["action"], "give_item")
        result2 = _keyword_fallback("Give Vesh ring of undertow", [])
        self.assertEqual(result2["action"], "give_item")

    def test_give_item_dative_phrasing_with_a_lowercase_recipient_name_is_classified_correctly(self):
        # Real live bug (2026-08-08, confirmed via topic-activity
        # monitoring): "Give pan health potion" fell through to a silent
        # "chat" reply -- both dative-construction checks above rely on
        # capitalization (or an @-tag) as their only signal that a word
        # is a real recipient name, but real players type in lowercase
        # constantly, especially mid-combat. Must not newly false-
        # positive on pronoun/article phrasing now that capitalization
        # is no longer required to gate this.
        self.assertEqual(_keyword_fallback("Give pan health potion", [])["action"], "give_item")
        self.assertEqual(_keyword_fallback("trade sarah my sword", [])["action"], "give_item")
        self.assertEqual(_keyword_fallback("give me a hand", [])["action"], "chat")
        self.assertEqual(_keyword_fallback("give it a try", [])["action"], "chat")
        self.assertEqual(_keyword_fallback("send them away", [])["action"], "chat")
        self.assertEqual(_keyword_fallback("give the sword away", [])["action"], "chat")
        self.assertEqual(_keyword_fallback("give my sword away", [])["action"], "chat")

    async def test_give_item_transfers_between_characters_at_the_same_location(self):
        use_test_db("tests/tmp/give_item_test.db")
        giver_id, recipient_id = 900001, 900002
        make_basic_character(giver_id, "Giver", current_location="crossroads_tavern")
        make_basic_character(recipient_id, "Receiver", current_location="crossroads_tavern")
        db.add_item(giver_id, -999, "healing_potion", 2)

        sink = []
        await bot._do_give_item(
            FakeUpdate(giver_id, "give my healing potion to Receiver", sink),
            "give my healing potion to Receiver",
        )
        combined = " ".join(sink)
        self.assertIn("Receiver", combined)

        giver = db.get_character(giver_id, -999)
        recipient = db.get_character(recipient_id, -999)
        self.assertEqual(giver["inventory"].get("healing_potion", 0), 1)
        self.assertEqual(recipient["inventory"].get("healing_potion", 0), 1)

    async def test_give_item_rejects_recipient_at_a_different_location(self):
        use_test_db("tests/tmp/give_item_test2.db")
        giver_id, elsewhere_id = 900003, 900004
        make_basic_character(giver_id, "Giver2", current_location="crossroads_tavern")
        make_basic_character(elsewhere_id, "Farflung", current_location="whispering_wood")
        db.add_item(giver_id, -999, "healing_potion", 1)

        sink = []
        await bot._do_give_item(
            FakeUpdate(giver_id, "give my healing potion to Farflung", sink),
            "give my healing potion to Farflung",
        )
        giver = db.get_character(giver_id, -999)
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
        recipient = db.get_character(recipient_id, -999)
        self.assertEqual(recipient["inventory"].get("healing_potion", 0), 0)

    # -- Potions were completely non-functional: no action anywhere ever
    #    read a consumable's heal_dice/effect field (v1.10.8) ----------
    def test_use_item_phrasing_classified_correctly(self):
        for text in ["drink the healing potion", "I use my antitoxin", "quaff the potion"]:
            self.assertEqual(_keyword_fallback(text, [])["action"], "use_item", text)

    def test_use_item_doesnt_shadow_arcane_recovery(self):
        self.assertEqual(_keyword_fallback("I use arcane recovery", [])["action"], "arcane_recovery")

    def test_use_item_on_target_without_an_article_is_classified_correctly(self):
        # Real live bug (2026-08-08, confirmed via a real dev-topic
        # screenshot AND independently via topic-activity monitoring):
        # "Use health potion on pan" fell through to a silent "chat"
        # reply -- this only ever matched "use (the|my|a|an) ...", so
        # phrasing that skips the article entirely never matched at
        # all, regardless of the recipient's capitalization.
        self.assertEqual(_keyword_fallback("Use health potion on pan", [])["action"], "use_item")
        self.assertEqual(_keyword_fallback("use antitoxin on sarah", [])["action"], "use_item")
        # Must not shadow the environment-use check, checked earlier.
        self.assertEqual(_keyword_fallback("use the environment against it", [])["action"], "use_environment")

    async def test_use_item_heals_and_consumes_the_potion(self):
        use_test_db("tests/tmp/use_item_test.db")
        user_id = 900101
        make_basic_character(user_id, "Drinker", current_location="crossroads_tavern", hp_max=20)
        db.update_character(user_id, -999, hp_current=5)
        db.add_item(user_id, -999, "healing_potion", 2)

        sink = []
        await bot._do_use_item(FakeUpdate(user_id, "drink the healing potion", sink), "drink the healing potion")
        combined = " ".join(sink)
        self.assertIn("healing", combined.lower())

        character = db.get_character(user_id, -999)
        self.assertGreater(character["hp_current"], 5)
        self.assertEqual(character["inventory"].get("healing_potion", 0), 1)  # consumed exactly 1

    async def test_use_item_cures_poison_mid_combat(self):
        import sessions
        sessions.end_session(-999)
        user_id = 900102
        make_basic_character(user_id, "Poisoned", current_location="crossroads_tavern")
        db.add_item(user_id, -999, "antitoxin", 1)
        character = db.get_character(user_id, -999)
        character["conditions"] = ["poisoned"]
        session = sessions.start_session(-999, [character], {user_id: "party"})

        sink = []
        await bot._do_use_item(FakeUpdate(user_id, "I use my antitoxin", sink), "I use my antitoxin")
        combined = " ".join(sink)
        self.assertIn("neutralized", combined.lower())
        live = next(p for p in session.participants if p["telegram_user_id"] == user_id)
        self.assertNotIn("poisoned", live.get("conditions", []))
        sessions.end_session(-999)

    async def test_use_item_restores_spell_slots_and_consumes_the_tonic(self):
        """
        Real feature request (2026-08-10, per Coffee: "make an item to
        replenish spell slots... Like Final Fantasy games, they have
        Ethers"), built right after fixing Support's answer that spell
        slots have NO in-battle recovery option at all -- these tonics
        actually give players one for real, not just a wiki correction.
        """
        use_test_db("tests/tmp/use_item_spellslot_test.db")
        user_id = 900201
        make_basic_character(
            user_id, "Caster", char_class="Wizard", current_location="crossroads_tavern", spell_slots_max=5,
        )
        db.update_character(user_id, -999, spell_slots_current=0)
        db.add_item(user_id, -999, "spell_tonic", 1)

        sink = []
        await bot._do_use_item(FakeUpdate(user_id, "drink the spell tonic", sink), "drink the spell tonic")
        combined = " ".join(sink)
        self.assertIn("2 spell slot", combined)
        self.assertIn("(2/5)", combined)

        character = db.get_character(user_id, -999)
        self.assertEqual(character["spell_slots_current"], 2)
        self.assertEqual(character["inventory"].get("spell_tonic", 0), 0)  # consumed

    async def test_use_item_elixir_restores_all_spell_slots(self):
        use_test_db("tests/tmp/use_item_spellslot_test2.db")
        user_id = 900202
        make_basic_character(
            user_id, "FullCaster", char_class="Sorcerer", current_location="crossroads_tavern", spell_slots_max=8,
        )
        db.update_character(user_id, -999, spell_slots_current=1)
        db.add_item(user_id, -999, "elixir_of_the_arcane_circle", 1)

        sink = []
        await bot._do_use_item(FakeUpdate(user_id, "drink the elixir of the arcane circle", sink), "drink the elixir of the arcane circle")
        combined = " ".join(sink)
        self.assertIn("(8/8)", combined)

        character = db.get_character(user_id, -999)
        self.assertEqual(character["spell_slots_current"], 8)

    async def test_use_item_restores_spell_slots_mid_combat_on_the_live_participant(self):
        import sessions
        sessions.end_session(-999)
        user_id = 900203
        make_basic_character(
            user_id, "MidFight", char_class="Cleric", current_location="crossroads_tavern", spell_slots_max=4,
        )
        db.update_character(user_id, -999, spell_slots_current=0)
        db.add_item(user_id, -999, "greater_spell_tonic", 1)
        character = db.get_character(user_id, -999)
        session = sessions.start_session(-999, [character], {user_id: "party"})

        sink = []
        await bot._do_use_item(
            FakeUpdate(user_id, "use my greater spell tonic", sink), "use my greater spell tonic",
        )
        live = next(p for p in session.participants if p["telegram_user_id"] == user_id)
        self.assertEqual(live["spell_slots_current"], 4)  # 0 + 5, capped at max 4
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

    def test_glimmerdeep_moss_is_a_real_gatherable_rare_material(self):
        """
        Real feature (2026-08-10, per Coffee: "make crafting Ethers
        possible but difficult, shud be rare herbs to craft this") --
        confirms the new rare herb is a real items.py material AND
        actually gatherable from a real resource node in the live
        campaign, not just a name referenced by a recipe with no way to
        ever obtain it.
        """
        material = items_module.get_item("glimmerdeep_moss")
        self.assertEqual(material["type"], "material")
        self.assertEqual(material["rarity"], "rare")

        found_node = False
        for layer in bot.CAMPAIGN["locations"].values():
            for location in layer.values():
                for node in location.get("resource_nodes", []):
                    if node.get("material") == "glimmerdeep_moss":
                        found_node = True
        self.assertTrue(found_node, "glimmerdeep_moss has no real gathering node anywhere")

    def test_spell_tonic_recipes_are_harder_than_the_previous_dc_ceiling(self):
        """
        Real feature (2026-08-10): "advanced crafting recipes" per
        Coffee -- confirms all three craftable tonic tiers require the
        rare herb and exceed chain_mail's DC 18, the highest DC
        anywhere else in RECIPES before this. Deliberately checked
        against RECIPES, not ADVANCED_RECIPES -- that system produces
        procedurally generated gear (rules/item_generator.py), the
        wrong shape for a fixed-effect catalog consumable.
        """
        from rules.crafting import RECIPES, ADVANCED_RECIPES
        prior_ceiling = max(
            r["dc"] for rid, r in RECIPES.items()
            if rid not in ("spell_tonic", "greater_spell_tonic", "supreme_spell_tonic")
        )
        self.assertEqual(prior_ceiling, 18)
        for recipe_id in ("spell_tonic", "greater_spell_tonic", "supreme_spell_tonic"):
            recipe = RECIPES[recipe_id]
            self.assertGreater(recipe["dc"], prior_ceiling)
            self.assertIn("glimmerdeep_moss", recipe["materials"])
            self.assertEqual(recipe["result_item"], recipe_id)
        # The top-tier full-restore item stays deliberately uncraftable --
        # boss-drop/hidden-treasure only, same as this file's existing
        # Fireball/Revivify convention.
        self.assertNotIn("elixir_of_the_arcane_circle", RECIPES)
        self.assertNotIn("elixir_of_the_arcane_circle", ADVANCED_RECIPES)

    def test_crafting_a_spell_tonic_succeeds_with_materials_and_consumes_them(self):
        from rules.crafting import resolve_craft
        from unittest.mock import patch
        character = make_basic_character(
            900301, "Alchemist", char_class="Wizard", current_location="crossroads_tavern",
        )
        db.add_item(900301, -999, "moonpetal", 2)
        db.add_item(900301, -999, "glimmerdeep_moss", 1)
        character = db.get_character(900301, -999)

        with patch("rules.dice.roll_d20", return_value=20):
            result = resolve_craft(character, "spell_tonic")
        self.assertEqual(result["outcome"], "success")
        self.assertEqual(result["result_item"], "spell_tonic")
        self.assertEqual(result["materials_consumed"], {"moonpetal": 2, "glimmerdeep_moss": 1})

    def test_crafting_a_spell_tonic_fails_cleanly_without_the_rare_herb(self):
        from rules.crafting import resolve_craft
        character = make_basic_character(
            900302, "NoHerbs", char_class="Wizard", current_location="crossroads_tavern",
        )
        db.add_item(900302, -999, "moonpetal", 2)  # no glimmerdeep_moss at all
        character = db.get_character(900302, -999)

        result = resolve_craft(character, "spell_tonic")
        self.assertEqual(result["outcome"], "missing_materials")
        self.assertIn("glimmerdeep_moss", result["missing"])

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
        db.add_item(user_id, -999, "greataxe", 1)

        default_weapon = bot._weapon_for_attacker(db.get_character(user_id, -999))
        self.assertNotEqual(default_weapon["damage_dice"], "1d12")

        success, message, updated = db.equip_item(user_id, -999, "greataxe")
        self.assertTrue(success)
        self.assertEqual(updated["equipped_weapon"], "greataxe")
        equipped_weapon = bot._weapon_for_attacker(updated)
        self.assertEqual(equipped_weapon["damage_dice"], "1d12")
        self.assertEqual(equipped_weapon["ability"], "strength")

    def test_equipping_armor_recomputes_armor_class(self):
        use_test_db("tests/tmp/equip_test2.db")
        user_id = 900202
        character = make_basic_character(user_id, "Armored", current_location="crossroads_tavern")
        db.add_item(user_id, -999, "chain_mail", 1)

        success, message, updated = db.equip_item(user_id, -999, "chain_mail")
        self.assertTrue(success)
        from rules.dice import ability_modifier
        expected_ac = 16 + ability_modifier(character["dexterity"])  # chain_mail's ac_base is 16
        self.assertEqual(updated["armor_class"], expected_ac)
        self.assertEqual(updated["equipped_armor"], "chain_mail")

    def test_equip_rejects_item_not_carried(self):
        use_test_db("tests/tmp/equip_test3.db")
        user_id = 900203
        make_basic_character(user_id, "Empty2", current_location="crossroads_tavern")
        success, message, _ = db.equip_item(user_id, -999, "longsword")
        self.assertFalse(success)

    def test_equip_rejects_non_equippable_item(self):
        use_test_db("tests/tmp/equip_test4.db")
        user_id = 900204
        make_basic_character(user_id, "Drinker2", current_location="crossroads_tavern")
        db.add_item(user_id, -999, "healing_potion", 1)
        success, message, _ = db.equip_item(user_id, -999, "healing_potion")
        self.assertFalse(success)

    async def test_do_equip_item_handler_end_to_end(self):
        use_test_db("tests/tmp/equip_test5.db")
        user_id = 900205
        make_basic_character(user_id, "Handler", current_location="crossroads_tavern")
        db.add_item(user_id, -999, "longsword", 1)

        sink = []
        await bot._do_equip_item(FakeUpdate(user_id, "equip my longsword", sink), "equip my longsword")
        combined = " ".join(sink)
        self.assertIn("Longsword", combined)
        character = db.get_character(user_id, -999)
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
        db.add_item(user_id, -999, "wooden_shield", 1)
        success, message, updated = db.equip_item(user_id, -999, "wooden_shield")
        self.assertTrue(success)
        self.assertEqual(updated["equipped_shield"], "wooden_shield")
        self.assertEqual(updated["armor_class"], 18)  # 16 + wooden_shield's ac_bonus of 2

    def test_equipping_new_armor_preserves_an_already_equipped_shields_bonus(self):
        use_test_db("tests/tmp/shield_test2.db")
        user_id = 900302
        character = make_basic_character(user_id, "Upgrader", current_location="crossroads_tavern")
        db.add_item(user_id, -999, "wooden_shield", 1)
        db.add_item(user_id, -999, "chain_mail", 1)
        db.equip_item(user_id, -999, "wooden_shield")
        success, message, updated = db.equip_item(user_id, -999, "chain_mail")
        self.assertTrue(success)
        from rules.dice import ability_modifier
        expected = 16 + ability_modifier(character["dexterity"]) + 2  # chain_mail + dex + shield
        self.assertEqual(updated["armor_class"], expected)

    def test_auto_equip_picks_the_real_best_weapon_and_armor(self):
        use_test_db("tests/tmp/auto_equip_test.db")
        user_id = 900303
        make_basic_character(user_id, "Auto", current_location="crossroads_tavern")
        db.add_item(user_id, -999, "rusty_dagger", 1)   # 1d4, worse
        db.add_item(user_id, -999, "greataxe", 1)       # 1d12, better
        db.add_item(user_id, -999, "leather_armor", 1)  # ac_base 11, worse
        db.add_item(user_id, -999, "chain_mail", 1)     # ac_base 16, better
        summary, character = db.auto_equip_best_gear(user_id, -999)
        self.assertEqual(character["equipped_weapon"], "greataxe")
        self.assertEqual(character["equipped_armor"], "chain_mail")

    async def test_auto_equip_handler_end_to_end(self):
        use_test_db("tests/tmp/auto_equip_test2.db")
        user_id = 900304
        make_basic_character(user_id, "AutoHandler", current_location="crossroads_tavern")
        db.add_item(user_id, -999, "longsword", 1)
        sink = []
        await bot._do_auto_equip_gear(FakeUpdate(user_id, "auto equip my character", sink), "auto equip my character")
        character = db.get_character(user_id, -999)
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

    # -- Real live bug (2026-08-10, found via topic-activity monitoring):
    #    "Look at the shard of dim light in my inventory" matched
    #    check_inventory's "my inventory" trigger and dumped the whole
    #    backpack instead of examining the named item -- confirmed by
    #    the very next message from the same player, "Examine the shard
    #    of dim light" (same item, no "in my inventory" suffix), which
    #    correctly returned 'examine'. A named item "in my inventory/
    #    backpack/bag" must route to examine with that item as the
    #    target, never the generic full-list action. ---------------------
    def test_named_item_in_inventory_routes_to_examine_not_check_inventory(self):
        cases = [
            ("Look at the shard of dim light in my inventory", "shard of dim light"),
            ("Check the shard of dim light in my inventory", "shard of dim light"),
            ("Examine the healing potion in my backpack", "healing potion"),
            ("inspect the rusty key in my bag", "rusty key"),
        ]
        for text, expected_target in cases:
            result = _keyword_fallback(text, [])
            self.assertEqual(result["action"], "examine", text)
            self.assertEqual(result["target"], expected_target, text)

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

    def test_support_prompt_shrinks_for_a_topic_specific_question(self):
        """
        Real live bug (2026-08-10, found via topic-activity monitoring):
        both real Support questions ever asked in the live log failed
        with "Pandora AI is genuinely overloaded" -- 2 for 2, not a rare
        fluke. Root cause: the old SUPPORT_SYSTEM_PROMPT unconditionally
        baked in the ENTIRE item/spell/guild/crafting/race catalog
        (~16,600 chars, ~4,150 tokens) into every single question
        regardless of topic, on top of the header -- real, direct
        prompt-processing latency on this box's CPU-only, single-slot
        inference, paid even by a question about exactly one narrow
        thing. The real prompt from the exact question that actually
        failed live ("What guilds can i join...") must now be
        dramatically smaller -- confirmed it drops by more than half.
        """
        from ai.support_agent import _build_prompt
        real_failed_question = "What guilds can i join and what do they do for the characters?"
        filtered = _build_prompt(real_failed_question)
        broad = _build_prompt("What can I do in this game?")
        self.assertLess(len(filtered), len(broad) * 0.6)
        self.assertIn("REAL GUILDS IN THIS GAME", filtered)
        self.assertNotIn("REAL ITEMS IN THIS GAME", filtered)
        self.assertNotIn("REAL SPELLS IN THIS GAME", filtered)

    def test_support_catalog_reference_falls_back_to_everything_for_an_ambiguous_question(self):
        """
        A genuinely broad/ambiguous question (no real keyword match to
        any specific category) must NOT lose grounding -- it gets the
        full catalog, identical to this function's original always-
        everything behavior, same safety-net design as every other
        "when in doubt, don't under-ground" convention in this file.
        """
        from ai.support_agent import _build_catalog_reference
        full = _build_catalog_reference(None)
        ambiguous = _build_catalog_reference("What can I do in this game?")
        self.assertEqual(full, ambiguous)
        for section in ("REAL ITEMS IN THIS GAME", "REAL SPELLS IN THIS GAME",
                         "REAL GUILDS IN THIS GAME", "REAL CRAFTING RECIPES IN THIS GAME",
                         "REAL RACE ABILITY SCORE BONUSES IN THIS GAME"):
            self.assertIn(section, ambiguous)

    def test_support_catalog_reference_matches_multiple_relevant_sections(self):
        """A question naming two real categories gets both sections, not just one."""
        from ai.support_agent import _build_catalog_reference
        result = _build_catalog_reference("What weapons and spells does a Fighter get?")
        self.assertIn("REAL ITEMS IN THIS GAME", result)
        self.assertIn("REAL SPELLS IN THIS GAME", result)
        self.assertNotIn("REAL GUILDS IN THIS GAME", result)

    def test_support_call_active_flag_is_set_during_the_retry_loop_and_cleared_after(self):
        """
        Real live bug (dev-topic screenshot, 2026-08-10, Coffee: "Support
        topic is still not working" even after the catalog-size fix):
        Ollama's own request log showed bot.py's 60s world tick (NPC
        heartbeat, hourly status, AI-party turns, Moltbook chatter)
        saturating the single inference slot back-to-back for the whole
        ~18 minutes a real Support question was retrying -- it never got
        a gap to land in. is_support_call_active() is the flag the world
        tick now checks to pause its own ambient Ollama calls while a
        real Support answer is in flight. Confirms the flag is True for
        the full span of the retry loop (both mid-attempt and during the
        backoff sleep) and False again once the loop gives up.
        """
        import ai.support_agent as support_agent_module
        from unittest.mock import patch

        self.assertFalse(support_agent_module.is_support_call_active())
        seen_active_mid_attempt = []

        def fake_post(*a, **k):
            seen_active_mid_attempt.append(support_agent_module.is_support_call_active())
            raise requests.exceptions.ConnectionError("no real Ollama in this test")

        with patch("ai.support_agent.requests.post", side_effect=fake_post), \
             patch("ai.support_agent.time.sleep") as mock_sleep:
            answer = support_agent_module.answer_support_question("What can I do in this game?")
        self.assertIn("overloaded", answer.lower())
        self.assertTrue(seen_active_mid_attempt, "requests.post was never actually called")
        self.assertTrue(all(seen_active_mid_attempt), "flag must be True during every retry attempt")
        # Confirms the sleeps themselves (not just the request attempts) fall
        # inside the active window, since that's most of the real 18-minute span.
        for call in mock_sleep.call_args_list:
            self.assertIn(call.args[0], (5, 10, 20, 30))
        self.assertFalse(support_agent_module.is_support_call_active())

    async def test_world_tick_skips_ambient_ai_calls_while_a_support_answer_is_in_flight(self):
        """
        Real wiring check for the fix above: bot.py's world-tick loop
        body must actually SKIP its Ollama-touching sub-calls (world
        heartbeat, hourly status, AI-party autonomous turns, Moltbook
        social tick) when is_support_call_active() is True, while still
        running the deterministic, non-Ollama ones (NPC wander, world
        boss spawn check, board-quest expiry, dice auto-roll, Moltbook's
        plain HTTP activity check) every cycle regardless. Runs exactly
        one real iteration of the loop body by making asyncio.sleep
        raise on its second call, so the `while True` loop stops itself
        after one pass instead of needing an external cancel.
        """
        from unittest.mock import patch, AsyncMock, Mock

        class _StopLoop(Exception):
            pass

        sleep_calls = {"n": 0}

        async def fake_sleep(seconds):
            sleep_calls["n"] += 1
            if sleep_calls["n"] > 1:
                raise _StopLoop()

        with patch("bot.asyncio.sleep", side_effect=fake_sleep), \
             patch("bot.is_support_call_active", return_value=True), \
             patch("bot._check_idle_characters", new=AsyncMock()), \
             patch("bot._check_combat_timeouts", new=AsyncMock()), \
             patch("bot._apply_passive_party_regen", new=Mock()), \
             patch("bot._maybe_revive_standalone_ai_companions", new=Mock()), \
             patch("bot.db.get_all_chat_ids", return_value=[999999]), \
             patch("bot._wander_npcs", new=Mock()) as mock_wander, \
             patch("bot._maybe_post_world_heartbeat", new=AsyncMock()) as mock_heartbeat, \
             patch("bot._maybe_post_hourly_status_update", new=AsyncMock()) as mock_hourly, \
             patch("bot._maybe_spawn_world_boss", new=AsyncMock()) as mock_boss, \
             patch("bot.db.expire_stale_board_quests", new=Mock()) as mock_expire, \
             patch("bot._maybe_auto_roll_pending_dice", new=AsyncMock()) as mock_dice, \
             patch("bot._ai_party_autonomous_tick", new=AsyncMock()) as mock_ai_party, \
             patch("bot._maybe_check_moltbook_activity", new=AsyncMock()) as mock_moltbook_check, \
             patch("bot._maybe_run_moltbook_social_tick", new=AsyncMock()) as mock_moltbook_social:
            fake_app = SimpleNamespace(bot=object())
            with self.assertRaises(_StopLoop):
                await bot._idle_inactivity_loop(fake_app)

        # Ollama-touching ambient calls: skipped this cycle.
        mock_heartbeat.assert_not_called()
        mock_hourly.assert_not_called()
        mock_ai_party.assert_not_called()
        mock_moltbook_social.assert_not_called()
        # Deterministic / non-Ollama calls: still run every cycle.
        mock_wander.assert_called_once()
        mock_boss.assert_called_once()
        mock_expire.assert_called_once()
        mock_dice.assert_called_once()
        mock_moltbook_check.assert_called_once()

    def test_support_retries_and_logs_when_a_real_response_has_no_text_after_stripping_think_tags(self):
        """
        Real live bug (2026-08-10, Coffee: "How do i enchant my weapon?"
        still failed even after the world-tick priority fix). journalctl
        -u ollama showed all 5 attempts got a genuine HTTP 200 in a
        normal ~25-45s each -- Ollama was healthy and answering every
        time. The real failure was silent: strip_think_tags() left an
        empty string on every attempt because num_predict=600 (half of
        ai/dm_agent.py's proven 1200) let this thinking model exhaust
        its whole budget reasoning before ever emitting a real answer,
        and the old code had no log line and no backoff for that case --
        straight to the next attempt, indistinguishable from a working
        loop from the outside. Confirms this case now logs a real
        diagnostic and still backs off, and that num_predict was bumped
        to match dm_agent's working value.
        """
        import ai.support_agent as support_agent_module
        from unittest.mock import patch, Mock

        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {"response": "<think>reasoning that never concludes"}

        captured_options = []

        def fake_post(*a, **k):
            captured_options.append(k["json"]["options"])
            return FakeResponse()

        with patch("ai.support_agent.requests.post", side_effect=fake_post), \
             self.assertLogs("pandora_mmo", level="WARNING") as log_ctx, \
             patch("ai.support_agent.time.sleep") as mock_sleep:
            answer = support_agent_module.answer_support_question("How do i enchant my weapon?")
        self.assertIn("overloaded", answer.lower())
        self.assertTrue(any("no usable text" in m for m in log_ctx.output))
        self.assertEqual(mock_sleep.call_count, 4)
        self.assertTrue(captured_options, "requests.post was never actually called")
        for options in captured_options:
            self.assertEqual(options["num_predict"], 1200)

    def test_support_spell_slot_restoration_question_is_answered_deterministically(self):
        """
        Real live bug (2026-08-10, found via topic-activity monitoring):
        the same real player asked "How can i replenish spell slot in
        battle?" three separate times, and even with the correct fact
        already in SUPPORT_SYSTEM_PROMPT_HEADER ("I rest ... heals HP
        and spell slots over real-world elapsed time"), the model
        answered "casting spells again or activating abilities" -- a
        real hallucination that flatly contradicts the game's actual
        mechanics (spell slots cannot be restored mid-battle at all).
        Same failure shape as the 2026-07-10 XP hallucination this file
        already works around. No Ollama call should happen at all for
        this question now.
        """
        import ai.support_agent as support_agent_module
        from unittest.mock import patch

        with patch("ai.support_agent.requests.post") as mock_post:
            answer = support_agent_module.answer_support_question("How can i replenish spell slot in battle?")
        mock_post.assert_not_called()
        self.assertIn("resting", answer.lower())
        self.assertNotIn("casting spells again", answer.lower())
        self.assertNotIn("activating abilities", answer.lower())
        # Coffee's real dev-topic follow-up on the wrong answer: "what can
        # we do in battle ... once we run out" -- both the broader phrasing
        # and the actual honest fallback (cantrips) must be covered.
        with patch("ai.support_agent.requests.post") as mock_post:
            followup_answer = support_agent_module.answer_support_question(
                "What can we do in battle once we run out of spell slots?"
            )
        mock_post.assert_not_called()
        self.assertIn("cantrip", followup_answer.lower())

        warlock = make_basic_character(555555, "Zeraphine", char_class="Warlock")
        with patch("ai.support_agent.requests.post") as mock_post:
            warlock_answer = support_agent_module.answer_support_question(
                "How do i restore spell slots?", character=warlock,
            )
        mock_post.assert_not_called()
        self.assertIn("warlock", warlock_answer.lower())

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

    async def test_compound_message_still_sends_its_location_image(self):
        """
        Real live bug, caught via topic-activity monitoring on a real
        player's compound message (2026-08-06): a genuinely compound
        message (2+ intents) dispatches through _BufferingChatProxy,
        which stood in for update.effective_chat but only ever
        implemented .send_message() -- _maybe_send_location_image (and
        every other _maybe_send_*_image helper) calls .send_photo()
        directly, so it raised AttributeError every time, silently
        swallowed by _send_generated_image's broad except-and-log. The
        location image for "look" simply never sent whenever it was
        part of a compound message, with no visible error to the
        player at all. Fixed by giving the proxy a real send_photo
        that forwards straight to the actual chat.
        """
        use_test_db("tests/tmp/compound_image_test.db")
        user_id = 900575
        make_basic_character(user_id, "CompoundLooker", current_location="crossroads_tavern")
        sink = []
        update = FakeUpdate(user_id, "Look around, and check my inventory", sink)
        await bot.adventure_master_handler(update, DummyContext())
        self.assertTrue(update.effective_chat.sent_photos, "the location image never sent during a compound message")
        combined = " ".join(sink)
        self.assertIn("backpack", combined.lower())

    async def test_equip_can_target_another_party_member(self):
        use_test_db("tests/tmp/equip_other_test.db")
        equipper_id, helped_id = 900305, 900306
        make_basic_character(equipper_id, "Helper", current_location="crossroads_tavern")
        helped = make_basic_character(helped_id, "Helped", current_location="crossroads_tavern")
        db.add_item(helped_id, -999, "longsword", 1)  # in HELPED's own inventory, not the helper's

        sink = []
        await bot._do_equip_item(
            FakeUpdate(equipper_id, "equip Helped with the longsword", sink),
            "equip Helped with the longsword",
        )
        combined = " ".join(sink)
        self.assertIn("Helped", combined)
        helped_after = db.get_character(helped_id, -999)
        self.assertEqual(helped_after["equipped_weapon"], "longsword")
        equipper_after = db.get_character(equipper_id, -999)
        self.assertIsNone(equipper_after["equipped_weapon"])  # the HELPER didn't equip anything

    def test_sheet_shows_equipped_and_carried_not_equipped_gear(self):
        use_test_db("tests/tmp/sheet_gear_test.db")
        user_id = 900307
        make_basic_character(user_id, "SheetTest", current_location="crossroads_tavern")
        db.add_item(user_id, -999, "longsword", 1)
        db.add_item(user_id, -999, "shortsword", 1)
        db.equip_item(user_id, -999, "longsword")
        character = db.get_character(user_id, -999)
        sheet = bot._format_character_sheet(character)
        self.assertIn("Equipped: Longsword", sheet)
        self.assertIn("Carried but not equipped: Shortsword", sheet)

    def test_weapon_stats_line_shows_damage_dice_and_element(self):
        """
        Real live gap (2026-08-06, per Coffee: "im not seeing the attack
        strength, stats, resistences, elements ... when i look at
        weapons"): _format_item_stats_line never read damage_dice at
        all -- a plain physical weapon's stats line said nothing about
        its damage whatsoever, and an elemental one (Flametongue
        Shortsword) showed "deals fire damage" with no dice either.
        """
        physical = items_module.get_item("longsword")
        stats = bot._format_item_stats_line(physical)
        self.assertIn("1d8", stats)
        self.assertIn("uses Strength", stats)

        elemental = items_module.get_item("flametongue_shortsword")
        estats = bot._format_item_stats_line(elemental)
        self.assertIn("1d6+2", estats)
        self.assertIn("fire damage", estats)

    def test_character_sheet_shows_equipped_weapon_and_armor_stats(self):
        """
        Real live gap (2026-08-06, per Coffee): equipping a weapon/armor
        only ever showed its bare NAME on the character sheet, with zero
        indication of what it actually does mechanically -- the exact
        same stats _format_item_stats_line already computes for market/
        examine were never surfaced here at all.
        """
        use_test_db("tests/tmp/sheet_gear_test2.db")
        user_id = 900308
        make_basic_character(user_id, "GearStatsTest", current_location="crossroads_tavern")
        db.add_item(user_id, -999, "longsword", 1)
        db.add_item(user_id, -999, "chain_shirt", 1)
        db.equip_item(user_id, -999, "longsword")
        db.equip_item(user_id, -999, "chain_shirt")
        character = db.get_character(user_id, -999)
        sheet = bot._format_character_sheet(character)
        self.assertIn("1d8", sheet)
        self.assertIn("AC 13", sheet)

    # -- Rings/amulets/wondrous items were completely non-functional too
    #    (same shape as potions/equipment): Ring of Protection, Ring of
    #    the Undertow, Amulet of Health, Cloak of Elvenkind all had real
    #    mechanical fields nothing ever read (v1.10.11) ------------------
    def test_equipping_a_ring_adds_its_ac_bonus(self):
        use_test_db("tests/tmp/ring_test.db")
        user_id = 900401
        make_basic_character(user_id, "RingBearer", current_location="crossroads_tavern", armor_class=15)
        db.add_item(user_id, -999, "ring_of_protection", 1)
        success, message, updated = db.equip_item(user_id, -999, "ring_of_protection")
        self.assertTrue(success)
        self.assertEqual(updated["armor_class"], 16)
        self.assertIn("ring_of_protection", updated["equipped_accessories"])

    def test_two_rings_stack_their_ac_bonus(self):
        use_test_db("tests/tmp/ring_test2.db")
        user_id = 900402
        make_basic_character(user_id, "TwoRings", current_location="crossroads_tavern", armor_class=15)
        db.add_item(user_id, -999, "ring_of_protection", 1)
        db.add_item(user_id, -999, "ring_of_the_undertow", 1)
        db.equip_item(user_id, -999, "ring_of_protection")
        success, message, updated = db.equip_item(user_id, -999, "ring_of_the_undertow")
        self.assertTrue(success)
        self.assertEqual(updated["armor_class"], 17)

    def test_equipping_armor_after_rings_preserves_ring_bonus(self):
        use_test_db("tests/tmp/ring_test3.db")
        user_id = 900403
        character = make_basic_character(user_id, "RingThenArmor", current_location="crossroads_tavern")
        db.add_item(user_id, -999, "ring_of_protection", 1)
        db.add_item(user_id, -999, "chain_mail", 1)
        db.equip_item(user_id, -999, "ring_of_protection")
        success, message, updated = db.equip_item(user_id, -999, "chain_mail")
        self.assertTrue(success)
        from rules.dice import ability_modifier
        expected = 16 + ability_modifier(character["dexterity"]) + 1  # chain_mail + dex + ring
        self.assertEqual(updated["armor_class"], expected)

    def test_amulet_of_health_sets_constitution(self):
        use_test_db("tests/tmp/amulet_test.db")
        user_id = 900404
        make_basic_character(user_id, "Amuleted", current_location="crossroads_tavern")
        db.add_item(user_id, -999, "amulet_of_health", 1)
        success, message, updated = db.equip_item(user_id, -999, "amulet_of_health")
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
        db.add_item(user_id, -999, "amulet_of_health", 1)
        success, message, updated = db.equip_item(user_id, -999, "amulet_of_health")
        self.assertTrue(success)
        self.assertEqual(updated["constitution"], 20)

    def test_cannot_equip_the_same_ring_twice(self):
        use_test_db("tests/tmp/ring_test4.db")
        user_id = 900406
        make_basic_character(user_id, "Careful", current_location="crossroads_tavern")
        db.add_item(user_id, -999, "ring_of_protection", 1)
        db.equip_item(user_id, -999, "ring_of_protection")
        success, message, _ = db.equip_item(user_id, -999, "ring_of_protection")
        self.assertFalse(success)

    def test_auto_equip_wears_every_carried_ring_and_amulet(self):
        use_test_db("tests/tmp/auto_equip_accessories_test.db")
        user_id = 900407
        make_basic_character(user_id, "AutoAccessory", current_location="crossroads_tavern")
        db.add_item(user_id, -999, "ring_of_protection", 1)
        db.add_item(user_id, -999, "ring_of_the_undertow", 1)
        db.add_item(user_id, -999, "amulet_of_health", 1)
        summary, character = db.auto_equip_best_gear(user_id, -999)
        self.assertIn("ring_of_protection", character["equipped_accessories"])
        self.assertIn("ring_of_the_undertow", character["equipped_accessories"])
        self.assertIn("amulet_of_health", character["equipped_accessories"])

    def test_equip_item_rejects_non_equippable_types_still(self):
        use_test_db("tests/tmp/ring_test5.db")
        user_id = 900408
        make_basic_character(user_id, "StillChecked", current_location="crossroads_tavern")
        db.add_item(user_id, -999, "waterlogged_journal", 1)
        success, message, _ = db.equip_item(user_id, -999, "waterlogged_journal")
        self.assertFalse(success)

    def test_cloak_of_elvenkind_grants_advantage_on_sneak_checks(self):
        use_test_db("tests/tmp/cloak_test.db")
        user_id = 900409
        make_basic_character(user_id, "Sneaky", current_location="crossroads_tavern")
        db.add_item(user_id, -999, "cloak_of_elvenkind", 1)
        db.equip_item(user_id, -999, "cloak_of_elvenkind")
        character = db.get_character(user_id, -999)
        self.assertTrue(
            bot._cloak_of_elvenkind_grants_advantage(character, "dexterity", "I try to sneak past the guard")
        )

    def test_cloak_of_elvenkind_doesnt_buff_non_stealth_dex_checks(self):
        use_test_db("tests/tmp/cloak_test2.db")
        user_id = 900410
        make_basic_character(user_id, "Climber", current_location="crossroads_tavern")
        db.add_item(user_id, -999, "cloak_of_elvenkind", 1)
        db.equip_item(user_id, -999, "cloak_of_elvenkind")
        character = db.get_character(user_id, -999)
        self.assertFalse(
            bot._cloak_of_elvenkind_grants_advantage(character, "dexterity", "I try to climb the wall")
        )

    def test_no_advantage_without_the_cloak(self):
        use_test_db("tests/tmp/cloak_test3.db")
        user_id = 900411
        make_basic_character(user_id, "NoCloak", current_location="crossroads_tavern")
        character = db.get_character(user_id, -999)
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
        db.update_character(user_id, -999, level=2)

        enemy_id = -2_500_040
        enemy = {
            "telegram_user_id": enemy_id, "name": "Goblin", "dexterity": 10,
            "hp_current": 7, "monster_key": "goblin",
        }
        player = db.get_character(user_id, -999)
        player["telegram_user_id"] = user_id
        session = sessions.start_session(-999, [player, enemy], {enemy_id: "enemy", user_id: "party"})

        sink = []
        await bot._do_channel_divinity(FakeUpdate(user_id, "channel divinity", sink))
        self.assertTrue(any("no undead" in m for m in sink))
        sessions.end_session(-999)

    async def test_channel_divinity_frightens_undead_and_is_once_per_rest(self):
        import sessions
        sessions.end_session(-999)
        user_id = 900453
        make_basic_character(user_id, "Chaplain", char_class="Cleric", current_location="crossroads_tavern")
        db.update_character(user_id, -999, level=2)

        undead_id = -2_500_041
        undead = {
            "telegram_user_id": undead_id, "name": "Shadow Wisp", "dexterity": 18,
            "hp_current": 22, "monster_key": "shadow_wisp", "conditions": [],
        }
        player = db.get_character(user_id, -999)
        player["telegram_user_id"] = user_id
        session = sessions.start_session(-999, [player, undead], {undead_id: "enemy", user_id: "party"})

        sink = []
        await bot._do_channel_divinity(FakeUpdate(user_id, "channel divinity", sink))
        self.assertIn("frightened", undead["conditions"])
        self.assertEqual(db.get_feature_uses(user_id, -999, "channel_divinity"), 1)

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
        character = db.get_character(user_id, -999)
        self.assertEqual(character["inventory"].get("raw_fish", 0), 0)

    async def test_fishing_works_with_pole_and_bait(self):
        user_id = 900461
        make_basic_character(user_id, "Angler", current_location="stonearch_bridge")
        db.add_item(user_id, -999, "fishing_pole", 1)
        db.add_item(user_id, -999, "bait", 1)
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
        db.update_character(user_id, -999, level=2)

        enemy_id = -2_500_050
        enemy = {"telegram_user_id": enemy_id, "name": "Dummy", "dexterity": 10, "hp_current": 100}
        player = db.get_character(user_id, -999)
        player["telegram_user_id"] = user_id
        session = sessions.start_session(-999, [player, enemy], {enemy_id: "enemy", user_id: "party"})

        sink = []
        await bot._do_action_surge(FakeUpdate(user_id, "action surge", sink))
        participant = next(p for p in session.participants if p["telegram_user_id"] == user_id)
        self.assertTrue(participant.get("action_surge_active"))
        self.assertEqual(db.get_feature_uses(user_id, -999, "action_surge"), 1)

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
        db.update_character(user_id, -999, spell_slots_max=2, spell_slots_current=2)
        sink = []
        await bot._do_divine_smite(FakeUpdate(user_id, "divine smite", sink))
        self.assertTrue(any("level 2" in m for m in sink))

    async def test_divine_smite_rejects_with_no_spell_slot(self):
        import sessions
        sessions.end_session(-999)
        user_id = 900476
        make_basic_character(user_id, "Squire", char_class="Paladin")
        db.update_character(user_id, -999, level=2, spell_slots_max=2, spell_slots_current=0)
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
        db.update_character(user_id, -999, level=2, spell_slots_max=2, spell_slots_current=2)

        enemy_id = -2_500_051
        enemy = {
            "telegram_user_id": enemy_id, "name": "Dummy", "dexterity": 10, "strength": 10,
            "armor_class": 1, "proficiency_bonus": 2,  # guaranteed hits
            "hp_current": 200, "hp_max": 200, "is_ai": 1, "monster_key": "goblin",
        }
        player = db.get_character(user_id, -999)
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
        self.assertEqual(db.get_character(user_id, -999)["spell_slots_current"], 1)
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
        db.update_character(user_id, -999, level=2)

        enemy_id = -2_500_052
        enemy = {"telegram_user_id": enemy_id, "name": "Dummy", "dexterity": 10, "hp_current": 100}
        player = db.get_character(user_id, -999)
        player["telegram_user_id"] = user_id
        session = sessions.start_session(-999, [player, enemy], {enemy_id: "enemy", user_id: "party"})

        sink = []
        await bot._do_flurry_of_blows(FakeUpdate(user_id, "flurry of blows", sink))
        participant = next(p for p in session.participants if p["telegram_user_id"] == user_id)
        self.assertEqual(participant.get("flurry_bonus_attacks"), 2)
        self.assertEqual(db.get_feature_uses(user_id, -999, "ki"), 1)

        # A level 2 Monk only has 2 ki points -- second use ok, third rejected.
        participant.pop("flurry_bonus_attacks", None)
        sink2 = []
        await bot._do_flurry_of_blows(FakeUpdate(user_id, "flurry of blows", sink2))
        self.assertEqual(db.get_feature_uses(user_id, -999, "ki"), 2)
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
        self.assertEqual(db.get_character(user_id, -999)["manual_dice_enabled"], 1)
        # Task #158 (2026-07-18): _safe_send now strips ** markers into real
        # Telegram bold entities instead of sending them as literal text.
        self.assertTrue(any("now ON" in m for m in sink))

        sink2 = []
        await bot._do_toggle_manual_dice(FakeUpdate(user_id, "let the game roll for me", sink2), "let the game roll for me")
        self.assertEqual(db.get_character(user_id, -999)["manual_dice_enabled"], 0)
        self.assertTrue(any("now OFF" in m for m in sink2))

    # -- Oversized-message truncation (2026-08-09, found triaging live logs) --
    def test_truncate_for_telegram_limit_leaves_short_text_alone(self):
        text = "A short line of narration."
        self.assertEqual(bot._truncate_for_telegram_limit(text), text)

    def test_truncate_for_telegram_limit_shrinks_an_oversized_message(self):
        """
        Real live bug (2026-08-09, bot_live_tmp.log): a narration message
        overran Telegram's real ~4096-char sendMessage ceiling and
        _safe_send's retry loop resent the SAME oversized text three
        times -- guaranteed to fail identically -- so the player got
        nothing at all. Confirms the truncated result is genuinely under
        Telegram's real limit (in UTF-16 units, matching how Telegram
        itself counts) and still ends with real content, not just "…".
        """
        text = "x" * 10_000
        truncated = bot._truncate_for_telegram_limit(text)
        self.assertLess(bot._utf16_len(truncated), bot._TELEGRAM_MESSAGE_LIMIT)
        self.assertTrue(truncated.endswith("…"))
        self.assertGreater(len(truncated), 100)

    async def test_safe_send_truncates_before_ever_calling_telegram(self):
        """
        End-to-end: _safe_send itself, not just the helper in isolation --
        confirms the actual text handed to effective_chat.send_message
        (what would really go over the wire to Telegram) is already
        truncated, for every caller, without each one needing its own
        length guard.
        """
        user_id = 900482
        make_basic_character(user_id, "Verbose", current_location="crossroads_tavern")
        sink = []
        update = FakeUpdate(user_id, "n/a", sink)
        await bot._safe_send(update, "y" * 10_000, speak=False)
        self.assertEqual(len(sink), 1)
        self.assertLess(bot._utf16_len(sink[0]), bot._TELEGRAM_MESSAGE_LIMIT)

    async def test_skill_check_prompts_for_manual_roll_and_resumes_with_it(self):
        user_id = 900481
        make_basic_character(user_id, "Sharpeyes", current_location="crossroads_tavern")
        db.update_character(user_id, -999, manual_dice_enabled=1)
        bot._chat_scoped_dict(bot._PENDING_DICE_ROLLS, -999).pop(user_id, None)

        sink = []
        await bot._do_skill_check(FakeUpdate(user_id, "I listen at the door", sink), "wisdom", "I listen at the door")
        # Prompt wording is lowercase "roll a d20" (only the character's
        # own name is capitalized/bolded) -- this test predates that
        # standardized wording and checked for a capital "Roll".
        self.assertTrue(any("roll a d20" in m.lower() for m in sink))
        self.assertIn(user_id, bot._chat_scoped_dict(bot._PENDING_DICE_ROLLS, -999))
        self.assertEqual(bot._chat_scoped_dict(bot._PENDING_DICE_ROLLS, -999)[user_id]["kind"], "skill_check")

        sink2 = []
        pending = bot._chat_scoped_dict(bot._PENDING_DICE_ROLLS, -999).pop(user_id)
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
        player = db.get_character(user_id, -999)
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

    async def test_attack_with_a_named_owned_weapon_auto_equips_it(self):
        """
        Real live bug, dev-topic screenshot (2026-08-08): "Attack spider
        4 with silvered dagger" -- "Was supposed to use the silvered
        dagger not shortsword." _weapon_for_attacker only ever reads
        the character's currently-equipped weapon; the "with <weapon>"
        clause was silently discarded, so a player naming a different
        carried weapon than what's equipped always still attacked with
        whatever was already equipped. A player who owns (but hasn't
        equipped) a silvered dagger, attacking "with silvered dagger",
        must end the attack with it actually equipped -- and the
        combat-result narration must name it, not the old shortsword.
        """
        import sessions
        sessions.end_session(-999)
        user_id = 900483
        make_basic_character(user_id, "DaggerWielder", current_location="crossroads_tavern",
                              inventory={"shortsword": 1, "silvered_dagger": 1})
        db.equip_item(user_id, -999, "shortsword")
        enemy_id = -2_500_054
        enemy = {
            "telegram_user_id": enemy_id, "name": "Spider 4", "strength": 10, "dexterity": 10,
            "armor_class": 5, "hp_current": 50, "hp_max": 50, "is_ai": 1, "monster_key": "goblin",
        }
        player = db.get_character(user_id, -999)
        self.assertEqual(player.get("equipped_weapon"), "shortsword")
        player["telegram_user_id"] = user_id
        session = sessions.start_session(-999, [player, enemy], {user_id: "party", enemy_id: "enemy"})
        session.turn_order = [user_id, enemy_id]
        session.current_turn_index = 0

        sink = []
        await bot._do_attack(
            FakeUpdate(user_id, "Attack spider 4 with silvered dagger", sink),
            "Attack spider 4 with silvered dagger", forced_roll=20,
        )
        live = next(p for p in session.participants if p["telegram_user_id"] == user_id)
        self.assertEqual(live.get("equipped_weapon"), "silvered_dagger")
        self.assertEqual(db.get_character(user_id, -999).get("equipped_weapon"), "silvered_dagger")
        self.assertTrue(any("silvered dagger" in line.lower() for line in sink))
        sessions.end_session(-999)

        # No regression: attacking with no weapon named, or already
        # holding the named one, changes nothing and equips nothing new.
        sessions.end_session(-999)
        user_id2 = 900484
        make_basic_character(user_id2, "PlainAttacker", current_location="crossroads_tavern")
        enemy2 = {
            "telegram_user_id": -2_500_055, "name": "Dummy2", "strength": 10, "dexterity": 10,
            "armor_class": 20, "hp_current": 50, "hp_max": 50, "is_ai": 1, "monster_key": "goblin",
        }
        player2 = db.get_character(user_id2, -999)
        equipped_before = player2.get("equipped_weapon")
        player2["telegram_user_id"] = user_id2
        session2 = sessions.start_session(-999, [player2, enemy2], {user_id2: "party", -2_500_055: "enemy"})
        session2.turn_order = [user_id2, -2_500_055]
        await bot._do_attack(FakeUpdate(user_id2, "I attack the dummy", []), "I attack the dummy", forced_roll=1)
        self.assertEqual(db.get_character(user_id2, -999).get("equipped_weapon"), equipped_before)
        sessions.end_session(-999)

    # -- Assassin Backstab + universal Throw (2026-08-08, per Coffee) --

    def test_backstab_tier_multiplier_spans_the_full_1_to_100_level_range(self):
        from rules.leveling import backstab_tier_multiplier
        self.assertEqual(backstab_tier_multiplier(1), 2)
        self.assertEqual(backstab_tier_multiplier(24), 2)
        self.assertEqual(backstab_tier_multiplier(25), 4)
        self.assertEqual(backstab_tier_multiplier(49), 4)
        self.assertEqual(backstab_tier_multiplier(50), 8)
        self.assertEqual(backstab_tier_multiplier(74), 8)
        self.assertEqual(backstab_tier_multiplier(75), 10)
        self.assertEqual(backstab_tier_multiplier(99), 10)

    def test_effective_backstab_multiplier_is_1_for_non_assassins(self):
        make_basic_character(950920, "ThiefRogue", char_class="Rogue")
        db.update_character(950920, -999, subclass="Thief", level=99)
        rogue_thief = db.get_character(950920, -999)
        make_basic_character(950921, "PlainFighter", char_class="Fighter")
        db.update_character(950921, -999, level=99)
        fighter = db.get_character(950921, -999)
        self.assertEqual(bot._effective_backstab_multiplier(rogue_thief), 1)
        self.assertEqual(bot._effective_backstab_multiplier(fighter), 1)

    def test_effective_backstab_multiplier_combines_base_and_level_tier(self):
        make_basic_character(950922, "TestAssassin", char_class="Rogue")
        db.update_character(950922, -999, subclass="Assassin", level=50)
        assassin = db.get_character(950922, -999)
        self.assertEqual(bot._effective_backstab_multiplier(assassin), 8)  # base 1 (default) * tier 8
        db.update_character(950922, -999, backstab_base_multiplier=3)
        assassin_boosted = db.get_character(950922, -999)
        self.assertEqual(bot._effective_backstab_multiplier(assassin_boosted), 24)  # base 3 * tier 8

    async def test_assassin_attack_is_relabeled_backstab_and_multiplies_damage(self):
        """
        Real live request (2026-08-08, per Coffee): "replace the attack
        command for assassins to Backstab" -- no separate command, an
        Assassin's ordinary attack automatically deals x-multiplied
        damage and is labeled Backstab in the combat log.
        """
        import sessions
        sessions.end_session(-999)
        user_id = 950923
        make_basic_character(user_id, "BackstabTester", char_class="Rogue", current_location="crossroads_tavern")
        # backstab_proficiency_pct=100 -- deterministic trigger, isolating
        # this test from the separate proficiency-grind mechanic (covered
        # by its own dedicated tests below).
        db.update_character(user_id, -999, subclass="Assassin", level=25, backstab_proficiency_pct=100.0)
        enemy = {"telegram_user_id": -2_500_060, "name": "BackstabDummy", "strength": 10, "dexterity": 10,
                 "hp_current": 500, "hp_max": 500, "armor_class": 5, "is_ai": 1, "monster_key": "goblin"}
        player = db.get_character(user_id, -999)
        player["telegram_user_id"] = user_id
        session = sessions.start_session(-999, [player, enemy], {user_id: "party", -2_500_060: "enemy"})
        session.turn_order = [user_id, -2_500_060]

        sink = []
        await bot._do_attack(FakeUpdate(user_id, "I attack the dummy", sink), "I attack the dummy", forced_roll=20)
        combined = "\n".join(sink)
        self.assertIn("Backstab", combined)
        # forced_roll=20 is a critical hit -- damage_dice doubles AND the
        # x4 backstab multiplier (level 25) both apply; just confirm it's
        # dramatically more than a single unmultiplied 1d8+mod swing could
        # ever be (a plain hit tops out well under 20 for this weapon).
        dummy_hp = next(p for p in session.participants if p["telegram_user_id"] == -2_500_060)["hp_current"]
        self.assertLess(dummy_hp, 500 - 20, "backstab multiplier doesn't appear to have been applied")
        sessions.end_session(-999)

    async def test_rebirth_locks_in_the_assassins_earned_backstab_multiplier(self):
        """
        Real live spec (2026-08-08, per Coffee, verbatim: "after they
        rebirth and lv goes back to 1 they keep the backstab
        multipliar, it is now set at that for the character... that
        multiplier can then be increased again... this will allow a
        player to 'break the game'"). A rebirth must fold the FULLY
        earned live multiplier (base * this life's level tier) back
        into the persistent base, not reset it, so the next life's own
        climb through the same tiers multiplies an already-inflated
        number instead of starting flat.
        """
        user_id = 950924
        make_basic_character(user_id, "RebirthAssassin", char_class="Rogue", current_location="crossroads_tavern")
        db.update_character(user_id, -999, subclass="Assassin", level=bot.MAX_LEVEL, backstab_base_multiplier=1)
        before = db.get_character(user_id, -999)
        live_multiplier_before_rebirth = bot._effective_backstab_multiplier(before)
        self.assertGreaterEqual(live_multiplier_before_rebirth, 1)

        sink = []
        await bot._do_rebirth(FakeUpdate(user_id, "rebirth", sink))
        after = db.get_character(user_id, -999)
        self.assertEqual(after["level"], 1)
        # The new base must equal exactly what was earned before rebirth --
        # and applying THIS life's own level-1 tier on top of it must never
        # drop below what they already had (level 1 always yields tier x2,
        # so effective multiplier right after rebirth = old_base * 2).
        self.assertEqual(after["backstab_base_multiplier"], live_multiplier_before_rebirth)
        self.assertGreaterEqual(bot._effective_backstab_multiplier(after), live_multiplier_before_rebirth)

        # Leveling back up in the new life climbs even higher than the
        # old life's own ceiling ever could have alone.
        db.update_character(user_id, -999, level=75)
        after_releveled = db.get_character(user_id, -999)
        self.assertEqual(
            bot._effective_backstab_multiplier(after_releveled),
            after["backstab_base_multiplier"] * 10,
        )

    def test_rebirth_does_not_touch_backstab_base_for_non_assassins(self):
        user_id = 950925
        make_basic_character(user_id, "RebirthFighter", char_class="Fighter", current_location="crossroads_tavern")
        db.update_character(user_id, -999, level=bot.MAX_LEVEL, backstab_base_multiplier=5)  # shouldn't happen naturally, but confirm no mutation
        import asyncio as _asyncio

        async def run():
            await bot._do_rebirth(FakeUpdate(user_id, "rebirth", []))

        _asyncio.run(run())
        after = db.get_character(user_id, -999)
        self.assertEqual(after["backstab_base_multiplier"], 5)

    async def test_throw_weapon_still_consumes_the_item_and_narrates_with_throws_not_casts(self):
        """
        Real live spec (2026-08-08, per Coffee, verbatim): "for
        throwing dont use the players attack damage only use the
        weapons attack damage - so even weak players can have a good
        attack" -- STILL true for every character-derived bonus (rage,
        guild %, subclass %, sneak attack, etc.), just no longer true
        for the thrower's own ability modifier specifically (see
        test_throw_damage_now_adds_the_weapons_own_ability_modifier
        below for that real 2026-08-10 reversal).

        Also covers a real live regression (2026-08-10, Coffee,
        dev-bridge screenshot): "Pan casts Throw (Rusty Dagger) at ..."
        read wrong -- "casts" implies spellcasting. Confirms the
        message now says "throws" (lowercase, the real verb), never
        the old "Throw" action-label text.
        """
        import sessions
        sessions.end_session(-999)
        user_id = 950926
        make_basic_character(
            user_id, "WeakThrower", char_class="Wizard", current_location="crossroads_tavern",
            ability_scores={"strength": 6, "dexterity": 8, "constitution": 10,
                             "intelligence": 16, "wisdom": 10, "charisma": 10},
            inventory={"shortsword": 1, "silvered_dagger": 1},
        )
        db.equip_item(user_id, -999, "shortsword")
        enemy = {"telegram_user_id": -2_500_061, "name": "ThrowDummy", "strength": 10, "dexterity": 10,
                 "hp_current": 500, "hp_max": 500, "armor_class": 5, "is_ai": 1, "monster_key": "goblin"}
        player = db.get_character(user_id, -999)
        player["telegram_user_id"] = user_id
        session = sessions.start_session(-999, [player, enemy], {user_id: "party", -2_500_061: "enemy"})
        session.turn_order = [user_id, -2_500_061]
        session.current_turn_index = session.turn_order.index(user_id)

        sink = []
        await bot._do_throw_weapon(
            FakeUpdate(user_id, "throw silvered dagger at ThrowDummy", sink),
            "throw silvered dagger at ThrowDummy",
        )
        combined = "\n".join(sink)
        self.assertIn("throws", combined)
        self.assertNotIn("casts", combined)
        # The dagger must actually be gone from inventory (thrown/consumed).
        after = db.get_character(user_id, -999)
        self.assertNotIn("silvered_dagger", after.get("inventory", {}))
        # Equipped weapon (shortsword) must be untouched -- only the
        # explicitly-named, non-equipped weapon was thrown.
        self.assertEqual(after.get("equipped_weapon"), "shortsword")
        self.assertIn("shortsword", after.get("inventory", {}))
        sessions.end_session(-999)

    def test_assassin_backstab_narration_says_performs_not_casts(self):
        """
        Real bug, same class as the Throw fix above but missed at the
        time (found via direct code read, 2026-08-10): an Assassin's
        ordinary attack sets action_label to "Backstab (xN)" but never
        overrode verb, so _format_combat_result's default ("casts")
        applied -- every Assassin attack read "Pan casts **Backstab
        (x8)** at Goblin", which is wrong; Backstab is a martial
        technique, not a spell. Pure-function check against
        _format_combat_result directly (no Ollama call) -- this is the
        same deterministic line the live bug lived in, no narration
        text involved.
        """
        result = {"hit": True, "damage_dealt": 40, "critical_hit": False, "critical_fail": False}
        message = bot._format_combat_result(
            "", result, actor_label="Pan", defender_label="Goblin",
            action_label="Backstab (x8)", verb="performs",
        )
        self.assertIn("performs **Backstab (x8)** at", message)
        self.assertNotIn("casts", message)

    def test_throw_damage_now_adds_the_weapons_own_ability_modifier(self):
        """
        Real live bug (2026-08-10, Coffee, dev-bridge screenshot): a
        thrown Rusty Dagger (1d4, previously zero ability modifier at
        all) hit for a real, confirmed 1 damage against a 200 HP boss
        -- "that is pathetic lol.. maybe use the players stats in some
        way?! Which stat do u think?" resolve_thrown_attack now adds
        the attacker's modifier for whichever ability the weapon
        already uses for its OWN to-hit roll (weapon["ability"] --
        dexterity for a finesse weapon like a dagger, matching real 5E
        thrown-weapon rules), answering "which stat" with the same one
        the accuracy roll already reads from that weapon's own real
        data. Forces the damage die to its minimum (1) so the only
        variable left is the ability modifier itself.
        """
        from rules.combat import resolve_thrown_attack
        attacker = {"name": "Pan", "dexterity": 16, "strength": 8}
        weapon = {"name": "Rusty Dagger", "damage_dice": "1d4", "ability": "dexterity", "weapon_category": "simple", "damage_type": "physical"}
        defender = {"name": "The Unspoken 3", "armor_class": 10, "hp_current": 200, "hp_max": 200, "conditions": []}
        result = resolve_thrown_attack(attacker, defender, weapon, forced_hit=True, forced_damage_roll=1)
        # Dexterity 16 -> +3 modifier (real 5E: (16-10)//2). 1 (forced
        # minimum roll) + 3 (dex mod) = 4, not the old flat 1.
        self.assertEqual(result["damage_dealt"], 4)

    async def test_assassin_throw_is_a_guaranteed_hit_no_roll(self):
        import sessions
        sessions.end_session(-999)
        user_id = 950927
        make_basic_character(user_id, "AssassinThrower", char_class="Rogue", current_location="crossroads_tavern",
                              inventory={"shortsword": 1, "silvered_dagger": 1})
        db.update_character(user_id, -999, subclass="Assassin")
        db.equip_item(user_id, -999, "shortsword")
        # Impossibly high AC -- a normal roll could never hit this; only
        # a genuine forced_hit (Assassin's Throw bonus) could land.
        enemy = {"telegram_user_id": -2_500_062, "name": "ImpossibleDodge", "strength": 10, "dexterity": 10,
                 "hp_current": 500, "hp_max": 500, "armor_class": 500, "is_ai": 1, "monster_key": "goblin"}
        player = db.get_character(user_id, -999)
        player["telegram_user_id"] = user_id
        session = sessions.start_session(-999, [player, enemy], {user_id: "party", -2_500_062: "enemy"})
        session.turn_order = [user_id, -2_500_062]
        session.current_turn_index = session.turn_order.index(user_id)

        before_hp = enemy["hp_current"]
        await bot._do_throw_weapon(
            FakeUpdate(user_id, "throw silvered dagger at ImpossibleDodge", []),
            "throw silvered dagger at ImpossibleDodge",
        )
        live_enemy = next((p for p in session.participants if p["telegram_user_id"] == -2_500_062), None)
        after_hp = live_enemy["hp_current"] if live_enemy else 0
        self.assertLess(after_hp, before_hp, "Assassin's Throw must guarantee a hit even against impossible AC")
        sessions.end_session(-999)

    async def test_throw_intent_classified_and_battle_menu_offers_a_target_picker(self):
        from ai.intent_parser import _keyword_fallback
        r = _keyword_fallback("Throw the dagger at the goblin", [])
        self.assertEqual(r["action"], "throw_weapon")

        import sessions
        sessions.end_session(-999)
        user_id = 950928
        make_basic_character(user_id, "MenuThrower", current_location="crossroads_tavern",
                              inventory={"shortsword": 1, "silvered_dagger": 1})
        db.equip_item(user_id, -999, "shortsword")
        enemy_a = {"telegram_user_id": -2_500_063, "name": "MenuGoblinA", "hp_current": 20, "hp_max": 20,
                   "is_ai": 1, "strength": 10, "dexterity": 10, "armor_class": 10}
        enemy_b = {"telegram_user_id": -2_500_064, "name": "MenuGoblinB", "hp_current": 20, "hp_max": 20,
                   "is_ai": 1, "strength": 10, "dexterity": 10, "armor_class": 10}
        player = db.get_character(user_id, -999)
        player["telegram_user_id"] = user_id
        session = sessions.start_session(-999, [player, enemy_a, enemy_b],
                                          {user_id: "party", -2_500_063: "enemy", -2_500_064: "enemy"})
        session.turn_order = [user_id, -2_500_063, -2_500_064]
        session.current_turn_index = 0

        async def tap(data):
            sink = []
            await bot.battle_menu_callback(FakeCallbackUpdate(user_id, data, sink), DummyContext())
            return "\n".join(sink)

        weapon_picker = await tap("bm|more")
        self.assertIn("bm|throw", weapon_picker)

        item_picker = await tap("bm|throw")
        self.assertIn("bm|throwweapon|silvered_dagger", item_picker)
        self.assertNotIn("bm|throwweapon|shortsword", item_picker)  # equipped weapon excluded

        target_picker = await tap("bm|throwweapon|silvered_dagger")
        self.assertIn("MenuGoblinA", target_picker)
        self.assertIn("MenuGoblinB", target_picker)
        self.assertIn("bm|throwtarget|silvered_dagger|MenuGoblinB", target_picker)
        sessions.end_session(-999)

    # -- Grindable mastery proficiencies (2026-08-08, per Coffee) ------

    def test_roll_percentage_check_bounds_are_never_and_always(self):
        from rules.dice import roll_percentage_check
        # 0% must never succeed regardless of luck; 100% must always.
        self.assertFalse(any(roll_percentage_check(0.0) for _ in range(200)))
        self.assertTrue(all(roll_percentage_check(100.0) for _ in range(200)))

    def test_grind_flat_proficiency_increments_and_caps_at_100(self):
        user_id = 950929
        make_basic_character(user_id, "GrindTester", current_location="crossroads_tavern")
        used_value = bot._grind_flat_proficiency(user_id, -999, "backstab_proficiency_pct", 5.0)
        self.assertEqual(used_value, 5.0)  # returns the value AS IT STOOD before this use
        after = db.get_character(user_id, -999)
        self.assertAlmostEqual(after["backstab_proficiency_pct"], 5.01, places=5)

        bot._grind_flat_proficiency(user_id, -999, "backstab_proficiency_pct", 99.995)
        capped = db.get_character(user_id, -999)
        self.assertLessEqual(capped["backstab_proficiency_pct"], 100.0)

    def test_grind_dict_proficiency_is_keyed_per_category_independently(self):
        """
        Real live spec (2026-08-08, per Coffee): "if players choose to
        use another weapon or armour type they CAN and they can level
        them up to get better with them" -- switching categories must
        never touch a different category's own separately-earned %.
        """
        user_id = 950930
        make_basic_character(user_id, "GrindTester2", current_location="crossroads_tavern")
        bot._grind_dict_proficiency(user_id, -999, "weapon_proficiency_pct", {}, "martial")
        after1 = db.get_character(user_id, -999)
        self.assertAlmostEqual(after1["weapon_proficiency_pct"]["martial"], 1.01, places=5)
        self.assertNotIn("simple", after1["weapon_proficiency_pct"])

        bot._grind_dict_proficiency(user_id, -999, "weapon_proficiency_pct", after1["weapon_proficiency_pct"], "simple")
        after2 = db.get_character(user_id, -999)
        self.assertAlmostEqual(after2["weapon_proficiency_pct"]["simple"], 1.01, places=5)
        self.assertAlmostEqual(after2["weapon_proficiency_pct"]["martial"], 1.01, places=5)  # untouched by the other category's grind

    def test_equipped_proficiency_bonus_sums_matching_gear_only(self):
        user_id = 950931
        make_basic_character(user_id, "ProfGearTester", current_location="crossroads_tavern",
                              inventory={"shortsword": 1})
        db.equip_item(user_id, -999, "shortsword")
        ring_id = db.create_item_instance(
            item_type="ring", name="Ring of Martial Focus", rarity="rare", price=400,
            base_stats={"type": "ring"},
            affixes=[
                {"kind": "proficiency_bonus", "stat": "weapon", "category": "martial", "value": 5.0},
                {"kind": "proficiency_bonus", "stat": "backstab", "value": 3.0},
            ],
        )
        db.add_item(user_id, -999, ring_id, 1)
        db.equip_item(user_id, -999, ring_id)
        character = db.get_character(user_id, -999)
        self.assertEqual(bot._equipped_proficiency_bonus(character, "weapon", "martial"), 5.0)
        self.assertEqual(bot._equipped_proficiency_bonus(character, "weapon", "simple"), 0)  # wrong category, not counted
        self.assertEqual(bot._equipped_proficiency_bonus(character, "backstab"), 3.0)
        self.assertEqual(bot._equipped_proficiency_bonus(character, "throw"), 0)  # no throw affix equipped

    async def test_backstab_never_triggers_at_zero_proficiency_falls_back_to_normal_attack(self):
        import sessions
        sessions.end_session(-999)
        user_id = 950932
        make_basic_character(user_id, "ZeroProfAssassin", char_class="Rogue", current_location="crossroads_tavern")
        db.update_character(user_id, -999, subclass="Assassin", level=75, backstab_proficiency_pct=0.0)
        enemy = {"telegram_user_id": -2_500_065, "name": "ZeroProfDummy", "strength": 10, "dexterity": 10,
                 "hp_current": 500, "hp_max": 500, "armor_class": 5, "is_ai": 1, "monster_key": "goblin"}
        player = db.get_character(user_id, -999)
        player["telegram_user_id"] = user_id
        session = sessions.start_session(-999, [player, enemy], {user_id: "party", -2_500_065: "enemy"})
        session.turn_order = [user_id, -2_500_065]

        sink = []
        await bot._do_attack(FakeUpdate(user_id, "I attack the dummy", sink), "I attack the dummy", forced_roll=20)
        combined = "\n".join(sink)
        # Still labeled Backstab (that's automatic for an Assassin's
        # attack), but the x10 multiplier (level 75) must NOT have
        # applied -- a plain crit on this weak weapon can't plausibly
        # exceed 30 damage, while a x10 multiplied crit would dwarf it.
        dummy_hp = next(p for p in session.participants if p["telegram_user_id"] == -2_500_065)["hp_current"]
        self.assertGreater(dummy_hp, 500 - 40, "0% proficiency must fall back to a normal x1 attack, not x10")
        sessions.end_session(-999)
        # Grinding still happened despite the failed roll.
        after = db.get_character(user_id, -999)
        self.assertAlmostEqual(after["backstab_proficiency_pct"], 0.01, places=5)

    # -- Inactive party members' XP share (2026-07-16, per Coffee) ----
    async def test_absent_party_member_gets_partial_xp_share(self):
        import sessions
        sessions.end_session(-999)

        fighter_id = 900483
        absent_id = 900484
        make_basic_character(fighter_id, "Fighter", current_location="crossroads_tavern")
        make_basic_character(absent_id, "Homebody", current_location="crossroads_tavern")
        party_id = db.create_party(fighter_id, -999)
        db.update_character(absent_id, -999, party_id=party_id)

        enemy_id = -2_500_054
        enemy = {
            "telegram_user_id": enemy_id, "name": "Goblin", "dexterity": 10, "xp_reward": 100,
        }
        fighter = db.get_character(fighter_id, -999)
        fighter["telegram_user_id"] = fighter_id
        session = sessions.start_session(-999, [fighter, enemy], {enemy_id: "enemy", fighter_id: "party"})
        session.turn_order = [fighter_id, enemy_id]

        before_absent = db.get_character(absent_id, -999)["xp"]
        before_fighter = db.get_character(fighter_id, -999)["xp"]
        await bot._award_victory_xp(FakeUpdate(fighter_id, "", []), session)
        after_absent = db.get_character(absent_id, -999)["xp"]
        after_fighter = db.get_character(fighter_id, -999)["xp"]

        self.assertEqual(after_fighter - before_fighter, 100)
        self.assertEqual(after_absent - before_absent, 50)  # INACTIVE_PARTY_XP_SHARE raised to 50% (2026-07-24)
        sessions.end_session(-999)

    async def test_ai_companions_never_get_the_absent_party_bonus(self):
        import sessions
        sessions.end_session(-999)

        fighter_id = 900485
        make_basic_character(fighter_id, "Fighter", current_location="crossroads_tavern")
        party_id = db.create_party(fighter_id, -999)
        companion = db.create_ai_companion(
            -999, "Buddy", "Human", "Fighter",
            ability_scores={"strength": 15, "dexterity": 14, "constitution": 13,
                             "intelligence": 10, "wisdom": 10, "charisma": 10},
            hp_max=12, armor_class=15, gold=10, inventory={},
        )
        db.add_ai_companion_to_party(companion["telegram_user_id"], -999, party_id)

        enemy_id = -2_500_055
        enemy = {"telegram_user_id": enemy_id, "name": "Goblin", "dexterity": 10, "xp_reward": 50}
        fighter = db.get_character(fighter_id, -999)
        fighter["telegram_user_id"] = fighter_id
        session = sessions.start_session(-999, [fighter, enemy], {enemy_id: "enemy", fighter_id: "party"})
        session.turn_order = [fighter_id, enemy_id]

        # Should not raise even if an AI companion shares the party, and
        # the companion (is_ai) must never receive the absent-member bonus.
        companion_xp_before = companion["xp"]
        await bot._award_victory_xp(FakeUpdate(fighter_id, "", []), session)
        companion_xp_after = db.get_character(companion["telegram_user_id"], -999)["xp"]
        self.assertEqual(companion_xp_after, companion_xp_before)
        sessions.end_session(-999)

    def test_generated_item_can_be_kept_equipped_and_affects_combat(self):
        """
        Real Phase 1 deliverable of the magic item system (2026-08-02,
        per Coffee: "make loot real" -- see the plan file's Context
        section). Before this, rules/item_generator.py already rolled
        real tier-weighted weapon/armor stats, but the roll had no
        stable item_id and nothing could persist it -- the only caller
        converted it straight to gold instead of letting anyone keep it.
        Also covers the tier-bonus-as-affix refactor (no longer baked
        into damage_dice/ac_base as a dice-string/int concatenation) and
        the real find_item_mentioned_in_text landmine fix (direct
        ITEMS[...] indexing would KeyError the moment a generated item
        ever reached it).
        """
        from rules.item_generator import generate_weapon, generate_armor
        user_id = 950901
        make_basic_character(user_id, "LootTester2", current_location="crossroads_tavern")

        legendary_weapon = generate_weapon(tier="legendary")
        self.assertNotIn("+", legendary_weapon["damage_dice"])
        self.assertTrue(any(
            a.get("kind") == "stat_bonus" and a.get("field") == "damage_bonus" and a.get("value") == 4
            for a in legendary_weapon["affixes"]
        ))
        common_weapon = generate_weapon(tier="common")
        self.assertEqual(common_weapon["affixes"], [])

        gold_before = db.get_character(user_id, -999)["gold"]
        item_id = db.create_item_instance(
            item_type=legendary_weapon["type"], name=legendary_weapon["name"], rarity=legendary_weapon["rarity"],
            price=legendary_weapon["price"], base_stats=legendary_weapon, affixes=legendary_weapon["affixes"],
        )
        self.assertTrue(item_id.startswith("gi") and item_id[2:].isdigit())
        db.add_item(user_id, -999, item_id, 1)
        character = db.get_character(user_id, -999)
        self.assertEqual(character["inventory"].get(item_id), 1)
        self.assertEqual(character["gold"], gold_before)  # kept, not sold

        resolved = items_module.get_item(item_id)
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.get("damage_bonus"), 4)
        self.assertEqual(resolved["rarity"], "legendary")

        success, _msg, _updated = db.equip_item(user_id, -999, item_id)
        self.assertTrue(success)
        weapon_for_attack = bot._weapon_for_attacker(db.get_character(user_id, -999))
        self.assertEqual(weapon_for_attack.get("damage_bonus"), 4)
        self.assertEqual(weapon_for_attack.get("damage_dice"), legendary_weapon["damage_dice"])

        legendary_armor = generate_armor(tier="legendary")
        armor_id = db.create_item_instance(
            item_type=legendary_armor["type"], name=legendary_armor["name"], rarity=legendary_armor["rarity"],
            price=legendary_armor["price"], base_stats=legendary_armor, affixes=legendary_armor["affixes"],
        )
        db.add_item(user_id, -999, armor_id, 1)
        expected_bonus = next(a["value"] for a in legendary_armor["affixes"] if a["field"] == "ac_base")
        self.assertEqual(expected_bonus, 4)
        resolved_armor = items_module.get_item(armor_id)
        self.assertEqual(resolved_armor["ac_base"], legendary_armor["ac_base"] + expected_bonus)
        ac_success, _ac_msg, _ac_updated = db.equip_item(user_id, -999, armor_id)
        self.assertTrue(ac_success)

        # Landmine: find_item_mentioned_in_text must not KeyError on a
        # generated item id, and must actually resolve it by name.
        candidates = list(db.get_character(user_id, -999)["inventory"].keys())
        result = items_module.find_item_mentioned_in_text(
            f"equip the {legendary_weapon['name'].lower()}", candidate_ids=candidates,
        )
        self.assertEqual(result, item_id)

    async def test_generated_loot_is_kept_not_auto_sold_on_combat_victory(self):
        """
        Full-stack version of the test above -- proves the REAL combat-
        victory call site (_award_victory_xp's loot section, via the new
        _grant_generated_loot helper) actually keeps the roll instead of
        the old "convert to gold" behavior.
        """
        import sessions
        sessions.end_session(-999)
        leader_id = 950902
        make_basic_character(leader_id, "LootVictoryLeader", current_location="crossroads_tavern")
        gold_before = db.get_character(leader_id, -999)["gold"]

        enemy = {"telegram_user_id": -2_600_301, "name": "LootGoblin", "dexterity": 10, "xp_reward": 10}
        leader = db.get_character(leader_id, -999)
        leader["telegram_user_id"] = leader_id
        session = sessions.start_session(-999, [leader, enemy], {leader_id: "party", -2_600_301: "enemy"})
        session.turn_order = [leader_id, -2_600_301]

        summary, _level_up_notes = await bot._award_victory_xp(FakeUpdate(leader_id, "", []), session)
        self.assertIn("loots a", summary)
        self.assertIn("real", summary)

        updated_character = db.get_character(leader_id, -999)
        generated_ids = [iid for iid in updated_character["inventory"] if iid.startswith("gi")]
        self.assertEqual(len(generated_ids), 1)
        self.assertEqual(updated_character["gold"], gold_before)  # no gold awarded for the roll itself
        sessions.end_session(-999)

    async def test_boss_defeat_can_drop_a_spell_tonic(self):
        """
        Real feature (2026-08-10, per Coffee: "make it so the ethers are
        hard to find, they can be dropped after boss battles or found
        as hidden treasure") -- removed from every shop entirely, only
        obtainable this way or from one of a handful of hidden chests
        in campaign.json now. Gated to real is_boss fights.
        """
        import sessions
        from unittest.mock import patch, AsyncMock
        sessions.end_session(-999)
        leader_id = 950905
        make_basic_character(leader_id, "BossSlayer", current_location="crossroads_tavern")
        enemy = {
            "telegram_user_id": -2_600_304, "name": "Test Boss", "dexterity": 10,
            "xp_reward": 10, "is_boss": True,
        }
        leader = db.get_character(leader_id, -999)
        leader["telegram_user_id"] = leader_id
        session = sessions.start_session(-999, [leader, enemy], {leader_id: "party", -2_600_304: "enemy"})
        session.turn_order = [leader_id, -2_600_304]

        # random.random() < MAP_LOOT_DROP_CHANCE (0.08) also fires at 0.0 --
        # that's fine (real, independent roll), but random.choice needs to
        # stay correct for BOTH that draw and this test's own finder draw.
        def fake_choice(seq):
            return leader_id if leader_id in seq else seq[0]

        with patch("bot._grant_generated_loot", new=AsyncMock(return_value="")), \
             patch("bot.random.random", return_value=0.0), \
             patch("bot.random.choices", return_value=["spell_tonic"]), \
             patch("bot.random.choice", side_effect=fake_choice):
            summary, _ = await bot._award_victory_xp(FakeUpdate(leader_id, "", []), session)
        self.assertIn("Spell Tonic", summary)
        self.assertIn("boss's remains", summary)

        updated = db.get_character(leader_id, -999)
        self.assertEqual(updated["inventory"].get("spell_tonic", 0), 1)
        sessions.end_session(-999)

    async def test_non_boss_defeat_never_drops_a_spell_tonic(self):
        import sessions
        from unittest.mock import patch, AsyncMock
        sessions.end_session(-999)
        leader_id = 950906
        make_basic_character(leader_id, "RegularSlayer", current_location="crossroads_tavern")
        enemy = {
            "telegram_user_id": -2_600_305, "name": "Regular Goblin", "dexterity": 10, "xp_reward": 10,
        }
        leader = db.get_character(leader_id, -999)
        leader["telegram_user_id"] = leader_id
        session = sessions.start_session(-999, [leader, enemy], {leader_id: "party", -2_600_305: "enemy"})
        session.turn_order = [leader_id, -2_600_305]

        with patch("bot._grant_generated_loot", new=AsyncMock(return_value="")), \
             patch("bot.random.random", return_value=0.0):
            summary, _ = await bot._award_victory_xp(FakeUpdate(leader_id, "", []), session)
        self.assertNotIn("boss's remains", summary)
        updated = db.get_character(leader_id, -999)
        self.assertNotIn("spell_tonic", updated["inventory"])
        sessions.end_session(-999)

    def test_spell_tonics_are_not_sold_in_any_shop(self):
        """
        Real feature (2026-08-10): the whole point of "hard to find" is
        that gold alone can't get you one -- confirms none of the four
        tiers appear in any shop's inventory in the live campaign data.
        """
        tonic_ids = {"spell_tonic", "greater_spell_tonic", "supreme_spell_tonic", "elixir_of_the_arcane_circle"}
        for shop_id, shop in bot.CAMPAIGN["shops"].items():
            overlap = tonic_ids & set(shop["inventory"])
            self.assertFalse(overlap, f"{shop_id} sells {overlap}, but tonics must be find-only")

    def test_spell_tonics_are_reachable_as_real_hidden_treasure(self):
        """Confirms at least one lockable chest in the live campaign actually carries each tonic tier."""
        tonic_ids = {"spell_tonic", "greater_spell_tonic", "supreme_spell_tonic", "elixir_of_the_arcane_circle"}
        found = set()
        for layer in bot.CAMPAIGN["locations"].values():
            for location in layer.values():
                for lockable in location.get("lockables", []):
                    found |= tonic_ids & set(lockable.get("loot", {}) or {})
        self.assertEqual(found, tonic_ids)

    def test_equipped_elemental_resistance_actually_halves_matching_damage(self):
        """
        Real Phase 2 deliverable of the magic item system (2026-08-02):
        rules.combat.apply_damage_type_modifier already read
        resistances/vulnerabilities/immunities off ANY defender dict
        (monster templates have set these for a while), but no real
        PLAYER ever had them populated -- equipped gear's resistance
        affix now reaches combat via
        bot._apply_equipped_elemental_profile, live-summed at
        combat-start time in bot._get_real_party_combatants, same
        pattern as _equipped_regen_bonus/_equipped_ring_ac_bonus.
        """
        from rules.combat import apply_damage_type_modifier
        from rules.item_generator import generate_armor
        user_id = 950903
        character = make_basic_character(user_id, "ElementalTester2", current_location="crossroads_tavern")

        self.assertEqual(apply_damage_type_modifier(20, "fire", character), 20)

        armor = generate_armor(tier="legendary")
        armor["affixes"] = [a for a in armor["affixes"] if a.get("kind") != "resistance"]
        armor["affixes"].append({"kind": "resistance", "damage_type": "fire"})
        armor_id = db.create_item_instance(
            item_type=armor["type"], name=armor["name"], rarity=armor["rarity"],
            price=armor["price"], base_stats=armor, affixes=armor["affixes"],
        )
        db.add_item(user_id, -999, armor_id, 1)
        success, _msg, _updated = db.equip_item(user_id, -999, armor_id)
        self.assertTrue(success)
        self.assertIn("fire", items_module.get_item(armor_id).get("resistances", []))

        party = bot._get_real_party_combatants(db.get_character(user_id, -999))
        combatant = next(p for p in party if p["telegram_user_id"] == user_id)
        self.assertIn("fire", combatant.get("resistances", []))
        self.assertEqual(apply_damage_type_modifier(20, "fire", combatant), 10)
        self.assertEqual(apply_damage_type_modifier(20, "cold", combatant), 20)

    async def test_equipped_item_can_grant_a_spell_gated_by_feature_uses(self):
        """
        Real Phase 3 deliverable of the magic item system (2026-08-02):
        _do_cast_spell already had a real precedent for "temporary access
        to a spell you don't otherwise know" -- a carried scroll. This
        adds a third fallback: an EQUIPPED item can grant a spell too,
        but gated by feature_uses (a permanently-worn item isn't a
        one-shot consumable like a scroll) instead of spending a real
        spell slot or ever touching known_spells.
        """
        user_id = 950904
        character = make_basic_character(user_id, "GearCaster2", char_class="Fighter", current_location="crossroads_tavern")
        self.assertNotIn("cure_wounds", character["known_spells"])
        known_spells_before = list(character["known_spells"])
        slots_before = character["spell_slots_current"]

        ring_id = db.create_item_instance(
            item_type="ring", name="Ring of the Ember Spark", rarity="rare", price=500,
            base_stats={"type": "ring"},
            affixes=[{"kind": "grants_spell", "spell_id": "cure_wounds", "uses": 2}],
        )
        db.add_item(user_id, -999, ring_id, 1)
        success, _msg, _updated = db.equip_item(user_id, -999, ring_id)
        self.assertTrue(success)

        async def cast():
            sink = []
            await bot._do_cast_spell(FakeUpdate(user_id, "cast cure wounds", sink), "cast cure wounds")
            return "\n".join(sink)

        reply1 = await cast()
        self.assertNotIn("don't know a spell", reply1.lower())
        reply2 = await cast()
        self.assertNotIn("don't know a spell", reply2.lower())
        self.assertNotIn("already used", reply2.lower())
        reply3 = await cast()
        self.assertIn("already used", reply3.lower())

        after = db.get_character(user_id, -999)
        self.assertEqual(after["known_spells"], known_spells_before)
        self.assertEqual(after["spell_slots_current"], slots_before)
        self.assertEqual(db.get_feature_uses(user_id, -999, f"item_spell_{ring_id[2:]}"), 2)

    async def test_cast_spell_with_no_slots_mid_combat_resends_the_battle_menu(self):
        """
        Real live bug, dev-topic screenshot (2026-08-08, Coffee): "If
        they have no slots and the spell didn't work, can you please
        give them push buttons and indicate it is still thier turn so
        they know to make their move?" A failed cast (no spell slots
        left) used to leave nothing but a bare "no slots" text message
        -- mid-combat, that left the player with no battle menu and no
        visible confirmation it was still their turn. Casting a real
        level-1 damage spell with spell_slots_current forced to 0 must
        now ALSO re-send the same round/turn announcement + tappable
        battle menu every other in-combat failure path already shows.
        """
        import sessions
        sessions.end_session(-999)
        user_id = 950906
        make_basic_character(user_id, "SlotlessCleric", char_class="Cleric", current_location="crossroads_tavern",
                              known_spells=["guiding_bolt"])
        db.update_character(user_id, -999, spell_slots_current=0)
        enemy = {
            "telegram_user_id": -2_500_056, "name": "SlotTestGoblin", "dexterity": 10,
            "hp_current": 20, "hp_max": 20, "is_ai": 1, "monster_key": "goblin",
        }
        player = db.get_character(user_id, -999)
        player["telegram_user_id"] = user_id
        session = sessions.start_session(-999, [player, enemy], {user_id: "party", -2_500_056: "enemy"})
        session.turn_order = [user_id, -2_500_056]
        session.current_turn_index = 0

        sink = []
        await bot._do_cast_spell(FakeUpdate(user_id, "cast guiding bolt on the goblin", sink),
                                  "cast guiding bolt on the goblin")
        combined = "\n".join(sink)
        self.assertIn("no spell slots remaining", combined.lower())
        self.assertIn("round", combined.lower())
        self.assertTrue(any("turn" in line.lower() for line in sink))
        sessions.end_session(-999)

    async def test_equipped_profession_bonus_crosses_a_real_dc_boundary(self):
        """
        Real Phase 4 deliverable of the magic item system (2026-08-02):
        _do_gather/_do_craft already summed multiple additive bonus
        sources (_practiced_bonus_for + class_profession_affinity_bonus)
        into one int before comparing to the flat SKILL_CHECK_DC -- a
        third source, live-summed off equipped gear
        (bot._equipped_profession_bonus), slots into that exact same
        accumulator. Proven with a real forced_roll that fails the DC
        without the item and passes with it, not a unit check of the
        number alone.
        """
        user_id = 950905
        make_basic_character(
            user_id, "MiningTester2", char_class="Rogue", current_location="sunken_root_caverns",
            ability_scores={"strength": 10, "dexterity": 14, "constitution": 12,
                             "intelligence": 10, "wisdom": 10, "charisma": 10},
        )
        db.add_item(user_id, -999, "pickaxe", 1)
        character = db.get_character(user_id, -999)
        self.assertEqual(bot._equipped_profession_bonus(character, "mining"), 0)

        ring_id = db.create_item_instance(
            item_type="ring", name="Ring of the Deep Delver", rarity="rare", price=400,
            base_stats={"type": "ring"},
            affixes=[{"kind": "profession_bonus", "profession": "mining", "value": 3}],
        )

        async def gather():
            sink = []
            await bot._do_gather(FakeUpdate(user_id, "I mine for sulfur", sink), "I mine for sulfur", forced_roll=10)
            return "\n".join(sink)

        before = db.get_character(user_id, -999)["inventory"].get("sulfur_dust", 0)
        await gather()  # roll of 10, no bonus -> total 10 < DC 13, fails
        self.assertEqual(db.get_character(user_id, -999)["inventory"].get("sulfur_dust", 0), before)

        db.add_item(user_id, -999, ring_id, 1)
        success, _msg, _updated = db.equip_item(user_id, -999, ring_id)
        self.assertTrue(success)
        self.assertEqual(bot._equipped_profession_bonus(db.get_character(user_id, -999), "mining"), 3)

        before2 = db.get_character(user_id, -999)["inventory"].get("sulfur_dust", 0)
        await gather()  # same roll of 10, +3 from the ring -> total 13 == DC 13, succeeds
        self.assertGreater(db.get_character(user_id, -999)["inventory"].get("sulfur_dust", 0), before2)

    async def test_set_bonus_applies_at_threshold_and_drops_off_on_unequip(self):
        """
        Real Phase 5 deliverable of the magic item system (2026-08-02):
        a real 3-state sequence through the actual handlers -- equip one
        piece of a hand-authored named set (no bonus yet), equip the
        second (the 2-piece bonus activates, armor_class reflects it via
        the new db.recompute_armor_class), unequip one piece (the bonus
        drops off). Also the first real exercise of db.unequip_accessory/
        bot._do_unequip_item -- a genuinely new feature this game never
        had before (confirmed by grep: no unequip path existed at all).

        The test's OWN baseline is captured after the FIRST equip, not
        at character creation -- the fixture's armor_class default is a
        flat, dexterity-disconnected value, and the very first real
        equip event now correctly recomputes AC from scratch, which is
        a genuine correction, not something this test should treat as
        a false baseline.
        """
        from rules.item_sets import ITEM_SETS
        user_id = 950906
        make_basic_character(
            user_id, "SetTester2", current_location="crossroads_tavern",
            ability_scores={"strength": 10, "dexterity": 12, "constitution": 10,
                             "intelligence": 10, "wisdom": 10, "charisma": 10},
        )
        set_id = "emberwoven_vanguard"
        set_def = ITEM_SETS[set_id]
        expected_bonus = set_def["pieces"][2][0]["value"]

        ring1_id = db.create_item_instance(
            item_type="ring", name="Emberwoven Band", rarity="rare", price=300,
            base_stats={"type": "ring"}, affixes=[], set_id=set_id,
        )
        ring2_id = db.create_item_instance(
            item_type="amulet", name="Emberwoven Pendant", rarity="rare", price=300,
            base_stats={"type": "amulet"}, affixes=[], set_id=set_id,
        )

        db.add_item(user_id, -999, ring1_id, 1)
        success1, _msg1, _u1 = db.equip_item(user_id, -999, ring1_id)
        self.assertTrue(success1)
        ac_one_piece = db.get_character(user_id, -999)["armor_class"]

        db.add_item(user_id, -999, ring2_id, 1)
        success2, msg2, _u2 = db.equip_item(user_id, -999, ring2_id)
        self.assertTrue(success2)
        after_two = db.get_character(user_id, -999)
        self.assertEqual(after_two["armor_class"], ac_one_piece + expected_bonus)
        self.assertIn("AC is now", msg2)

        sink = []
        await bot._do_unequip_item(FakeUpdate(user_id, "take off my Emberwoven Band", sink), "take off my Emberwoven Band")
        after_unequip = db.get_character(user_id, -999)
        self.assertEqual(after_unequip["armor_class"], ac_one_piece)
        self.assertNotIn(ring1_id, after_unequip["equipped_accessories"])
        self.assertIn(ring2_id, after_unequip["equipped_accessories"])

    def test_mythic_ignore_resistance_deals_full_damage_through_a_resistance(self):
        """
        Real Phase 6 deliverable of the magic item system (2026-08-02):
        a mythic-exclusive ignore_resistance affix is a genuinely new
        mechanical effect, not a bigger number -- full damage against a
        resistant defender, where a legendary (or any non-mythic)
        attacker of the same base would be halved by the exact same
        rules.combat.apply_damage_type_modifier every other damage-type
        interaction in this game already uses, unchanged.
        """
        from rules.combat import apply_damage_type_modifier
        defender = {"resistances": ["fire"]}
        non_mythic_hit = apply_damage_type_modifier(20, "fire", defender, attacker={"rebirth_count": 0})
        self.assertEqual(non_mythic_hit, 10)
        mythic_hit = apply_damage_type_modifier(
            20, "fire", defender, attacker={"rebirth_count": 0, "ignores_resistance": True},
        )
        self.assertEqual(mythic_hit, 20)

    def test_mythic_equip_gated_behind_real_progression(self):
        """
        Real Phase 6 deliverable: db.equip_item refuses a mythic item on
        a zero-progression character and succeeds once a real threshold
        (any ONE of rebirth_count/echo_trial_tier/a specific completed
        quest) is met -- the reward-loop mechanic real progression
        unlocks, not pure drop luck alone.
        """
        from rules.item_generator import generate_weapon, MYTHIC_EQUIP_REQUIREMENT
        user_id = 950907
        make_basic_character(user_id, "MythicWielder2", current_location="crossroads_tavern")

        mythic_weapon = generate_weapon(tier="mythic")
        self.assertEqual(mythic_weapon["equip_requirement"], MYTHIC_EQUIP_REQUIREMENT)
        self.assertTrue(any(
            a["kind"] in ("ignore_resistance", "free_extra_attack") for a in mythic_weapon["affixes"]
        ))

        affixes = mythic_weapon.pop("affixes", [])
        item_id = db.create_item_instance(
            item_type=mythic_weapon["type"], name=mythic_weapon["name"], rarity=mythic_weapon["rarity"],
            price=mythic_weapon["price"], base_stats=mythic_weapon, affixes=affixes,
        )
        db.add_item(user_id, -999, item_id, 1)

        success_before, _msg, _u1 = db.equip_item(user_id, -999, item_id)
        self.assertFalse(success_before)
        self.assertNotEqual(db.get_character(user_id, -999).get("equipped_weapon"), item_id)

        db.update_character(user_id, -999, rebirth_count=1)
        success_after, _msg2, _u2 = db.equip_item(user_id, -999, item_id)
        self.assertTrue(success_after)
        self.assertEqual(db.get_character(user_id, -999).get("equipped_weapon"), item_id)

    def test_free_extra_attack_adds_exactly_one_attack_regardless_of_class(self):
        """Real Phase 6 deliverable: the free_extra_attack mythic affix is a flat +1, checked unconditionally."""
        character = make_basic_character(950908, "ExtraAttackTester", char_class="Wizard", current_location="crossroads_tavern")
        base_attacks = bot._attacks_per_turn(character)
        character["free_extra_attack"] = True
        self.assertEqual(bot._attacks_per_turn(character), base_attacks + 1)

    def test_forging_bumps_tier_and_stat_bonus_in_place_same_item_id(self):
        """
        Real Phase 7 deliverable of the magic item system (2026-08-02):
        forging bumps an EXISTING generated item's rarity and stat_bonus
        affix value up one real tier, same instance_id/item_id -- no
        migration, every existing inventory/equip reference stays valid.
        """
        from rules.item_generator import generate_weapon
        base_weapon = generate_weapon(tier="common")
        affixes = base_weapon.pop("affixes", [])
        item_id = db.create_item_instance(
            item_type=base_weapon["type"], name=base_weapon["name"], rarity=base_weapon["rarity"],
            price=base_weapon["price"], base_stats=base_weapon, affixes=affixes,
        )
        before = db.materialize_item_instance(item_id)
        self.assertEqual(before.get("damage_bonus", 0), 0)

        ok, _msg, forged = db.forge_item_instance(item_id)
        self.assertTrue(ok)
        self.assertEqual(forged["rarity"], "uncommon")
        self.assertEqual(forged.get("damage_bonus"), 1)
        self.assertEqual(forged["instance_id"], before["instance_id"])

        ok2, _msg2, forged2 = db.forge_item_instance(item_id)
        self.assertTrue(ok2)
        self.assertEqual(forged2["rarity"], "rare")
        self.assertEqual(forged2.get("damage_bonus"), 2)

        mythic_weapon = generate_weapon(tier="mythic")
        mythic_affixes = mythic_weapon.pop("affixes", [])
        mythic_item_id = db.create_item_instance(
            item_type=mythic_weapon["type"], name=mythic_weapon["name"], rarity=mythic_weapon["rarity"],
            price=mythic_weapon["price"], base_stats=mythic_weapon, affixes=mythic_affixes,
        )
        ok3, _msg3, forged3 = db.forge_item_instance(mythic_item_id)
        self.assertFalse(ok3)
        self.assertIsNone(forged3)

        ok4, _msg4, forged4 = db.forge_item_instance("longsword")
        self.assertFalse(ok4)
        self.assertIsNone(forged4)

    def test_enchanting_appends_a_new_affix_to_an_existing_forged_item(self):
        """
        Real Phase 7 deliverable: enchanting/imbuing appends ONE new
        affix (here, grants_spell) to an existing item's affix list --
        the same shared vocabulary db._apply_affix already understands,
        proving the item stays fully consistent (keeps its forge-bumped
        damage_bonus AND gains the new effect) with zero new cast-side
        code, since materialize_item_instance folds it on unchanged.
        """
        from rules.item_generator import generate_weapon
        base_weapon = generate_weapon(tier="common")
        affixes = base_weapon.pop("affixes", [])
        item_id = db.create_item_instance(
            item_type=base_weapon["type"], name=base_weapon["name"], rarity=base_weapon["rarity"],
            price=base_weapon["price"], base_stats=base_weapon, affixes=affixes,
        )
        db.forge_item_instance(item_id)

        ok, _msg, enchanted = db.enchant_item_instance(
            item_id, {"kind": "grants_spell", "spell_id": "magic_missile", "uses": 2},
        )
        self.assertTrue(ok)
        self.assertEqual(enchanted.get("damage_bonus"), 1)
        self.assertEqual(enchanted.get("grants_spell"), "magic_missile")
        self.assertEqual(enchanted.get("grants_spell_uses"), 2)

    def test_advanced_craft_recipe_rolls_a_real_generated_item_at_fixed_tier(self):
        """
        Real Phase 7 deliverable: an advanced recipe reuses
        rules/item_generator.py (the same roll combat loot already uses)
        to produce a REAL generated item at the recipe's fixed tier on
        success, instead of a flat catalog item -- and reports missing
        materials honestly without ever rolling, same convention as the
        static RECIPES above.
        """
        from rules.crafting import resolve_advanced_craft
        character = make_basic_character(950909, "AdvancedCraftTester", char_class="Fighter", current_location="crossroads_tavern")
        result = resolve_advanced_craft(character, "masterwork_longsword", practiced_bonus=0)
        self.assertEqual(result["outcome"], "missing_materials")

        db.add_item(950909, -999, "iron_ore", 6)
        db.add_item(950909, -999, "moonpetal", 1)
        character = db.get_character(950909, -999)
        succeeded = False
        for _ in range(30):
            result = resolve_advanced_craft(character, "masterwork_longsword", practiced_bonus=50)
            if result["outcome"] == "success":
                succeeded = True
                break
        self.assertTrue(succeeded)
        self.assertEqual(result["generated_item"]["type"], "weapon")
        self.assertEqual(result["generated_item"]["rarity"], "rare")

    def test_forge_and_enchant_keyword_fallback_classification(self):
        """Real Phase 7 deliverable: "forge my X" / "enchant my X" / "imbue the X" classify correctly without a model call."""
        self.assertEqual(_keyword_fallback("forge my longsword", [])["action"], "forge_item")
        self.assertEqual(_keyword_fallback("enchant my longsword with flame", [])["action"], "enchant_item")
        self.assertEqual(_keyword_fallback("imbue the shield with warding", [])["action"], "enchant_item")

    async def test_real_forge_handler_advances_a_carried_items_tier(self):
        """
        Real Phase 7 deliverable: bot._do_forge_item, run through the
        real dispatch/narration pipeline via a real FakeUpdate (not just
        the underlying db function), genuinely advances a carried
        generated item's tier and spends the real gold/material cost.
        """
        from rules.item_generator import generate_weapon
        user_id = 950910
        make_basic_character(
            user_id, "HandlerForgeTester2", char_class="Fighter", current_location="crossroads_tavern",
            ability_scores={"strength": 20, "dexterity": 10, "constitution": 14,
                             "intelligence": 10, "wisdom": 10, "charisma": 10},
            gold=10000,
        )
        weapon = generate_weapon(tier="common")
        affixes = weapon.pop("affixes", [])
        item_id = db.create_item_instance(
            item_type=weapon["type"], name=weapon["name"], rarity=weapon["rarity"],
            price=weapon["price"], base_stats=weapon, affixes=affixes,
        )
        db.add_item(user_id, -999, item_id, 1)
        db.add_item(user_id, -999, "iron_ore", 100)

        advanced = False
        for _ in range(15):
            sink = []
            await bot._do_forge_item(FakeUpdate(user_id, f"forge my {weapon['name']}", sink), f"forge my {weapon['name']}")
            if db.materialize_item_instance(item_id)["rarity"] != "common":
                advanced = True
                break
        self.assertTrue(advanced)

    def test_topic_routing_falls_back_to_home_group_constants_with_no_per_tenant_row(self):
        """
        Real multi-tenant scaling Phase 3 deliverable (2026-08-02, task
        #250): topics.py's is_adventure/is_support/is_development/
        is_main/thread_id_for all gained a required chat_id parameter,
        looking up db.chat_topic_config first -- but ANY chat with no
        row there (including every real message this bot's own live
        group has ever sent, which has never run /set_topic) falls back
        to the exact same config.py constants as before. This is the
        core backward-compat guarantee: the live group's behavior is
        byte-for-byte unchanged by this phase.
        """
        unregistered_chat_id = 424242
        self.assertTrue(topics.is_adventure(unregistered_chat_id, config.TOPIC_ADVENTURE_ID))
        self.assertTrue(topics.is_support(unregistered_chat_id, config.TOPIC_SUPPORT_ID))
        self.assertTrue(topics.is_development(unregistered_chat_id, config.TOPIC_DEVELOPMENT_ID))
        self.assertTrue(topics.is_main(unregistered_chat_id, None))
        self.assertTrue(topics.is_adventure(None, config.TOPIC_ADVENTURE_ID))
        self.assertEqual(topics.thread_id_for(unregistered_chat_id, "adventure"), config.TOPIC_ADVENTURE_ID)

    async def test_a_second_tenant_chat_routes_through_its_own_configured_topic_id(self):
        """
        Real multi-tenant scaling Phase 3 deliverable: a chat that has
        configured its OWN "adventure" thread_id (via db.set_chat_topic_id,
        what /set_topic does under the hood) via a thread_id completely
        different from this bot's real config.TOPIC_ADVENTURE_ID
        genuinely routes through bot._route_text_message to
        adventure_master_handler -- the actual proof multi-tenant
        routing works end-to-end, not just that the function signatures
        compile. The home group's own real Adventure thread_id stays
        correctly gated for its own (unregistered) chat_id throughout.
        """
        tenant_chat_id = -5551234
        tenant_adventure_thread_id = 999001
        db.set_chat_topic_id(tenant_chat_id, "adventure", tenant_adventure_thread_id)
        self.assertTrue(topics.is_adventure(tenant_chat_id, tenant_adventure_thread_id))
        self.assertEqual(topics.get_topic_name(tenant_chat_id, tenant_adventure_thread_id), "adventure")

        home_chat_id = 111111
        self.assertTrue(topics.is_adventure(home_chat_id, config.TOPIC_ADVENTURE_ID))

        user_id = 700001
        make_basic_character(user_id, "TenantPlayer", chat_id=tenant_chat_id, current_location="crossroads_tavern")
        sink = []
        update = FakeUpdate(user_id, "check my sheet", sink, thread_id=tenant_adventure_thread_id, chat_id=tenant_chat_id)
        await bot._route_text_message(update, DummyContext())
        transcript = "\n".join(sink)
        self.assertIn("TenantPlayer", transcript)

    async def test_set_topic_command_gates_and_persists_correctly(self):
        """
        Real multi-tenant scaling Phase 3 deliverable: /set_topic is
        gated on real Telegram admin/owner status (not the Dev-topic
        allowlist), refuses to ever configure a "development" topic for
        any chat other than this bot's own home group (per Coffee's hard
        instruction that the Dev bridge never extends to another group),
        and actually persists a real mapping on success.
        """
        # Non-admin refusal
        sink = []
        update = FakeUpdate(700002, "/set_topic adventure", sink, thread_id=555, chat_id=-5559999)
        context = DummyContext(bot=FakeBot(status="member"), args=["adventure"])
        await bot.set_topic_command(update, context)
        self.assertIn("Only a group admin", "\n".join(sink))
        self.assertIsNone(db.get_chat_topic_id(-5559999, "adventure"))

        # "development" refused for a non-home chat, even for a real owner
        sink2 = []
        non_home_chat = (config.TELEGRAM_CHAT_ID + 999999) if config.TELEGRAM_CHAT_ID else -777777
        update2 = FakeUpdate(700003, "/set_topic development", sink2, thread_id=777, chat_id=non_home_chat)
        context2 = DummyContext(bot=FakeBot(status="creator"), args=["development"])
        await bot.set_topic_command(update2, context2)
        self.assertIn("exclusive to this bot's own home group", "\n".join(sink2))
        self.assertIsNone(db.get_chat_topic_id(non_home_chat, "development"))

        # Real success for an actual Telegram admin
        sink3 = []
        new_tenant_chat = -5558888
        update3 = FakeUpdate(700004, "/set_topic support", sink3, thread_id=444, chat_id=new_tenant_chat, chat_title="Some Other Group")
        context3 = DummyContext(bot=FakeBot(status="administrator"), args=["support"])
        await bot.set_topic_command(update3, context3)
        self.assertIn("now set as", "\n".join(sink3))
        self.assertEqual(db.get_chat_topic_id(new_tenant_chat, "support"), 444)

    async def test_outbound_replies_route_to_each_chats_own_configured_topic(self):
        """
        Real multi-tenant scaling Phase 3 deliverable (2026-08-02): the
        outbound-reply sweep. ~340 bot.py call sites that used to send
        replies straight to config.py's hardcoded home-group thread ids
        now resolve per-chat via topics.thread_id_for -- this is the
        actual proof that sweep accomplished something real: a second
        tenant's own OUTBOUND reply lands in ITS configured thread_id,
        not the home group's, while the home group (no chat_topic_config
        row) keeps landing exactly where it always did. Also covers the
        Dev-topic admin gate, now topics.is_development(...) instead of
        a raw == comparison against config.TOPIC_DEVELOPMENT_ID -- still
        correctly refuses/accepts for the home group.
        """
        home_chat_id = 222222
        make_basic_character(800001, "HomePlayer", chat_id=home_chat_id, current_location="crossroads_tavern")
        sink = []
        update = FakeUpdate(800001, "check my sheet", sink, thread_id=config.TOPIC_ADVENTURE_ID, chat_id=home_chat_id)
        await bot._route_text_message(update, DummyContext())
        self.assertTrue(update.effective_chat.sent_thread_ids)
        self.assertTrue(all(tid == config.TOPIC_ADVENTURE_ID for tid in update.effective_chat.sent_thread_ids))

        tenant_chat_id = -8887777
        tenant_adventure_thread_id = 424999
        db.set_chat_topic_id(tenant_chat_id, "adventure", tenant_adventure_thread_id)
        make_basic_character(800002, "TenantPlayer2", chat_id=tenant_chat_id, current_location="crossroads_tavern")
        sink2 = []
        update2 = FakeUpdate(800002, "check my sheet", sink2, thread_id=tenant_adventure_thread_id, chat_id=tenant_chat_id)
        await bot._route_text_message(update2, DummyContext())
        self.assertTrue(update2.effective_chat.sent_thread_ids)
        self.assertTrue(all(tid == tenant_adventure_thread_id for tid in update2.effective_chat.sent_thread_ids))
        self.assertNotEqual(tenant_adventure_thread_id, config.TOPIC_ADVENTURE_ID)

        sink3 = []
        wrong_thread_update = FakeUpdate(700010, "/ban", sink3, thread_id=config.TOPIC_ADVENTURE_ID, chat_id=config.TELEGRAM_CHAT_ID or 111)
        await bot.ban_command(wrong_thread_update, DummyContext(bot=FakeBot(status="creator")))
        self.assertEqual(sink3, [])

        sink4 = []
        right_thread_update = FakeUpdate(700011, "/ban", sink4, thread_id=config.TOPIC_DEVELOPMENT_ID, chat_id=config.TELEGRAM_CHAT_ID or 111)
        await bot.ban_command(right_thread_update, DummyContext(bot=FakeBot(status="creator"), args=[]))
        self.assertTrue(len(sink4) > 0)

    def test_removed_combat_participants_dont_leak_in_user_session_forever(self):
        """
        Real live bug (2026-08-02, found via a Development-topic report:
        "Battle is not initiating" -- two real players sharing a party,
        Ravenloft and Laurienna, were permanently refused new combat
        with "Someone in your party is already in another fight right
        now" followed by "No combat is active right now," even though
        sessions_snapshot.json showed zero active sessions).

        Root cause: Session.remove_defeated() (drops a defeated AI
        participant from turn_order -- covers real AI party companions
        like Grask Emberscale/Wren Hollowbrook, not just monsters) and
        Session.remove_dead_player() (permanent player death, also
        reused by the flee-to-safety path) both mutated turn_order
        directly but never cleared the removed participant's entry in
        the module-level _USER_SESSION index that start_session() uses
        to refuse double-booking a participant into two fights at once.
        end_session()'s own cleanup only clears entries for participants
        STILL in turn_order, so anyone removed by either method stayed
        permanently "stuck," refusing every future fight naming them
        until the whole bot process restarted (confirmed live: a plain
        restart was the only thing that cleared the two real players'
        stuck state, since _USER_SESSION is pure in-memory and never
        persisted to sessions_snapshot.json).
        """
        import sessions

        def member(uid, name, hp=50, is_ai=True):
            return {"telegram_user_id": uid, "name": name, "hp_current": hp, "hp_max": hp,
                    "is_ai": is_ai, "strength": 12, "dexterity": 12, "armor_class": 12, "initiative": 10}

        chat_id = -999001
        companion = member(-960001, "TestCompanion", hp=0, is_ai=True)
        real_player = member(960002, "TestPlayer", hp=200, is_ai=False)
        enemy = member(-960003, "TestGoblin", hp=25, is_ai=True)
        sides = {companion["telegram_user_id"]: "party", real_player["telegram_user_id"]: "party",
                 enemy["telegram_user_id"]: "enemy"}
        session1 = sessions.start_session(chat_id, [companion, real_player, enemy], sides=sides)
        self.assertIsNotNone(session1)
        self.assertEqual(sessions._USER_SESSION.get(-960001), session1.session_id)

        removed = session1.remove_defeated()
        self.assertTrue(any(r["telegram_user_id"] == -960001 for r in removed))
        self.assertNotIn(-960001, sessions._USER_SESSION)
        sessions.end_session(chat_id, session1)

        # The real proof: a brand new fight naming that same companion must now succeed.
        party2 = [member(-960001, "TestCompanion", hp=106, is_ai=True), member(960002, "TestPlayer", hp=200, is_ai=False)]
        enemy2 = member(-960004, "TestGoblin2", hp=25, is_ai=True)
        sides2 = {p["telegram_user_id"]: "party" for p in party2}
        sides2[enemy2["telegram_user_id"]] = "enemy"
        session2 = sessions.start_session(chat_id, party2 + [enemy2], sides=sides2)
        self.assertIsNotNone(session2)
        sessions.end_session(chat_id, session2)

        # Same proof for remove_dead_player (permanent death / flee).
        dying_player = member(960005, "TestDyingPlayer", hp=0, is_ai=False)
        enemy3 = member(-960006, "TestGoblin3", hp=25, is_ai=True)
        sides3 = {dying_player["telegram_user_id"]: "party", enemy3["telegram_user_id"]: "enemy"}
        session3 = sessions.start_session(chat_id, [dying_player, enemy3], sides=sides3)
        self.assertIsNotNone(session3)
        self.assertEqual(sessions._USER_SESSION.get(960005), session3.session_id)
        session3.remove_dead_player(960005)
        self.assertNotIn(960005, sessions._USER_SESSION)
        sessions.end_session(chat_id, session3)

        session4 = sessions.start_session(
            chat_id, [member(960005, "TestDyingPlayer", hp=327, is_ai=False), member(-960007, "TestGoblin4", hp=25, is_ai=True)],
            sides={960005: "party", -960007: "enemy"},
        )
        self.assertIsNotNone(session4)

    async def test_in_battle_timeout_warns_then_forces_a_default_attack(self):
        """
        Real in-battle inactivity timeout (2026-08-02, per Coffee's own
        spec given live in the Development topic): a quiet player mid-
        combat gets warned at COMBAT_TIMEOUT_WARNING_SECONDS, then has
        their turn auto-resolved as a real ATTACK (default action =
        "Fight") at COMBAT_TIMEOUT_ACTION_SECONDS -- not just a passed
        turn. Escalates to a short 30s/60s window on any LATER turn once
        they've been auto-timed-out once this fight; real activity
        since their turn began resets that back to the normal window.
        Uses backdated session.turn_started_at timestamps instead of
        real sleeps to exercise this deterministically and fast.
        """
        import sessions

        class _FakeSendBot:
            def __init__(self):
                self.sent = []

            async def send_message(self, chat_id=None, message_thread_id=None, text=None, **kwargs):
                self.sent.append(text)
                return None

            async def send_photo(self, chat_id=None, message_thread_id=None, photo=None, caption=None, **kwargs):
                return None

            async def send_audio(self, chat_id=None, message_thread_id=None, audio=None, **kwargs):
                return None

        fake_bot = _FakeSendBot()
        player_id = 991001
        make_basic_character(player_id, "TimeoutTester", chat_id=-999500, current_location="crossroads_tavern")

        def new_fight(player_hp=None):
            character = db.get_character(player_id, -999500)
            if player_hp is not None:
                character["hp_current"] = player_hp
            companion = {"telegram_user_id": -700001, "name": "TestCompanion", "hp_current": 50, "hp_max": 50,
                         "is_ai": True, "strength": 12, "dexterity": 12, "armor_class": 12,
                         "damage_dice": "1d6", "damage_bonus": 0, "damage_type": "physical", "proficiency_bonus": 2}
            foe = {"telegram_user_id": -700002, "name": "TestDummy", "hp_current": 200, "hp_max": 200,
                   "is_ai": True, "strength": 10, "dexterity": 10, "armor_class": 8,
                   "damage_dice": "1d4", "damage_bonus": 0, "damage_type": "physical",
                   "proficiency_bonus": 2, "xp_reward": 10, "monster_key": "test_dummy"}
            sides = {player_id: "party", -700001: "party", -700002: "enemy"}
            session = sessions.start_session(-999500, [character, companion, foe], sides=sides)
            idx = session.turn_order.index(player_id)
            session.current_turn_index = idx
            session.turn_started_at[player_id] = time.time()
            return session

        # Warning threshold: warned, no forced action yet.
        session1 = new_fight()
        session1.turn_started_at[player_id] = time.time() - (bot.COMBAT_TIMEOUT_WARNING_SECONDS + 5)
        await bot._check_combat_timeouts(fake_bot)
        self.assertIn(player_id, session1.timeout_warned)
        self.assertEqual(session1.current_participant_id(), player_id)
        self.assertNotIn(player_id, session1.timeout_escalated)
        sessions.end_session(-999500, session1)

        # Action threshold: a real attack happens, player is escalated for next time.
        # Real flaky-test bug (2026-08-08, caught by the regression suite
        # itself): the original assertion here was "foe HP dropped OR the
        # current participant changed" -- but _do_attack chains into
        # _resolve_ai_turns, which auto-resolves every AI turn instantly;
        # with only one AI companion and one AI foe besides this test's
        # lone human player, that loop can wrap the turn index all the way
        # back around to the SAME player before this call returns, even
        # though the forced attack genuinely fired. Combined with a real,
        # unmocked dice roll that can miss (leaving HP unchanged too),
        # both halves of that OR could spuriously fail on an unlucky roll
        # despite the production code behaving correctly -- non-
        # deterministic despite this test's own "deterministic and fast"
        # docstring claim. Checking for the "attack on instinct" message
        # instead is deterministic: bot.py's _check_combat_timeouts sends
        # it unconditionally the moment the forced-attack branch fires,
        # before any dice are rolled or turns advance.
        session2 = new_fight()
        session2.turn_started_at[player_id] = time.time() - (bot.COMBAT_TIMEOUT_ACTION_SECONDS + 5)
        fake_bot.sent.clear()
        await bot._check_combat_timeouts(fake_bot)
        self.assertIn(player_id, session2.timeout_escalated)
        self.assertTrue(any("attack on instinct" in (m or "") for m in fake_bot.sent))
        sessions.end_session(-999500, session2)

        # Escalated: forced action fires after only the short 60s window.
        session3 = new_fight()
        session3.timeout_escalated.add(player_id)
        session3.turn_started_at[player_id] = time.time() - (bot.COMBAT_TIMEOUT_ESCALATED_ACTION_SECONDS + 5)
        fake_bot.sent.clear()
        await bot._check_combat_timeouts(fake_bot)
        self.assertTrue(any("attack on instinct" in (m or "") for m in fake_bot.sent))
        sessions.end_session(-999500, session3)

        # Real activity since the turn began resets escalation back to normal.
        session4 = new_fight()
        session4.timeout_escalated.add(player_id)
        session4.turn_started_at[player_id] = time.time() - (bot.COMBAT_TIMEOUT_ESCALATED_WARNING_SECONDS + 5)
        db.update_character(player_id, -999500, last_active_at=datetime.now(timezone.utc).isoformat())
        await bot._check_combat_timeouts(fake_bot)
        self.assertNotIn(player_id, session4.timeout_escalated)
        sessions.end_session(-999500, session4)

        # A downed player at the action threshold is pulled to safety, not force-attacked.
        session5 = new_fight(player_hp=0)
        session5.turn_started_at[player_id] = time.time() - (bot.COMBAT_TIMEOUT_ACTION_SECONDS + 5)
        await bot._check_combat_timeouts(fake_bot)
        after_char = db.get_character(player_id, -999500)
        self.assertEqual(after_char.get("is_inactive"), 1)
        self.assertEqual(after_char.get("current_location"), "crossroads_tavern")
        self.assertNotIn(player_id, session5.turn_order)
        self.assertNotIn(player_id, sessions._USER_SESSION)

    async def test_combat_timeout_check_skips_a_contended_session_instead_of_blocking_on_it(self):
        """
        Real live bug, found via dev-topic reports pointing in BOTH
        directions (2026-08-08): "it thinks that I timed out" despite a
        real action already in flight, and separately a turn sitting
        stuck well past its own escalated 60s threshold with no forced
        attack at all. Root cause: _check_combat_timeouts used to
        `await` every active session's lock in strict sequence -- a
        real player's own action holds THEIR session's lock for its
        whole duration (narration is offloaded to a thread via
        asyncio.to_thread, so it doesn't block the event loop, but the
        asyncio.Lock itself stays held the entire time regardless,
        commonly 30-160s+ under real Ollama latency). Under multi-
        tenant load, one session with a slow-resolving real action
        blocked this whole cycle from ever reaching any LATER session
        in the same iteration -- starving completely unrelated fights'
        timeout checks for as long as that one lock stayed held, cycle
        after cycle. Fixed by skipping (not awaiting) any session whose
        lock is already held -- a locked session means real activity is
        already in progress there, so there's nothing to force anyway.

        Verified deterministically: session A's lock is held by an
        asyncio.Event the test alone controls (never auto-released by a
        timer), so if this ever regressed back to blocking, this test
        would hang instead of silently passing.
        """
        import asyncio
        import sessions

        class _FakeSendBot:
            def __init__(self):
                self.sent = []

            async def send_message(self, chat_id=None, message_thread_id=None, text=None, **kwargs):
                self.sent.append(text)
                return None

            async def send_photo(self, chat_id=None, message_thread_id=None, photo=None, caption=None, **kwargs):
                return None

        fake_bot = _FakeSendBot()

        # Session A: deliberately contended -- its lock is held until
        # this test explicitly releases it.
        a_player_id = 991002
        make_basic_character(a_player_id, "ContendedTester", chat_id=-999501, current_location="crossroads_tavern")
        foe_a = {"telegram_user_id": -700003, "name": "DummyA", "hp_current": 200, "hp_max": 200,
                 "is_ai": True, "strength": 10, "dexterity": 10, "armor_class": 8,
                 "damage_dice": "1d4", "damage_bonus": 0, "damage_type": "physical", "proficiency_bonus": 2}
        session_a = sessions.start_session(-999501, [db.get_character(a_player_id, -999501), foe_a],
                                            sides={a_player_id: "party", -700003: "enemy"})
        session_a.turn_started_at[a_player_id] = time.time()

        # Session B: a completely unrelated fight, past its escalated
        # 60s action threshold, needs a forced attack.
        b_player_id = 991003
        make_basic_character(b_player_id, "StarvedTester", chat_id=-999502, current_location="crossroads_tavern")
        foe_b = {"telegram_user_id": -700004, "name": "DummyB", "hp_current": 200, "hp_max": 200,
                 "is_ai": True, "strength": 10, "dexterity": 10, "armor_class": 8,
                 "damage_dice": "1d4", "damage_bonus": 0, "damage_type": "physical", "proficiency_bonus": 2}
        session_b = sessions.start_session(-999502, [db.get_character(b_player_id, -999502), foe_b],
                                            sides={b_player_id: "party", -700004: "enemy"})
        session_b.timeout_escalated.add(b_player_id)
        session_b.turn_started_at[b_player_id] = time.time() - (bot.COMBAT_TIMEOUT_ESCALATED_ACTION_SECONDS + 5)

        release_a = asyncio.Event()

        async def hold_lock_a():
            async with sessions.get_session_lock(session_a.session_id):
                await release_a.wait()

        hold_task = asyncio.create_task(hold_lock_a())
        await asyncio.sleep(0.05)  # let it actually acquire the lock first
        self.assertTrue(sessions.get_session_lock(session_a.session_id).locked())

        # Session A's lock is ONLY ever released by this test (release_a.set()
        # below) -- a true regression back to blocking would hang forever,
        # not just run long, so a generous timeout still reliably catches
        # it without false-failing on session B's own legitimate real
        # narration time (documented 30-160s+ under real Ollama load).
        try:
            await asyncio.wait_for(bot._check_combat_timeouts(fake_bot), timeout=280)
        except asyncio.TimeoutError:
            self.fail(
                "_check_combat_timeouts blocked on session A's contended lock instead of skipping "
                "it, starving session B's own timeout check (this would hang forever on a real "
                "regression -- 280s is just a generous safety net, not an expected duration)"
            )

        # Session A's lock must still be held -- proves the check never
        # touched it at all (skipped outright, not raced-and-won).
        self.assertTrue(sessions.get_session_lock(session_a.session_id).locked())
        self.assertTrue(any("attack on instinct" in (m or "") for m in fake_bot.sent))

        release_a.set()
        await hold_task
        sessions.end_session(-999501, session_a)
        sessions.end_session(-999502, session_b)

    async def test_examine_a_generated_item_no_longer_crashes(self):
        """
        Real live crash (2026-08-02, caught via a Development-topic
        report): "Look at the runed dagger of embers" (a real generated
        magic item, equipped) crashed the whole handler with KeyError:
        'description' -- generated items (rules/item_generator.py) never
        set that field, only hand-authored items.py entries do. The
        player's own follow-up ("How can I see the stats... I have no
        idea how strong it is") also exposed a real gap: even once
        fixed, there was no way to see a magic item's actual affixes
        anywhere in the game -- _format_item_stats_line fixes that too.
        """
        from rules.item_generator import generate_weapon
        user_id = 993001
        make_basic_character(user_id, "ExamineTester", current_location="crossroads_tavern")
        weapon = generate_weapon(tier="rare")
        affixes = weapon.pop("affixes", [])
        item_id = db.create_item_instance(
            item_type=weapon["type"], name=weapon["name"], rarity=weapon["rarity"],
            price=weapon["price"], base_stats=weapon, affixes=affixes,
        )
        db.add_item(user_id, -999, item_id, 1)
        materialized = db.materialize_item_instance(item_id)
        self.assertNotIn("description", materialized)

        sink = []
        await bot._do_examine(FakeUpdate(user_id, f"look at the {materialized['name']}", sink), f"look at the {materialized['name']}")
        transcript = "\n".join(sink)
        self.assertTrue(len(transcript) > 0)
        self.assertIn("Stats:", transcript)

    async def test_generated_items_can_be_sold_and_market_shows_real_stats(self):
        """
        Real live bugs (2026-08-02, Development topic): (1)
        items.is_sellable used a direct ITEMS.get(...) lookup instead of
        get_item()'s fallback, so shop.sell_item unconditionally
        rejected EVERY generated magic item as unsellable -- an earlier
        investigation wrongly concluded this function was dead code; it
        was actually reachable via bot._do_sell the whole time. (2) "the
        generated item shud show ... full list of stats ... this for the
        market ... so players kno when to use the items and what they
        can sell for" -- the player market listing view showed a bare
        item name with no stats at all.
        """
        from rules.item_generator import generate_weapon
        import shop as shop_module

        weapon = generate_weapon(tier="rare")
        affixes = weapon.pop("affixes", [])
        item_id = db.create_item_instance(
            item_type=weapon["type"], name=weapon["name"], rarity=weapon["rarity"],
            price=weapon["price"], base_stats=weapon, affixes=affixes,
        )
        self.assertTrue(items_module.is_sellable(item_id))

        seller_id = 994001
        make_basic_character(seller_id, "SellTester", current_location="crossroads_tavern", gold=0)
        db.add_item(seller_id, -999, item_id, 1)
        ok, _msg = shop_module.sell_item(seller_id, -999, item_id, 1)
        self.assertTrue(ok)
        after_sell = db.get_character(seller_id, -999)
        self.assertGreater(after_sell["gold"], 0)
        self.assertEqual(after_sell["inventory"].get(item_id, 0), 0)

        lister_id = 994002
        make_basic_character(lister_id, "MarketLister", current_location="crossroads_tavern")
        weapon2 = generate_weapon(tier="very_rare")
        affixes2 = weapon2.pop("affixes", [])
        item_id2 = db.create_item_instance(
            item_type=weapon2["type"], name=weapon2["name"], rarity=weapon2["rarity"],
            price=weapon2["price"], base_stats=weapon2, affixes=affixes2,
        )
        db.add_item(lister_id, -999, item_id2, 1)

        sink1 = []
        await bot._do_sell_market(
            FakeUpdate(lister_id, f"/sell_market 1 500 {weapon2['name']}", sink1),
            ["1", "500", weapon2["name"]],
        )
        self.assertIn("Stats:", "\n".join(sink1))

        sink2 = []
        await bot._do_check_market(FakeUpdate(994003, "check market", sink2))
        self.assertIn("📊", "\n".join(sink2))

        buyer_id = 994004
        make_basic_character(buyer_id, "MarketBuyer", current_location="crossroads_tavern", gold=10000)
        listings = db.get_market_listings(-999)
        listing_id = next(l["listing_id"] for l in listings if l["item_id"] == item_id2)
        sink3 = []
        update3 = FakeUpdate(buyer_id, f"/buy_market {listing_id}", sink3)
        await bot._do_buy_market(update3, [str(listing_id)])
        self.assertIn("Stats:", "\n".join(sink3))
        self.assertTrue(len(update3.effective_chat.sent_photos) > 0)

    async def test_reimage_command_regenerates_a_real_tracked_image(self):
        """
        Real live request (2026-08-03, Coffee, Development topic): "if i
        dont like an image generated or used i can reply with /reimage
        and it will make a new image based on its description." Every
        image-sending helper now routes through the one shared
        _send_generated_image choke point, which tracks (chat_id,
        message_id) -> prompt in _SENT_IMAGE_PROMPTS; reimage_command
        looks a reply's real message_id up there and regenerates with
        the same prompt but a fresh random seed.
        """
        from rules.item_generator import generate_weapon
        user_id = 995001
        make_basic_character(user_id, "ReimageTester", current_location="crossroads_tavern")
        weapon = generate_weapon(tier="rare")
        affixes = weapon.pop("affixes", [])
        item_id = db.create_item_instance(
            item_type=weapon["type"], name=weapon["name"], rarity=weapon["rarity"],
            price=weapon["price"], base_stats=weapon, affixes=affixes,
        )
        materialized = db.materialize_item_instance(item_id)
        sink = []
        update = FakeUpdate(user_id, "check my sheet", sink)
        await bot._maybe_send_item_image(update, item_id, materialized)
        first_photo_msg = update.effective_chat.last_sent_message
        self.assertIsNotNone(first_photo_msg.message_id)

        sink_no_reply = []
        no_reply_update = FakeUpdate(user_id, "/reimage", sink_no_reply)
        await bot.reimage_command(no_reply_update, DummyContext())
        self.assertIn("Reply to a real image", "\n".join(sink_no_reply))

        sink_untracked = []
        fake_untracked = type("FakeUntrackedMessage", (), {"message_id": 999999})()
        untracked_update = FakeUpdate(user_id, "/reimage", sink_untracked, reply_to_message=fake_untracked)
        await bot.reimage_command(untracked_update, DummyContext())
        self.assertIn("don't have a record", "\n".join(sink_untracked))

        sink_real = []
        real_reply_update = FakeUpdate(user_id, "/reimage", sink_real, reply_to_message=first_photo_msg)
        real_reply_update.effective_chat = update.effective_chat
        await bot.reimage_command(real_reply_update, DummyContext())
        self.assertEqual(len(update.effective_chat.sent_photos), 2)

    async def test_looted_item_gets_a_view_button_with_working_actions(self):
        """
        Real live requests (2026-08-03, Coffee, Development topic):
        "when we loot the item put a button so we can click and view the
        item" and "when viewing the item put a button to sell, put
        market equip, or give." Proves the whole chain end-to-end: a
        real combat loot grant sends a real View Item button, tapping it
        shows the item's real stats+image with action buttons attached,
        and tapping Equip actually equips the real item.
        """
        make_basic_character(996001, "LootWinner", current_location="whispering_wood")
        sink = []
        loot_update = FakeUpdate(996001, "combat victory", sink)
        loot_line = await bot._grant_generated_loot(loot_update, [996001])
        self.assertIn("loots a", loot_line)
        self.assertTrue(len(loot_update.effective_chat.sent_photos) == 0)  # the loot line has no photo of its own
        self.assertTrue(len(loot_update.effective_chat._sink) > 0)

        winner_char = db.get_character(996001, -999)
        generated_ids = [k for k in winner_char["inventory"] if k.startswith("gi")]
        self.assertTrue(len(generated_ids) > 0)
        item_id = generated_ids[0]

        sink_show = []
        show_update = FakeCallbackUpdate(996001, f"itemview|show|{item_id}", sink_show)
        await bot.itemview_callback(show_update, DummyContext())
        self.assertTrue(len(show_update.effective_chat.sent_photos) > 0)
        self.assertIsNotNone(show_update.effective_chat.sent_photos[-1]["reply_markup"])

        sink_equip = []
        equip_update = FakeCallbackUpdate(996001, f"itemview|equip|{item_id}", sink_equip)
        await bot.itemview_callback(equip_update, DummyContext())
        after_equip = db.get_character(996001, -999)
        self.assertTrue(after_equip.get("equipped_weapon") == item_id or after_equip.get("equipped_armor") == item_id)

    def test_equipable_worth_shown_in_stats_line(self):
        """Real live request (2026-08-03): "in the description of the items can u show what it is worth? do this for equipables"."""
        from rules.item_generator import generate_weapon
        weapon = generate_weapon(tier="rare")
        affixes = weapon.pop("affixes", [])
        item_id = db.create_item_instance(
            item_type=weapon["type"], name=weapon["name"], rarity=weapon["rarity"],
            price=weapon["price"], base_stats=weapon, affixes=affixes,
        )
        item = db.materialize_item_instance(item_id)
        stats_line = bot._format_item_stats_line(item)
        self.assertIsNotNone(stats_line)
        self.assertIn("worth", stats_line)
        self.assertIn("g", stats_line)

    def test_generated_item_notes_reflect_real_affixes(self):
        """
        Real live request (2026-08-03): "make sure the generated items
        have proper indetail descriptions so the image generation works
        properly" -- the note field (which the image prompt reads) used
        to be the exact same tier-only sentence for every item
        regardless of what actually got rolled.
        """
        from rules.item_generator import generate_weapon
        fire_weapon = None
        for _ in range(60):
            w = generate_weapon(tier="rare")
            if any(a.get("kind") == "elemental_damage" for a in w["affixes"]):
                fire_weapon = w
                break
        self.assertIsNotNone(fire_weapon)
        dmg_type = next(a["damage_type"] for a in fire_weapon["affixes"] if a["kind"] == "elemental_damage")
        self.assertIn(dmg_type, fire_weapon["note"])

        plain_weapon = generate_weapon(tier="common")
        self.assertTrue(plain_weapon["note"].startswith("A common find"))

    async def test_reimage_preserves_item_view_action_buttons(self):
        """
        Real gap caught on self-review right after shipping v1.27.82
        (2026-08-03): /reimage-ing an item-view screen (image +
        Equip/Sell/Market/Give buttons) regenerated a plain photo with
        NO buttons at all, since _send_generated_image's tracking dict
        never stored the original reply_markup alongside the prompt.
        Fixed by tracking reply_markup too.
        """
        from rules.item_generator import generate_weapon
        user_id = 998001
        make_basic_character(user_id, "ReimageButtonTester", current_location="crossroads_tavern")
        weapon = generate_weapon(tier="rare")
        affixes = weapon.pop("affixes", [])
        item_id = db.create_item_instance(
            item_type=weapon["type"], name=weapon["name"], rarity=weapon["rarity"],
            price=weapon["price"], base_stats=weapon, affixes=affixes,
        )
        db.add_item(user_id, -999, item_id, 1)

        sink_show = []
        show_update = FakeCallbackUpdate(user_id, f"itemview|show|{item_id}", sink_show)
        await bot.itemview_callback(show_update, DummyContext())
        first_photo = show_update.effective_chat.sent_photos[-1]
        first_msg = show_update.effective_chat.last_sent_message
        self.assertIsNotNone(first_photo["reply_markup"])

        sink_reimage = []
        reimage_update = FakeUpdate(user_id, "/reimage", sink_reimage, reply_to_message=first_msg)
        reimage_update.effective_chat = show_update.effective_chat
        await bot.reimage_command(reimage_update, DummyContext())
        second_photo = show_update.effective_chat.sent_photos[-1]
        self.assertIsNotNone(second_photo["reply_markup"])

    async def test_ai_companion_actually_fighting_gets_real_combat_xp(self):
        """
        Real live bug (2026-08-01, Coffee: "They didn't get experience
        for being in battle" -- Wren Hollowbrook, a real, present,
        actively-fighting AI companion, had 0 XP after a genuine combat
        win). _award_victory_xp used to exclude EVERY is_ai participant
        from combat XP outright, even ones actually in session.turn_order
        fighting alongside a human -- contradicting
        _share_quest_rewards_with_party's own correct "AI companions
        included" design. Distinct from the test above: THIS companion
        is a real combat PARTICIPANT (in turn_order), not merely a party
        member sitting out this fight.
        """
        import sessions
        sessions.end_session(-999)
        leader_id = 900490
        make_basic_character(leader_id, "XpLeader", current_location="crossroads_tavern")
        party_id = db.create_party(leader_id, -999)
        companion = db.create_ai_companion(
            -999, "XpBuddy", "Elf", "Ranger",
            ability_scores={"strength": 12, "dexterity": 17, "constitution": 13,
                             "intelligence": 11, "wisdom": 15, "charisma": 10},
            hp_max=30, armor_class=14, gold=0, inventory={},
        )
        db.add_ai_companion_to_party(companion["telegram_user_id"], -999, party_id)
        companion_id = companion["telegram_user_id"]

        enemy_id = -2_500_060
        enemy = {"telegram_user_id": enemy_id, "name": "XpGoblin", "dexterity": 10, "xp_reward": 90}
        leader = db.get_character(leader_id, -999)
        leader["telegram_user_id"] = leader_id
        companion_p = db.get_character(companion_id, -999)
        companion_p["telegram_user_id"] = companion_id
        session = sessions.start_session(
            -999, [leader, companion_p, enemy],
            {leader_id: "party", companion_id: "party", enemy_id: "enemy"},
        )
        session.turn_order = [leader_id, companion_id, enemy_id]

        leader_xp_before = db.get_character(leader_id, -999)["xp"]
        companion_xp_before = db.get_character(companion_id, -999)["xp"]
        await bot._award_victory_xp(FakeUpdate(leader_id, "", []), session)
        leader_xp_after = db.get_character(leader_id, -999)["xp"]
        companion_xp_after = db.get_character(companion_id, -999)["xp"]

        self.assertGreater(leader_xp_after, leader_xp_before)
        self.assertGreater(companion_xp_after, companion_xp_before)
        self.assertEqual(leader_xp_after - leader_xp_before, companion_xp_after - companion_xp_before)
        sessions.end_session(-999)

    async def test_multi_fight_sessions_do_not_cross_contaminate(self):
        """
        The real Phase 1 deliverable (2026-08-01 multi-fight rewrite, per
        Coffee: "I want this built so it works for 1000s of users" --
        supporting more than one simultaneous fight was the first
        concrete piece). Before this rewrite, sessions.py kept exactly
        ONE Session per chat_id, unconditionally overwritten by
        start_session -- so two different parties fighting at the same
        time in the same chat was flatly impossible; starting a second
        fight silently clobbered the first one's turn order and combat
        log outright. Confirms two independent session_ids can now
        coexist in the same chat with zero shared state, and that a
        player already in one fight can't be double-drafted into a
        second one.
        """
        import sessions
        for s in list(sessions.get_sessions_in_chat(-999)):
            sessions.end_session(-999, s)

        player_a, player_b = 900500, 900501
        char_a = make_basic_character(player_a, "MultiFightA", current_location="crossroads_tavern")
        char_b = make_basic_character(player_b, "MultiFightB", current_location="crossroads_tavern")
        enemy_a = {"telegram_user_id": -2_500_070, "name": "GoblinA", "dexterity": 10}
        enemy_b = {"telegram_user_id": -2_500_071, "name": "GoblinB", "dexterity": 10}

        session_a = sessions.start_session(
            -999, [char_a, enemy_a], {player_a: "party", enemy_a["telegram_user_id"]: "enemy"},
        )
        session_b = sessions.start_session(
            -999, [char_b, enemy_b], {player_b: "party", enemy_b["telegram_user_id"]: "enemy"},
        )

        self.assertIsNotNone(session_a)
        self.assertIsNotNone(session_b)
        self.assertNotEqual(session_a.session_id, session_b.session_id)
        self.assertEqual(len(sessions.get_sessions_in_chat(-999)), 2)
        self.assertIs(sessions.get_session_for_user(-999, player_a), session_a)
        self.assertIs(sessions.get_session_for_user(-999, player_b), session_b)

        # A player already fighting can't be drafted into a second fight --
        # start_session must reject (return None), not silently clobber
        # the fight they're already in.
        dup_enemy_id = -2_500_072
        dup = sessions.start_session(
            -999, [char_a, {"telegram_user_id": dup_enemy_id, "name": "GoblinC", "dexterity": 10}],
            {player_a: "party", dup_enemy_id: "enemy"},
        )
        self.assertIsNone(dup)
        self.assertIs(sessions.get_session_for_user(-999, player_a), session_a)
        self.assertEqual(len(sessions.get_sessions_in_chat(-999)), 2)  # still just the original two

        sessions.end_session(-999, session_a)
        sessions.end_session(-999, session_b)
        self.assertEqual(len(sessions.get_sessions_in_chat(-999)), 0)

    # -- Party bench + active-combat cap (2026-07-31, per Coffee: "cap it
    #    at 6 per fight, all members in party get experience and gold
    #    tho") -----------------------------------------------------------
    async def test_active_combat_roster_caps_at_six_leader_always_included(self):
        leader_id = 950001
        make_basic_character(leader_id, "BenchLeader", current_location="crossroads_tavern")
        party_id = db.create_party(leader_id, -999)
        for i, name in enumerate(["A1", "A2", "A3", "A4", "A5", "A6"]):
            uid = 950002 + i
            make_basic_character(uid, name, current_location="crossroads_tavern")
            db.update_character(uid, -999, party_id=party_id)

        leader = db.get_character(leader_id, -999)
        combatants = bot._get_real_party_combatants(leader)
        self.assertEqual(len(combatants), bot.config.PARTY_ACTIVE_COMBAT_CAP)
        self.assertTrue(any(c["telegram_user_id"] == leader_id for c in combatants))

    async def test_benched_member_excluded_from_combat_but_stays_in_party(self):
        leader_id = 950101
        member_id = 950102
        make_basic_character(leader_id, "BenchLeader2", current_location="crossroads_tavern")
        make_basic_character(member_id, "Benchee", current_location="crossroads_tavern")
        party_id = db.create_party(leader_id, -999)
        db.update_character(member_id, -999, party_id=party_id)

        await bot._do_bench_member(FakeUpdate(leader_id, "bench Benchee", []), "Benchee")
        benched = db.get_character(member_id, -999)
        self.assertTrue(benched.get("is_benched"))
        self.assertEqual(benched.get("party_id"), party_id)  # still a real party member

        combatants = bot._get_real_party_combatants(db.get_character(leader_id, -999))
        self.assertNotIn(member_id, {c["telegram_user_id"] for c in combatants})

    async def test_unbench_refused_when_active_roster_already_full(self):
        leader_id = 950201
        make_basic_character(leader_id, "BenchLeader3", current_location="crossroads_tavern")
        party_id = db.create_party(leader_id, -999)
        names = ["B1", "B2", "B3", "B4", "B5", "Extra"]
        ids = {}
        for i, name in enumerate(names):
            uid = 950202 + i
            ids[name] = uid
            make_basic_character(uid, name, current_location="crossroads_tavern")
            db.update_character(uid, -999, party_id=party_id)
        db.update_character(ids["Extra"], -999, is_benched=1)  # 6 already active (leader + B1-B5)

        sink = []
        await bot._do_unbench_member(FakeUpdate(leader_id, "unbench Extra", sink), "Extra")
        reply = "\n".join(sink)
        self.assertIn("already full", reply.lower())
        self.assertTrue(db.get_character(ids["Extra"], -999).get("is_benched"))

    async def test_party_max_members_allows_all_six_recruitable_companions_plus_the_player(self):
        """
        Real live report (2026-08-09, Coffee): "i cant invite more than
        6 players to the party... we shud be able to have all the
        characters but then use party to pick with ones we want. RN its
        not letting me invite anymore of the AI Characters." There are
        exactly 6 recruitable AI companions in campaign.json -- with the
        human player themselves also counting toward PARTY_MAX_MEMBERS
        (db.get_party_size counts every characters row with the
        party_id, not just AI ones), the old cap of 6 meant a solo
        player could only ever have 5 of the 6 along at once. Confirms
        the raised roster cap (config.PARTY_MAX_MEMBERS) actually lets a
        real player recruit all 6 through the real _do_recruit_npc
        handler, with none of them hitting "already full" -- separate
        from PARTY_ACTIVE_COMBAT_CAP (still 6), which is the real
        bench/unbench "pick who fights" system Coffee is describing.
        """
        player_id = 900530
        make_basic_character(player_id, "CollectorPlayer", current_location="crossroads_tavern")
        companions = ["Sarah", "Borin Ironjaw", "Wren Hollowbrook", "Pip Thistledown",
                      "Grask Emberscale", "Vesh Nightglass"]
        for name in companions:
            sink = []
            await bot._do_recruit_npc(FakeUpdate(player_id, f"recruit {name}", sink), name)
            reply = "\n".join(sink)
            self.assertNotIn("already full", reply.lower(), f"{name} failed to join: {reply}")
            self.assertIn("joins your party", reply.lower(), f"{name} didn't actually join: {reply}")

        player = db.get_character(player_id, -999)
        self.assertEqual(db.get_party_size(player["party_id"]), 7)  # player + all 6 companions

    def test_accept_party_invite_full_message_reflects_the_real_configured_cap(self):
        """
        db.accept_party_invite's rejection message used to hardcode the
        literal string "(6 members)" regardless of the real configured
        PARTY_MAX_MEMBERS -- would have silently gone stale and wrong
        the moment the cap above was raised. Confirms it reports the
        real live configured value instead.
        """
        leader_id = 900540
        make_basic_character(leader_id, "CapLeader", current_location="crossroads_tavern")
        party_id = db.create_party(leader_id, -999)
        for i in range(db.PARTY_MAX_MEMBERS - 1):
            uid = 900541 + i
            make_basic_character(uid, f"CapFill{i}", current_location="crossroads_tavern")
            db.update_character(uid, -999, party_id=party_id)
        self.assertEqual(db.get_party_size(party_id), db.PARTY_MAX_MEMBERS)

        latecomer_id = 900541 + db.PARTY_MAX_MEMBERS
        make_basic_character(latecomer_id, "Latecomer", current_location="crossroads_tavern")
        db.set_pending_party_invite(latecomer_id, -999, party_id)
        ok, message = db.accept_party_invite(latecomer_id, -999)
        self.assertFalse(ok)
        self.assertIn(str(db.PARTY_MAX_MEMBERS), message)

    async def test_benched_member_still_gets_xp_and_gold_share(self):
        import sessions
        sessions.end_session(-999)
        leader_id = 950301
        benched_id = 950302
        make_basic_character(leader_id, "BenchLeader4", current_location="crossroads_tavern")
        make_basic_character(benched_id, "BenchedEarner", current_location="crossroads_tavern")
        party_id = db.create_party(leader_id, -999)
        db.update_character(benched_id, -999, party_id=party_id, is_benched=1)

        enemy_id = -2_600_101
        enemy = {"telegram_user_id": enemy_id, "name": "Goblin", "dexterity": 10, "xp_reward": 100}
        leader = db.get_character(leader_id, -999)
        leader["telegram_user_id"] = leader_id
        session = sessions.start_session(-999, [leader, enemy], {enemy_id: "enemy", leader_id: "party"})
        session.turn_order = [leader_id, enemy_id]

        before = db.get_character(benched_id, -999)["xp"]
        await bot._award_victory_xp(FakeUpdate(leader_id, "", []), session)
        after = db.get_character(benched_id, -999)["xp"]
        self.assertEqual(after - before, int(100 * bot.INACTIVE_PARTY_XP_SHARE))
        sessions.end_session(-999)

    # -- Battle formations (2026-08-01, per Coffee: "character placement
    #    has an effect in battle... front row get targeted... back row
    #    have a higher evade%... allow us to customize the formations...
    #    enemies use battle formations too") -----------------------------
    def test_back_row_gets_the_configured_ac_bonus_in_resolve_attack(self):
        import rules.combat as combat
        front = {"armor_class": 15, "formation_row": "front"}
        back = {"armor_class": 15, "formation_row": "back"}
        self.assertEqual(combat.formation_ac_bonus(front), 0)
        self.assertEqual(combat.formation_ac_bonus(back), bot.config.BACK_ROW_AC_BONUS)
        self.assertGreater(bot.config.BACK_ROW_AC_BONUS, 0)

    def test_formation_targeting_prefers_front_row_but_can_still_hit_back(self):
        import random
        front_alive = {"telegram_user_id": 1, "name": "Tank", "hp_current": 50, "formation_row": "front"}
        back_alive = {"telegram_user_id": 2, "name": "Mage", "hp_current": 50, "formation_row": "back"}
        random.seed(7)
        picks = [bot._pick_formation_weighted_target([front_alive, back_alive])["name"] for _ in range(300)]
        front_share = picks.count("Tank") / len(picks)
        self.assertAlmostEqual(front_share, bot.config.FRONT_ROW_TARGET_CHANCE, delta=0.1)
        self.assertGreater(picks.count("Mage"), 0)  # still targetable, never immune

    def test_formation_targeting_falls_back_to_back_row_when_front_wiped(self):
        back_alive = {"telegram_user_id": 2, "name": "Mage", "hp_current": 50, "formation_row": "back"}
        target = bot._pick_formation_weighted_target([back_alive])
        self.assertEqual(target["name"], "Mage")

    def test_formation_targeting_no_longer_locks_onto_the_single_lowest_hp_member(self):
        """
        Real live bug (2026-08-10, Coffee, dev-bridge screenshot: "Is
        there a reason why bram is the only one getting targeted? Is
        that a glitch is there agro im not aware of"). Root cause: the
        within-row pick used to be a hard `min(pool, key=hp_current)`
        -- not a tie-break, the entire rule -- so the moment one member
        dropped even slightly below their row-mates' HP, every future
        attack against that row locked onto them again forever, while
        full-HP row-mates could never be picked. Confirmed live: Bram
        at 3/325 HP, Wren at a completely untouched 507/507. This
        reproduces that exact shape (one member heavily damaged, three
        others at/near full HP, all same row so row-selection logic
        never enters into it) over many real trials and asserts EVERY
        member gets picked at least once -- the previous code would
        have picked the damaged member 100% of the time, every trial.
        """
        import random
        random.seed(11)
        pool = [
            {"telegram_user_id": 1, "name": "Bram", "hp_current": 3, "hp_max": 325, "formation_row": "back"},
            {"telegram_user_id": 2, "name": "Charvenna", "hp_current": 253, "hp_max": 290, "formation_row": "back"},
            {"telegram_user_id": 3, "name": "Vesh", "hp_current": 282, "hp_max": 282, "formation_row": "back"},
            {"telegram_user_id": 4, "name": "Pan", "hp_current": 159, "hp_max": 159, "formation_row": "back"},
        ]
        picks = [bot._pick_formation_weighted_target(pool)["name"] for _ in range(2000)]
        picked_names = set(picks)
        self.assertEqual(
            picked_names, {"Bram", "Charvenna", "Vesh", "Pan"},
            f"at least one party member was never targeted across 2000 trials: only {picked_names} were picked",
        )
        # Still real, tactical weighting -- the badly wounded member
        # should be picked noticeably more often than a full-HP one,
        # just never exclusively.
        self.assertGreater(picks.count("Bram"), picks.count("Vesh"))

    def test_enemy_formation_row_heuristic_grounded_in_real_monster_data(self):
        self.assertEqual(bot._enemy_formation_row("goblin", {"name": "Goblin"}), "front")
        self.assertEqual(bot._enemy_formation_row("goblin_shaman", {"name": "Goblin Shaman"}), "back")
        # Explicit content-authored field always wins over the heuristic.
        self.assertEqual(
            bot._enemy_formation_row("goblin", {"name": "Goblin", "formation_row": "back"}), "back",
        )

    async def test_set_formation_row_persists_and_defaults_to_self(self):
        uid = 950401
        make_basic_character(uid, "FormationSolo", current_location="crossroads_tavern")
        sink = []
        await bot._do_set_formation_row(FakeUpdate(uid, "move me to the back", sink), "", "back")
        self.assertEqual(db.get_character(uid, -999).get("formation_row"), "back")
        self.assertIn("back row", "\n".join(sink).lower())

    async def test_set_formation_row_targets_named_party_member_not_requester(self):
        leader_id = 950402
        member_id = 950403
        make_basic_character(leader_id, "FormationLeader", current_location="crossroads_tavern")
        make_basic_character(member_id, "FormationMember", current_location="crossroads_tavern")
        party_id = db.create_party(leader_id, -999)
        db.update_character(member_id, -999, party_id=party_id)

        await bot._do_set_formation_row(
            FakeUpdate(leader_id, "move FormationMember to the back row", []), "FormationMember", "back",
        )
        self.assertEqual(db.get_character(member_id, -999).get("formation_row"), "back")
        self.assertEqual(db.get_character(leader_id, -999).get("formation_row", "front"), "front")

    async def test_set_formation_row_matches_a_party_member_by_first_name_alone(self):
        """
        Real live bug (2026-08-09, Coffee, dev-topic screenshot): "Move
        Vesh to the backrow" said 'No one named "Vesh" is in your
        party' even though Vesh Nightglass was plainly fighting in the
        very same battle status line shown moments earlier --
        _match_member_by_name_or_username only ever matched a member's
        COMPLETE stored name verbatim, with no fallback for the
        entirely natural "refer to them by first name" phrasing. Two-
        word display name here mirrors the real recruitable companions
        (e.g. "Vesh Nightglass").
        """
        leader_id = 950406
        member_id = 950407
        make_basic_character(leader_id, "FirstNameLeader", current_location="crossroads_tavern")
        make_basic_character(member_id, "Vesh Nightglass", current_location="crossroads_tavern")
        party_id = db.create_party(leader_id, -999)
        db.update_character(member_id, -999, party_id=party_id)

        sink = []
        await bot._do_set_formation_row(FakeUpdate(leader_id, "move vesh to the backrow", sink), "vesh", "back")
        self.assertNotIn("no one named", "\n".join(sink).lower(), "\n".join(sink))
        self.assertEqual(db.get_character(member_id, -999).get("formation_row"), "back")

    def test_first_name_fallback_never_guesses_between_two_shared_first_names(self):
        """
        Sibling to the first-name fix above: two party members sharing
        a first name must stay unmatched rather than guessed -- same
        "don't guess when ambiguous" philosophy as every other fallback
        in this codebase.
        """
        members = [
            {"name": "Vesh Nightglass", "telegram_user_id": -1, "telegram_username": None, "party_id": 1},
            {"name": "Vesh Ironhand", "telegram_user_id": -2, "telegram_username": None, "party_id": 1},
        ]
        self.assertIsNone(bot._match_member_by_name_or_username("vesh", members))
        # The full name is still unambiguous even with a shared first name.
        self.assertEqual(bot._match_member_by_name_or_username("vesh ironhand", members)["telegram_user_id"], -2)

    async def test_set_formation_row_refuses_a_non_party_member(self):
        leader_id = 950404
        stranger_id = 950405
        make_basic_character(leader_id, "FormationLeader2", current_location="crossroads_tavern")
        make_basic_character(stranger_id, "Stranger", current_location="crossroads_tavern")
        db.create_party(leader_id, -999)  # stranger is deliberately NOT added

        sink = []
        await bot._do_set_formation_row(
            FakeUpdate(leader_id, "move Stranger to the back row", sink), "Stranger", "back",
        )
        self.assertIn("no one named", "\n".join(sink).lower())
        self.assertEqual(db.get_character(stranger_id, -999).get("formation_row", "front"), "front")

    # -- Real-time mid-fight formation switching (2026-08-01, per Coffee:
    #    "let the party use formations to move forward and pull back in
    #    battle... it shudnt cost them a turn... Run, Give, Formation,
    #    Equip"). ---------------------------------------------------------
    def test_formation_command_naming_a_known_companion_is_not_shadowed_by_talk_npc(self):
        """
        Real live bug, caught via topic-activity monitoring (2026-08-08):
        a real player said "Pull vesh back to the back row" and later
        "Move Vesh to the backrow" -- both came back as talk_npc, and a
        Dev-topic screenshot from the same player confirmed it: "I have
        not seen any indication that this action has worked. We are
        trying to move Vesh to the back row during battle." Root cause:
        the battle-formation block used to sit AFTER the known-NPC-name
        loop, so ANY formation command naming a real companion -- which
        is nearly always, since you have to say who to move -- got
        intercepted as talk_npc first. Separately, "pull X back to the
        back/front row" (as opposed to a bare trailing "...back") never
        matched the old pull-back regex at all, since it required the
        message to literally END in "back". Both fixed together; this
        also guards the bare "back row"/"front row" mention fallback
        doesn't win over a real name just because "back row" happens to
        be a substring of "...to the back row".
        """
        npc_names = ["Vesh", "Zara"]
        r = _keyword_fallback("Pull vesh back to the back row", npc_names)
        self.assertEqual(r["action"], "set_back_row")
        self.assertEqual((r.get("target") or "").lower(), "vesh")

        r = _keyword_fallback("Move Vesh to the backrow", npc_names)
        self.assertEqual(r["action"], "set_back_row")
        self.assertEqual((r.get("target") or "").lower(), "vesh")

        r = _keyword_fallback("pull vesh back to the front row", npc_names)
        self.assertEqual(r["action"], "set_front_row")
        self.assertEqual((r.get("target") or "").lower(), "vesh")

        # No regression: talking to a known NPC by name still works.
        r = _keyword_fallback("Say hello to Vesh", npc_names)
        self.assertEqual(r["action"], "talk_npc")
        r = _keyword_fallback("Talk to Vesh about the quest", npc_names)
        self.assertEqual(r["action"], "talk_npc")

    def test_tactical_phrasing_maps_to_formation_not_flee(self):
        self.assertEqual(_keyword_fallback("pull back", [])["action"], "set_back_row")
        result = _keyword_fallback("pull Zara back", [])
        self.assertEqual(result["action"], "set_back_row")
        self.assertEqual(result["target"], "Zara")
        self.assertEqual(_keyword_fallback("cover me", [])["action"], "set_back_row")
        self.assertEqual(_keyword_fallback("fall back", [])["action"], "set_back_row")
        self.assertEqual(_keyword_fallback("push up", [])["action"], "set_front_row")
        self.assertEqual(_keyword_fallback("hold the line", [])["action"], "set_front_row")
        self.assertEqual(_keyword_fallback("move up", [])["action"], "set_front_row")
        # No regression: real flee phrasing is unaffected.
        self.assertEqual(_keyword_fallback("flee the fight", [])["action"], "flee")
        self.assertEqual(_keyword_fallback("run away", [])["action"], "flee")
        # False-positive guard: an unrelated sentence ending in "back"
        # after an earlier "pull " must not misfire as a formation command.
        self.assertNotEqual(
            _keyword_fallback("I pull the lever, then head back to the tavern.", [])["action"],
            "set_back_row",
        )

    async def test_formation_change_mid_fight_updates_the_live_session_not_just_the_db(self):
        import sessions
        sessions.end_session(-999)
        leader_id = 950601
        make_basic_character(leader_id, "MidFightLeader", current_location="crossroads_tavern",
                              hp_max=100, armor_class=15)
        db.update_character(leader_id, -999, hp_current=100)
        enemy = {"telegram_user_id": -5100001, "name": "MidFightGoblin", "dexterity": 10,
                 "hp_current": 200, "hp_max": 200, "armor_class": 12}
        leader = db.get_character(leader_id, -999)
        leader["telegram_user_id"] = leader_id
        session = sessions.start_session(-999, [leader, enemy], {leader_id: "party", -5100001: "enemy"})
        session.turn_order = [leader_id, -5100001]

        await bot._do_set_formation_row(FakeUpdate(leader_id, "pull back", []), "", "back")

        live = next(p for p in session.participants if p["telegram_user_id"] == leader_id)
        self.assertEqual(live.get("formation_row"), "back")
        self.assertEqual(db.get_character(leader_id, -999).get("formation_row"), "back")
        self.assertEqual(session.current_participant_id(), leader_id)  # turn unchanged
        sessions.end_session(-999)

    def test_turn_announcement_shows_the_formation_split_every_turn(self):
        """
        Real live request (2026-08-08, Coffee, prompted by the same
        "move Vesh to the back row" incident above): the front/back
        formation line used to only ever appear once, in the message
        announcing combat had started -- with no way to see who's
        currently front vs back on any later turn without scrolling
        back. _turn_announcement (the real per-turn message, shown
        every single turn) must now include it too, using the exact
        same _format_formation_line the start-of-combat messages
        already use, and must stay silent for either side that has
        nobody in the back row -- same as the original start-of-combat
        behavior, just repeated every turn instead of shown once.
        """
        import sessions
        sessions.end_session(-999)
        leader_id = 950605
        make_basic_character(leader_id, "AnnounceLeader", current_location="crossroads_tavern")
        db.update_character(leader_id, -999, formation_row="back")
        enemy_front = {"telegram_user_id": -5100004, "name": "AnnounceGoblin", "dexterity": 10,
                        "hp_current": 20, "hp_max": 20, "formation_row": "front"}
        enemy_back = {"telegram_user_id": -5100005, "name": "AnnounceGoblinShaman", "dexterity": 10,
                       "hp_current": 15, "hp_max": 15, "formation_row": "back"}
        leader = db.get_character(leader_id, -999)
        leader["telegram_user_id"] = leader_id
        session = sessions.start_session(
            -999, [leader, enemy_front, enemy_back],
            {leader_id: "party", -5100004: "enemy", -5100005: "enemy"},
        )
        session.turn_order = [leader_id, -5100004, -5100005]

        text = bot._turn_announcement(session)
        self.assertIn("Back:", text)
        self.assertIn("AnnounceLeader", text)
        self.assertIn("AnnounceGoblinShaman", text)
        self.assertIn("Front:", text)
        self.assertIn("AnnounceGoblin", text)
        sessions.end_session(-999)

        # No regression: a fight where nobody's split (everyone default
        # front row) stays silent, same as _format_formation_line's own
        # original "nothing new to say" behavior.
        sessions.end_session(-999)
        leader_id2 = 950606
        make_basic_character(leader_id2, "AnnounceLeader2", current_location="crossroads_tavern")
        enemy2 = {"telegram_user_id": -5100006, "name": "AnnounceGoblin2", "dexterity": 10,
                  "hp_current": 20, "hp_max": 20, "formation_row": "front"}
        leader2 = db.get_character(leader_id2, -999)
        leader2["telegram_user_id"] = leader_id2
        session2 = sessions.start_session(-999, [leader2, enemy2], {leader_id2: "party", -5100006: "enemy"})
        session2.turn_order = [leader_id2, -5100006]
        text2 = bot._turn_announcement(session2)
        self.assertNotIn("Back:", text2)
        self.assertNotIn("Front:", text2)
        sessions.end_session(-999)

    def test_battle_menu_shows_more_button_not_a_bare_run_button(self):
        import sessions
        sessions.end_session(-999)
        leader_id = 950602
        make_basic_character(leader_id, "MenuLeader", current_location="crossroads_tavern")
        enemy = {"telegram_user_id": -5100002, "name": "MenuGoblin", "dexterity": 10, "hp_current": 20, "hp_max": 20}
        leader = db.get_character(leader_id, -999)
        leader["telegram_user_id"] = leader_id
        session = sessions.start_session(-999, [leader, enemy], {leader_id: "party", -5100002: "enemy"})
        session.turn_order = [leader_id, -5100002]
        kb = bot._battle_menu_keyboard(session)
        labels = [btn.text for row in kb.inline_keyboard for btn in row]
        self.assertFalse(any("Run" in l for l in labels))
        self.assertTrue(any("More" in l for l in labels))
        sessions.end_session(-999)

    async def test_give_via_battle_menu_more_submenu_does_not_cost_a_turn(self):
        import sessions
        sessions.end_session(-999)
        leader_id = 950603
        ally_id = 950604
        make_basic_character(leader_id, "GiveMenuLeader", current_location="crossroads_tavern")
        make_basic_character(ally_id, "GiveMenuAlly", current_location="crossroads_tavern")
        db.update_character(leader_id, -999, inventory={"healing_potion": 1})
        enemy = {"telegram_user_id": -5100003, "name": "GiveMenuGoblin", "dexterity": 10, "hp_current": 20, "hp_max": 20}
        leader = db.get_character(leader_id, -999)
        leader["telegram_user_id"] = leader_id
        session = sessions.start_session(-999, [leader, enemy], {leader_id: "party", -5100003: "enemy"})
        session.turn_order = [leader_id, -5100003]

        async def tap(data):
            sink = []
            update = FakeCallbackUpdate(leader_id, data, sink)
            await bot.battle_menu_callback(update, DummyContext())
            return "\n".join(sink)

        await tap("bm|more")
        await tap("bm|give")
        await tap("bm|giveitem|healing_potion")
        reply = await tap("bm|giveto|healing_potion|GiveMenuAlly")
        self.assertIn("1x", reply)
        self.assertEqual(db.get_character(ally_id, -999)["inventory"].get("healing_potion", 0), 1)
        self.assertEqual(session.current_participant_id(), leader_id)  # turn unchanged
        sessions.end_session(-999)

    async def test_hex_cast_via_battle_menu_offers_an_enemy_target_picker(self):
        """
        Real gap found in the 2026-08-05 audit ("make sure if any of the
        27 abilities needs tagetting it is there"): Hex has a real
        per-target mechanic (_cast_utility_spell marks whoever it's cast
        on), but the battle-menu button used to cast it immediately with
        no target chosen at all, always defaulting to the first living
        enemy -- never a real choice when more than one enemy is present.
        """
        import sessions
        sessions.end_session(-999)
        caster_id = 950801
        make_basic_character(caster_id, "HexCaster", char_class="Warlock",
                              known_spells=["hex"], spell_slots_max=3, current_location="crossroads_tavern")
        goblin_a = {"telegram_user_id": -5200001, "name": "HexGoblinA", "dexterity": 10,
                    "hp_current": 20, "hp_max": 20, "conditions": []}
        goblin_b = {"telegram_user_id": -5200002, "name": "HexGoblinB", "dexterity": 10,
                    "hp_current": 20, "hp_max": 20, "conditions": []}
        caster = db.get_character(caster_id, -999)
        caster["telegram_user_id"] = caster_id
        session = sessions.start_session(-999, [caster, goblin_a, goblin_b],
                                          {caster_id: "party", -5200001: "enemy", -5200002: "enemy"})
        session.turn_order = [caster_id, -5200001, -5200002]

        async def tap(data):
            sink = []
            await bot.battle_menu_callback(FakeCallbackUpdate(caster_id, data, sink), DummyContext())
            return "\n".join(sink)

        picker = await tap("bm|cast|hex")
        self.assertIn("HexGoblinA", picker)
        self.assertIn("HexGoblinB", picker)
        self.assertIn("bm|casttarget|hex|HexGoblinB", picker)

        await tap("bm|casttarget|hex|HexGoblinB")
        caster_p = next(p for p in session.participants if p["telegram_user_id"] == caster_id)
        self.assertEqual(caster_p.get("marked_target_id"), -5200002)  # the chosen goblin, not the default first one
        sessions.end_session(-999)

    async def test_shield_cast_via_battle_menu_offers_an_ally_target_picker(self):
        """
        Same gap, ally-facing side: Shield genuinely applies its AC
        bonus to whoever it's cast on (_cast_utility_spell), but with
        more than one living ally present the button used to always
        cast on the caster themselves with no way to protect someone
        else.
        """
        import sessions
        sessions.end_session(-999)
        caster_id, ally_id = 950802, 950803
        make_basic_character(caster_id, "ShieldCaster", char_class="Wizard",
                              known_spells=["shield"], spell_slots_max=3, current_location="crossroads_tavern")
        make_basic_character(ally_id, "ShieldAlly", current_location="crossroads_tavern")
        enemy = {"telegram_user_id": -5200003, "name": "ShieldGoblin", "dexterity": 10,
                 "hp_current": 20, "hp_max": 20, "conditions": []}
        caster = db.get_character(caster_id, -999)
        caster["telegram_user_id"] = caster_id
        ally = db.get_character(ally_id, -999)
        ally["telegram_user_id"] = ally_id
        session = sessions.start_session(-999, [caster, ally, enemy],
                                          {caster_id: "party", ally_id: "party", -5200003: "enemy"})
        session.turn_order = [caster_id, ally_id, -5200003]

        async def tap(data):
            sink = []
            await bot.battle_menu_callback(FakeCallbackUpdate(caster_id, data, sink), DummyContext())
            return "\n".join(sink)

        picker = await tap("bm|cast|shield")
        self.assertIn("ShieldAlly", picker)
        self.assertIn("— you", picker)  # the caster's own entry, per the heal-picker pattern this reuses

        await tap("bm|casttarget|shield|ShieldAlly")
        ally_p = next(p for p in session.participants if p["telegram_user_id"] == ally_id)
        caster_p = next(p for p in session.participants if p["telegram_user_id"] == caster_id)
        self.assertIn("shield_active", ally_p.get("conditions", []))
        self.assertNotIn("shield_active", caster_p.get("conditions", []))
        sessions.end_session(-999)

    async def test_spare_the_dying_via_battle_menu_targets_the_one_down_ally_not_self(self):
        """
        Spare the Dying only ever has a real effect on someone at 0 HP
        (_cast_utility_spell's own check) -- a plain ally-picker like
        shield's would be wrong here since most allies at full HP
        aren't valid targets at all. With exactly one real candidate,
        the button must cast on THEM directly rather than falling
        through to the free-text default of "on the caster" (who isn't
        down and isn't a valid target).
        """
        import sessions
        sessions.end_session(-999)
        caster_id, down_ally_id = 950804, 950805
        make_basic_character(caster_id, "SpareDyingCaster", char_class="Cleric",
                              known_spells=["spare_the_dying"], spell_slots_max=0, current_location="crossroads_tavern")
        make_basic_character(down_ally_id, "SpareDyingAlly", current_location="crossroads_tavern")
        enemy = {"telegram_user_id": -5200004, "name": "SpareDyingGoblin", "dexterity": 10,
                 "hp_current": 20, "hp_max": 20, "conditions": []}
        caster = db.get_character(caster_id, -999)
        caster["telegram_user_id"] = caster_id
        down_ally = db.get_character(down_ally_id, -999)
        down_ally["telegram_user_id"] = down_ally_id
        down_ally["hp_current"] = 0
        session = sessions.start_session(-999, [caster, down_ally, enemy],
                                          {caster_id: "party", down_ally_id: "party", -5200004: "enemy"})
        session.turn_order = [caster_id, down_ally_id, -5200004]
        down_p = next(p for p in session.participants if p["telegram_user_id"] == down_ally_id)
        down_p["hp_current"] = 0

        sink = []
        await bot.battle_menu_callback(FakeCallbackUpdate(caster_id, "bm|cast|spare_the_dying", sink), DummyContext())
        reply = "\n".join(sink)
        self.assertIn("SpareDyingAlly", reply)
        self.assertTrue(down_ally_id in session.stabilized_ids)
        sessions.end_session(-999)

    async def test_command_cast_via_battle_menu_offers_target_and_word_together(self):
        """
        Command genuinely needs BOTH a target and a command word
        ("flee"/"drop") to do anything (_cast_utility_spell checks the
        free text for one) -- a bare enemy-picker alone would leave
        every button tap failing with "needs a real command word".
        """
        import sessions
        sessions.end_session(-999)
        caster_id = 950806
        make_basic_character(caster_id, "CommandCaster", char_class="Cleric",
                              known_spells=["command"], spell_slots_max=3, current_location="crossroads_tavern")
        enemy = {"telegram_user_id": -5200005, "name": "CommandGoblin", "dexterity": 10,
                 "hp_current": 20, "hp_max": 20, "conditions": []}
        caster = db.get_character(caster_id, -999)
        caster["telegram_user_id"] = caster_id
        session = sessions.start_session(-999, [caster, enemy], {caster_id: "party", -5200005: "enemy"})
        session.turn_order = [caster_id, -5200005]

        async def tap(data):
            sink = []
            await bot.battle_menu_callback(FakeCallbackUpdate(caster_id, data, sink), DummyContext())
            return "\n".join(sink)

        picker = await tap("bm|cast|command")
        self.assertIn("CommandGoblin: Drop", picker)
        self.assertIn("bm|casttarget|command|CommandGoblin, drop", picker)

        reply = await tap("bm|casttarget|command|CommandGoblin, drop")
        self.assertNotIn("needs a real command word", reply)
        enemy_p = next(p for p in session.participants if p["telegram_user_id"] == -5200005)
        self.assertIn("disarmed", enemy_p.get("conditions", []))
        sessions.end_session(-999)

    async def test_scroll_shows_up_and_targets_correctly_in_battle_menu_items(self):
        """
        Real live bug (2026-08-02, Coffee: "i didnt see a button for the
        revive scroll either in battle in the item menu as a push
        button... mae sure all scrolls are usable in battle with
        targetting enabled"). Both the battle menu's top-level Items
        button visibility check and its own item listing only ever
        filtered to type=="consumable" -- a scroll (type=="scroll")
        never showed up, and a character carrying nothing BUT scrolls
        never even got an Items button at all. Also covers the
        previously-missing resurrect-effect target picker (a dead party
        member, not a living one) for both the scroll flow and its
        known-spell-caster "cast" counterpart.
        """
        import sessions
        sessions.end_session(-999)
        leader_id = 950801
        make_basic_character(leader_id, "ScrollMenuLeader", char_class="Cleric", current_location="crossroads_tavern")
        db.add_item(leader_id, -999, "scroll_revivify", 1)
        char = db.get_character(leader_id, -999)
        self.assertIn("scroll_revivify", bot._battle_usable_item_ids(char))

        dead_companion = db.create_ai_companion(
            -999, "ScrollMenuFallen", "Elf", "Ranger",
            ability_scores={"strength": 12, "dexterity": 17, "constitution": 13,
                             "intelligence": 11, "wisdom": 15, "charisma": 10},
            hp_max=30, armor_class=14, gold=0, inventory={},
        )
        party_id = db.create_party(leader_id, -999)
        db.add_ai_companion_to_party(dead_companion["telegram_user_id"], -999, party_id)
        db.update_character_by_id(dead_companion["character_id"], is_dead=1, hp_current=0)
        char = db.get_character(leader_id, -999)

        enemy = {
            "telegram_user_id": -2_600_101, "name": "ScrollMenuGoblin", "dexterity": 10, "strength": 10,
            "armor_class": 12, "hp_current": 20, "hp_max": 20, "proficiency_bonus": 2, "is_ai": 1,
        }
        session = sessions.start_session(-999, [char, enemy], {leader_id: "party", -2_600_101: "enemy"})
        if session.current_participant_id() != leader_id:
            session.current_turn_index = session.turn_order.index(leader_id)

        keyboard = bot._battle_menu_keyboard(session)
        button_labels = [btn.text for row in keyboard.inline_keyboard for btn in row]
        self.assertTrue(any("Items" in label for label in button_labels))

        sink = []
        await bot.battle_menu_callback(FakeCallbackUpdate(leader_id, "bm|scroll|scroll_revivify", sink), DummyContext())
        self.assertTrue(any("ScrollMenuFallen" in m for m in sink))

        sink2 = []
        await bot.battle_menu_callback(
            FakeCallbackUpdate(leader_id, "bm|scrolltarget|scroll_revivify|ScrollMenuFallen", sink2), DummyContext(),
        )
        revived = db.get_character_by_id(dead_companion["character_id"])
        self.assertFalse(revived.get("is_dead"))
        self.assertEqual(db.get_character(leader_id, -999)["inventory"].get("scroll_revivify", 0), 0)
        sessions.end_session(-999, session)

    async def test_equip_via_battle_menu_offers_present_party_members_not_just_self(self):
        """
        Real live bug (2026-08-01, Coffee: "when i clicked equip it
        only showed my character and no one else"). _do_equip_item has
        always supported equipping gear for another present party
        member ("equip Sarah with the longbow") -- the battle-menu
        button just never offered anyone but the caller. Also covers
        Coffee's explicit follow-up ask: "put an auto equip button".
        """
        import sessions
        sessions.end_session(-999)
        leader_id = 950701
        ally_id = 950702
        make_basic_character(leader_id, "EquipMenuLeader", current_location="crossroads_tavern")
        make_basic_character(ally_id, "EquipMenuAlly", current_location="crossroads_tavern")
        db.update_character(ally_id, -999, inventory={"rusty_dagger": 1})
        enemy = {"telegram_user_id": -5100004, "name": "EquipMenuGoblin", "dexterity": 10,
                 "hp_current": 20, "hp_max": 20}
        leader = db.get_character(leader_id, -999)
        leader["telegram_user_id"] = leader_id
        ally = db.get_character(ally_id, -999)
        ally["telegram_user_id"] = ally_id
        session = sessions.start_session(-999, [leader, ally, enemy],
                                          {leader_id: "party", ally_id: "party", -5100004: "enemy"})
        session.turn_order = [leader_id, ally_id, -5100004]

        async def tap(data):
            sink = []
            update = FakeCallbackUpdate(leader_id, data, sink)
            await bot.battle_menu_callback(update, DummyContext())
            return "\n".join(sink)

        await tap("bm|more")
        equip_menu = await tap("bm|equip")
        self.assertIn("Auto Equip", equip_menu)
        self.assertIn("EquipMenuAlly", equip_menu)  # the actual bug: ally used to be missing

        await tap(f"bm|equipfor|{ally_id}")
        reply = await tap(f"bm|equipitem|{ally_id}|rusty_dagger")
        self.assertEqual(db.get_character(ally_id, -999).get("equipped_weapon"), "rusty_dagger")
        self.assertNotEqual(db.get_character(leader_id, -999).get("equipped_weapon"), "rusty_dagger")
        self.assertEqual(session.current_participant_id(), leader_id)  # turn unchanged
        sessions.end_session(-999)

    # -- AI companions rush to the fight regardless of location (2026-08-01,
    #    real live report: Coffee had Grask Emberscale (a real, active,
    #    non-benched party member) sit out a fight purely because he'd
    #    wandered to a different location under the living-world system --
    #    "I want the AI players that are in the party to be in the
    #    battles... that's the point of picking the active party members
    #    for combats"). ---------------------------------------------------
    async def test_challenge_and_accept_duel_starts_a_real_fight(self):
        """
        Real coverage for the duel flow, never previously tested --
        also exercises _PENDING_DUELS' chat-scoping (2026-08-01 Phase 2):
        the pending challenge is now stored per-chat, not one bare
        module-level dict shared across every Telegram group the bot is
        ever added to.
        """
        import sessions
        sessions.end_session(-999)
        challenger_id, target_id = 950601, 950602
        make_basic_character(challenger_id, "DuelChallenger", current_location="whispering_wood")
        make_basic_character(target_id, "DuelTarget", current_location="whispering_wood")

        sink = []
        await bot._do_challenge_duel(FakeUpdate(challenger_id, "I challenge DuelTarget", sink), "I challenge DuelTarget")
        self.assertTrue(any("challenges" in m for m in sink))
        self.assertEqual(
            bot._chat_scoped_dict(bot._PENDING_DUELS, -999).get(target_id), challenger_id,
        )

        sink2 = []
        await bot._do_accept_duel(FakeUpdate(target_id, "accept duel", sink2))
        self.assertTrue(any("duel begins" in m for m in sink2))
        session = sessions.get_session_for_user(-999, target_id)
        self.assertIsNotNone(session)
        self.assertIs(sessions.get_session_for_user(-999, challenger_id), session)
        self.assertNotIn(target_id, bot._chat_scoped_dict(bot._PENDING_DUELS, -999))
        sessions.end_session(-999, session)

    def test_chat_scoped_globals_do_not_leak_across_tenant_chats(self):
        """
        Real Phase 2 deliverable (2026-08-01, chat-scoping bot.py's other
        bare globals): a door picked, an NPC defeated, or a challenge
        issued in one tenant's world must never show up as already
        unlocked/defeated/pending in a completely different tenant's
        chat. Before this pass, _UNLOCKED/_DEFEATED_NPCS/_PENDING_DUELS
        were bare sets/dicts shared across every Telegram chat the bot
        is ever added to -- harmless with exactly one real chat, wrong
        the moment a second tenant exists.
        """
        chat_a, chat_b = -999, -888

        bot._chat_scoped_set(bot._UNLOCKED, chat_a).add("sealed_stone_door")
        self.assertNotIn("sealed_stone_door", bot._chat_scoped_set(bot._UNLOCKED, chat_b))

        bot._chat_scoped_set(bot._DEFEATED_NPCS, chat_a).add("some_npc_id")
        self.assertNotIn("some_npc_id", bot._chat_scoped_set(bot._DEFEATED_NPCS, chat_b))

        bot._chat_scoped_dict(bot._PENDING_DUELS, chat_a)[123] = 456
        self.assertNotIn(123, bot._chat_scoped_dict(bot._PENDING_DUELS, chat_b))

        bot._chat_scoped_dict(bot._NPC_LOCATIONS, chat_a)["sera_wanderer"] = "market_row"
        self.assertNotIn("sera_wanderer", bot._chat_scoped_dict(bot._NPC_LOCATIONS, chat_b))

        bot._log_world_event("crossroads_tavern", "a leaked test event", chat_id=chat_a)
        self.assertFalse(any(
            "a leaked test event" in text for (_when, _loc, text) in bot._RECENT_WORLD_EVENTS.get(chat_b, [])
        ))

        context_a = bot._chat_scoped_dict(bot._AI_PLAYER_CONTEXTS, chat_a).setdefault(789, bot._AiPlayerContext())
        context_a.user_data["human_guidance"] = "retreat"
        self.assertNotIn(789, bot._chat_scoped_dict(bot._AI_PLAYER_CONTEXTS, chat_b))

        # Real multi-tenant gap fixed (2026-08-08): _PENDING_DICE_ROLLS/
        # _PENDING_ASI_CHOICE/_PENDING_DESCRIPTION/_PENDING_PRONOUNS were
        # bare telegram_user_id-keyed globals -- a player active in two
        # tenant chats at once could have one chat's pending prompt
        # silently overwritten/cleared by the other. Now chat-scoped the
        # same way as everything else in this test.
        bot._chat_scoped_dict(bot._PENDING_DICE_ROLLS, chat_a)[321] = bot._new_pending_roll("attack", "I attack", chat_a)
        self.assertNotIn(321, bot._chat_scoped_dict(bot._PENDING_DICE_ROLLS, chat_b))

        bot._chat_scoped_set(bot._PENDING_ASI_CHOICE, chat_a).add(654)
        self.assertNotIn(654, bot._chat_scoped_set(bot._PENDING_ASI_CHOICE, chat_b))

        bot._chat_scoped_set(bot._PENDING_DESCRIPTION, chat_a).add(654)
        self.assertNotIn(654, bot._chat_scoped_set(bot._PENDING_DESCRIPTION, chat_b))

        bot._chat_scoped_set(bot._PENDING_PRONOUNS, chat_a).add(654)
        self.assertNotIn(654, bot._chat_scoped_set(bot._PENDING_PRONOUNS, chat_b))

        # Real regression (2026-08-08, caught by the regression suite
        # itself): chat_a is -999, the same shared test chat_id reused
        # by dozens of other tests in this file (including
        # test_fast_travel_blocks_on_locked_connection, which also uses
        # the door id "sealed_stone_door") -- leaving these bare
        # module-level globals mutated with no cleanup meant this test,
        # once it happened to run first alphabetically, permanently
        # "unlocked" that door for every later test sharing chat -999,
        # a real false pass/fail depending on test run order, not a
        # production bug (_do_fast_travel's own logic was always
        # correct). Undo everything this test added to chat_a so it
        # cannot leak into any other test.
        bot._chat_scoped_set(bot._UNLOCKED, chat_a).discard("sealed_stone_door")
        bot._chat_scoped_set(bot._DEFEATED_NPCS, chat_a).discard("some_npc_id")
        bot._chat_scoped_dict(bot._PENDING_DUELS, chat_a).pop(123, None)
        bot._chat_scoped_dict(bot._NPC_LOCATIONS, chat_a).pop("sera_wanderer", None)
        events_a = bot._RECENT_WORLD_EVENTS.get(chat_a, [])
        events_a[:] = [e for e in events_a if e[2] != "a leaked test event"]
        bot._chat_scoped_dict(bot._AI_PLAYER_CONTEXTS, chat_a).pop(789, None)
        bot._chat_scoped_dict(bot._PENDING_DICE_ROLLS, chat_a).pop(321, None)
        bot._chat_scoped_set(bot._PENDING_ASI_CHOICE, chat_a).discard(654)
        bot._chat_scoped_set(bot._PENDING_DESCRIPTION, chat_a).discard(654)
        bot._chat_scoped_set(bot._PENDING_PRONOUNS, chat_a).discard(654)

    async def test_onboarding_consent_flow_auto_setup_and_private_world(self):
        """
        Real feature (2026-08-08, per Coffee: "tell them what u need to
        do, ask them if they want that, then continue if they agree" +
        "ask if they want to connect to the main database or have
        thier own (private world)"): a brand-new group's admin is asked
        two real questions -- auto vs. manual topic setup, then Private
        World vs. the shared Public World (which just points at the
        real invite link, since every tenant chat's own isolated
        Private World is the only backend that actually exists). This
        drives the real bot_added_to_group_handler/_route_text_message/
        _handle_onboarding_reply path end to end with a fake Bot that
        reports forum mode on and can_manage_topics=True.
        """
        class _OnboardingFakeForumTopic:
            def __init__(self, message_thread_id):
                self.message_thread_id = message_thread_id

        class _OnboardingFakeChat:
            def __init__(self, chat_id, is_forum):
                self.id = chat_id
                self.title = "Test Group"
                self.is_forum = is_forum

        class _OnboardingFakeChatMember:
            def __init__(self, status, can_manage_topics=False):
                self.status = status
                self.can_manage_topics = can_manage_topics

        class _OnboardingFakeBot:
            def __init__(self, bot_id, forum):
                self.id = bot_id
                self.forum = forum
                self.sent = []
                self._next_thread_id = 100

            async def send_message(self, chat_id, text, message_thread_id=None, parse_mode=None):
                self.sent.append(text)
                return SimpleNamespace(message_id=len(self.sent))

            async def get_chat(self, chat_id):
                return _OnboardingFakeChat(chat_id, self.forum)

            async def get_chat_member(self, chat_id, user_id):
                if user_id == self.id:
                    return _OnboardingFakeChatMember("administrator", can_manage_topics=True)
                return _OnboardingFakeChatMember("administrator")

            async def create_forum_topic(self, chat_id, name):
                self._next_thread_id += 1
                return _OnboardingFakeForumTopic(self._next_thread_id)

        chat_id, admin_id, fake_bot_id = -5101, 611, 999999
        tg_bot = _OnboardingFakeBot(fake_bot_id, forum=True)
        ctx = SimpleNamespace(bot=tg_bot, user_data={}, args=[])

        added = SimpleNamespace(
            chat=SimpleNamespace(id=chat_id, title="Test Group"),
            old_chat_member=SimpleNamespace(status="left"),
            new_chat_member=SimpleNamespace(status="member", user=SimpleNamespace(id=fake_bot_id)),
        )
        await bot.bot_added_to_group_handler(
            SimpleNamespace(effective_chat=None, effective_user=None, my_chat_member=added), ctx,
        )
        self.assertEqual(bot._PENDING_ONBOARDING.get(chat_id), "awaiting_setup_choice")

        def make_text_update(text):
            msg = SimpleNamespace(text=text, message_thread_id=None)
            return SimpleNamespace(
                effective_chat=SimpleNamespace(id=chat_id, type="group"),
                effective_user=SimpleNamespace(id=admin_id),
                effective_message=msg,
            )

        await bot._route_text_message(make_text_update("auto"), ctx)
        self.assertTrue(any("Auto-setup done" in t for t in tg_bot.sent))
        self.assertIsNotNone(db.get_chat_topic_id(chat_id, "adventure"))
        self.assertIsNotNone(db.get_chat_topic_id(chat_id, "support"))
        self.assertEqual(bot._PENDING_ONBOARDING.get(chat_id), "awaiting_world_choice")

        await bot._route_text_message(make_text_update("private please"), ctx)
        self.assertTrue(any("Private World" in t and "You're all set" in t for t in tg_bot.sent))
        self.assertNotIn(chat_id, bot._PENDING_ONBOARDING)

    async def test_onboarding_public_world_choice_points_at_the_real_invite_link(self):
        """
        Companion case to the test above: choosing "public" must never
        try to share this tenant's data with the home group's chat_id
        (no such cross-tenant sharing exists, deliberately -- see
        [[project_multi_tenant_scaling_status]]) -- it just tells the
        admin the real Telegram invite link for the shared Public World
        and leaves this chat on its own Private World regardless.
        """
        chat_id, admin_id = -5102, 622
        bot._PENDING_ONBOARDING[chat_id] = "awaiting_world_choice"
        sent = []

        class _StubBot:
            async def send_message(self, chat_id, text, message_thread_id=None, parse_mode=None):
                sent.append(text)

            async def get_chat_member(self, chat_id, user_id):
                return SimpleNamespace(status="administrator")

        ctx = SimpleNamespace(bot=_StubBot(), user_data={}, args=[])
        msg = SimpleNamespace(text="I'd like public", message_thread_id=None)
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=chat_id, type="group"),
            effective_user=SimpleNamespace(id=admin_id),
            effective_message=msg,
        )
        await bot._route_text_message(update, ctx)
        self.assertTrue(any(bot._PUBLIC_WORLD_INVITE_LINK in t for t in sent))
        self.assertNotIn(chat_id, bot._PENDING_ONBOARDING)

    async def test_private_dm_to_the_bot_gets_a_real_getting_started_reply(self):
        """
        Real gap found 2026-08-08 (per Coffee: "if someone messages the
        Bot it serves the same purpose as getting started"): before
        this, ANY private DM to the bot -- including Telegram's own
        auto-sent /start on first opening a chat with it -- got
        silently dropped by _route_text_message, since a private chat's
        message_thread_id is always None, same as the home group's Main
        topic ("human-to-human only"). Both a plain DM and /start must
        now point the user at joining the main group or adding the bot
        to their own.
        """
        sent = []

        class _StubChat:
            id = -7001
            type = "private"

            async def send_message(self, text, parse_mode=None):
                sent.append(text)

        stub_chat = _StubChat()
        msg = SimpleNamespace(text="hi", message_thread_id=None)
        update = SimpleNamespace(effective_chat=stub_chat, effective_user=SimpleNamespace(id=701),
                                  effective_message=msg)
        await bot._route_text_message(update, SimpleNamespace(bot=None, user_data={}, args=[]))
        self.assertTrue(any(bot._PUBLIC_WORLD_INVITE_LINK in t for t in sent))
        self.assertTrue(any("add me" in t.lower() for t in sent))

        sent.clear()
        start_update = SimpleNamespace(effective_chat=stub_chat, effective_user=SimpleNamespace(id=701))
        await bot.start_command(start_update, SimpleNamespace(bot=None, user_data={}, args=[]))
        self.assertTrue(any(bot._PUBLIC_WORLD_INVITE_LINK in t for t in sent))

    def test_get_party_members_does_not_leak_across_tenant_chats(self):
        """
        Real cross-tenant leak, found and fixed 2026-08-08 (multi-tenant
        audit): bot._get_party_members() used to have no chat_id filter
        at all, returning every active character across EVERY tenant
        chat -- since this feeds combat targeting, dueling, and "give
        item to nearby party member" (via
        _get_combat_eligible_party_members), two different tenant
        chats' characters could target/duel/give-items to each other.
        Two characters made in two different real chat_ids here --
        each chat's own _get_party_members(chat_id) call must see only
        its own character, never the other chat's.
        """
        chat_a, chat_b = -999601, -999602
        make_basic_character(970601, "TenantA_Hero", chat_id=chat_a, current_location="crossroads_tavern")
        make_basic_character(970602, "TenantB_Hero", chat_id=chat_b, current_location="crossroads_tavern")

        members_a = bot._get_party_members(chat_a)
        members_b = bot._get_party_members(chat_b)

        names_a = {m["name"] for m in members_a}
        names_b = {m["name"] for m in members_b}
        self.assertIn("TenantA_Hero", names_a)
        self.assertNotIn("TenantB_Hero", names_a)
        self.assertIn("TenantB_Hero", names_b)
        self.assertNotIn("TenantA_Hero", names_b)

        eligible_a = bot._get_combat_eligible_party_members("crossroads_tavern", chat_a)
        self.assertIn("TenantA_Hero", {m["name"] for m in eligible_a})
        self.assertNotIn("TenantB_Hero", {m["name"] for m in eligible_a})

    async def test_active_ai_companion_joins_combat_despite_being_elsewhere(self):
        leader_id = 950501
        make_basic_character(leader_id, "RushLeader", current_location="crossroads_tavern")
        party_id = db.create_party(leader_id, -999)
        companion = db.create_ai_companion(
            -999, "RushCompanion", "Dragonborn", "Fighter",
            ability_scores={"strength": 16, "dexterity": 12, "constitution": 14,
                             "intelligence": 8, "wisdom": 10, "charisma": 10},
            hp_max=61, armor_class=16, gold=0, inventory={},
        )
        db.add_ai_companion_to_party(companion["telegram_user_id"], -999, party_id)
        db.update_character_by_id(companion["character_id"], current_location="whispering_woods")

        combatants = bot._get_real_party_combatants(db.get_character(leader_id, -999))
        self.assertIn(companion["telegram_user_id"], {c["telegram_user_id"] for c in combatants})
        self.assertEqual(
            db.get_character_by_id(companion["character_id"])["current_location"], "crossroads_tavern",
        )
        rushed = next(c for c in combatants if c["telegram_user_id"] == companion["telegram_user_id"])
        self.assertTrue(rushed.get("_rushed_in"))
        self.assertIn("RushCompanion", bot._rushed_in_note(combatants))

    async def test_benched_ai_companion_still_sits_out_regardless_of_location(self):
        leader_id = 950502
        make_basic_character(leader_id, "RushLeader2", current_location="crossroads_tavern")
        party_id = db.create_party(leader_id, -999)
        companion = db.create_ai_companion(
            -999, "BenchedRushCompanion", "Dragonborn", "Fighter",
            ability_scores={"strength": 16, "dexterity": 12, "constitution": 14,
                             "intelligence": 8, "wisdom": 10, "charisma": 10},
            hp_max=61, armor_class=16, gold=0, inventory={},
        )
        db.add_ai_companion_to_party(companion["telegram_user_id"], -999, party_id)
        db.update_character_by_id(companion["character_id"], current_location="whispering_woods", is_benched=1)

        combatants = bot._get_real_party_combatants(db.get_character(leader_id, -999))
        self.assertNotIn(companion["telegram_user_id"], {c["telegram_user_id"] for c in combatants})

    async def test_real_human_party_member_elsewhere_is_not_teleported(self):
        leader_id = 950503
        human_id = 950504
        make_basic_character(leader_id, "RushLeader3", current_location="crossroads_tavern")
        make_basic_character(human_id, "RushHuman", current_location="whispering_woods")
        party_id = db.create_party(leader_id, -999)
        db.update_character(human_id, -999, party_id=party_id)

        combatants = bot._get_real_party_combatants(db.get_character(leader_id, -999))
        self.assertNotIn(human_id, {c["telegram_user_id"] for c in combatants})
        self.assertEqual(db.get_character(human_id, -999)["current_location"], "whispering_woods")

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
        db.accept_quest(player_id, -999, "the_hush_stage3_the_unspoken")

        boss_id = -2_500_010
        boss = {
            "telegram_user_id": boss_id, "name": "The Unspoken", "dexterity": 16,
            "is_ai": 1, "monster_key": "the_unspoken",
        }
        session = sessions.start_session(-999, [boss], {boss_id: "enemy", player_id: "party"})
        session.turn_order = [boss_id, player_id]

        sink = []
        await bot._check_quest_completions_defeat_monster(FakeUpdate(player_id, "irrelevant", sink), session)

        character = db.get_character(player_id, -999)
        self.assertIn("the_hush_stage3_the_unspoken", character["completed_quests"])
        self.assertNotIn("the_hush_stage3_the_unspoken", character["active_quests"])
        sessions.end_session(-999)

    async def test_the_first_city_quest_requires_defeating_the_waking_ember(self):
        import sessions
        sessions.end_session(-999)

        player_id = 999911
        make_basic_character(player_id, "Delver", current_location="the_first_city")
        db.accept_quest(player_id, -999, "the_first_city_quest")

        boss_id = -2_500_011
        boss = {
            "telegram_user_id": boss_id, "name": "The Waking Ember", "dexterity": 12,
            "is_ai": 1, "monster_key": "the_waking_ember",
        }
        session = sessions.start_session(-999, [boss], {boss_id: "enemy", player_id: "party"})
        session.turn_order = [boss_id, player_id]

        sink = []
        await bot._check_quest_completions_defeat_monster(FakeUpdate(player_id, "irrelevant", sink), session)

        character = db.get_character(player_id, -999)
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

    # -- Task #218, per Coffee: an optional hidden superboss + legendary
    #    artifact chain, deliberately outside the main story_arcs (never
    #    part of chapter progression, never blocking or gating anything
    #    else) -- reachable only through a hidden lockable door off an
    #    existing far location, same door/lockpick mechanism as every
    #    other hidden passage in this campaign.
    async def test_hidden_superboss_quest_grants_the_legendary_reward(self):
        import sessions
        sessions.end_session(-997)

        player_id = 999912
        make_basic_character(player_id, "DepthSeeker", chat_id=-997, current_location="the_first_city_forgotten_depth")
        db.accept_quest(player_id, -997, "the_unrepeating_depth")

        boss_id = -2_500_012
        boss = {
            "telegram_user_id": boss_id, "name": "The Unrepeating", "dexterity": 16,
            "is_ai": 1, "monster_key": "the_unrepeating",
        }
        session = sessions.start_session(-997, [boss], {boss_id: "enemy", player_id: "party"})
        session.turn_order = [boss_id, player_id]

        sink = []
        await bot._check_quest_completions_defeat_monster(FakeUpdate(player_id, "irrelevant", sink, chat_id=-997), session)

        character = db.get_character(player_id, -997)
        self.assertIn("the_unrepeating_depth", character["completed_quests"])
        self.assertNotIn("the_unrepeating_depth", character["active_quests"])
        self.assertEqual(character["inventory"].get("the_last_word", 0), 1)
        sessions.end_session(-997)

    def test_hidden_superboss_door_and_quest_are_outside_the_main_story(self):
        loc = cl.get_location(bot.CAMPAIGN, "the_first_city_deepest_record")
        self.assertEqual(loc["locked_connections"]["the_first_city_forgotten_depth"], "record_hidden_seam")
        lockable = next(lk for lk in loc["lockables"] if lk["id"] == "record_hidden_seam")
        self.assertEqual(lockable["leads_to"], "the_first_city_forgotten_depth")
        arc_info = bot._story_arc_for_quest("the_unrepeating_depth")
        self.assertIsNone(arc_info, "the hidden superboss quest must not be part of any story arc")

    # -- Task #137, per Coffee: a hidden, standalone "Pandora" legend --
    #    original content, never referenced by or influencing the real
    #    storyline -- just discoverable flavor text, same interactables
    #    mechanism as every other examinable object in this campaign.
    def test_standalone_pandora_legend_exists_and_is_isolated(self):
        loc = cl.get_location(bot.CAMPAIGN, "greymoor_downs_below_the_cairn")
        marker = loc["interactables"]["the_wedged_marker"]
        self.assertIn("Pandora", marker["description"])
        # Isolation check: this location must not be any quest's location/
        # objective_location, nor named in any quest's reach_location
        # trigger -- confirming the legend never gates or feeds the real
        # story, per Coffee's explicit "never influencing the main story."
        for quest_id, quest in bot.CAMPAIGN["quests"].items():
            self.assertNotEqual(quest.get("location"), "greymoor_downs_below_the_cairn", quest_id)
            self.assertNotEqual(quest.get("objective_location"), "greymoor_downs_below_the_cairn", quest_id)
            trigger = quest.get("trigger", {})
            self.assertNotEqual(trigger.get("location"), "greymoor_downs_below_the_cairn", quest_id)

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
        db.update_character(user_id, -999, level=6)  # 3d6 tier
        player = db.get_character(user_id, -999)
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
        self.assertEqual(db.get_feature_uses(user_id, -999, "breath_weapon"), 1)

        # Second use this same rest should be rejected.
        sink2 = []
        await bot._do_breath_weapon(FakeUpdate(user_id, "breath weapon", sink2))
        self.assertTrue(any("already used" in m for m in sink2))
        sessions.end_session(-999)

    def test_breath_weapon_phrasing_classified_correctly(self):
        for text in ("I use my breath weapon", "breathe fire", "unleash my breath", "breath weapon"):
            self.assertEqual(_keyword_fallback(text, [])["action"], "breath_weapon", text)

    def test_attack_with_dragon_breath_classifies_as_breath_weapon_not_a_weapon_attack(self):
        """
        Real live bug (2026-08-07, topic-activity log: "Attack spider 1
        with dragon breath"). The original 2026-07-22 "dragon breath"
        fix (see test_breath_weapon_phrasing_classified_correctly above)
        has always been unreachable whenever the message ALSO contains a
        bare attack word ("attack"/"hit"/"swing"/etc.) -- the generic
        attack_words match ran first and returned immediately, so only
        an attack-word-free phrasing ("use dragon breath on X") ever
        actually got classified as breath_weapon; the arguably more
        natural "attack X with dragon breath" fell through to a mundane
        weapon attack instead, silently discarding the racial ability.
        Same root cause and same fix shape as the "attack X with
        <spell>" bug fixed the same day -- the breath-weapon phrase
        check was moved ahead of the generic attack match instead of
        after it.
        """
        self.assertEqual(
            _keyword_fallback("Attack spider 1 with dragon breath", [])["action"], "breath_weapon"
        )
        self.assertEqual(
            _keyword_fallback("I attack the goblin with my breath weapon", [])["action"], "breath_weapon"
        )

    def test_bare_fight_classifies_as_attack_without_misfiring_on_the_fighter_class_name(self):
        """
        Real live bug (2026-08-07, topic-activity log: Coffee typed a
        bare "Fight" and got silent "chat" -- no reply, no game effect).
        COMBAT_START_WORDS only matches "fight the"/"fight some"/"let's
        fight" etc, never a standalone "Fight", and attack_words never
        included "fight" at all. Fixed with a whole-WORD check (not
        folded into attack_words' plain substring check) specifically
        because "fight" is a substring of "Fighter" -- one of this
        game's own 12 real class names -- a substring check would have
        misclassified any ordinary mention of the class as an attack.
        """
        self.assertEqual(_keyword_fallback("Fight", [])["action"], "attack")
        self.assertEqual(_keyword_fallback("Fight!", [])["action"], "attack")
        self.assertNotEqual(_keyword_fallback("I made a Fighter", [])["action"], "attack")
        self.assertNotEqual(_keyword_fallback("switch to my Fighter", [])["action"], "attack")

    def test_negated_fight_phrasing_never_misclassifies_as_attack(self):
        """
        Real live bug (2026-08-10, topic-activity log): "we dont have
        to fight" (half of a compound message also addressing an NPC
        by name, "Wait kess, we dont have to fight") matched the bare
        "fight" word rule above and returned 'attack' -- exactly
        backwards, a plea to AVOID combat, not start it.
        conditional_words already guarded hypothetical phrasing ("if
        X") but never negation. A real attack that happens to contain
        the word "not" elsewhere ("fight the goblin, not the spider")
        must still classify as attack -- this isn't a blanket "not"
        strip, just the specific negated-fight phrasings.
        """
        for text in ["we dont have to fight", "we don't have to fight", "no need to fight",
                     "we won't fight", "we refuse to fight", "we don't want to fight"]:
            self.assertNotEqual(_keyword_fallback(text, [])["action"], "attack", text)
        # A real attack containing "not" elsewhere must still work.
        self.assertEqual(_keyword_fallback("fight the goblin, not the spider", [])["action"], "attack")
        self.assertEqual(_keyword_fallback("lets fight", [])["action"], "attack")

    def test_bare_compass_direction_movement_no_longer_misclassifies_as_chat(self):
        """
        Real live bug (2026-08-10, topic-activity log): "Travel west"
        got silently misclassified as chat. move_words only recognized
        a movement verb paired with a NAMED destination ("travel to
        X"), never a bare compass direction with no place name --
        bot.py's _do_move already fully supports this (it re-derives
        the destination from the raw text via the current location's
        real "directions" map, campaign.json), the classifier just
        never routed compass-direction phrasing there. Deliberately a
        movement VERB + direction word, not the bare direction word
        alone -- "west" by itself is too likely to be part of
        something else (a name/sentence containing it).
        """
        for text in ["Travel west", "Go north", "Head south", "I want to travel east",
                     "let's move north", "ride west", "walk south", "march east"]:
            self.assertEqual(_keyword_fallback(text, [])["action"], "move", text)
        # A bare direction word with no movement verb, or the word
        # appearing as part of something else, must NOT false-positive.
        self.assertNotEqual(_keyword_fallback("west of here is dangerous", [])["action"], "move")
        self.assertNotEqual(_keyword_fallback("talk to Westley", [])["action"], "move")

    def test_break_open_routes_to_a_real_strength_check_not_passive_examine(self):
        """
        Real live bug (2026-08-10, topic-activity log): "Break open the
        barrel with a chalk symbol on it" got classified as a passive
        "examine" instead of a real strength skill_check. The examine
        path's own "open"-verb exclusion list (added 2026-07-19 for
        the sibling bug "Try opening the barrel...") already excludes
        "force open"/"break down"/"smash" specifically so the object
        can resist and needs forcing -- but never listed "break open",
        so it slipped through that exclusion. Fixed in two places: the
        exclusion list (so it stops being swallowed as examine) and the
        strength-ability trigger list (so it actually resolves as a
        real skill_check once it isn't).
        """
        result = _keyword_fallback("Break open the barrel with a chalk symbol on it", [])
        self.assertEqual(result["action"], "skill_check")
        self.assertEqual(result["ability"], "strength")
        # The sibling case this exclusion list already existed for
        # must keep working -- an unobstructed "open" is still just a
        # passive look, no forcing implied.
        self.assertEqual(_keyword_fallback("Try opening the barrel with a chalk symbol", [])["action"], "examine")

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

    def test_overtuned_monster_stat_multiplier_shrinks_only_a_too_strong_monster(self):
        """
        Real live bug (2026-08-10, per Coffee: "Make it so when these
        ai characters attack, they are the average party level. This
        one was clearly way out of its league."). Pure-function checks
        against overtuned_monster_stat_multiplier directly.
        """
        from rules.leveling import overtuned_monster_stat_multiplier
        # A level-1 party's medium budget (50) is nowhere close to a
        # 1800-xp monster -- floored at 0.2, never lower.
        self.assertAlmostEqual(overtuned_monster_stat_multiplier([1], 1800), 0.2)
        # A level-10 party (budget 1200) vs the same monster (1800) --
        # a real, non-floored ratio (1200/1800).
        self.assertAlmostEqual(overtuned_monster_stat_multiplier([10], 1800), 1200 / 1800)
        # One-directional: an UNDERtuned monster (xp well under budget)
        # is never buffed -- multiplier stays exactly 1.0.
        self.assertEqual(overtuned_monster_stat_multiplier([10], 50), 1.0)
        # No party / no real xp_reward -- safe no-op, never a crash or
        # a zero-division.
        self.assertEqual(overtuned_monster_stat_multiplier([], 1800), 1.0)
        self.assertEqual(overtuned_monster_stat_multiplier([1], 0), 1.0)

    async def test_start_combat_shrinks_an_overtuned_wild_monster_to_party_level(self):
        """
        Real end-to-end check: a level-1 party fighting bound_loom_warden
        (a real, genuinely strong non-boss campaign monster: 1800 xp,
        646 hp) must get a real, floored-down monster (0.2x = 129 hp),
        never the full, crushing template stats. armor_class and
        damage_dice stay UNCHANGED by design (only HP/damage/xp scale,
        per Coffee's own "way out of its league" report being about raw
        danger, not accuracy).
        """
        import sessions
        from unittest.mock import patch, AsyncMock
        sessions.end_session(-999)
        user_id = 900700
        make_basic_character(user_id, "Underdog", char_class="Fighter", current_location="crossroads_tavern")
        sink = []
        # Combat start rolls initiative -- if the enemy happens to go
        # first, _resolve_ai_turns would make it act immediately,
        # touching real Ollama narration. _maybe_send_monster_image is
        # unconditional (real Pollinations network fetch, genuinely
        # slow on a first-ever request for a seed/prompt -- confirmed
        # live, this alone hung the test past its own timeout). Both
        # patched to no-ops so this test only ever exercises the
        # deterministic enemy-construction stats it actually cares
        # about -- same "never touch a real network call in the
        # regression suite" convention as the spell/ability image
        # prompt tests above.
        with patch("bot._resolve_ai_turns", new=AsyncMock()), \
             patch("bot._maybe_send_monster_image", new=AsyncMock()):
            await bot._do_start_combat(FakeUpdate(user_id, "fight the loom warden", sink),
                                        monster_key="bound_loom_warden", count=1)
        session = sessions.get_session_for_user(-999, user_id)
        enemy = next(p for p in session.participants if p["telegram_user_id"] != user_id)
        import campaign_loader as cl
        template = cl.get_monster_template(bot.CAMPAIGN, "bound_loom_warden")
        self.assertEqual(enemy["hp_max"], round(template["hp_max"] * 0.2))
        self.assertEqual(enemy["hp_current"], enemy["hp_max"])
        self.assertLess(enemy["hp_max"], template["hp_max"])
        self.assertEqual(enemy["armor_class"], template["armor_class"])
        self.assertEqual(enemy["damage_dice"], template.get("damage_dice"))
        self.assertLess(enemy["xp_reward"], template.get("xp_reward", 0))
        sessions.end_session(-999)

    async def test_start_combat_never_shrinks_a_hand_placed_boss(self):
        """
        A story/world boss's difficulty spike is deliberate -- must
        NEVER be touched by overtuned_monster_stat_multiplier even
        when its xp_reward implies a level far above the party.
        the_unasked is a deliberately absurd secret superboss (1,000,000
        xp, 100,000,000 hp) -- if boss exclusion silently broke, this
        would be the case most likely to reveal it (a visibly wrong,
        still-floored-but-nonsensical HP number).
        """
        import sessions
        from unittest.mock import patch, AsyncMock, Mock
        sessions.end_session(-999)
        user_id = 900701
        make_basic_character(user_id, "Overmatched", char_class="Fighter", current_location="crossroads_tavern")
        sink = []
        # is_boss also triggers a real, separate Ollama call
        # (narrate_boss_intro, a sync function run via
        # asyncio.to_thread) that _resolve_ai_turns' own patch above
        # doesn't cover -- confirmed live via faulthandler.
        # dump_traceback_later after this exact test hung past its own
        # timeout: the stuck thread was genuinely blocked inside
        # requests.post from narrate_boss_intro. Patched with a plain
        # Mock (not AsyncMock), matching its real sync signature.
        with patch("bot._resolve_ai_turns", new=AsyncMock()), \
             patch("bot._maybe_send_monster_image", new=AsyncMock()), \
             patch("bot.narrate_boss_intro", new=Mock(return_value="A shadow falls.")):
            await bot._do_start_combat(FakeUpdate(user_id, "fight the unasked", sink),
                                        monster_key="the_unasked", count=1)
        session = sessions.get_session_for_user(-999, user_id)
        enemy = next(p for p in session.participants if p["telegram_user_id"] != user_id)
        import campaign_loader as cl
        template = cl.get_monster_template(bot.CAMPAIGN, "the_unasked")
        self.assertEqual(enemy["hp_max"], template["hp_max"])
        self.assertEqual(enemy["xp_reward"], template.get("xp_reward", 0))
        sessions.end_session(-999)

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
        db.update_character(bard_id, -999, level=2)
        party_id = db.create_party(user_id, -999)
        db.update_character(bard_id, -999, party_id=party_id)
        fighter = db.get_character(user_id, -999)
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
        db.update_character(user_id, -999, level=3)
        character = db.get_character(user_id, -999)
        spell = {"damage_dice": "3d6"}
        result = {"rolls": [1, 2, 6], "damage_dealt": 9}
        with patch("bot.roll", return_value=[5]):
            new_result = bot._apply_empowered_spell(user_id, character, spell, result)
        self.assertEqual(new_result["rolls"], [5, 5, 6])
        self.assertEqual(new_result["damage_dealt"], 9 + (5 - 1) + (5 - 2))

    def test_empowered_spell_only_once_per_rest(self):
        user_id = 900505
        make_basic_character(user_id, "Sparky2", char_class="Sorcerer")
        db.update_character(user_id, -999, level=3)
        character = db.get_character(user_id, -999)
        spell = {"damage_dice": "3d6"}
        result = {"rolls": [1, 1, 1], "damage_dealt": 3}
        first = bot._apply_empowered_spell(user_id, character, spell, result)
        self.assertNotEqual(first["rolls"], [1, 1, 1])
        second = bot._apply_empowered_spell(user_id, character, spell, dict(result))
        self.assertEqual(second["rolls"], [1, 1, 1], "already used this rest")

    def test_empowered_spell_requires_sorcerer_level_3(self):
        user_id = 900506
        make_basic_character(user_id, "Lowbie", char_class="Sorcerer")
        character = db.get_character(user_id, -999)
        spell = {"damage_dice": "3d6"}
        result = {"rolls": [1, 1, 1], "damage_dealt": 3}
        unchanged = bot._apply_empowered_spell(user_id, character, spell, result)
        self.assertEqual(unchanged["rolls"], [1, 1, 1])

        user_id2 = 900507
        make_basic_character(user_id2, "WrongClass", char_class="Fighter")
        db.update_character(user_id2, -999, level=5)
        character2 = db.get_character(user_id2, -999)
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
        # Fixed die value 4: level 1 = 1 sneak die (4) + weapon (4) = 8 pre-scale,
        # level 10 = 5 sneak dice (20) + weapon (4) = 24 pre-scale -- a +16 delta
        # before any player-power scaling. The 2026-07-26 rebalance (per Coffee:
        # "rebalance everything... all skills and abilities") multiplies a real
        # character's own damage by power_scale_ratio(level, rebirth_count), so
        # each side now also scales by its OWN attacker's ratio -- recomputed
        # here via the real function rather than hardcoded, so this stays valid
        # if the ratio formula itself is ever retuned again.
        from rules.leveling import power_scale_ratio
        expected_low = int(8 * power_scale_ratio(1, 0))
        expected_high = int(24 * power_scale_ratio(10, 0))
        self.assertEqual(result_low["damage_dealt"], expected_low)
        self.assertEqual(result_high["damage_dealt"], expected_high)

    # -- Board quest completion bugs (2026-07-16, task #93) -------------
    def test_completed_board_quest_frees_a_slot_for_a_fresh_one(self):
        import board_quests as board_quests_module
        location_id = "stonearch_bridge"
        # Simulate a full day already posted, with one already completed --
        # previously this permanently occupied a slot for the rest of the
        # day instead of a fresh quest ever being generated to replace it.
        stale = db.create_board_quest(
            location_id, -999, board_quests_module._day_key(), "Old bounty", "...", None,
            "defeat_monster", "goblin", 2, 50, 20,
        )
        db.complete_board_quest(stale["board_quest_id"])

        active = board_quests_module.get_or_generate_board_quests(bot.CAMPAIGN, location_id, -999)
        self.assertEqual(len(active), board_quests_module.DAILY_BOARD_QUEST_COUNT)
        self.assertTrue(all(not q.get("completed_at") for q in active),
                         "a completed quest should never be returned for display")

    def test_board_quests_completed_counter_increments(self):
        user_id = 900490
        make_basic_character(user_id, "Bountyhunter")
        self.assertEqual(db.get_character(user_id, -999)["board_quests_completed"], 0)
        db.increment_board_quests_completed(user_id, -999)
        db.increment_board_quests_completed(user_id, -999)
        self.assertEqual(db.get_character(user_id, -999)["board_quests_completed"], 2)

    async def test_check_quests_shows_board_quest_completion_count(self):
        user_id = 900491
        make_basic_character(user_id, "Bountyhunter2")
        db.increment_board_quests_completed(user_id, -999)
        sink = []
        await bot._do_check_quests(FakeUpdate(user_id, "check quests", sink))
        self.assertIn("Board quests completed", " ".join(sink))

    def test_board_quests_are_isolated_per_chat(self):
        """
        Multi-tenant scaling Phase 4d (2026-08-06): board_quests
        generation/read functions now take a real chat_id, so the same
        location in two different tenant chats gets genuinely
        independent boards -- not one shared set of rows silently
        cross-visible/cross-acceptable between chats.
        """
        import board_quests as board_quests_module
        location_id = "stonearch_bridge"
        chat_a, chat_b = -999, -998

        quests_a = board_quests_module.get_or_generate_all_board_quests(bot.CAMPAIGN, location_id, chat_a)
        quests_b = board_quests_module.get_or_generate_all_board_quests(bot.CAMPAIGN, location_id, chat_b)
        self.assertTrue(quests_a)
        self.assertTrue(quests_b)
        ids_a = {q["board_quest_id"] for q in quests_a}
        ids_b = {q["board_quest_id"] for q in quests_b}
        self.assertFalse(ids_a & ids_b, "the two chats' boards shared a real row -- not actually isolated")

        user_a = 900700
        make_basic_character(user_a, "TenantA", current_location=location_id, chat_id=chat_a)
        db.accept_board_quest(quests_a[0]["board_quest_id"], user_a, chat_a)
        self.assertEqual(
            len(db.get_accepted_board_quests_for_user(user_a, chat_a)), 1,
        )
        self.assertEqual(
            len(db.get_accepted_board_quests_for_user(user_a, chat_b)), 0,
            "a quest accepted in chat A leaked into chat B's view for the same telegram_user_id",
        )

    def test_board_quest_chat_id_backfill_heals_bad_null_rows_from_the_create_board_quest_bug(self):
        """
        Real live bug found 2026-08-06 during this exact retrofit:
        create_board_quest never wrote the chat_id column at all after
        Phase 4a added it, so every quest generated since then --
        including 2 real, currently-accepted quests for an active
        player on the live DB -- had chat_id stuck at NULL. Fixed at
        the source (create_board_quest now always writes it) plus a
        one-time, unconditional (safe-to-rerun) backfill in init_db()
        for whatever bad rows the bug already left behind.
        """
        with db.get_connection() as conn:
            conn.execute(
                "INSERT INTO board_quests (location_id, day_key, title, description, objective_type, "
                "objective_target, objective_count, reward_xp, reward_gold, generated_at, tier) "
                "VALUES ('stonearch_bridge', '2026-08-06', 'Pre-fix stray quest', '...', "
                "'defeat_monster', 'goblin', 2, 50, 20, '2026-08-06T00:00:00+00:00', 'daily')"
            )
            stray_id = conn.execute(
                "SELECT board_quest_id FROM board_quests WHERE title = 'Pre-fix stray quest'"
            ).fetchone()[0]
            self.assertIsNone(
                conn.execute("SELECT chat_id FROM board_quests WHERE board_quest_id = ?", (stray_id,)).fetchone()[0]
            )

        db.init_db()

        with db.get_connection() as conn:
            healed_chat_id = conn.execute(
                "SELECT chat_id FROM board_quests WHERE board_quest_id = ?", (stray_id,)
            ).fetchone()[0]
        self.assertEqual(healed_chat_id, config.TELEGRAM_CHAT_ID)

    # -- Real player-driven ASI level-up (2026-07-16, per Coffee) -------
    def test_level_up_phrasing_classified_correctly(self):
        for text in ("level up", "I want to level up", "Level up!"):
            self.assertEqual(_keyword_fallback(text, [])["action"], "level_up", text)

    async def test_add_xp_banks_asi_points_instead_of_auto_applying(self):
        user_id = 900492
        make_basic_character(user_id, "Leveler")
        before_strength = db.get_character(user_id, -999)["strength"]
        after = db.add_xp(user_id, -999, 2700)  # crosses level 4, a real ASI level
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
        db.update_character(user_id, -999, pending_asi_points=2)
        sink = []
        await bot._do_level_up(FakeUpdate(user_id, "level up", sink), "level up")
        self.assertIn(user_id, bot._chat_scoped_set(bot._PENDING_ASI_CHOICE, -999))
        self.assertTrue(any("Which ability" in m for m in sink))

        before_con = db.get_character(user_id, -999)["constitution"]
        sink2 = []
        await bot._do_level_up(FakeUpdate(user_id, "constitution", sink2), "constitution")
        after = db.get_character(user_id, -999)
        self.assertEqual(after["constitution"], before_con + 2)
        self.assertEqual(after["pending_asi_points"], 0)
        self.assertNotIn(user_id, bot._chat_scoped_set(bot._PENDING_ASI_CHOICE, -999))

    async def test_level_up_auto_assigns_to_class_primary_ability(self):
        user_id = 900495
        make_basic_character(user_id, "Leveler4")  # Fighter -> primary ability strength
        db.update_character(user_id, -999, pending_asi_points=4)
        before_strength = db.get_character(user_id, -999)["strength"]
        sink = []
        await bot._do_level_up(FakeUpdate(user_id, "level up, auto", sink), "level up, auto")
        after = db.get_character(user_id, -999)
        self.assertEqual(after["strength"], before_strength + 4)
        self.assertEqual(after["pending_asi_points"], 0)
        self.assertNotIn(user_id, bot._chat_scoped_set(bot._PENDING_ASI_CHOICE, -999))

    async def test_level_up_single_ability_choice_caps_at_two_and_keeps_remainder(self):
        user_id = 900496
        make_basic_character(user_id, "Leveler5")
        db.update_character(user_id, -999, pending_asi_points=4)
        before_wis = db.get_character(user_id, -999)["wisdom"]
        sink = []
        await bot._do_level_up(FakeUpdate(user_id, "wisdom", sink), "wisdom")
        after = db.get_character(user_id, -999)
        self.assertEqual(after["wisdom"], before_wis + 2)
        self.assertEqual(after["pending_asi_points"], 2)
        self.assertTrue(any("still have 2" in m for m in sink))

    async def test_check_sheet_shows_pending_asi_points(self):
        user_id = 900497
        make_basic_character(user_id, "Leveler6")
        db.update_character(user_id, -999, pending_asi_points=2)
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
        board_quests_module.get_or_generate_board_quests(bot.CAMPAIGN, location_id, -999)
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
        character = db.get_character(user_id, -999)
        self.assertEqual(character["description"], "A grizzled dwarf who never smiles")

    async def test_set_description_with_no_content_prompts_then_saves_on_next_message(self):
        user_id = 900509
        make_basic_character(user_id, "Sable")
        sink = []
        await bot._do_set_description(
            FakeUpdate(user_id, "I'd like to add a character description to my player", sink),
            "I'd like to add a character description to my player",
        )
        self.assertIsNone(db.get_character(user_id, -999)["description"])
        self.assertIn(user_id, bot._chat_scoped_set(bot._PENDING_DESCRIPTION, -999))
        self.assertIn("what would you like your character's description", sink[-1])

        sink2 = []
        await bot._do_set_description(
            FakeUpdate(user_id, "A quiet elven ranger who speaks rarely but shoots true.", sink2),
            "A quiet elven ranger who speaks rarely but shoots true.",
            from_prompt=True,
        )
        self.assertEqual(
            db.get_character(user_id, -999)["description"],
            "A quiet elven ranger who speaks rarely but shoots true.",
        )

    async def test_set_description_skip_leaves_it_blank(self):
        user_id = 900510
        make_basic_character(user_id, "Skippy")
        sink = []
        await bot._do_set_description(FakeUpdate(user_id, "skip", sink), "skip", from_prompt=True)
        self.assertIsNone(db.get_character(user_id, -999)["description"])

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
        character = db.update_character(user_id, -999, description="A former sellsword.")
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
        self.assertEqual(db.get_character(user_id, -999)["pronouns"], "she/her")

    async def test_set_pronouns_with_no_content_prompts_then_saves_on_resume(self):
        user_id = 900531
        make_basic_character(user_id, "Vex")
        sink = []
        await bot._do_set_pronouns(FakeUpdate(user_id, "I'd like to set my pronouns", sink), "I'd like to set my pronouns")
        self.assertIsNone(db.get_character(user_id, -999)["pronouns"])
        self.assertIn(user_id, bot._chat_scoped_set(bot._PENDING_PRONOUNS, -999))

        sink2 = []
        await bot._do_set_pronouns(FakeUpdate(user_id, "he/him", sink2), "he/him", from_prompt=True)
        self.assertEqual(db.get_character(user_id, -999)["pronouns"], "he/him")

    async def test_set_pronouns_skip_leaves_it_unset(self):
        user_id = 900532
        make_basic_character(user_id, "Ambiguous")
        sink = []
        await bot._do_set_pronouns(FakeUpdate(user_id, "skip", sink), "skip", from_prompt=True)
        self.assertIsNone(db.get_character(user_id, -999)["pronouns"])

    def test_set_pronouns_trigger_recognized(self):
        from ai.intent_parser import _keyword_fallback
        result = _keyword_fallback("set my pronouns to she/her", known_npc_names=[])
        self.assertEqual(result["action"], "set_pronouns")

    def test_character_sheet_shows_pronouns_when_present(self):
        user_id = 900533
        make_basic_character(user_id, "Told")
        character = db.update_character(user_id, -999, pronouns="they/them")
        sheet = bot._format_character_sheet(character)
        self.assertIn("they/them", sheet)

    def test_narration_prompt_uses_real_pronouns_not_a_guess(self):
        from ai.dm_agent import _build_skill_check_prompt
        user_id = 900534
        make_basic_character(user_id, "Set")
        character = db.update_character(user_id, -999, pronouns="he/him")
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
        character = db.update_character(user_id, -999, pending_asi_points=2)
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
        updated = db.get_character(user_id, -999)
        self.assertIn("goblin", updated["known_monsters"])
        sessions.end_session(-999)

        sink2 = []
        await bot._do_bestiary(FakeUpdate(user_id, "bestiary", sink2))
        combined = " ".join(sink2)
        self.assertIn("Goblin", combined)
        self.assertIn("HP", combined)
        self.assertIn("XP", combined)

    async def test_monster_image_not_duplicated_when_enemy_attacks_first(self):
        # Real live bug (2026-08-05, Coffee, Development topic screenshot:
        # "I noticed every now and then it posts two images. Is this a
        # glitch?"): when an enemy wins initiative and takes the very
        # first action of the fight, _resolve_ai_turns' own "show the
        # monster's art again the moment it attacks" flavor fired
        # immediately after _do_start_combat's own encounter-start image,
        # posting an identical duplicate seconds apart. Low dexterity
        # here + a patched roll_d20 guarantees the goblin wins initiative
        # and attacks on the very first turn, reproducing the exact
        # reported scenario.
        from unittest.mock import patch
        import sessions
        user_id = 900515
        sessions.end_session(-999)
        character = make_basic_character(
            user_id, "Slowpoke", current_location="crossroads_tavern",
            ability_scores={"strength": 15, "dexterity": 8, "constitution": 13,
                             "intelligence": 10, "wisdom": 10, "charisma": 10},
        )
        sink = []
        with patch("bot._get_combat_eligible_party_members", return_value=[character]), \
             patch("rules.combat.roll_d20", return_value=10):
            await bot._do_start_combat(FakeUpdate(user_id, "fight a goblin", sink), monster_key="goblin", count=1)
        photo_count = sum(1 for line in sink if line.startswith("<photo:") and "Goblin" in line)
        self.assertEqual(photo_count, 1, f"expected exactly 1 Goblin image, got {photo_count}: {sink}")
        sessions.end_session(-999)

    async def test_monster_image_shown_once_for_multiple_identical_enemies(self):
        """
        Real live feedback (2026-08-09, Coffee): "if you have three of
        the same enemy only show one image of them - its abit spammy."
        The old per-PARTICIPANT dedup (_opening_image_shown) only ever
        stopped the immediate same-instance duplicate the sibling test
        above covers -- it did nothing for 3 genuinely DIFFERENT
        participants sharing the same monster_key, each re-posting the
        identical picture on their own turn. Forces all 3 Giant Spiders
        to take a real attack turn back to back (turn order rigged so
        the human goes first, then all 3 spiders resolve consecutively
        via _resolve_ai_turns) and confirms the art posts exactly once
        total for the whole fight, not once per spider.
        """
        from unittest.mock import patch
        import sessions
        sessions.end_session(-999)
        human_id = 900931
        make_basic_character(human_id, "SpamTester", current_location="crossroads_tavern", hp_max=300)
        human = db.get_character(human_id, -999)
        spiders = [
            {
                "telegram_user_id": -700700 - i, "name": f"Giant Spider {i + 1}", "is_ai": True,
                "hp_current": 108, "hp_max": 108, "armor_class": 14,
                "strength": 14, "dexterity": 16, "proficiency_bonus": 3,
                "monster_key": "giant_spider", "xp_reward": 200, "conditions": [],
            }
            for i in range(3)
        ]
        sides = {human_id: "party"}
        for spider in spiders:
            sides[spider["telegram_user_id"]] = "enemy"
        session = sessions.start_session(-999, [human] + spiders, sides)
        session.turn_order = [human_id] + [s["telegram_user_id"] for s in spiders]
        session.current_turn_index = 1  # first spider's turn is up next, human already "went"

        sink = []
        update = FakeUpdate(human_id, "irrelevant", sink)

        def fake_get(url, timeout=None):
            return SimpleNamespace(status_code=200, headers={"content-type": "image/jpeg"})

        with patch("bot.requests.get", side_effect=fake_get), \
             patch("bot.narrate_action", return_value="The spider strikes."):
            await bot._resolve_ai_turns(update, session)

        photo_markers = [line for line in sink if line.startswith("<photo:") and "Giant Spider" in line]
        self.assertEqual(
            len(photo_markers), 1,
            f"expected exactly 1 monster image for 3 identical spiders, got {len(photo_markers)}: {sink}",
        )
        sessions.end_session(-999)

    async def test_multiattack_announcement_not_repeated_on_a_crash_and_retry(self):
        # Real live bug (2026-08-05, Coffee, Development topic screenshot:
        # "This was a double prompt also" -- "Grask Emberscale has 2
        # attacks this turn!" posted twice back to back, with only ONE
        # real attack result following): a transient failure partway
        # through resolving an AI's multiattack turn (e.g. a DB write
        # under this box's known disk-I/O contention) can leave
        # _resolve_ai_turns having already announced the multiattack
        # count for a turn that never actually advanced -- the very next
        # retry of that same still-current turn re-announced it.
        # Verified via a forced RuntimeError on the first _sync_player_
        # to_db call (simulating exactly that transient failure);
        # confirms the retry still completes the turn correctly.
        from unittest.mock import patch
        import sessions
        sessions.end_session(-999)
        grask = {
            "telegram_user_id": -700501, "name": "Grask Emberscale", "is_ai": True,
            "char_class": "Fighter", "level": 5, "race": "Half-Orc",
            "strength": 16, "dexterity": 12, "constitution": 14,
            "hp_current": 40, "hp_max": 40, "armor_class": 15,
            "proficiency_bonus": 3, "damage_dice": "1d12", "damage_bonus": 3,
            "damage_type": "physical", "chat_id": -999, "conditions": [],
        }
        human_id = 900902
        make_basic_character(human_id, "RealPlayer2", current_location="crossroads_tavern")
        human = db.get_character(human_id, -999)
        goblin = {
            "telegram_user_id": -700503, "name": "Goblin X", "is_ai": True,
            "hp_current": 200, "hp_max": 200, "armor_class": 1,
            "strength": 8, "dexterity": 14, "proficiency_bonus": 2,
            "monster_key": "goblin", "xp_reward": 10, "conditions": [],
        }
        session = sessions.start_session(
            -999, [grask, human, goblin], {-700501: "party", human_id: "party", -700503: "enemy"}
        )
        # is_combat_over() checks turn_order membership by side, so the
        # goblin must stay in turn_order for combat to count as ongoing --
        # placed after the human so the loop naturally stops at the
        # human's turn (not is_ai), isolating exactly Grask's one turn.
        session.turn_order = [-700501, human_id, -700503]
        session.current_turn_index = 0

        sink = []
        update = FakeUpdate(-700501, "irrelevant", sink)

        async def fast_post_narrated(update, character, action_text, result, session, **kwargs):
            sink.append(f"<narrated attack result, hit={result.get('hit')}>")

        call_count = {"n": 0}
        real_sync = bot._sync_player_to_db

        def flaky_sync(character):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("simulated transient DB failure")
            return real_sync(character)

        with patch("bot._sync_player_to_db", side_effect=flaky_sync), \
             patch("bot._post_narrated", side_effect=fast_post_narrated):
            with self.assertRaises(RuntimeError):
                await bot._resolve_ai_turns(update, session)

        self.assertEqual(session.current_participant_id(), -700501, "turn should still be stuck on Grask after the crash")
        self.assertEqual(sum(1 for line in sink if "has 2 attacks" in line), 1)

        with patch("bot._post_narrated", side_effect=fast_post_narrated):
            await bot._resolve_ai_turns(update, session)

        self.assertEqual(
            sum(1 for line in sink if "has 2 attacks" in line), 1,
            f"multiattack announcement should appear exactly once total across the crash+retry, got: {sink}",
        )
        self.assertEqual(session.current_participant_id(), human_id, "Grask's turn should have genuinely advanced after the successful retry")
        sessions.end_session(-999)

    async def test_itemview_falls_back_to_text_when_image_generation_fails(self):
        # Real live bug (2026-08-05, Coffee, Development topic screenshot:
        # "i clicked to view the item and its not processing" -- log
        # confirmed two real image-generation failures for the exact
        # item, "gi21", he tapped): _send_generated_image used to swallow
        # a failed generate/send with only a log line, silently dropping
        # BOTH the picture and the stats/action-buttons caption that
        # would have gone with it -- a real request the player just
        # tapped a button for, left with no response at all.
        from unittest.mock import patch
        uid = 900530
        make_basic_character(uid, "ImageFailTester")
        item_id = db.create_item_instance(
            item_type="weapon", name="Sturdy Greataxe", rarity="common", price=50,
            base_stats={"type": "weapon", "damage_dice": "1d12"}, affixes=[],
        )
        db.add_item(uid, -999, item_id, 1)
        sink = []
        update = FakeCallbackUpdate(uid, f"itemview|show|{item_id}", sink)

        async def failing_send(*args, **kwargs):
            return False

        with patch("bot._send_generated_image", side_effect=failing_send):
            await bot.itemview_callback(update, context=None)

        self.assertTrue(
            any("Sturdy Greataxe" in line for line in sink),
            f"expected a text fallback naming the item when the image fails, got: {sink}",
        )

    async def test_turn_prompt_not_repeated_on_a_resolve_ai_turns_reentry(self):
        # Real live bug (2026-08-05, Coffee, Development topic screenshot:
        # "What os happening?! I have two attacks now?!"): the "Round N
        # -- it's now X's turn!" prompt posted twice back to back with
        # nothing in between. Same root cause as the multiattack-
        # announcement bug above: whatever handler originally triggered
        # an AI-turn-resolution chain can get retried after a downstream
        # failure, and since the human's turn never actually advanced,
        # the retry re-enters _resolve_ai_turns and, with no AI turns
        # left to resolve, goes straight back to "not is_ai" and used to
        # re-announce the same prompt.
        import sessions
        sessions.end_session(-999)
        human_id = 900921
        make_basic_character(human_id, "Ravenloft2", current_location="crossroads_tavern")
        human = db.get_character(human_id, -999)
        goblin = {
            "telegram_user_id": -700602, "name": "Goblin 2", "is_ai": True,
            "hp_current": 25, "hp_max": 25, "armor_class": 10,
            "strength": 8, "dexterity": 14, "proficiency_bonus": 2,
            "monster_key": "goblin", "xp_reward": 10, "conditions": [],
        }
        session = sessions.start_session(-999, [human, goblin], {human_id: "party", -700602: "enemy"})
        session.turn_order = [human_id, -700602]
        session.current_turn_index = 0

        sink = []
        update = FakeUpdate(human_id, "irrelevant", sink)

        await bot._resolve_ai_turns(update, session)
        self.assertEqual(sum(1 for line in sink if "What do you do" in line), 1)

        # Simulated re-entry: the same still-current, un-advanced human
        # turn gets resolved again.
        await bot._resolve_ai_turns(update, session)
        self.assertEqual(
            sum(1 for line in sink if "What do you do" in line), 1,
            f"turn prompt should appear exactly once total across the re-entry, got: {sink}",
        )
        self.assertEqual(session.current_participant_id(), human_id)
        sessions.end_session(-999)

    async def test_human_attack_multiattack_announcement_not_repeated_on_a_crash_and_retry(self):
        # Real gap found in a follow-up audit (2026-08-05) after fixing
        # the AI-turn version of this bug: _do_attack's OWN "X has N
        # attacks this turn!" line (Extra Attack/Action Surge/Flurry of
        # Blows, for a REAL human player) had no guard at all, unlike
        # its twin in _resolve_ai_turns fixed earlier the same day.
        # Same crash-then-retry root cause (a downstream failure, e.g.
        # this box's known disk-I/O contention, leaves the turn stuck
        # un-advanced; the next retry re-announces it).
        from unittest.mock import patch
        import sessions
        sessions.end_session(-999)
        uid = 900951
        make_basic_character(uid, "Fighter5b", char_class="Fighter", current_location="crossroads_tavern")
        db.update_character(uid, -999, level=5)  # real Extra Attack (2 attacks/turn)
        character = db.get_character(uid, -999)
        character["telegram_user_id"] = uid
        goblin = {
            "telegram_user_id": -700702, "name": "Goblin Y", "is_ai": True,
            "hp_current": 200, "hp_max": 200, "armor_class": 1,
            "strength": 8, "dexterity": 14, "proficiency_bonus": 2,
            "monster_key": "goblin", "xp_reward": 10, "conditions": [],
        }
        session = sessions.start_session(-999, [character, goblin], {uid: "party", -700702: "enemy"})
        session.turn_order = [uid, -700702]
        session.current_turn_index = 0

        sink = []
        update = FakeUpdate(uid, "I attack", sink)

        call_count = {"n": 0}
        real_sync = bot._sync_player_to_db

        def flaky_sync(char):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("simulated transient DB failure")
            return real_sync(char)

        with patch("bot._sync_player_to_db", side_effect=flaky_sync):
            with self.assertRaises(RuntimeError):
                await bot._do_attack(update, "I attack")

        self.assertEqual(sum(1 for line in sink if "has 2 attacks" in line), 1)

        await bot._do_attack(update, "I attack")
        self.assertEqual(
            sum(1 for line in sink if "has 2 attacks" in line), 1,
            f"multiattack announcement should appear exactly once total across the crash+retry, got: {sink}",
        )
        sessions.end_session(-999)

    async def test_boss_decision_flavor_not_repeated_on_a_crash_and_retry(self):
        # Same family of bug, same audit (2026-08-05): the boss "sizing
        # up its target" flavor line in _resolve_ai_turns had no guard
        # either, unlike its neighbors (enrage_warned/_opening_image_
        # shown/_multiattack_announced right around it).
        from unittest.mock import patch
        import sessions
        sessions.end_session(-999)
        boss = {
            "telegram_user_id": -700802, "name": "Boss Y", "is_ai": True, "is_boss": True,
            "hp_current": 100, "hp_max": 100, "armor_class": 15,
            "strength": 16, "dexterity": 12, "proficiency_bonus": 3,
            "monster_key": "goblin", "xp_reward": 50, "conditions": [],
        }
        human_id = 900952
        make_basic_character(human_id, "Ravenloft4", current_location="crossroads_tavern")
        session = sessions.start_session(-999, [boss, db.get_character(human_id, -999)], {-700802: "enemy", human_id: "party"})
        session.turn_order = [-700802, human_id]
        session.current_turn_index = 0

        sink = []
        update = FakeUpdate(-700802, "irrelevant", sink)

        async def fast_post_narrated(update, character, action_text, result, session, **kwargs):
            sink.append(f"<narrated attack result, hit={result.get('hit')}>")

        call_count = {"n": 0}
        real_sync = bot._sync_player_to_db

        def flaky_sync(char):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("simulated transient DB failure")
            return real_sync(char)

        with patch("bot._sync_player_to_db", side_effect=flaky_sync), \
             patch("bot._post_narrated", side_effect=fast_post_narrated), \
             patch("bot.narrate_boss_decision", return_value="sizes up its prey"):
            with self.assertRaises(RuntimeError):
                await bot._resolve_ai_turns(update, session)

        self.assertEqual(sum(1 for line in sink if "sizes up its prey" in line), 1)

        with patch("bot._post_narrated", side_effect=fast_post_narrated), \
             patch("bot.narrate_boss_decision", return_value="sizes up its prey"):
            await bot._resolve_ai_turns(update, session)

        self.assertEqual(
            sum(1 for line in sink if "sizes up its prey" in line), 1,
            f"boss-decision line should appear exactly once total across the crash+retry, got: {sink}",
        )
        sessions.end_session(-999)

    async def test_ai_companions_never_learn_monsters_for_the_human(self):
        # mark_known_monster is only ever called for non-AI party members
        # in _do_start_combat -- an AI companion in the same fight must
        # not somehow cause the monster to show up on ITS OWN row (it has
        # no bestiary of its own to check, but this guards the loop's
        # is_ai filter against a future regression).
        ai_companion = db.create_ai_companion(
            -999, "Buddy", "Human", "Fighter",
            {"strength": 15, "dexterity": 14, "constitution": 13, "intelligence": 10, "wisdom": 10, "charisma": 10},
            hp_max=12, armor_class=15, gold=0, inventory={},
        )
        result = db.mark_known_monster(ai_companion["telegram_user_id"], -999, "goblin")
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
        char = db.get_character(user_id, -999)
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
        char = db.get_character(user_id, -999)
        self.assertEqual(char["inventory"].get("healing_potion"), 1)

    async def test_multi_item_give_hands_over_every_named_item(self):
        giver_id, recipient_id = 900523, 900524
        make_basic_character(giver_id, "Giver", current_location="market_row")
        make_basic_character(recipient_id, "Receiver", current_location="market_row")
        db.add_item(giver_id, -999, "torch", 5)
        db.add_item(giver_id, -999, "shears", 2)
        sink = []
        await bot._do_give_item(FakeUpdate(giver_id, "", sink), "give 3 torches and 2 shears to Receiver")
        giver = db.get_character(giver_id, -999)
        recipient = db.get_character(recipient_id, -999)
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
        db.add_item(user_id, -999, "woodcutters_axe", 1)
        bq = db.create_board_quest(
            "whispering_wood", -999, "2026-07-17", "A supply run for Wood",
            "Bring wood back to the board.", None, "gather_material", "wood", 1, 50, 20,
        )
        db.accept_board_quest(bq["board_quest_id"], user_id, -999)

        sink = []
        with patch("bot.roll_ability_check", return_value={
            "raw_roll": 20, "modifier": 0, "proficiency": 0, "total": 20,
        }), patch("bot.narrate_skill_check", return_value="You chop the timber cleanly."):
            await bot._do_gather(FakeUpdate(user_id, "", sink), "Use my axe and chop lumber")

        updated = db.get_active_board_quest("whispering_wood", -999, "2026-07-17")
        self.assertIsNotNone(updated["completed_at"])
        self.assertEqual(db.get_character(user_id, -999)["gold"], 70)
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
        db.add_xp(900940, -999, 500)
        make_basic_character(900941, "LastPlace", gold=10)
        db.add_xp(900941, -999, 50)
        db.create_ai_companion(
            chat_id=-999, name="CombatOnly", race="Human", char_class="Fighter",
            ability_scores={"strength": 15, "dexterity": 14, "constitution": 13,
                             "intelligence": 10, "wisdom": 10, "charisma": 10},
            hp_max=12, armor_class=15, gold=0, inventory={},
        )
        ranked = db.get_leaderboard(-999, limit=10)
        names = [c["name"] for c in ranked]
        self.assertLess(names.index("TopDog"), names.index("LastPlace"))
        self.assertNotIn("CombatOnly", names)

    def test_leaderboard_find_by_name_and_list_active_players_are_chat_scoped(self):
        """
        Multi-tenant scaling Phase 4e (2026-08-06): get_leaderboard,
        find_character_by_name, and list_all_active_real_players all
        now take a real chat_id -- two different tenant chats can have
        same-named characters and completely separate leaderboards/
        player rosters without ever leaking into each other.
        """
        # 901100/901101 (not 900950/900951): the 9009xx range is already
        # used by several other, unrelated tests earlier in this same
        # class -- confirmed live via a real full-suite run that a bare
        # ID collision (900951 already existed as a genuinely different
        # character in chat_a from an earlier test) produced a false
        # failure here, since chat_a really did already have that exact
        # user_id active, just as a different character. Not a bug in
        # get_leaderboard/list_all_active_real_players themselves -- the
        # queries were correctly scoped -- purely a test-isolation bug
        # in this test's own hardcoded IDs.
        chat_a, chat_b = -999, -997
        make_basic_character(901100, "SameName", chat_id=chat_a, gold=10)
        db.add_xp(901100, chat_a, 999)
        make_basic_character(901101, "SameName", chat_id=chat_b, gold=10)
        db.add_xp(901101, chat_b, 1)

        found_a = db.find_character_by_name("SameName", chat_a)
        found_b = db.find_character_by_name("SameName", chat_b)
        self.assertEqual(found_a["telegram_user_id"], 901100)
        self.assertEqual(found_b["telegram_user_id"], 901101)

        ranked_a = db.get_leaderboard(chat_a, limit=10)
        ranked_b = db.get_leaderboard(chat_b, limit=10)
        self.assertIn(901100, [c["telegram_user_id"] for c in ranked_a])
        self.assertNotIn(901101, [c["telegram_user_id"] for c in ranked_a])
        self.assertIn(901101, [c["telegram_user_id"] for c in ranked_b])
        self.assertNotIn(901100, [c["telegram_user_id"] for c in ranked_b])

        players_a = db.list_all_active_real_players(chat_a)
        players_b = db.list_all_active_real_players(chat_b)
        self.assertIn(901100, [c["telegram_user_id"] for c in players_a])
        self.assertNotIn(901101, [c["telegram_user_id"] for c in players_a])
        self.assertIn(901101, [c["telegram_user_id"] for c in players_b])
        self.assertNotIn(901100, [c["telegram_user_id"] for c in players_b])

    def test_create_party_writes_chat_id_and_backfill_heals_bad_null_rows(self):
        """
        Real live bug found 2026-08-06 during this same retrofit pass:
        create_party already took chat_id as a parameter but never
        wrote it into the INSERT, so a real party on the live DB ended
        up with chat_id NULL. Fixed at the source plus a matching
        one-time (safe-to-rerun) backfill in init_db(), same shape as
        the board_quests fix.
        """
        leader_id = 901102
        make_basic_character(leader_id, "PartyLeader", chat_id=-999)
        party_id = db.create_party(leader_id, -999)
        with db.get_connection() as conn:
            row = conn.execute("SELECT chat_id FROM parties WHERE party_id = ?", (party_id,)).fetchone()
        self.assertEqual(row["chat_id"], -999)

        with db.get_connection() as conn:
            conn.execute(
                "INSERT INTO parties (created_by, created_at) VALUES (?, ?)",
                (leader_id, "2026-08-06T00:00:00+00:00"),
            )
            stray_id = conn.execute(
                "SELECT party_id FROM parties WHERE created_by = ? AND chat_id IS NULL", (leader_id,)
            ).fetchone()[0]

        db.init_db()

        with db.get_connection() as conn:
            healed = conn.execute("SELECT chat_id FROM parties WHERE party_id = ?", (stray_id,)).fetchone()[0]
        self.assertEqual(healed, config.TELEGRAM_CHAT_ID)

    async def test_idle_warning_fires_independently_per_chat_for_the_same_user(self):
        """
        Multi-tenant scaling gap found 2026-08-06 during this same
        retrofit pass: _IDLE_WARNED was keyed on bare telegram_user_id,
        with no chat_id at all -- the same real person idle in TWO
        different tenant chats at once would only ever get warned in
        whichever chat's check ran first; the second chat's warning was
        silently suppressed because the set already "knew" that user_id.
        Fixed by keying on the (telegram_user_id, chat_id) pair instead.
        """
        from datetime import timedelta
        from unittest.mock import AsyncMock

        user_id = 900953
        chat_a, chat_b = -999, -996
        make_basic_character(user_id, "DualTenant", chat_id=chat_a, current_location="crossroads_tavern")
        make_basic_character(user_id, "DualTenant", chat_id=chat_b, current_location="crossroads_tavern")
        idle_since = (datetime.now(timezone.utc) - timedelta(seconds=bot.IDLE_WARNING_SECONDS + 60)).isoformat()
        db.update_character(user_id, chat_a, last_active_at=idle_since)
        db.update_character(user_id, chat_b, last_active_at=idle_since)
        bot._IDLE_WARNED.discard((user_id, chat_a))
        bot._IDLE_WARNED.discard((user_id, chat_b))

        mock_bot = AsyncMock()
        await bot._check_idle_characters(mock_bot)

        warned_chat_ids = {call.kwargs.get("chat_id") for call in mock_bot.send_message.await_args_list}
        self.assertIn(chat_a, warned_chat_ids)
        self.assertIn(chat_b, warned_chat_ids)
        self.assertIn((user_id, chat_a), bot._IDLE_WARNED)
        self.assertIn((user_id, chat_b), bot._IDLE_WARNED)

    async def test_idle_timeout_in_combat_forces_a_real_attack_not_a_silent_pass(self):
        """
        Real live bug, reported twice via dev-topic screenshots
        (2026-08-08): "If Charvenna timed out, they should've attacked
        they shouldn't be idle until after the battle is completed" and
        later "First, it said I missed my turn... If my character timed
        out, it should default in the attack." The screenshot for the
        second report showed the exact OLD message this fixes: "Pan is
        idle — turn passed" with no attack at all, followed by Coffee
        manually typing "Fight" to unstick it. This backstop (60-minute
        clock, only ever reached when _check_combat_timeouts's own much
        faster mechanism somehow hasn't already caught it -- see that
        function's own starvation fix, tested separately) must now
        force a real attack, same as the fast path already does, not
        silently pass the turn.
        """
        import sessions
        from datetime import timedelta
        sessions.end_session(-999503)
        user_id = 991004
        make_basic_character(user_id, "IdleForcedAttacker", chat_id=-999503, current_location="crossroads_tavern")
        foe = {"telegram_user_id": -700005, "name": "IdleForcedGoblin", "hp_current": 200, "hp_max": 200,
               "is_ai": True, "strength": 10, "dexterity": 10, "armor_class": 8,
               "damage_dice": "1d4", "damage_bonus": 0, "damage_type": "physical", "proficiency_bonus": 2}
        session = sessions.start_session(-999503, [db.get_character(user_id, -999503), foe],
                                          sides={user_id: "party", -700005: "enemy"})
        session.current_turn_index = session.turn_order.index(user_id)
        session.turn_started_at[user_id] = time.time()

        idle_since = (datetime.now(timezone.utc) - timedelta(seconds=bot.IDLE_TIMEOUT_SECONDS + 60)).isoformat()
        db.update_character(user_id, -999503, last_active_at=idle_since)
        bot._IDLE_WARNED.discard((user_id, -999503))

        from unittest.mock import AsyncMock
        mock_bot = AsyncMock()
        await bot._check_idle_characters(mock_bot)

        sent_texts = [call.kwargs.get("text") or (call.args[0] if call.args else "") for call in mock_bot.send_message.await_args_list]
        self.assertFalse(any("turn passed" in t for t in sent_texts), "old silent-pass message must be gone")
        self.assertTrue(any("attack on instinct" in t for t in sent_texts), "must force a real attack instead")
        sessions.end_session(-999503, session)

    def test_scroll_use_on_a_named_npc_still_classifies_as_cast_spell(self):
        """
        Real live bug (2026-08-02, Coffee, mid-fight: "Use a scroll of
        revivify on Wren" produced an unrelated ambient "Wren says a
        line about her garden" reply instead of any real item/spell
        result). The scroll->cast_spell check already existed (fixed
        once before, 2026-07-24, for "...on Laurrienna"), but it was
        positioned AFTER _keyword_fallback's generic known-NPC-name loop
        -- so it only ever actually won when the scroll message did NOT
        also name a real known NPC/companion, which is precisely the
        common case (using an item ON someone). Moved earlier so naming
        the target no longer defeats it.
        """
        known = ["Wren Hollowbrook", "Laurrienna"]
        for text in (
            "Use a scroll of revivify on Wren",
            "use the scroll of revivify on Laurrienna",
            "I cast revivify on Wren",
        ):
            self.assertEqual(_keyword_fallback(text, known)["action"], "cast_spell", text)

    def test_use_item_on_a_named_npc_still_classifies_as_use_item(self):
        """
        Real live bug (2026-08-08, confirmed live twice -- a real dev-
        topic screenshot AND independently via topic-activity
        monitoring): "Use health potion on Vesh Nightglass" got
        hijacked to talk_npc by the same known-NPC-name loop as the
        scroll->cast_spell bug just above, since Vesh is a real known
        NPC/companion name -- but for the far more common case of
        using an ordinary consumable (not a scroll) ON a named ally.
        Same "move the check before the loop" fix pattern.
        """
        known = ["Vesh Nightglass", "Wren Hollowbrook"]
        for text in (
            "Use health potion on Vesh Nightglass",
            "use the health potion on Vesh Nightglass",
        ):
            self.assertEqual(_keyword_fallback(text, known)["action"], "use_item", text)
        # Recruiting/talking to the same real NPCs must still work --
        # this fix must not shadow anything else that already worked.
        self.assertEqual(_keyword_fallback("recruit Vesh Nightglass", known)["action"], "recruit_npc")
        self.assertEqual(_keyword_fallback("talk to Wren Hollowbrook", known)["action"], "talk_npc")

    def test_leaderboard_trigger_recognized(self):
        from ai.intent_parser import _keyword_fallback
        for text in ("show me the leaderboard", "who's the best"):
            self.assertEqual(_keyword_fallback(text, [])["action"], "leaderboard", text)

    async def test_do_leaderboard_produces_a_real_reply(self):
        make_basic_character(900942, "Ranked", gold=10)
        db.add_xp(900942, -999, 300)
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
        db.update_character(user_id, -999, known_monsters=["wolf"])
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
        db.update_character(player_id, -999, level=2)
        player = db.get_character(player_id, -999)
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
        player = db.get_character(player_id, -999)
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
        db.mark_visited(user_id, -999, "the_unmoored_isle")
        db.mark_visited(user_id, -999, "the_first_city")
        sink = []
        await bot._do_fast_travel(FakeUpdate(user_id, "", sink), "fast travel to the unmoored isle")
        self.assertIn("missing something needed", sink[-1])
        self.assertEqual(db.get_character(user_id, -999)["current_location"], "the_first_city")

        db.add_item(user_id, -999, "shard_of_dim_light", 1)
        sink.clear()
        await bot._do_fast_travel(FakeUpdate(user_id, "", sink), "fast travel to the unmoored isle")
        self.assertEqual(db.get_character(user_id, -999)["current_location"], "the_unmoored_isle")

    async def test_fast_travel_blocks_on_locked_connection(self):
        user_id = 900531
        make_basic_character(user_id, "Locktest", current_location="the_weeping_well")
        db.mark_visited(user_id, -999, "the_weeping_well")
        db.mark_visited(user_id, -999, "glimmerdeep_grotto")
        sink = []
        await bot._do_fast_travel(FakeUpdate(user_id, "", sink), "fast travel to glimmerdeep grotto")
        self.assertIn("blocked by", sink[-1])
        self.assertEqual(db.get_character(user_id, -999)["current_location"], "the_weeping_well")

        bot._chat_scoped_set(bot._UNLOCKED, -999).add("sealed_stone_door")
        sink.clear()
        await bot._do_fast_travel(FakeUpdate(user_id, "", sink), "fast travel to glimmerdeep grotto")
        self.assertEqual(db.get_character(user_id, -999)["current_location"], "glimmerdeep_grotto")

    async def test_fast_travel_brings_real_ai_companions_along(self):
        """
        Real live bug found investigating Coffee's report ("It's not
        letting me send to wren, who is in my party"): _do_move
        (on-foot travel) has always moved real AI companions along
        with the party; _do_fast_travel (waypoint warp) never got the
        same fix, so warping anywhere silently stranded every AI
        companion at the old location -- confirmed live, Ravenloft
        fast-traveled to Market Row while Wren Hollowbrook stayed
        behind at the Sunken Root Caverns, so "give X to Wren" correctly
        (if confusingly) said she wasn't "here" -- she genuinely wasn't.
        """
        leader_id = 950801
        make_basic_character(leader_id, "FastTravelLeader2", current_location="crossroads_tavern")
        db.update_character(leader_id, -999, visited_locations=["crossroads_tavern", "market_row"])
        party_id = db.create_party(leader_id, -999)
        companion = db.create_ai_companion(
            -999, "FastTravelBuddy", "Elf", "Ranger",
            ability_scores={"strength": 12, "dexterity": 17, "constitution": 13,
                             "intelligence": 11, "wisdom": 15, "charisma": 10},
            hp_max=30, armor_class=14, gold=0, inventory={},
        )
        db.add_ai_companion_to_party(companion["telegram_user_id"], -999, party_id)

        sink = []
        await bot._do_fast_travel(FakeUpdate(leader_id, "", sink), "fast travel to market row")
        self.assertEqual(db.get_character(leader_id, -999)["current_location"], "market_row")
        self.assertEqual(
            db.get_character_by_id(companion["character_id"])["current_location"], "market_row",
        )

    # -- Per Coffee, 2026-07-19: fishing loses bait by a real 50/50 d20
    #    roll on every attempt (catch or miss), not a fixed schedule.
    async def test_fishing_loses_bait_on_a_low_roll_not_a_high_one(self):
        from unittest.mock import patch

        user_id = 900532
        make_basic_character(user_id, "Fishtest", current_location="stonearch_bridge")
        db.add_item(user_id, -999, "fishing_pole", 1)
        db.add_item(user_id, -999, "bait", 1)

        with patch("bot.roll_ability_check", return_value={
            "raw_roll": 15, "modifier": 0, "proficiency": 0, "total": 15,
        }), patch("bot.narrate_skill_check", return_value="You cast your line."), \
             patch("bot.roll_d20", return_value=3):
            sink = []
            await bot._do_gather(FakeUpdate(user_id, "", sink), "fish in the stream")
            self.assertIn("bait comes free", "\n".join(sink))
            self.assertEqual(db.get_character(user_id, -999)["inventory"].get("bait", 0), 0)

        db.add_item(user_id, -999, "bait", 1)
        with patch("bot.roll_ability_check", return_value={
            "raw_roll": 15, "modifier": 0, "proficiency": 0, "total": 15,
        }), patch("bot.narrate_skill_check", return_value="You cast your line."), \
             patch("bot.roll_d20", return_value=18):
            sink = []
            await bot._do_gather(FakeUpdate(user_id, "", sink), "fish in the stream")
            self.assertNotIn("bait comes free", "\n".join(sink))
            self.assertEqual(db.get_character(user_id, -999)["inventory"].get("bait", 0), 1)

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
            self.assertGreaterEqual(db.get_character(user_id, -999)["inventory"].get("bait", 0), 1)

    # -- World expansion (2026-07-19/20, per Coffee): compass navigation
    #    ("directions", a display/nav layer over "connections") added
    #    alongside the world's large-area expansion into many new
    #    location ids.
    async def test_compass_direction_resolves_to_the_right_location(self):
        user_id = 900550
        make_basic_character(user_id, "Compasstest", current_location="whispering_wood")
        # Sequential dungeon gating (2026-07-24) now requires clearing
        # whispering_wood before advancing to whispering_wood_deep_glade
        # -- irrelevant to what THIS test actually checks (compass-word
        # resolution), so satisfy the gate directly rather than fighting
        # a real battle just to test unrelated direction-parsing logic.
        db.mark_location_cleared(user_id, -999, "whispering_wood")
        sink = []
        await bot._do_move(FakeUpdate(user_id, "", sink), "go south")
        self.assertEqual(db.get_character(user_id, -999)["current_location"], "whispering_wood_deep_glade")

    async def test_compass_word_with_no_directions_field_falls_through_harmlessly(self):
        user_id = 900551
        make_basic_character(user_id, "Compasstest2", current_location="market_row")
        sink = []
        await bot._do_move(FakeUpdate(user_id, "", sink), "go north")
        # market_row has no "directions" field -- must not crash, and must
        # not move the character anywhere (no location name matched either).
        self.assertEqual(db.get_character(user_id, -999)["current_location"], "market_row")

    async def test_look_shows_compass_labels_for_directed_connections(self):
        user_id = 900552
        make_basic_character(user_id, "Compasstest3", current_location="greymoor_downs")
        sink = []
        await bot._do_look(FakeUpdate(user_id, "", sink))
        # _do_look also sends a real location image after the text (see
        # _maybe_send_location_image) -- skip past any trailing <photo:...>
        # sink entries to get the actual narration text.
        reply = next(s for s in reversed(sink) if not s.startswith("<photo:"))
        self.assertIn("North:", reply)
        self.assertIn("West:", reply)

    async def test_arriving_on_foot_auto_shows_look_around_detail_for_a_real_player(self):
        """
        Real feature request (2026-08-07, Coffee: "when we enter a
        location after clicking the button to go there or after we say
        go to a location can you then prompt 'look around' in that
        area once we arrive there to keep the narration flowing"). Real
        players only (Coffee's own answer to the scope question).
        _do_move used to only ever show the destination's bare name and
        description on arrival -- none of what "look around" shows
        (who's here, exits, interactables, resources). Now it appends
        that same detail as a real follow-up message right after
        arrival, extracted into the shared _location_extra_detail
        helper (also used by _do_look itself, so this isn't a second,
        divergent implementation of the same listing).
        """
        user_id = 900560
        make_basic_character(user_id, "ArrivalTest", current_location="whispering_wood")
        db.mark_location_cleared(user_id, -999, "whispering_wood")
        sink = []
        await bot._do_move(FakeUpdate(user_id, "", sink), "go south")
        text_messages = [s for s in sink if not s.startswith("<photo:")]
        self.assertEqual(len(text_messages), 2)
        self.assertIn("travels to", text_messages[0])
        self.assertIn("You can travel to:", text_messages[1])

    async def test_arriving_on_foot_does_not_auto_look_for_an_ai_companion(self):
        """
        Companion to the test above: the auto "look around" on arrival
        must NOT fire for an AI companion's own autonomous move -- would
        spam the Adventure feed with a full room listing on every AI
        step, and Coffee's own answer to the scope question was "real
        players only."
        """
        user_id = 900561
        make_basic_character(user_id, "ArrivalTestAI", current_location="whispering_wood", is_ai=True)
        db.mark_location_cleared(user_id, -999, "whispering_wood")
        sink = []
        await bot._do_move(FakeUpdate(user_id, "", sink), "go south")
        text_messages = [s for s in sink if not s.startswith("<photo:")]
        self.assertEqual(len(text_messages), 1)
        self.assertIn("travels to", text_messages[0])

    async def test_fast_travel_arrival_auto_shows_look_around_detail_for_a_real_player(self):
        """
        Same feature as the on-foot version above, covering the
        waypoint-button warp path too (_do_fast_travel) -- Coffee's
        request explicitly named both "clicking the button to go there
        or after we say go to a location."
        """
        user_id = 900562
        character = make_basic_character(user_id, "FastTravelArrivalTest", current_location="crossroads_tavern")
        db.update_character_by_id(
            character["character_id"],
            visited_locations=list(set((character.get("visited_locations") or []) + ["market_row"])),
        )
        sink = []
        await bot._do_fast_travel(FakeUpdate(user_id, "", sink), "fast travel to market row")
        text_messages = [s for s in sink if not s.startswith("<photo:")]
        self.assertEqual(len(text_messages), 2)
        self.assertIn("fast-travel to", text_messages[0])
        self.assertIn("You can travel to:", text_messages[1])

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
        match = bot._find_own_character_by_name_fragment(user_id, -999, "my character elduinn")
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
        # MAX_LEVEL is 99, not 20, since the rebirth system shipped
        # (2026-07-22) -- this test predates that and was never updated.
        db.update_character(user_id, -999, level=99)
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
        roster = bot.db.list_characters(user_id, -999)
        active = bot.db.get_character(user_id, -999)
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
        active = bot.db.get_character(user_id, -999)
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
        bot.db.switch_character(user_id, -999, first["character_id"])
        sessions.start_session(-999, [{"telegram_user_id": user_id, "name": "Gamma", "dexterity": 10}], {user_id: "party"})
        sink = []
        update = FakeCallbackUpdate(user_id, f"roster|switch|{second['character_id']}", sink)
        await bot.roster_menu_callback(update, DummyContext())
        active = bot.db.get_character(user_id, -999)
        self.assertEqual(active["name"], "Gamma")
        sessions.end_session(-999)

    # -- Coffee, 2026-07-20: "can u put the switch characters option in
    #    the menu?" + "in the switch character option include (Create
    #    New Character)" -- the roster/switch feature (task #213) already
    #    existed via /roster, just wasn't reachable from the main /menu,
    #    and had no way to start a new character from that same screen.
    def test_main_menu_includes_switch_character_row(self):
        character = {"pending_asi_points": 0}
        keyboard = bot._main_menu_keyboard(character)
        labels = [btn.text for row in keyboard.inline_keyboard for btn in row]
        self.assertIn("🎭 Switch Character", labels)

    async def test_main_menu_switch_character_button_opens_roster(self):
        user_id = 900559
        make_basic_character(user_id, "MenuRosterTest")
        sink = []
        update = FakeCallbackUpdate(user_id, "menu|roster", sink)
        await bot.menu_callback(update, DummyContext())
        self.assertTrue(any("tap one to switch" in m for m in sink))

    def test_roster_keyboard_includes_create_new_character_button(self):
        roster = [{"character_id": 1, "name": "Solo", "level": 1, "char_class": "Fighter"}]
        kb = bot._roster_keyboard(roster, 1)
        labels = [btn.text for row in kb.inline_keyboard for btn in row]
        self.assertIn("✨ Create New Character", labels)

    async def test_roster_create_new_character_button_starts_creation(self):
        user_id = 900560
        make_basic_character(user_id, "ExistingOne")
        sink = []
        context = DummyContext()
        update = FakeCallbackUpdate(user_id, "roster|new", sink)
        await bot.roster_menu_callback(update, context)
        self.assertEqual(context.user_data.get("creation", {}).get("step"), "name")
        self.assertTrue(any("create" in m.lower() or "name" in m.lower() for m in sink))

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

    # -- Task #212, per Coffee: real tap-buttons for character creation's
    #    choice steps (race/class/dice-preference/pronouns/score-
    #    assignment), not just free text.
    async def test_character_creation_full_button_flow(self):
        from unittest.mock import patch

        user_id = 900562
        sink = []
        context = DummyContext()
        context.user_data = {}
        await bot._begin_character_creation(FakeUpdate(user_id, "", sink), context)
        self.assertEqual(context.user_data["creation"]["step"], "name")

        await bot._continue_character_creation(FakeUpdate(user_id, "", sink), context, text="ButtonHero")
        self.assertEqual(context.user_data["creation"]["step"], "race")
        # The race prompt should carry real tap buttons for every valid race.
        reply_markup = sink[-1] if not isinstance(sink[-1], str) else None

        def tap(field, value):
            return FakeCallbackUpdate(user_id, f"create|{field}|{value}", sink)

        await bot.creation_menu_callback(tap("race", "Elf"), context)
        self.assertEqual(context.user_data["creation"]["step"], "class")

        await bot.creation_menu_callback(tap("class", "Fighter"), context)
        self.assertEqual(context.user_data["creation"]["step"], "assign_scores")

        await bot.creation_menu_callback(tap("scores", "auto"), context)
        self.assertEqual(context.user_data["creation"]["step"], "dice_preference")

        await bot.creation_menu_callback(tap("dice", "yes"), context)
        self.assertEqual(context.user_data["creation"]["step"], "pronouns")

        await bot.creation_menu_callback(tap("pronouns", "they/them"), context)
        self.assertEqual(context.user_data["creation"]["step"], "description")

        with patch("bot.narrate_welcome", return_value="Welcome to the Crossroads."):
            await bot._continue_character_creation(FakeUpdate(user_id, "", sink), context, text="skip")

        character = bot.db.get_character(user_id, -999)
        self.assertEqual(character["race"], "Elf")
        self.assertEqual(character["char_class"], "Fighter")
        self.assertEqual(character["pronouns"], "they/them")
        self.assertEqual(character["manual_dice_enabled"], 1)
        self.assertNotIn("creation", context.user_data)

    def test_creation_button_keyboards_cover_every_valid_choice(self):
        race_labels = {btn.text for row in bot._race_keyboard().inline_keyboard for btn in row}
        self.assertEqual(race_labels, set(bot.VALID_RACES))
        class_labels = {btn.text for row in bot._class_keyboard().inline_keyboard for btn in row}
        self.assertEqual(class_labels, set(bot.VALID_CLASSES))

    async def test_stale_creation_button_tap_is_a_harmless_no_op(self):
        user_id = 900563
        sink = []
        context = DummyContext()
        context.user_data = {}  # no "creation" in progress at all
        update = FakeCallbackUpdate(user_id, "create|race|Elf", sink)
        await bot.creation_menu_callback(update, context)  # must not raise
        self.assertNotIn("creation", context.user_data)

    # -- Multi-tenant scaling Phase 4a: schema + migration (2026-08-03) --
    def test_phase_4a_chat_id_columns_and_composite_pks_exist(self):
        """
        Real db.py migration, first sub-phase of the DB multi-tenancy
        retrofit (see plans/sequential-tinkering-quokka.md): every
        currently-global table gets a real chat_id, with
        active_characters/npc_relationships/faction_standing rebuilt to
        a genuinely composite PK (SQLite can't ALTER a PK in place).
        Deliberately zero behavior change in this sub-phase -- nothing
        reads/filters by chat_id yet -- so this just proves the schema
        landed correctly on the class's real shared test DB.
        """
        with db.get_connection() as conn:
            for table in ("characters", "market_listings", "board_quests", "parties"):
                cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
                self.assertIn("chat_id", cols, table)
            for table, expected_pk in (
                ("active_characters", {"telegram_user_id", "chat_id"}),
                ("npc_relationships", {"telegram_user_id", "chat_id", "npc_id"}),
                ("faction_standing", {"telegram_user_id", "chat_id", "faction_id"}),
            ):
                info = conn.execute(f"PRAGMA table_info({table})").fetchall()
                pk_cols = {r["name"] for r in info if r["pk"] > 0}
                self.assertEqual(pk_cols, expected_pk, table)

    def test_get_relationship_and_faction_standing_auto_vivify_after_phase_4a(self):
        """
        Real regression caught by the mandatory full-suite run right
        after the Phase 4a rebuild (2026-08-03): get_relationship() and
        get_faction_standing()'s auto-vivify INSERTs didn't supply the
        newly NOT NULL chat_id column, so ANY brand-new NPC relationship
        or faction standing lookup (is_banned_by_npc, shop purchases,
        etc.) crashed with sqlite3.IntegrityError. Fixed by writing
        config.TELEGRAM_CHAT_ID internally, same shape as the
        _set_active_character ON CONFLICT fix. Phase 4c (2026-08-06)
        threaded a real chat_id parameter through these functions
        instead of the hardcoded config.TELEGRAM_CHAT_ID -- updated to
        pass one explicitly.
        """
        user_id = 900564
        chat_id = config.TELEGRAM_CHAT_ID
        rel = db.get_relationship(user_id, chat_id, "some_new_npc")
        self.assertEqual(rel["banned"], 0)
        self.assertFalse(db.is_banned_by_npc(user_id, chat_id, "some_new_npc"))
        standing = db.get_faction_standing(user_id, chat_id, "some_new_faction")
        self.assertEqual(standing, 0)
        self.assertEqual(db.adjust_faction_standing(user_id, chat_id, "some_new_faction", 5), 5)

    def test_npc_relationships_and_faction_standing_are_isolated_per_chat(self):
        """
        Multi-tenant scaling Phase 4c (2026-08-06): npc_relationships and
        faction_standing now take a real chat_id, so the same
        telegram_user_id in two different tenant chats gets genuinely
        independent NPC rapport and faction standing -- not the same
        shared row silently overwritten by whichever chat spoke last.
        """
        user_id = 900700
        chat_a = config.TELEGRAM_CHAT_ID
        chat_b = config.TELEGRAM_CHAT_ID + 999

        db.adjust_affinity(user_id, chat_a, "some_isolated_npc", 30, event="Helped in chat A")
        db.adjust_affinity(user_id, chat_b, "some_isolated_npc", -20, event="Wronged in chat B")
        rel_a = db.get_relationship(user_id, chat_a, "some_isolated_npc")
        rel_b = db.get_relationship(user_id, chat_b, "some_isolated_npc")
        self.assertEqual(rel_a["affinity"], 30)
        self.assertEqual(rel_b["affinity"], -20)
        self.assertEqual(rel_a["memory_events"], ["Helped in chat A"])
        self.assertEqual(rel_b["memory_events"], ["Wronged in chat B"])

        db.set_banned_by_npc(user_id, chat_b, "some_isolated_npc", True)
        self.assertFalse(db.is_banned_by_npc(user_id, chat_a, "some_isolated_npc"))
        self.assertTrue(db.is_banned_by_npc(user_id, chat_b, "some_isolated_npc"))

        db.resolve_companion(user_id, chat_a, "some_isolated_npc", "resolved_loyal")
        self.assertEqual(db.get_companion_resolution(user_id, chat_a, "some_isolated_npc"), "resolved_loyal")
        self.assertEqual(db.get_companion_resolution(user_id, chat_b, "some_isolated_npc"), "unresolved")

        db.adjust_faction_standing(user_id, chat_a, "some_isolated_faction", 40)
        db.adjust_faction_standing(user_id, chat_b, "some_isolated_faction", -40)
        self.assertEqual(db.get_faction_standing(user_id, chat_a, "some_isolated_faction"), 40)
        self.assertEqual(db.get_faction_standing(user_id, chat_b, "some_isolated_faction"), -40)

    # -- Dev-topic edited-message crash (2026-08-03, Coffee) -----------
    async def test_dev_topic_photo_handler_survives_an_edited_message(self):
        """
        Real live crash (2026-08-03, Development topic): Coffee edited a
        screenshot's caption to ask "How can I see the stats and the
        details of this?" -- Telegram delivers an edit as
        update.edited_message, not update.message, so update.message is
        None. dev_topic_photo_handler (and the matching document/video
        handlers) read update.message.photo/.caption directly, crashing
        with AttributeError on every edited screenshot. Fixed by
        switching to update.effective_message throughout.
        """
        class FakePhotoSize:
            def __init__(self, file_id, file_unique_id):
                self.file_id = file_id
                self.file_unique_id = file_unique_id

        class FakeEditedMessage:
            def __init__(self, photo, caption, thread_id):
                self.photo = photo
                self.caption = caption
                self.message_thread_id = thread_id
                self.reply_to_message = None

        class FakeEditedUpdate:
            def __init__(self, user_id, sink, photo, caption, thread_id):
                self.effective_user = FakeUser(user_id)
                self.effective_chat = FakeChat(sink, chat_id=config.TELEGRAM_CHAT_ID)
                self.message = None
                self.effective_message = FakeEditedMessage(photo, caption, thread_id)

        class FakeFile:
            async def download_to_drive(self, path):
                with open(path, "wb") as f:
                    f.write(b"fake-jpeg-bytes")

        class FakeBotWithGetFile(FakeBot):
            async def get_file(self, file_id):
                return FakeFile()

        sink = []
        update = FakeEditedUpdate(
            user_id=7052163553,
            sink=sink,
            photo=[FakePhotoSize("small_id", "small_uniq"), FakePhotoSize("big_id", "big_uniq")],
            caption="How can I see the stats and the details of this?",
            thread_id=config.TOPIC_DEVELOPMENT_ID,
        )
        context = DummyContext(bot=FakeBotWithGetFile(status="creator"))
        await bot.dev_topic_photo_handler(update, context)
        self.assertTrue(sink, "handler produced no reply -- still crashing on edited messages")
        self.assertIn("Got it", sink[-1])
        saved_path = sink[-1].split("`")[1]
        if os.path.exists(saved_path):
            os.remove(saved_path)

    # -- /cancel_market command + natural-language routing (2026-08-03) --
    async def test_cancel_market_lets_the_seller_pull_back_their_own_listing(self):
        """
        Real live request (2026-08-03, Coffee, Development topic): "How
        do I cancel an item that I accidentally put on the market?" --
        db.remove_market_listing existed but had zero player-facing
        caller before this (only ever ran as part of completing a
        sale). /cancel_market <#> lets only the original seller pull
        their own listing back; the item returns to inventory, no gold
        changes hands.
        """
        seller_id, buyer_id = 900571, 900572
        make_basic_character(seller_id, "MarketSeller", inventory={"rusty_dagger": 1})
        make_basic_character(buyer_id, "MarketBuyer", gold=1000)

        sink = []
        update = FakeUpdate(seller_id, "", sink)
        await bot._do_sell_market(update, ["1", "50", "rusty dagger"])
        self.assertIn("/cancel_market", sink[-1])
        listing_id = db.get_market_listings(-999)[-1]["listing_id"]

        sink2 = []
        await bot._do_cancel_market(FakeUpdate(buyer_id, "", sink2), [str(listing_id)])
        self.assertIn("not your listing", sink2[-1])
        self.assertIsNotNone(db.get_market_listing(listing_id, -999))

        seller_before = db.get_character(seller_id, -999)
        self.assertEqual(seller_before["inventory"].get("rusty_dagger", 0), 0)

        sink3 = []
        await bot._do_cancel_market(FakeUpdate(seller_id, "", sink3), [str(listing_id)])
        self.assertIn("Cancelled listing", sink3[-1])
        self.assertIsNone(db.get_market_listing(listing_id, -999))
        seller_after = db.get_character(seller_id, -999)
        self.assertEqual(seller_after["inventory"].get("rusty_dagger", 0), 1)
        self.assertEqual(seller_after["gold"], seller_before["gold"])

    def test_cancel_market_natural_language_routes_correctly(self):
        """
        Real live gap (2026-08-03, Coffee): typed "Cancel my listing in
        the market" in plain English -- this game's whole design is "no
        slash commands required" -- but the deterministic keyword
        fallback's existing "the market"/"marketplace" check only ever
        pointed at check_market, so it showed him the market instead of
        cancelling anything. Fixed with a "cancel" sub-check ahead of
        the generic one, mirroring the existing "duel"/"accept" pattern.
        """
        self.assertEqual(_keyword_fallback("Cancel my listing in the market", [])["action"], "cancel_market")
        self.assertEqual(_keyword_fallback("Check the market", [])["action"], "check_market")

    def test_go_to_market_row_moves_instead_of_opening_the_market(self):
        """
        Real live bug (2026-08-09, found via topic-activity monitoring):
        "Go to the market row" came back check_market instead of move --
        Market Row is a real, travelable location (campaign.json's
        "market_row"), and its name contains "market" as a substring of
        "the market", so the general marketplace-listing check above
        swallowed an explicit "go to <real place>" travel command before
        move_words ever got a chance. Same "specific case before the
        general one" shape as the cancel_market fix right above --
        "market row" is excluded from the marketplace trigger so it
        falls through to the real move classification instead, without
        touching any other genuine marketplace phrase (verified below).
        """
        self.assertEqual(_keyword_fallback("Go to the market row", [])["action"], "move")
        self.assertEqual(_keyword_fallback("go to market row", [])["action"], "move")
        self.assertEqual(_keyword_fallback("head to Market Row", [])["action"], "move")
        # Genuine marketplace phrasing must still work exactly as before.
        self.assertEqual(_keyword_fallback("the marketplace", [])["action"], "check_market")
        self.assertEqual(_keyword_fallback("show me the market listings", [])["action"], "check_market")
        self.assertEqual(_keyword_fallback("Cancel my listing in the market", [])["action"], "cancel_market")

    def test_move_with_accept_quest_purpose_clause_classified_as_move(self):
        """
        Real live bug (2026-08-09, found via topic-activity monitoring):
        "I head to The Goblin Warrens to accept the quest." came back as
        accept_quest instead of move -- the old "accept the quest" check
        unconditionally won even though it's only the stated PURPOSE of
        an explicit "head to <destination>" travel clause. accept_quest
        always resolves against the character's CURRENT location (never
        a stated destination), so this silently either accepted whatever
        was available where the player already stood, or replied
        "nothing to accept" -- either way the player never actually
        moved. Same "specific case before the general one" shape as the
        market-row fix just above.
        """
        self.assertEqual(_keyword_fallback("I head to The Goblin Warrens to accept the quest.", [])["action"], "move")
        self.assertEqual(_keyword_fallback("go to the tavern to accept the quest", [])["action"], "move")
        self.assertEqual(_keyword_fallback("travel to the crossroads to accept the quest", [])["action"], "move")
        # Genuine, non-movement accept_quest phrasing must still work exactly as before.
        self.assertEqual(_keyword_fallback("Accept the quest", [])["action"], "accept_quest")
        self.assertEqual(_keyword_fallback("I accept this quest", [])["action"], "accept_quest")
        self.assertEqual(_keyword_fallback("accept that quest on the board", [])["action"], "accept_quest")

    async def test_cancel_market_intent_auto_resolves_a_single_listing(self):
        seller_id = 900573
        make_basic_character(seller_id, "SoloSeller", inventory={"rusty_dagger": 1})
        sink = []
        await bot._do_sell_market(FakeUpdate(seller_id, "", sink), ["1", "50", "rusty dagger"])

        sink2 = []
        await bot._do_cancel_market_intent(FakeUpdate(seller_id, "", sink2), "Cancel my listing in the market")
        self.assertIn("Cancelled listing", sink2[-1])
        self.assertEqual(db.get_character(seller_id, -999)["inventory"].get("rusty_dagger", 0), 1)

    async def test_cancel_market_intent_asks_which_one_when_ambiguous(self):
        seller_id = 900574
        make_basic_character(seller_id, "MultiSeller", inventory={"rusty_dagger": 2, "silvered_dagger": 1})
        sink = []
        update = FakeUpdate(seller_id, "", sink)
        await bot._do_sell_market(update, ["1", "50", "rusty dagger"])
        await bot._do_sell_market(update, ["1", "80", "silvered dagger"])

        sink2 = []
        await bot._do_cancel_market_intent(FakeUpdate(seller_id, "", sink2), "Cancel my listing")
        self.assertIn("more than one listing", sink2[-1])
        remaining = [l for l in db.get_market_listings(-999) if l["seller_id"] == seller_id]
        self.assertEqual(len(remaining), 2)

    # -- Task #265, per Coffee ("i wasted a turn ... u said it was
    #    complete"): 27 spells whose effect type (buff/negate/ac_bonus)
    #    fell into one shared flavor-only branch that still spent the
    #    slot/turn. Promoted from the standalone verification script
    #    used to build the real mechanics below. --
    async def test_death_ward_saves_defender_at_1hp_and_is_consumed(self):
        uid = 800101
        make_basic_character(uid, "Warded", char_class="Cleric", hp_max=20)
        char = db.get_character(uid, -999)
        enemy_id = -800101
        enemy = {"telegram_user_id": enemy_id, "name": "Test Dummy", "hp_current": 20, "hp_max": 20,
                 "armor_class": 5, "strength": 10, "dexterity": 10, "is_ai": 1, "xp_reward": 0, "conditions": []}
        player = dict(char)
        player["conditions"] = ["death_warded"]
        player["hp_current"] = 5
        import sessions
        session = sessions.start_session(-999, [player, enemy], {uid: "party", enemy_id: "enemy"})
        weapon = {"damage_dice": "1d1", "damage_bonus": 100, "ability": "strength", "damage_type": "physical"}
        result = resolve_attack(enemy, player, weapon, advantage=False, disadvantage=False,
                                 defender_relentless_endurance_available=False, round_number=1,
                                 forced_roll=20)
        self.assertEqual(player["hp_current"], 1)
        self.assertTrue(result["death_ward_triggered"])
        self.assertNotIn("death_warded", player["conditions"])
        sessions.end_session(-999, session)

    async def test_bless_hex_and_shield_have_real_combat_effects(self):
        caster_id, ally_id, enemy_id = 800102, 800103, -800102
        make_basic_character(caster_id, "Blesser", char_class="Cleric")
        make_basic_character(ally_id, "Ally", char_class="Fighter")
        enemy = {"telegram_user_id": enemy_id, "name": "Goblin", "hp_current": 30, "hp_max": 30,
                 "armor_class": 10, "strength": 10, "dexterity": 10, "is_ai": 1, "xp_reward": 10, "conditions": []}
        import sessions
        session = sessions.start_session(
            -999, [db.get_character(caster_id, -999), db.get_character(ally_id, -999), enemy],
            {caster_id: "party", ally_id: "party", enemy_id: "enemy"},
        )
        caster_p = next(p for p in session.participants if p["telegram_user_id"] == caster_id)
        ally_p = next(p for p in session.participants if p["telegram_user_id"] == ally_id)

        bot._apply_timed_condition(caster_p, "blessed", 10, session)
        bot._apply_timed_condition(ally_p, "blessed", 10, session)
        self.assertIn("blessed", ally_p["conditions"])

        caster_p["marked_target_id"] = enemy_id
        bot._apply_timed_condition(caster_p, "hunters_mark", 10, session)
        weapon = {"damage_dice": "1d1", "damage_bonus": 0, "ability": "strength", "damage_type": "physical"}
        result = resolve_attack(caster_p, enemy, weapon, advantage=False, disadvantage=False,
                                 defender_relentless_endurance_available=False, round_number=1, forced_roll=15,
                                 forced_damage_roll=1)
        self.assertGreaterEqual(result["damage_dealt"], 2)

        bot._apply_timed_condition(ally_p, "shield_active", 1, session)
        ac_before = ally_p["armor_class"]
        weak_attack = resolve_attack(enemy, ally_p, weapon, advantage=False, disadvantage=False,
                                      defender_relentless_endurance_available=False, round_number=1,
                                      forced_roll=ac_before + 3)
        self.assertFalse(weak_attack["hit"])
        sessions.end_session(-999, session)

    async def test_hunters_mark_bonus_requires_a_real_matching_target(self):
        # Regression for the None == None bug: an unmarked attacker with
        # no marked_target_id must NOT get bonus damage against a
        # defender that also lacks a telegram_user_id key.
        attacker = {
            "name": "Unmarked", "conditions": [], "marked_target_id": None,
            "dexterity": 14, "strength": 16, "armor_class": 15, "hp_current": 20, "hp_max": 20,
        }
        defender = {"name": "Goblin", "dexterity": 10, "armor_class": 5, "hp_current": 20, "hp_max": 20}
        weapon = {"damage_dice": "1d1", "damage_bonus": 0, "ability": "strength", "damage_type": "physical"}
        result = resolve_attack(attacker, defender, weapon, advantage=False, disadvantage=False,
                                 defender_relentless_endurance_available=False, round_number=1, forced_roll=15,
                                 forced_damage_roll=1)
        self.assertEqual(result["damage_dealt"], 1)

    async def test_hold_person_and_invisibility_affect_advantage(self):
        attacker = {"conditions": [], "char_class": "Fighter"}
        held_defender = {"conditions": ["paralyzed"]}
        adv, disadv = bot._attack_advantage_disadvantage(attacker, held_defender)
        self.assertTrue(adv)

        invis_defender = {"conditions": ["invisible"]}
        adv2, disadv2 = bot._attack_advantage_disadvantage(attacker, invis_defender)
        self.assertTrue(disadv2)

        invis_attacker = {"conditions": ["invisible"], "char_class": "Rogue"}
        plain_defender = {"conditions": []}
        adv3, disadv3 = bot._attack_advantage_disadvantage(invis_attacker, plain_defender)
        self.assertTrue(adv3)

    async def test_counterspell_is_reaction_only_and_spends_nothing(self):
        # Real live bug (2026-08, Coffee: "i wasted a turn because of
        # this"): casting Counterspell used to spend a turn/slot and
        # narrate a flavor-only non-effect. It's reaction-only in real
        # 5E and there's no monster spellcasting to react to yet, so
        # proactive casting must be honestly refused, spending nothing.
        uid = 800104
        make_basic_character(uid, "Countersplr", char_class="Wizard",
                              known_spells=["counterspell"], spell_slots_max=5)
        sink = []
        await bot._do_cast_spell(FakeUpdate(uid, "cast counterspell", sink), "cast counterspell")
        reply = "\n".join(sink)
        self.assertIn("REACTION", reply)
        fresh = db.get_character(uid, -999)
        self.assertEqual(fresh["spell_slots_current"], 5)

    async def test_dancing_lights_grants_a_real_light_source(self):
        uid = 800105
        make_basic_character(uid, "Torchless", char_class="Wizard", known_spells=["dancing_lights"],
                              spell_slots_max=3, current_location="crossroads_tavern")
        char = db.get_character(uid, -999)
        self.assertFalse(bot._has_light_source(char, -999))
        sink = []
        await bot._do_cast_spell(FakeUpdate(uid, "cast dancing lights", sink), "cast dancing lights")
        reply = "\n".join(sink)
        self.assertIn("light", reply.lower())
        fresh = db.get_character(uid, -999)
        self.assertTrue(bot._has_light_source(fresh, -999))

    async def test_guidance_grants_a_real_consumable_check_bonus(self):
        uid = 800106
        make_basic_character(uid, "Guided", char_class="Cleric", known_spells=["guidance"], spell_slots_max=0)
        sink = []
        await bot._do_cast_spell(FakeUpdate(uid, "cast guidance", sink), "cast guidance")
        reply = "\n".join(sink)
        self.assertIn("+2", reply)
        pending = bot._chat_scoped_dict(bot._PENDING_CHECK_BONUS, -999).get(uid)
        self.assertIsNotNone(pending)
        self.assertEqual(pending["amount"], 2)

    async def test_detect_magic_reports_real_carried_magic_items(self):
        uid = 800107
        make_basic_character(uid, "Detector", char_class="Wizard", known_spells=["detect_magic"],
                              spell_slots_max=2)
        ring_id = db.create_item_instance(
            item_type="ring", name="Ring of Testing", rarity="rare", price=100,
            base_stats={"type": "ring"}, affixes=[],
        )
        db.add_item(uid, -999, ring_id, 1)
        sink = []
        await bot._do_cast_spell(FakeUpdate(uid, "cast detect magic", sink), "cast detect magic")
        reply = "\n".join(sink)
        self.assertIn("Ring of Testing", reply)

    # -- Universal Manipulation (2026-08-06, per Coffee: "let players
    #    level up skills they already have to increase power... breaking
    #    out of the box / breaking the game") -- replaced the old
    #    single-purchase Skill Tree with unlimited, repeatable
    #    investment. See bot.py's UNIVERSAL_MANIPULATION_* dicts. --
    def test_skill_points_counts_repeated_investment(self):
        character = {"skill_tree_upgrades": ["hardened_resolve", "hardened_resolve", "vitality"]}
        self.assertEqual(bot._skill_points(character, "hardened_resolve"), 2)
        self.assertEqual(bot._skill_points(character, "vitality"), 1)
        self.assertEqual(bot._skill_points(character, "iron_will"), 0)

    def test_class_skill_investment_is_repeatable_not_a_one_time_unlock(self):
        uid = 800200
        make_basic_character(uid, "InvestFighter", char_class="Fighter")
        db.update_character(uid, -999, skill_points=10)
        member = db.get_character(uid, -999)
        line1 = bot._apply_class_skill_investment(member)
        self.assertIsNotNone(line1)
        member = db.get_character(uid, -999)
        line2 = bot._apply_class_skill_investment(member)
        self.assertIsNotNone(line2)
        member = db.get_character(uid, -999)
        self.assertEqual(member["skill_points"], 6)  # 10 - 2 - 2
        self.assertEqual(member["skill_tree_upgrades"].count("hardened_resolve"), 2)

    async def test_vitality_purchase_raises_hp_max_and_heals_by_the_same_delta(self):
        uid = 800201
        make_basic_character(uid, "VitBuyer", char_class="Fighter", hp_max=50)
        db.update_character(uid, -999, skill_points=10, hp_current=30)
        sink = []
        await bot.skilltree_menu_callback(
            FakeCallbackUpdate(uid, "skilltree|buy|vitality", sink), DummyContext(),
        )
        after = db.get_character(uid, -999)
        self.assertEqual(after["hp_max"], 55)  # +10%, compounding step
        self.assertEqual(after["hp_current"], 35)
        self.assertEqual(after["skill_points"], 7)  # 10 - 3

    async def test_arcane_reserve_purchase_raises_spell_slots_max(self):
        uid = 800202
        make_basic_character(uid, "ArcaneBuyer", char_class="Wizard", spell_slots_max=2)
        db.update_character(uid, -999, skill_points=10, spell_slots_current=1)
        sink = []
        await bot.skilltree_menu_callback(
            FakeCallbackUpdate(uid, "skilltree|buy|arcane_reserve", sink), DummyContext(),
        )
        after = db.get_character(uid, -999)
        self.assertEqual(after["spell_slots_max"], 3)  # min +1 floor
        self.assertEqual(after["spell_slots_current"], 2)

    async def test_weapon_mastery_purchase_is_one_time_and_widens_proficiency(self):
        uid = 800203
        make_basic_character(uid, "MasteryBuyer", char_class="Wizard")
        db.update_character(uid, -999, skill_points=10)
        sink = []
        await bot.skilltree_menu_callback(
            FakeCallbackUpdate(uid, "skilltree|buy|prof_martial_weapons", sink), DummyContext(),
        )
        after = db.get_character(uid, -999)
        self.assertIn("prof_martial_weapons", after["skill_tree_upgrades"])
        self.assertEqual(after["skill_points"], 8)  # 10 - 2
        # Re-buying an already-owned mastery is a real no-op.
        await bot.skilltree_menu_callback(
            FakeCallbackUpdate(uid, "skilltree|buy|prof_martial_weapons", sink), DummyContext(),
        )
        after2 = db.get_character(uid, -999)
        self.assertEqual(after2["skill_points"], 8)

        # A Wizard is normally NOT proficient with martial weapons --
        # Weapon Mastery should add the real proficiency bonus to the
        # attack roll (same forced_roll, only the mastery differs).
        base = {"name": "A", "char_class": "Wizard", "level": 1, "proficiency_bonus": 5,
                "strength": 16, "dexterity": 10, "armor_class": 10, "hp_current": 10, "hp_max": 10,
                "conditions": [], "temp_hp": 0}
        weapon = {"name": "Longsword", "weapon_category": "martial", "damage_dice": "1d8",
                  "ability": "strength", "damage_type": "physical"}

        def defender():
            return {"name": "D", "armor_class": 5, "hp_current": 20, "hp_max": 20, "conditions": [], "dexterity": 10, "temp_hp": 0}

        no_mastery = resolve_attack({**base, "skill_tree_upgrades": []}, defender(), weapon, forced_roll=15)
        with_mastery = resolve_attack({**base, "skill_tree_upgrades": ["prof_martial_weapons"]}, defender(), weapon, forced_roll=15)
        self.assertEqual(with_mastery["attack_roll"] - no_mastery["attack_roll"], 5)

    def test_disciples_grace_scales_with_points_invested(self):
        cleric_0 = {"char_class": "Cleric", "level": 3, "skill_tree_upgrades": [], "guild": None,
                    "subclass": None, "rebirth_count": 0}
        cleric_2 = {**cleric_0, "skill_tree_upgrades": ["disciples_grace", "disciples_grace"]}
        n = 30
        avg0 = sum(
            spells.resolve_heal_spell("cure_wounds", cleric_0, {"hp_current": 1, "hp_max": 1000})["healing_done"]
            for _ in range(n)
        ) / n
        avg2 = sum(
            spells.resolve_heal_spell("cure_wounds", cleric_2, {"hp_current": 1, "hp_max": 1000})["healing_done"]
            for _ in range(n)
        ) / n
        self.assertGreater(avg2, avg0)

    def test_killers_instinct_scales_with_points_invested(self):
        rogue_0 = {"name": "R0", "char_class": "Rogue", "level": 3, "skill_tree_upgrades": [],
                   "strength": 10, "dexterity": 18, "armor_class": 12, "hp_current": 20, "hp_max": 20,
                   "proficiency_bonus": 2, "conditions": [], "temp_hp": 0}
        rogue_3 = {**rogue_0, "name": "R3", "skill_tree_upgrades": ["killers_instinct"] * 3}
        weapon = {"name": "Dagger", "weapon_category": "simple", "damage_dice": "1d4", "ability": "dexterity", "damage_type": "physical"}

        def defender():
            return {"name": "D", "armor_class": 5, "hp_current": 200, "hp_max": 200, "conditions": [], "dexterity": 10, "temp_hp": 0}

        n = 25
        avg0 = sum(resolve_attack(rogue_0, defender(), weapon, advantage=True, forced_roll=15)["damage_dealt"] for _ in range(n)) / n
        avg3 = sum(resolve_attack(rogue_3, defender(), weapon, advantage=True, forced_roll=15)["damage_dealt"] for _ in range(n)) / n
        self.assertGreater(avg3, avg0 + 5)

    async def test_shrine_offering_can_revive_a_partyless_dead_alt_character(self):
        """
        Real live bug (2026-08-06, Coffee via dev-topic screenshot: "It's
        not letting me revive my player"). Root cause: both the shrine's
        free-text prayer (_do_give_offering) and its tap-to-revive menu
        (_do_shrine_offering_menu) only ever looked at the CALLER's
        party_id for who's revivable -- a dead character with no
        party_id at all (confirmed live: Coffee's "Pan", died at The
        Colosseum, party_id NULL) was invisible no matter which of his
        other characters brought the offering. Fixed by also merging in
        every character the CALLER owns (db.list_characters), not just
        party members.
        """
        uid = 800300
        alive = make_basic_character(uid, "AliveAlt", current_location="hollow_stump_shrine", gold=500)
        dead = make_basic_character(uid, "DeadAlt", current_location="the_colosseum")
        db.update_character_by_id(dead["character_id"], is_dead=1, hp_current=0)
        db.switch_character(uid, -999, alive["character_id"])

        sink = []
        await bot._do_give_offering(FakeUpdate(uid, "Pray for DeadAlt", sink), "Pray for DeadAlt")

        after = db.get_character_by_id(dead["character_id"])
        self.assertEqual(after["is_dead"], 0)
        self.assertEqual(after["hp_current"], after["hp_max"])

    def test_get_party_members_by_id_shows_a_member_who_is_not_the_owners_active_character(self):
        """
        Real live bug (2026-08-06, Coffee: "Why are all our characters
        getting dropped from party" -- confirmed the party was already
        established, no one had left it). Root cause: get_party_members_
        by_id used to JOIN against active_characters, so a real party
        member whose owner simply wasn't currently on that exact
        character (e.g. because they created ANOTHER new character,
        which immediately becomes their new active slot -- exactly what
        Coffee described) silently vanished from every party lookup:
        the party status screen, the bench/unbench roster, XP-sharing to
        absent members, "is this party all AI" detection, and more.
        Fixed by dropping the join (matching get_party_members_by_id_
        including_inactive_slots, the exact same fix already shipped for
        the shrine's revival flow on 2026-07-24, just never applied here
        too).
        """
        uid = 800301
        elduinn = make_basic_character(uid, "ReproElduinn2")
        db.update_character_by_id(elduinn["character_id"], party_id=4242)
        # Creating a second character makes IT this owner's new active
        # slot -- Elduinn is no longer "the" active character for uid.
        make_basic_character(uid, "ReproPan2")

        members = db.get_party_members_by_id(4242)
        names = [m["name"] for m in members]
        self.assertIn("ReproElduinn2", names)

    async def test_log_unhandled_error_answers_and_replies_to_a_crashed_callback_query(self):
        """
        Real live bug (2026-08-06, Sheri via dev-topic screenshot: "Was
        supposed to be skill fire ball"). Her tap on a battle-menu "cast
        Fireball" button crashed on a transient httpcore.ReadTimeout, but
        the old fallback-reply code only ever fired for `update.message`
        (a typed message), which is None for a callback-query-only update
        (a button tap) -- so ANY battle-menu/waypoint/shrine/skill-tree
        button tap that crashed mid-handler left the player with zero
        feedback: no error, no spinner resolution, nothing. Fixed
        _log_unhandled_error to also handle a crashed callback_query:
        acknowledge it (stop the tap from spinning forever) and send the
        same fallback text to the chat/topic the button lived in.

        This project's structural FakeUpdate/FakeCallbackUpdate test
        doubles are NOT subclasses of the real telegram.Update, so they
        can't exercise this function's `isinstance(update, Update)` guard
        (present in the original code, not new). This test builds a real
        (but minimal) telegram.Update/CallbackQuery/Message/Chat instead,
        with a fake Bot swapped in via set_bot() so no network call
        actually happens -- the only way to genuinely verify this guard.
        """
        import datetime as _dt

        from telegram import CallbackQuery as _CallbackQuery
        from telegram import Chat as _Chat
        from telegram import Message as _Message
        from telegram import Update as _Update
        from telegram import User as _User

        class _FakeBot:
            def __init__(self):
                self.answered = []
                self.sent = []

            async def answer_callback_query(self, **kwargs):
                self.answered.append(kwargs)
                return True

            async def send_message(self, **kwargs):
                self.sent.append(kwargs)
                return None

        class _DummyContext:
            error = RuntimeError("boom - simulated crash mid-callback")

        fake_bot = _FakeBot()
        user = _User(id=700900, first_name="Sheri", is_bot=False)
        chat = _Chat(id=-999500, type="supergroup")
        chat.set_bot(fake_bot)
        msg = _Message(
            message_id=555,
            date=_dt.datetime.now(_dt.timezone.utc),
            chat=chat,
            message_thread_id=23,
        )
        msg.set_bot(fake_bot)
        query = _CallbackQuery(
            id="12345", from_user=user, chat_instance="abc123",
            data="bm|casttarget|fireball|Crystal Spider 2", message=msg,
        )
        query.set_bot(fake_bot)
        update = _Update(update_id=1, callback_query=query)

        await bot._log_unhandled_error(update, _DummyContext())

        self.assertEqual(len(fake_bot.answered), 1)
        self.assertEqual(len(fake_bot.sent), 1)
        self.assertIn("tap", fake_bot.sent[0]["text"])
        self.assertEqual(fake_bot.sent[0]["message_thread_id"], 23)
        self.assertEqual(fake_bot.sent[0]["chat_id"], -999500)

        # The pre-existing typed-message path must still work unchanged.
        fake_bot2 = _FakeBot()
        chat2 = _Chat(id=-999501, type="supergroup")
        chat2.set_bot(fake_bot2)
        msg2 = _Message(
            message_id=1, date=_dt.datetime.now(_dt.timezone.utc),
            chat=chat2, message_thread_id=41, text="cast fireball",
            from_user=user,
        )
        msg2.set_bot(fake_bot2)
        update2 = _Update(update_id=2, message=msg2)
        await bot._log_unhandled_error(update2, _DummyContext())
        self.assertEqual(len(fake_bot2.sent), 1)
        self.assertNotIn("tap", fake_bot2.sent[0]["text"])
        self.assertEqual(len(fake_bot2.answered), 0)

        # A callback_query with no attached message (e.g. Telegram
        # couldn't reattach an expired one) must not crash.
        fake_bot3 = _FakeBot()
        query3 = _CallbackQuery(
            id="999", from_user=user, chat_instance="xyz",
            data="bm|whatever", message=None,
        )
        query3.set_bot(fake_bot3)
        update3 = _Update(update_id=3, callback_query=query3)
        await bot._log_unhandled_error(update3, _DummyContext())
        self.assertEqual(len(fake_bot3.answered), 1)
        self.assertEqual(len(fake_bot3.sent), 0)

    def test_monster_and_defeat_image_prompts_put_the_real_name_first(self):
        """
        Real live bug (2026-08-06, Coffee: "These images don't really
        look like a crystal spider"). Confirmed via real Pollinations.ai
        calls at the monster's own real deterministic seed
        (monster:crystal_spider -> seed 1906563205): the old prompt
        shape ("fantasy RPG a monster, Crystal Spider, ...") buried the
        real name after generic "a monster" framing, and the image model
        weighted that generic framing far more heavily -- render came
        back as a generic shaggy dark blob, nothing crystalline or
        spider-shaped. Moving the real name to the FRONT of the prompt
        produced a genuinely crystalline, glowing, multi-legged render
        at the same seed. Re-verified against Goblin (a monster whose
        old prompt already rendered fine) at its own real seed to
        confirm the reorder doesn't regress an already-working case.
        This test can't re-run the live image API itself (no network
        call belongs in the regression suite), so it verifies the one
        thing that actually matters here: the real monster name is now
        the first thing in the prompt string, for both a regular
        monster, a boss, and the defeat-portrait variant of both.
        """
        regular = {"name": "Crystal Spider", "is_boss": False}
        boss = {"name": "The Unspoken", "is_boss": True}
        self.assertTrue(bot._monster_image_prompt(regular).startswith("Crystal Spider,"))
        self.assertTrue(bot._monster_image_prompt(boss).startswith("The Unspoken,"))

        defeat_entry = {"name": "Goblin", "monster_key": "goblin"}
        self.assertTrue(bot._defeat_image_prompt(defeat_entry).startswith("Goblin,"))

        fallen_player = {"name": "ReproPlayer", "monster_key": None}
        self.assertTrue(bot._defeat_image_prompt(fallen_player).startswith("ReproPlayer,"))

    def test_spell_and_ability_image_prompts_exclude_a_person(self):
        """
        Real live feedback (2026-08-10, Coffee, dev-bridge screenshot):
        the generated "Mage Hand" image showed a human hand reaching
        toward the spell effect -- "please show the [effect] but not
        show a person or any human appendages or the monster... it
        looks pretty creepy." The old prompts said "spellcasting" and
        "character using a special ability... dynamic action pose,"
        both of which invited a human figure. This can't re-run the
        live image API (no network call belongs in the regression
        suite), so it verifies the one thing that actually matters:
        both prompts now explicitly exclude a person/hands/creature and
        no longer contain the old people-inviting phrasing.
        """
        spell = {"name": "Mage Hand", "effect": "utility"}
        spell_prompt = bot._spell_image_prompt(spell)
        self.assertIn("no person", spell_prompt)
        self.assertIn("no human hands or body parts", spell_prompt)
        self.assertIn("no creature or monster", spell_prompt)
        self.assertNotIn("spellcasting", spell_prompt)

        ability_prompt = bot._ability_image_prompt("Second Wind", "a burst of restorative vigor")
        self.assertIn("no person", ability_prompt)
        self.assertIn("no human hands or body parts", ability_prompt)
        self.assertIn("no creature or monster", ability_prompt)
        self.assertNotIn("dynamic action pose", ability_prompt)

    def test_interactable_image_prompt_nudges_against_warped_objects(self):
        """
        Real live feedback (2026-08-10, dev-bridge screenshot): "The
        bottles in the picture have bent necks that look unnatural.
        Glass Bottles don't normally bend" -- a real, visually
        confirmed Pollinations distortion artifact on a shelf-of-
        bottles interactable image (the same "warped hands/fingers"
        failure mode generative image models are broadly known for).
        A/B tested directly against the real reported prompt+seed
        (fetched both images, compared visually): adding "well-formed
        objects, correct proportions" produced visibly straighter,
        more consistent bottle necks. Can't re-run the live image API
        in the regression suite (no network call belongs here, same
        convention as the spell/ability image prompt test above) --
        this verifies the actual prompt text carries the addition.
        """
        prompt = bot._interactable_image_prompt(
            {"name": "a shelf of dusty bottles",
             "description": "Vintages going back further than the tavern's own sign out front."})
        self.assertIn("well-formed objects", prompt)
        self.assertIn("correct proportions", prompt)
        # Still grounded only in the real name/description -- no invented detail.
        self.assertIn("a shelf of dusty bottles", prompt)

    async def test_send_generated_image_pre_warms_the_url_before_handing_it_to_telegram(self):
        """
        Real live bug (2026-08-08, found via topic-activity monitoring):
        'Crystal Spider 4' defeat image failed twice live with Telegram's
        BadRequest('Wrong type of the web page content'). Root cause
        confirmed by directly fetching a genuinely never-before-requested
        Pollinations seed/prompt with real timing: the FIRST fetch of a
        cold image took 25.57s (x-cache: MISS); a second fetch of the
        exact same URL immediately after took 1.10s (x-cache: HIT).
        send_photo(photo=<url>) makes TELEGRAM fetch the URL server-side,
        and a 25s+ cold-generation delay is well beyond what Telegram's
        own fetch tolerates before giving up -- so a genuinely fresh
        image was always at real risk of failing on its very first
        request. Fix: _send_generated_image now pre-fetches the URL
        itself (forcing Pollinations to finish generating and cache it)
        before ever handing the URL to Telegram, so Telegram's own fetch
        always lands on the fast, cached path. This test can't wait out
        a real 25s generation (no network call belongs in the regression
        suite), so it verifies the two things that actually matter: (1)
        the pre-warm GET happens before send_photo is ever called, with
        the exact same URL both times, and (2) a pre-warm failure (the
        image API being briefly down) doesn't block the send attempt --
        Telegram still gets a real chance to fetch it itself, same as
        before this fix existed.
        """
        from unittest.mock import patch
        sink = []
        update = FakeUpdate(700601, "look", sink)
        call_order = []

        def fake_get(url, timeout=None):
            call_order.append(("prewarm", url))
            return SimpleNamespace(status_code=200, headers={"content-type": "image/jpeg"})

        with patch("bot.requests.get", side_effect=fake_get):
            ok = await bot._send_generated_image(update, "a test prompt", "caption")

        self.assertTrue(ok)
        self.assertEqual(len(update.effective_chat.sent_photos), 1)
        prewarm_url = call_order[0][1]
        sent_url = update.effective_chat.sent_photos[0]["photo"]
        self.assertEqual(prewarm_url, sent_url, "the pre-warmed URL must be the exact same one handed to Telegram")

        # A pre-warm failure (the image API briefly unreachable) must not
        # block the send attempt -- Telegram still gets its own real
        # chance to fetch the image, exactly like before this fix.
        sink2 = []
        update2 = FakeUpdate(700602, "look", sink2)

        def failing_get(url, timeout=None):
            raise requests.RequestException("simulated pre-warm failure")

        with patch("bot.requests.get", side_effect=failing_get):
            ok2 = await bot._send_generated_image(update2, "a test prompt", "caption")

        self.assertTrue(ok2, "a pre-warm failure must not prevent the actual send_photo attempt")
        self.assertEqual(len(update2.effective_chat.sent_photos), 1)

    async def test_still_working_notice_fires_only_for_a_genuinely_busy_user_in_adventure(self):
        """
        Real live bug (2026-08-06, Coffee via dev-topic screenshot: "the
        character typed the command, but it didn't say anything so they
        were unsure that it was still their time"). Confirmed via
        bot_live_tmp.log: Sugar's two attacks 3 minutes apart with no
        reply in between -- root cause is _run_in_user_order (by design,
        see its own docstring): this exact same player's SECOND message
        queues strictly behind the first's full processing, including a
        real 30-160s+ Ollama narration call, with zero acknowledgment
        that it's queued at all. text_message_router now checks
        bot._USER_BUSY (set by _user_queue_worker while actively running
        a job for that user) and sends an immediate "still working on
        your last action" notice before enqueuing, but only in Adventure
        (the "is it my turn" framing doesn't apply to Development/
        Support, which aren't turn-based) and only for a user who is
        genuinely already busy -- never for an ordinary single message,
        and never leaking across different users.
        """
        from unittest.mock import patch

        user_id = 900700
        other_user_id = 900701

        async def fake_route(update, context):
            return None

        with patch.object(bot, "_route_text_message", fake_route):
            sink1 = []
            await bot.text_message_router(FakeUpdate(user_id, "attack goblin", sink1), DummyContext())
            self.assertFalse(any("Still working" in m for m in sink1))

            bot._USER_BUSY.add(user_id)
            try:
                sink2 = []
                await bot.text_message_router(FakeUpdate(user_id, "attack goblin again", sink2), DummyContext())
                self.assertTrue(any("Still working" in m for m in sink2))
            finally:
                bot._USER_BUSY.discard(user_id)

            bot._USER_BUSY.add(user_id)
            try:
                sink3 = []
                await bot.text_message_router(
                    FakeUpdate(user_id, "some dev message", sink3, thread_id=config.TOPIC_DEVELOPMENT_ID),
                    DummyContext(),
                )
                self.assertFalse(any("Still working" in m for m in sink3))
            finally:
                bot._USER_BUSY.discard(user_id)

            bot._USER_BUSY.add(other_user_id)
            try:
                sink4 = []
                await bot.text_message_router(FakeUpdate(user_id, "attack goblin once more", sink4), DummyContext())
                self.assertFalse(any("Still working" in m for m in sink4))
            finally:
                bot._USER_BUSY.discard(other_user_id)

    def test_attack_with_a_named_spell_classifies_as_cast_spell_not_a_weapon_attack(self):
        """
        Real live bug, confirmed twice via dev-topic screenshots
        (2026-08-06): "Attack spider 2 with fire ball" and "Attack
        spider 3 with burning hands" both contain the bare word
        "attack", so _keyword_fallback's generic attack_words match
        claimed them before anything ever looked at what followed
        "with" -- both resolved as a mundane weapon swing, silently
        discarding the named spell (no fire damage, no spell slot
        spent). Sheri's own dev-topic report ("Was supposed to be skill
        fire ball") is exactly this. Fixed by checking for a real known
        spell name (spells.py, never invented) before the generic
        attack match. "fire ball" (two words) must still match the real
        spell "Fireball" (one word) -- the comparison strips spaces on
        both sides for exactly this reason. An ordinary weapon name
        ("long sword") must NOT be reclassified -- it isn't a spell.
        """
        fireball = parse_intents("Attack the spider with fire ball")[0]
        self.assertEqual(fireball["action"], "cast_spell")
        self.assertEqual(fireball["spell_name"], "Fireball")

        burning_hands = parse_intents("Attack spider 3 with burning hands")[0]
        self.assertEqual(burning_hands["action"], "cast_spell")
        self.assertEqual(burning_hands["spell_name"], "Burning Hands")

        weapon = parse_intents("Attack spider 3 with long sword")[0]
        self.assertEqual(weapon["action"], "attack")

    async def test_do_cast_spell_matches_a_spaced_out_spell_name_against_its_real_one_word_name(self):
        """
        Companion fix to the reclassification above: _do_cast_spell's
        own spell-name matching (character["known_spells"]) used an
        exact-substring check that would NOT have matched "fire ball"
        (two words, as players naturally type it) against the real
        spell's real name "Fireball" (one word) -- reclassifying to
        cast_spell alone would have just traded one wrong outcome
        (silent weapon attack) for another (an incorrect "you don't
        know a spell by that name", even though the character genuinely
        knows Fireball). _text_mentions_spell strips spaces on both
        sides to close this gap. Verified end-to-end: a real cast
        through _do_cast_spell with the exact natural two-word phrasing
        actually spends a real spell slot, confirming the spell was
        truly recognized and cast, not just that no error was raised.
        """
        import sessions
        sessions.end_session(-999)
        caster_id = 950900
        make_basic_character(
            caster_id, "FireballCaster", char_class="Wizard",
            known_spells=["fireball"], spell_slots_max=3, current_location="crossroads_tavern",
        )
        db.update_character(caster_id, -999, spell_slots_current=3)
        goblin = {"telegram_user_id": -5200900, "name": "SpaceGoblin", "dexterity": 10, "strength": 10,
                  "armor_class": 12, "hp_current": 20, "hp_max": 20, "conditions": [],
                  "is_ai": 1, "monster_key": "goblin"}
        caster = db.get_character(caster_id, -999)
        caster["telegram_user_id"] = caster_id
        session = sessions.start_session(-999, [caster, goblin], {caster_id: "party", -5200900: "enemy"})
        session.turn_order = [caster_id, -5200900]

        sink = []
        await bot._do_cast_spell(
            FakeUpdate(caster_id, "Attack the goblin with fire ball", sink),
            "Attack the goblin with fire ball",
        )
        after = db.get_character(caster_id, -999)
        self.assertEqual(after["spell_slots_current"], 2)
        self.assertTrue(any("Fireball" in m for m in sink))
        sessions.end_session(-999)

    def test_build_application_raises_telegram_api_timeouts_above_library_defaults(self):
        """
        Real live bug (2026-08-06, Coffee via dev-topic screenshot: "It
        gave two attacks. Is this a bug?" -- the exact same "Round 4 --
        It's now Ravenloft's turn!" battle-menu prompt sent twice, back
        to back). Confirmed via bot_live_tmp.log at the exact timestamp:
        a genuine "[message] send failed, retrying: TimedOut('Timed
        out')" right before the duplicate. python-telegram-bot's
        HTTPXRequest defaults to a 5s connect/read/write timeout (1s
        pool timeout) for every outbound Telegram API call -- too tight
        for this box's own already-documented characteristics (root
        filesystem on a USB flash drive, plus a CPU-bound local Ollama
        server this same process calls out to mid-request). _safe_send's
        retry-on-TimedOut can't tell "genuinely failed" from "succeeded
        but the ack was slow" apart (Telegram's sendMessage has no
        idempotency key), so a merely-slow-but-successful send gets
        retried and duplicated. Raising these timeouts doesn't eliminate
        that possibility, but makes a false timeout meaningfully less
        likely -- the same reasoning already applied to every Ollama
        call in ai/*.py (see CLAUDE.md). build_application() itself
        makes no network calls and doesn't touch bot_persistence.pickle
        (PicklePersistence loads lazily, only on Application.initialize(),
        never called here), so this is safe to build directly in a test.
        """
        app = bot.build_application()
        request = app.bot.request
        self.assertGreater(request._client.timeout.connect, 5.0)
        self.assertGreater(request.read_timeout, 5.0)
        self.assertGreater(request._client.timeout.write, 5.0)
        self.assertGreater(request._client.timeout.pool, 1.0)

    # -- Enemy battle banter (2026-08-09, task #9) -----------------------
    def test_build_prompt_includes_banter_instruction_only_when_requested(self):
        """
        Real live request (2026-08-09, per Coffee): "in battle give the
        enemies small talk banter, teasing, coaxing type narrations to
        keep the fights entertaining, enjoyable, funny." Pure prompt-
        construction check, no Ollama call -- confirms _BANTER_
        INSTRUCTION is actually threaded into the final prompt when
        include_banter=True and completely absent otherwise, so a false
        positive here can't silently mean the flag never reaches the
        model.
        """
        from ai.dm_agent import _BANTER_INSTRUCTION, _build_prompt
        character = {"name": "Grubnak", "char_class": None, "hp_current": 20, "hp_max": 20}
        result = {"hit": True, "damage_dealt": 5, "raw_roll": 12}

        with_banter = _build_prompt(character, "attacks Fenwick", result, include_banter=True)
        self.assertIn(_BANTER_INSTRUCTION, with_banter)

        without_banter = _build_prompt(character, "attacks Fenwick", result, include_banter=False)
        self.assertNotIn(_BANTER_INSTRUCTION, without_banter)
        default_omits = _build_prompt(character, "attacks Fenwick", result)
        self.assertNotIn(_BANTER_INSTRUCTION, default_omits, "include_banter must default to off")

    async def test_enemy_banter_only_rolled_for_enemy_side_attackers(self):
        """
        _post_narrated must only ever ask narrate_action for banter when
        the attacker is on the "enemy" side of the fight -- never a real
        player, and never a friendly AI-controlled party companion, even
        though both take their combat turn through this exact same
        function (see _resolve_ai_turns). Forces random.random() to 0.0
        (always below ENEMY_BANTER_CHANCE) so this tests the actual side
        check, not dice luck.
        """
        from unittest.mock import patch
        import sessions
        sessions.end_session(-997)
        enemy_id = -2_500_080
        companion_id = -2_500_081
        monster = {"telegram_user_id": enemy_id, "name": "BanterGoblin", "dexterity": 10, "strength": 10,
                   "hp_current": 20, "hp_max": 20, "armor_class": 10, "is_ai": 1, "monster_key": "goblin"}
        companion = {"telegram_user_id": companion_id, "name": "FriendlyAI", "dexterity": 10, "strength": 10,
                     "hp_current": 20, "hp_max": 20, "armor_class": 10, "is_ai": 1}
        session = sessions.start_session(-997, [monster, companion], {enemy_id: "enemy", companion_id: "party"})
        mechanical_result = {"hit": True, "damage_dealt": 5, "attacker": "BanterGoblin", "defender": "FriendlyAI",
                              "raw_roll": 15, "critical_hit": False, "critical_fail": False}

        captured = {}

        def fake_narrate_action(character, action_text, mech_result, recent_events=None,
                                 actor_personality=None, location_description=None, include_banter=False):
            captured["include_banter"] = include_banter
            return "The goblin swings."

        sink = []
        update = FakeUpdate(enemy_id, "n/a", sink)

        with patch("bot.narrate_action", fake_narrate_action), patch("bot.random.random", return_value=0.0):
            await bot._post_narrated(update, monster, "attacks FriendlyAI", mechanical_result, session)
        self.assertTrue(captured["include_banter"], "enemy-side attacker with a below-threshold roll should get banter")

        with patch("bot.narrate_action", fake_narrate_action), patch("bot.random.random", return_value=0.0):
            await bot._post_narrated(update, companion, "attacks BanterGoblin", mechanical_result, session)
        self.assertFalse(captured["include_banter"], "party-side (friendly AI companion) attacker must never get banter")

        sessions.end_session(-997)

    async def test_enemy_banter_respects_the_random_roll_not_always_on(self):
        """
        Even for a genuine enemy-side attacker, banter must only fire on
        a fraction of turns (ENEMY_BANTER_CHANCE) -- an above-threshold
        roll must NOT request banter, confirming this isn't accidentally
        wired to fire on every single enemy attack.
        """
        from unittest.mock import patch
        import sessions
        sessions.end_session(-996)
        enemy_id = -2_500_082
        monster = {"telegram_user_id": enemy_id, "name": "QuietGoblin", "dexterity": 10, "strength": 10,
                   "hp_current": 20, "hp_max": 20, "armor_class": 10, "is_ai": 1, "monster_key": "goblin"}
        session = sessions.start_session(-996, [monster], {enemy_id: "enemy"})
        mechanical_result = {"hit": True, "damage_dealt": 5, "attacker": "QuietGoblin", "defender": "Someone",
                              "raw_roll": 10, "critical_hit": False, "critical_fail": False}

        captured = {}

        def fake_narrate_action(character, action_text, mech_result, recent_events=None,
                                 actor_personality=None, location_description=None, include_banter=False):
            captured["include_banter"] = include_banter
            return "The goblin swings."

        sink = []
        update = FakeUpdate(enemy_id, "n/a", sink)
        with patch("bot.narrate_action", fake_narrate_action), patch("bot.random.random", return_value=0.999):
            await bot._post_narrated(update, monster, "attacks Someone", mechanical_result, session)
        self.assertFalse(captured["include_banter"])
        sessions.end_session(-996)

    # -- Battle formation image (2026-08-09, task #9-followup) -----------
    def test_render_battle_formation_produces_a_valid_png_reflecting_real_state(self):
        """
        Real live request (per Coffee): "show the formations and
        locations of where the players are battling." Pure rendering
        check, no bot.py/network involved -- confirms real PNG bytes
        come back at the expected canvas size for a real front/back,
        boss-included roster, so a false positive here can't silently
        mean the renderer is broken.
        """
        import io
        from PIL import Image
        import battle_render
        party = [
            {"name": "Ravenloft", "hp_current": 40, "hp_max": 40, "formation_row": "front"},
            {"name": "Willowmere", "hp_current": 18, "hp_max": 30, "formation_row": "back"},
        ]
        enemies = [
            {"name": "Goblin Boss", "hp_current": 60, "hp_max": 93, "formation_row": "front", "is_boss": True},
            {"name": "Goblin Shaman", "hp_current": 10, "hp_max": 25, "formation_row": "back"},
        ]
        png_bytes = battle_render.render_battle_formation(party, enemies)
        self.assertGreater(len(png_bytes), 0)
        image = Image.open(io.BytesIO(png_bytes))
        image.verify()
        image = Image.open(io.BytesIO(png_bytes))
        self.assertEqual(image.format, "PNG")
        self.assertEqual(image.size, (battle_render.CANVAS_WIDTH, battle_render.CANVAS_HEIGHT))

    def test_render_battle_formation_handles_a_single_combatant_per_side(self):
        """A 1v1 (the most common real fight shape) must render without error -- no divide-by-zero on row spacing."""
        import io
        from PIL import Image
        import battle_render
        png_bytes = battle_render.render_battle_formation(
            [{"name": "Solo", "hp_current": 20, "hp_max": 20, "formation_row": "front"}],
            [{"name": "Goblin", "hp_current": 25, "hp_max": 25, "formation_row": "front"}],
        )
        image = Image.open(io.BytesIO(png_bytes))
        self.assertEqual(image.format, "PNG")

    def test_battle_formation_crowded_row_tokens_dont_overlap(self):
        """
        Real live bug (2026-08-09, Coffee, Development-topic screenshot):
        4 party members stacked in the front row rendered with each
        token's circle overlapping the name/HP-bar text of the token
        above it -- the old code always drew every token at a fixed
        40px radius and just divided the available height evenly, which
        for 4+ per row left far less room than a full-size token's
        label/bar block actually needs. _row_token_radius now shrinks
        the token (and everything scaled off it) only as much as a
        crowded row needs; confirm directly that for a real 4-per-row
        formation shaped like the reported one, the computed radius is
        small enough that consecutive token centers are spaced further
        apart than one token's own diameter (i.e. the circles themselves
        can't overlap, and by construction via _FOOTPRINT_RATIO neither
        can the label/bar block below them).
        """
        import battle_render
        top_margin, bottom_margin = 100, 60
        canvas_height = battle_render._canvas_height(4)
        usable_height = canvas_height - top_margin - bottom_margin
        radius = battle_render._row_token_radius(4, usable_height)
        spacing = usable_height / 4
        self.assertGreaterEqual(spacing, radius * 2, "tokens are close enough their circles overlap")
        self.assertGreaterEqual(radius, battle_render._TOKEN_RADIUS_MIN)

    def test_battle_formation_skips_a_leading_article_for_a_meaningful_label(self):
        """
        Real live bug (2026-08-10, Coffee, dev-bridge screenshot): 4
        copies of a boss whose real name starts with "The" (fighting
        "The Unspoken") all rendered with an identical, useless "T"
        initial letter and "The" label -- both the token's big initial
        and its short name label took the LITERAL first word, and
        "The" is a real, common naming convention in this campaign (9
        of 56 monsters, overwhelmingly bosses: "The Unspoken," "The
        Waking Ember," "The Colosseum Champion," etc.). Confirms both
        the label helper and the initial-letter source skip a leading
        article and use the next real word instead.
        """
        import battle_render
        self.assertEqual(battle_render._meaningful_first_word("The Unspoken"), "Unspoken")
        self.assertEqual(battle_render._meaningful_first_word("The Waking Ember"), "Waking")
        self.assertEqual(battle_render._meaningful_first_word("A Nameless Dread"), "Nameless")
        # A name that's genuinely just "The" alone (no real second
        # word) falls back to the article itself rather than crashing
        # or returning empty.
        self.assertEqual(battle_render._meaningful_first_word("The"), "The")
        # Real, ordinary names (no leading article) are unaffected.
        self.assertEqual(battle_render._meaningful_first_word("Grimsby"), "Grimsby")
        self.assertEqual(battle_render._first_name("The Unspoken"), "Unspoken")

    def test_battle_formation_uses_first_name_to_save_space(self):
        """Real request (Coffee): "you can use first names to save in spacing."""
        import battle_render
        self.assertEqual(battle_render._first_name("Brandywine Fieldstone"), "Brandywine")
        self.assertEqual(battle_render._first_name("Grimsby"), "Grimsby")

    def test_condition_badge_text_reflects_real_active_conditions(self):
        """
        Self-initiated visual improvement (2026-08-10): sessions.py's
        real in-memory conditions (prone/poisoned/paralyzed/etc, see
        bot.py's condition-setting code around _apply_condition) were
        tracked and narrated in text but never shown on the formation
        image -- a player had to scroll back to remember who was
        currently prone mid-fight. _condition_badge_text is the pure
        (non-Pillow) piece of that feature -- direct, fast checks here.
        """
        import battle_render
        self.assertIsNone(battle_render._condition_badge_text(None))
        self.assertIsNone(battle_render._condition_badge_text([]))
        # An unrecognized/future condition string must never crash the
        # renderer -- silently omitted rather than shown as a raw key.
        self.assertIsNone(battle_render._condition_badge_text(["some_future_condition"]))
        self.assertEqual(battle_render._condition_badge_text(["prone"]), "PRONE")
        self.assertEqual(
            battle_render._condition_badge_text(["prone", "poisoned"]), "PRONE • POISONED",
        )

    def test_battle_formation_image_renders_cleanly_with_active_conditions(self):
        """
        End-to-end: a combatant with real active conditions must not
        break image rendering -- confirms render_battle_formation still
        returns valid PNG bytes with the new condition-badge drawing
        step wired into _draw_token.
        """
        import battle_render
        party = [{"name": "Pan", "hp_current": 50, "hp_max": 100, "conditions": ["prone", "poisoned"]}]
        enemies = [{"name": "Goblin", "hp_current": 10, "hp_max": 30, "conditions": []}]
        png_bytes = battle_render.render_battle_formation(party, enemies)
        self.assertTrue(png_bytes.startswith(b"\x89PNG"))

    def test_icon_key_for_combatant_prefers_class_falls_back_to_damage_type(self):
        """
        Real live request (2026-08-10, Coffee: "Is it possible too use
        face profile icons instead of letters?!"). Real portraits would
        need a per-combatant network image-generation call (at odds
        with this file's whole "instant, network-free" point) or an
        invented monster appearance this codebase never allows --
        instead, procedurally drawn pictograms keyed off REAL, already-
        existing facts: a party member's own char_class, or an enemy's
        own damage_type. Pure resolver checks, no Pillow needed.
        """
        import battle_render
        self.assertEqual(battle_render._icon_key_for_combatant({"char_class": "Wizard"}), "wizard")
        self.assertEqual(battle_render._icon_key_for_combatant({"char_class": "wizard"}), "wizard")
        self.assertEqual(battle_render._icon_key_for_combatant({"damage_type": "fire"}), "fire")
        # char_class wins when a dict somehow has both (never happens
        # in practice -- enemies don't have char_class, party members
        # don't have damage_type -- but the resolver order should
        # still be deterministic, not accidental).
        self.assertEqual(battle_render._icon_key_for_combatant({"char_class": "rogue", "damage_type": "fire"}), "rogue")
        # An unrecognized/missing value -- e.g. a companion mid-load,
        # or a genuinely new damage_type this file hasn't been taught
        # yet -- must fall back to None, never a crash or a made-up key.
        self.assertIsNone(battle_render._icon_key_for_combatant({}))
        self.assertIsNone(battle_render._icon_key_for_combatant({"char_class": "not_a_real_class"}))
        self.assertIsNone(battle_render._icon_key_for_combatant({"damage_type": "thunder"}))

    def test_every_real_class_and_damage_type_has_a_working_icon(self):
        """
        Every one of the 12 real classes (rules/leveling.CLASS_HIT_DICE)
        and every damage_type this codebase's own monster templates or
        ECHO_TRIAL_RESISTANT_TYPES actually use must have a real,
        crash-free icon -- confirmed by rendering each one directly
        against a real PIL ImageDraw surface (not just checking the
        dict has a key, in case a draw function itself has a bug that
        only Pillow would catch, e.g. a bad point count for .polygon()).
        """
        import battle_render
        from PIL import Image, ImageDraw
        from rules.leveling import CLASS_HIT_DICE
        img = Image.new("RGB", (100, 100))
        draw = ImageDraw.Draw(img)
        for char_class in CLASS_HIT_DICE:
            drawn = battle_render._draw_icon(draw, 50, 50, 40, char_class, background=(59, 110, 168))
            self.assertTrue(drawn, f"no icon for real class {char_class!r}")
        for damage_type in ["physical", "fire", "cold", "lightning", "poison",
                             "necrotic", "radiant", "force", "psychic"]:
            drawn = battle_render._draw_icon(draw, 50, 50, 40, damage_type, background=(168, 59, 59))
            self.assertTrue(drawn, f"no icon for real damage_type {damage_type!r}")
        # A radius at the crowded-row minimum must not crash either.
        for char_class in CLASS_HIT_DICE:
            battle_render._draw_icon(draw, 50, 50, battle_render._TOKEN_RADIUS_MIN, char_class, background=(59, 110, 168))

    def test_battle_formation_falls_back_to_initial_letter_without_a_real_class_or_damage_type(self):
        """
        A combatant with no char_class/damage_type at all (an older
        session snapshot from before this feature, or a genuinely
        untagged monster) must still render -- the existing
        initial-letter treatment, never a blank token.
        """
        import battle_render
        party = [{"name": "Mystery", "hp_current": 10, "hp_max": 10}]
        enemies = [{"name": "Blob", "hp_current": 5, "hp_max": 5}]
        png_bytes = battle_render.render_battle_formation(party, enemies)
        self.assertTrue(png_bytes.startswith(b"\x89PNG"))

    def test_battle_formation_renders_real_class_and_damage_type_icons_end_to_end(self):
        """Real end-to-end smoke test across every class/damage_type, matching the actual combatant shape sessions.py produces."""
        import battle_render
        from rules.leveling import CLASS_HIT_DICE
        party = [{"name": c.capitalize(), "hp_current": 50, "hp_max": 100, "char_class": c,
                  "formation_row": "front" if i % 2 == 0 else "back"}
                 for i, c in enumerate(CLASS_HIT_DICE)]
        enemies = [{"name": d.capitalize(), "hp_current": 30, "hp_max": 60, "damage_type": d,
                    "formation_row": "front" if i % 2 == 0 else "back"}
                   for i, d in enumerate(["physical", "fire", "cold", "lightning", "poison",
                                          "necrotic", "radiant", "force", "psychic"])]
        png_bytes = battle_render.render_battle_formation(party, enemies)
        self.assertTrue(png_bytes.startswith(b"\x89PNG"))

    def test_location_background_composites_and_falls_back_cleanly(self):
        """
        Real live request (Task #14, per Coffee: "instead of a plain
        background can we use location background?"). battle_render.py
        stays network-free itself -- bot.py's caller fetches the real
        location image bytes and hands them in. Tests here use a
        synthetic in-memory JPEG (no network call) to verify: (1) a
        real photo composites and cover-fits without distortion or a
        crash, (2) corrupt/non-image bytes fall back to the original
        plain gradient rather than breaking the whole formation image,
        (3) None (no bytes at all, e.g. the fetch failed) behaves
        identically to before this feature existed.
        """
        import battle_render
        from PIL import Image
        import io
        fake_photo = Image.new("RGB", (640, 480), (100, 150, 200))
        buf = io.BytesIO()
        fake_photo.save(buf, format="JPEG")
        fake_bytes = buf.getvalue()

        party = [{"name": "Pan", "hp_current": 50, "hp_max": 100, "formation_row": "front"}]
        enemies = [{"name": "Goblin", "hp_current": 20, "hp_max": 30, "formation_row": "front"}]

        with_bg = battle_render.render_battle_formation(party, enemies, background_image_bytes=fake_bytes)
        self.assertTrue(with_bg.startswith(b"\x89PNG"))

        without_bg = battle_render.render_battle_formation(party, enemies, background_image_bytes=None)
        self.assertTrue(without_bg.startswith(b"\x89PNG"))

        bad_bytes = battle_render.render_battle_formation(party, enemies, background_image_bytes=b"not a real image")
        self.assertTrue(bad_bytes.startswith(b"\x89PNG"))

    def test_composite_location_background_cover_fits_without_distorting(self):
        """
        Direct check of the actual compositing math: a photo with a
        DIFFERENT aspect ratio than the canvas must be cropped to fill
        it completely (cover-fit), never squashed/stretched -- confirms
        the pasted region is exactly the real canvas size, and that a
        genuinely corrupt image returns False (caller's cue to fall
        back) rather than raising.
        """
        import battle_render
        from PIL import Image
        import io
        # Deliberately square photo against this file's wide canvas --
        # a real mismatch, same shape real location art would have.
        square_photo = Image.new("RGB", (500, 500), (10, 20, 30))
        buf = io.BytesIO()
        square_photo.save(buf, format="PNG")
        canvas = Image.new("RGB", (battle_render.CANVAS_WIDTH, battle_render.CANVAS_HEIGHT), (0, 0, 0))
        composited = battle_render._composite_location_background(canvas, battle_render.CANVAS_HEIGHT, buf.getvalue())
        self.assertTrue(composited)
        self.assertEqual(canvas.size, (battle_render.CANVAS_WIDTH, battle_render.CANVAS_HEIGHT))

        canvas2 = Image.new("RGB", (battle_render.CANVAS_WIDTH, battle_render.CANVAS_HEIGHT), (0, 0, 0))
        self.assertFalse(battle_render._composite_location_background(canvas2, battle_render.CANVAS_HEIGHT, b"garbage"))

    async def test_fetch_location_background_bytes_returns_real_bytes_on_success(self):
        """
        Direct check of _fetch_location_background_bytes's success
        path -- requests.get mocked to a real, valid (synthetic) JPEG
        response (not just a bare SimpleNamespace, so .raise_for_status()
        and .content both behave like the real requests.Response they
        stand in for). Confirms the real location's own description
        genuinely drives the prompt (same grounding discipline as
        _location_image_prompt) and that the returned bytes are exactly
        what the mocked response provided, unmodified.
        """
        from unittest.mock import patch, Mock
        from PIL import Image
        import io
        fake_photo = Image.new("RGB", (100, 100), (5, 10, 15))
        buf = io.BytesIO()
        fake_photo.save(buf, format="JPEG")
        fake_bytes = buf.getvalue()
        fake_response = Mock(content=fake_bytes)
        fake_response.raise_for_status = Mock(return_value=None)

        with patch("bot.requests.get", return_value=fake_response) as mock_get:
            result = await bot._fetch_location_background_bytes("crossroads_tavern")

        self.assertEqual(result, fake_bytes)
        self.assertEqual(mock_get.call_count, 1)
        # The real URL must be built from the same real prompt
        # _location_image_prompt uses -- never invented, same grounding
        # discipline as every other _maybe_send_*_image prompt here.
        called_url = mock_get.call_args[0][0]
        self.assertIn("environment%20concept%20art", called_url)

    async def test_fetch_location_background_bytes_never_raises_on_a_bad_response(self):
        """
        A malformed/unexpected response shape (missing .raise_for_status,
        same as a genuinely broken API response could look) must
        degrade to None, never propagate an exception -- confirmed live
        via faulthandler after this exact gap let a mocked-but-
        incomplete response object silently break the WHOLE formation
        image send (background AND tokens) in
        test_battle_formation_image_sent_when_combat_starts before the
        except clause here was broadened from requests.RequestException
        to a plain Exception.
        """
        from unittest.mock import patch
        from types import SimpleNamespace
        with patch("bot.requests.get", return_value=SimpleNamespace(status_code=200)):
            result = await bot._fetch_location_background_bytes("crossroads_tavern")
        self.assertIsNone(result)

    async def test_fetch_location_background_bytes_returns_none_for_an_unknown_location(self):
        result = await bot._fetch_location_background_bytes("not_a_real_location_id")
        self.assertIsNone(result)

    def test_battle_formation_all_circles_share_one_uniform_size(self):
        """
        Real live request (2026-08-10, Coffee: "make all the players
        circle in formations the same size"). Before this, radius was
        computed PER ROW off that row's own member count -- a crowded
        4-member front row shrank while a sparser 2-member back row (or
        a small enemy side) stayed full-size, so the same image had
        visibly different-sized circles. render_battle_formation now
        sizes every token off the single most-crowded row across BOTH
        sides. Confirmed directly by spying on _draw_token (the real
        function that actually draws each circle) across a genuinely
        uneven formation -- 4 in the party's front row, 2 in its back
        row, 2 enemies -- and asserting every single call received the
        IDENTICAL radius, not one that varies by which row/side it's
        on.
        """
        import battle_render
        from unittest.mock import patch

        party = [
            {"name": "Pan", "hp_current": 129, "hp_max": 129, "formation_row": "back"},
            {"name": "Vesh", "hp_current": 282, "hp_max": 282, "formation_row": "back"},
            {"name": "Brandywine", "hp_current": 90, "hp_max": 100, "formation_row": "front"},
            {"name": "Wrenford", "hp_current": 80, "hp_max": 100, "formation_row": "front"},
            {"name": "Chelara", "hp_current": 70, "hp_max": 100, "formation_row": "front"},
            {"name": "Grask", "hp_current": 619, "hp_max": 619, "formation_row": "front"},
        ]
        enemies = [
            {"name": "Giant Spider 4", "hp_current": 60, "hp_max": 60, "formation_row": "front"},
            {"name": "Giant Spider 3", "hp_current": 55, "hp_max": 60, "formation_row": "front"},
        ]

        real_draw_token = battle_render._draw_token
        seen_radii = []

        def spy(draw, x, y, combatant, color, radius):
            seen_radii.append(radius)
            return real_draw_token(draw, x, y, combatant, color, radius)

        with patch("battle_render._draw_token", side_effect=spy):
            battle_render.render_battle_formation(party, enemies)

        self.assertEqual(len(seen_radii), 8, "expected one _draw_token call per combatant")
        self.assertEqual(len(set(seen_radii)), 1, f"circles rendered at more than one size: {seen_radii}")
        # And that shared size must actually be the SHRUNK one (fit for
        # the 4-member row), not the original full 40px -- otherwise
        # this test would trivially pass even if the bug regressed back
        # to "every row happens to get the same count."
        self.assertLess(seen_radii[0], battle_render._TOKEN_RADIUS)

    def test_battle_formation_sparse_row_keeps_the_original_full_size(self):
        """A normal, non-crowded 1-2 per row fight must render identically to before -- no unnecessary shrinking."""
        import battle_render
        self.assertEqual(battle_render._canvas_height(2), battle_render.CANVAS_HEIGHT)
        top_margin, bottom_margin = 100, 60
        usable_height = battle_render.CANVAS_HEIGHT - top_margin - bottom_margin
        self.assertEqual(battle_render._row_token_radius(2, usable_height), battle_render._TOKEN_RADIUS)

    async def test_maybe_send_battle_formation_image_sends_a_real_png_from_session_state(self):
        """
        bot._maybe_send_battle_formation_image must pull the REAL, live
        session state (session.living_on_side, which already excludes
        anyone at 0 HP) rather than anything invented, and hand back
        real decodable PNG bytes as the sent photo. _fetch_location_
        background_bytes (Task #14, a real Pollinations network call)
        is mocked to None -- this test cares about the token/HP layout
        being real, not the background art, same "never touch a real
        network call in the regression suite" convention as elsewhere.
        """
        import io
        from unittest.mock import patch, AsyncMock
        from PIL import Image
        import sessions
        sessions.end_session(-995)
        player_id = 900520
        make_basic_character(player_id, "FormationTester", chat_id=-995, current_location="crossroads_tavern")
        player = db.get_character(player_id, -995)
        player["telegram_user_id"] = player_id
        enemy = {"telegram_user_id": -2_500_090, "name": "FormationGoblin", "dexterity": 10, "strength": 10,
                 "hp_current": 25, "hp_max": 25, "armor_class": 10, "is_ai": 1, "monster_key": "goblin"}
        session = sessions.start_session(-995, [player, enemy], {player_id: "party", -2_500_090: "enemy"})

        sink = []
        update = FakeUpdate(player_id, "n/a", sink, chat_id=-995)
        with patch("bot._fetch_location_background_bytes", new=AsyncMock(return_value=None)):
            await bot._maybe_send_battle_formation_image(update, session)

        self.assertEqual(len(update.effective_chat.sent_photos), 1)
        photo = update.effective_chat.sent_photos[0]
        image = Image.open(io.BytesIO(photo["photo"]))
        self.assertEqual(image.format, "PNG")
        self.assertIn("formation", photo["caption"].lower())
        sessions.end_session(-995)

    async def test_maybe_send_battle_formation_image_skips_when_a_side_is_wiped(self):
        """
        Defensive guard: if this were ever called with one side already
        at 0 HP (living_on_side excludes them), it must skip silently
        rather than render a one-sided/empty diagram.
        """
        import sessions
        sessions.end_session(-994)
        player_id = 900521
        make_basic_character(player_id, "AloneTester", chat_id=-994, current_location="crossroads_tavern")
        player = db.get_character(player_id, -994)
        player["telegram_user_id"] = player_id
        enemy = {"telegram_user_id": -2_500_091, "name": "DeadGoblin", "dexterity": 10, "strength": 10,
                 "hp_current": 25, "hp_max": 25, "armor_class": 10, "is_ai": 1, "monster_key": "goblin"}
        session = sessions.start_session(-994, [player, enemy], {player_id: "party", -2_500_091: "enemy"})
        dead_enemy = next(p for p in session.participants if p["telegram_user_id"] == -2_500_091)
        dead_enemy["hp_current"] = 0

        sink = []
        update = FakeUpdate(player_id, "n/a", sink, chat_id=-994)
        await bot._maybe_send_battle_formation_image(update, session)
        self.assertEqual(len(update.effective_chat.sent_photos), 0)
        sessions.end_session(-994)

    async def test_battle_formation_image_sent_when_combat_starts(self):
        """
        End-to-end wiring check: a real combat start (_do_start_combat)
        must send both the existing monster-art image AND the new
        battle-formation image. requests.get is mocked (same pattern as
        test_send_generated_image_pre_warms_the_url_before_handing_it_
        to_telegram) so this never depends on a real Pollinations
        network call -- only the LOCAL battle_render call is real.
        _resolve_ai_turns is also mocked out (2026-08-10, found via
        faulthandler.dump_traceback_later after this exact test hung
        past its own timeout): whether this passes or hangs depended
        on a real, unforced initiative coin-flip -- if the single
        goblin enemy happened to win it, _resolve_ai_turns made it act
        immediately, touching real Ollama narration this test never
        accounted for. Pre-existing gap in this test, not something
        this task introduced, but blocks reliably verifying the new
        background-image wiring below it either way.
        """
        from unittest.mock import patch, AsyncMock
        import sessions
        sessions.end_session(-999)
        user_id = 900522
        character = make_basic_character(user_id, "ComboStarter", current_location="crossroads_tavern")

        def fake_get(url, timeout=None):
            return SimpleNamespace(status_code=200, headers={"content-type": "image/jpeg"})

        sink = []
        with patch("bot._get_combat_eligible_party_members", return_value=[character]), \
             patch("bot.requests.get", side_effect=fake_get), \
             patch("bot._resolve_ai_turns", new=AsyncMock()):
            await bot._do_start_combat(FakeUpdate(user_id, "fight a goblin", sink), monster_key="goblin", count=1)

        # sink only captures each sent photo's caption marker, same
        # convention test_monster_image_not_duplicated_when_enemy_
        # attacks_first already uses -- good enough to confirm both
        # images actually fired.
        photo_markers = [line for line in sink if line.startswith("<photo:")]
        self.assertTrue(any("Goblin" in m for m in photo_markers), f"monster art missing: {sink}")
        self.assertTrue(any("formation" in m.lower() for m in photo_markers), f"battle formation image missing: {sink}")

    async def test_battle_formation_image_refreshes_exactly_once_per_completed_round(self):
        """
        Task #10 (per Coffee follow-up): the battle-formation image
        previously only ever sent once, at combat start, and never
        reflected genuine mid-fight HP/formation changes. _resolve_ai_
        turns is now a thin wrapper (_resolve_ai_turns_inner does the
        real work) that compares session.round_number before/after its
        own call and sends a fresh formation image only if at least one
        full round actually completed during it -- same 3-identical-
        enemies rig as test_monster_image_shown_once_for_multiple_
        identical_enemies (human already "went", all 3 spiders resolve
        back to back, current_turn_index wraps back to the human at the
        end, which is exactly one round completing), confirming the
        image posts exactly ONCE for the whole call, not once per
        spider turn and not zero times.
        """
        from unittest.mock import patch
        import sessions
        sessions.end_session(-993)
        human_id = 900932
        make_basic_character(human_id, "RoundRefreshTester", chat_id=-993, current_location="crossroads_tavern", hp_max=300)
        human = db.get_character(human_id, -993)
        spiders = [
            {
                "telegram_user_id": -700800 - i, "name": f"Giant Spider {i + 1}", "is_ai": True,
                "hp_current": 108, "hp_max": 108, "armor_class": 14,
                "strength": 14, "dexterity": 16, "proficiency_bonus": 3,
                "monster_key": "giant_spider", "xp_reward": 200, "conditions": [],
            }
            for i in range(3)
        ]
        sides = {human_id: "party"}
        for spider in spiders:
            sides[spider["telegram_user_id"]] = "enemy"
        session = sessions.start_session(-993, [human] + spiders, sides)
        session.turn_order = [human_id] + [s["telegram_user_id"] for s in spiders]
        session.current_turn_index = 1  # first spider's turn is up next, human already "went"
        round_before = session.round_number

        sink = []
        update = FakeUpdate(human_id, "irrelevant", sink, chat_id=-993)

        def fake_get(url, timeout=None):
            return SimpleNamespace(status_code=200, headers={"content-type": "image/jpeg"})

        with patch("bot.requests.get", side_effect=fake_get), \
             patch("bot.narrate_action", return_value="The spider strikes."):
            await bot._resolve_ai_turns(update, session)

        self.assertEqual(session.round_number, round_before + 1, "test setup assumption broke: expected exactly one round to complete")
        formation_markers = [line for line in sink if line.startswith("<photo:") and "formation" in line.lower()]
        self.assertEqual(
            len(formation_markers), 1,
            f"expected exactly 1 formation-image refresh for one completed round, got {len(formation_markers)}: {sink}",
        )
        sessions.end_session(-993)

    async def test_battle_formation_image_does_not_refresh_mid_round(self):
        """
        Companion to the round-refresh test above: if _resolve_ai_turns
        resolves an AI turn WITHOUT completing a full round (the next
        turn belongs to another AI, not wrapping back to the first
        participant), no formation-image refresh should fire at all --
        it's not free to call battle_render on every single turn, only
        when the state has genuinely moved a full round forward.
        """
        from unittest.mock import patch
        import sessions
        sessions.end_session(-992)
        human_id = 900933
        make_basic_character(human_id, "NoMidRoundTester", chat_id=-992, current_location="crossroads_tavern", hp_max=300)
        human = db.get_character(human_id, -992)
        spiders = [
            {
                "telegram_user_id": -700900 - i, "name": f"Giant Spider {i + 1}", "is_ai": True,
                "hp_current": 108, "hp_max": 108, "armor_class": 14,
                "strength": 14, "dexterity": 16, "proficiency_bonus": 3,
                "monster_key": "giant_spider", "xp_reward": 200, "conditions": [],
            }
            for i in range(3)
        ]
        sides = {human_id: "party"}
        for spider in spiders:
            sides[spider["telegram_user_id"]] = "enemy"
        session = sessions.start_session(-992, [human] + spiders, sides)
        # Human "acted" but the mock only lets ONE spider resolve before
        # a second human turn is next -- achieved by putting a human
        # placeholder right after the first spider so the loop stops
        # there, never wrapping back to turn_order[0].
        session.turn_order = [human_id, spiders[0]["telegram_user_id"], human_id, spiders[1]["telegram_user_id"]]
        session.current_turn_index = 1  # first (only) spider's turn is up next
        round_before = session.round_number

        sink = []
        update = FakeUpdate(human_id, "irrelevant", sink, chat_id=-992)

        def fake_get(url, timeout=None):
            return SimpleNamespace(status_code=200, headers={"content-type": "image/jpeg"})

        with patch("bot.requests.get", side_effect=fake_get), \
             patch("bot.narrate_action", return_value="The spider strikes."):
            await bot._resolve_ai_turns(update, session)

        self.assertEqual(session.round_number, round_before, "test setup assumption broke: round shouldn't have advanced")
        formation_markers = [line for line in sink if line.startswith("<photo:") and "formation" in line.lower()]
        self.assertEqual(formation_markers, [], f"formation image should not refresh mid-round: {sink}")
        sessions.end_session(-992)

    async def test_battle_formation_image_sent_when_echo_trial_starts(self):
        """
        Task #10: v1 of the battle-formation image only ever wired into
        the primary wandering-encounter path (_do_start_combat) -- the
        echo-trial combat-start block (_do_start_echo_trial) never sent
        it at all. Real preconditions per _do_start_echo_trial: at
        the_colosseum, a Silver Wardens member, with at least one real
        known_monsters entry to mirror.
        """
        from unittest.mock import patch
        import sessions
        sessions.end_session(-991)
        user_id = 900934
        make_basic_character(user_id, "EchoStarter", chat_id=-991, current_location="the_colosseum")
        db.update_character(user_id, -991, guild="silver_wardens", known_monsters=["goblin"])

        def fake_get(url, timeout=None):
            return SimpleNamespace(status_code=200, headers={"content-type": "image/jpeg"})

        sink = []
        # narrate_action is mocked since an echo could win initiative and
        # act before the human -- deterministic and avoids a real,
        # slow (30-160s+) Ollama call this test has no need for.
        with patch("bot.requests.get", side_effect=fake_get), \
             patch("bot.narrate_action", return_value="The echo lashes out."):
            await bot._do_start_echo_trial(FakeUpdate(user_id, "start an echo trial", sink, chat_id=-991), "start an echo trial")

        formation_markers = [line for line in sink if line.startswith("<photo:") and "formation" in line.lower()]
        self.assertEqual(len(formation_markers), 1, f"battle formation image missing on echo-trial start: {sink}")
        sessions.end_session(-991)

    async def test_battle_formation_image_sent_when_hostile_npc_ambush_starts(self):
        """
        Task #10, second gap: the ambush combat-start block (the
        `disposition == "hostile"` branch inside _maybe_trigger_npc_
        encounter) never sent the battle-formation image either.
        AMBIENT_NPC_ENCOUNTER_CHANCE is patched to 1.0 (deterministic
        trigger) and random.random patched to 0.0 so the "hostile"
        branch is the one actually taken, not the two ambient-flavor
        branches this same function can also pick.
        """
        from unittest.mock import patch
        import sessions
        bot.setup_default_npcs()
        sessions.end_session(-990)
        user_id = 900935
        character = make_basic_character(user_id, "AmbushTarget", chat_id=-990, current_location="crossroads_tavern")

        # Disposition is mocked to "hostile" directly below regardless of
        # this NPC's actual alignment -- only needs a real NPC whose
        # stats block is actually combat-ready (_npc_combatant_from_
        # stats requires proficiency_bonus; the recruitable companions'
        # "stats" blocks are recruitment-preview data only and lack it —
        # kess_the_bandit is the one real NPC built for this).
        hostile_npc_id = next(
            (nid for nid, data in bot.CAMPAIGN["npcs"].items() if "proficiency_bonus" in data.get("stats", {})), None,
        )
        self.assertIsNotNone(hostile_npc_id, "test needs at least one real NPC with combat-ready stats in campaign.json")
        location = cl.get_location(bot.CAMPAIGN, "crossroads_tavern")

        def fake_get(url, timeout=None):
            return SimpleNamespace(status_code=200, headers={"content-type": "image/jpeg"})

        sink = []
        update = FakeUpdate(user_id, "irrelevant", sink, chat_id=-990)
        with patch("bot.requests.get", side_effect=fake_get), \
             patch("bot.AMBIENT_NPC_ENCOUNTER_CHANCE", 1.0), \
             patch("bot.random.random", return_value=0.0), \
             patch("bot._npcs_at_location", return_value=[hostile_npc_id]), \
             patch("bot._effective_disposition", return_value="hostile"), \
             patch("bot.generate_ambient_line", return_value="You won't escape!"), \
             patch("bot.narrate_action", return_value="Kess lunges in."):
            await bot._maybe_trigger_npc_encounter(update, character, location)

        formation_markers = [line for line in sink if line.startswith("<photo:") and "formation" in line.lower()]
        self.assertEqual(len(formation_markers), 1, f"battle formation image missing on ambush start: {sink}")
        sessions.end_session(-990)

    async def test_battle_formation_image_refreshes_when_a_player_joins_an_ongoing_fight(self):
        """
        Real live bug (2026-08-10, Coffee: "im not seeing the formations
        in our current battle"). Root cause: _do_join_battle -- a
        player traveling to and joining an ALREADY-running fight -- only
        ever sent text messages, never refreshed the formation image at
        all. The image is otherwise only sent at the fight's own start
        and on each completed round, so a late joiner (and everyone
        else in the chat) kept seeing a stale roster missing the new
        arrival until the next round happened to complete. No Ollama
        call is on this path (_do_join_battle never resolves a turn).
        _fetch_location_background_bytes (Task #14, a real Pollinations
        network call for the formation image's background art) IS
        mocked, though -- a real network call this test shouldn't
        depend on.
        """
        import sessions
        from unittest.mock import patch, AsyncMock
        sessions.end_session(-989)
        starter_id = 900940
        joiner_id = 900941
        make_basic_character(starter_id, "FightStarter", chat_id=-989, current_location="crossroads_tavern")
        make_basic_character(joiner_id, "LateJoiner", chat_id=-989, current_location="crossroads_tavern")
        starter = db.get_character(starter_id, -989)
        starter["telegram_user_id"] = starter_id
        enemy = {"telegram_user_id": -2_500_099, "name": "JoinTestGoblin", "dexterity": 10, "strength": 10,
                 "hp_current": 25, "hp_max": 25, "armor_class": 10, "is_ai": 1, "monster_key": "goblin"}
        sessions.start_session(-989, [starter, enemy], {starter_id: "party", -2_500_099: "enemy"})

        sink = []
        update = FakeUpdate(joiner_id, "join the battle", sink, chat_id=-989)
        with patch("bot._fetch_location_background_bytes", new=AsyncMock(return_value=None)):
            await bot._do_join_battle(update)

        formation_markers = [line for line in sink if line.startswith("<photo:") and "formation" in line.lower()]
        self.assertEqual(len(formation_markers), 1, f"battle formation image missing on join: {sink}")
        sessions.end_session(-989)

    # -- Task #11, real live request (2026-08-09, Coffee, Development-
    #    topic screenshot): "This does not look like a map. I want an
    #    accurate map... use circles and names with labels" -- the old
    #    _do_show_visual_map generated AI-painted atmospheric art from
    #    Pollinations (task #221), never an accurate schematic. Replaced
    #    with map_render.py, a real local Pillow renderer. ------------
    def test_visible_nodes_and_edges_enforces_fog_of_war(self):
        """
        The core fog-of-war rule, tested directly against real
        campaign.json data rather than through rendered pixels: an edge
        must exist ONLY between two ACTUALLY VISITED locations -- a
        connection leading to an unvisited (or merely map-revealed)
        location must never become a drawn line, since the line itself
        is the real spoiler (mirrors _do_show_map's text-map rule).
        """
        import map_render
        surface = bot.CAMPAIGN["locations"]["surface"]
        # crossroads_tavern's real connections include whispering_wood,
        # stonearch_bridge, market_row, tavern_cellar, tavern_upstairs,
        # the_colosseum (6 total) -- only market_row and the_colosseum
        # are marked visited here.
        visited = {"crossroads_tavern", "market_row", "the_colosseum"}
        revealed = {"the_weeping_well"}  # not connected to crossroads_tavern at all -- must never appear as a node
        visited_here, revealed_here, edges, unexplored = map_render._visible_nodes_and_edges(surface, visited, revealed)

        self.assertEqual(visited_here, sorted(visited))
        self.assertEqual(revealed_here, ["the_weeping_well"])
        self.assertIn(("crossroads_tavern", "market_row"), edges)
        self.assertIn(("crossroads_tavern", "the_colosseum"), edges)
        # whispering_wood/stonearch_bridge/tavern_cellar/tavern_upstairs are real
        # connections but NOT visited -- none of them may appear in any edge.
        for edge in edges:
            for loc_id in edge:
                self.assertIn(loc_id, visited, f"edge {edge} touches an unvisited location -- fog-of-war leak")
        self.assertEqual(unexplored["crossroads_tavern"], 4)  # the 4 real connections that aren't visited

    def test_visited_takes_priority_over_revealed_when_both_are_true(self):
        """A location that's genuinely been visited must never also show up in the weaker revealed-only list."""
        import map_render
        surface = bot.CAMPAIGN["locations"]["surface"]
        visited = {"crossroads_tavern", "market_row"}
        revealed = {"market_row"}  # revealed AND visited -- visited wins
        visited_here, revealed_here, edges, unexplored = map_render._visible_nodes_and_edges(surface, visited, revealed)
        self.assertIn("market_row", visited_here)
        self.assertNotIn("market_row", revealed_here)

    def test_render_layer_map_produces_a_valid_png(self):
        import io
        import map_render
        from PIL import Image
        surface = bot.CAMPAIGN["locations"]["surface"]
        png = map_render.render_layer_map(
            "surface", surface, {"crossroads_tavern", "market_row"}, {"the_weeping_well"}, "crossroads_tavern",
        )
        image = Image.open(io.BytesIO(png))
        self.assertEqual(image.format, "PNG")
        self.assertEqual(image.size, (map_render.CANVAS_WIDTH, map_render.CANVAS_HEIGHT))

    def test_render_layer_map_handles_a_single_visited_location(self):
        """Real edge case: a brand-new character has visited exactly one place (their start) -- must not crash with no edges at all."""
        import map_render
        surface = bot.CAMPAIGN["locations"]["surface"]
        png = map_render.render_layer_map("surface", surface, {"crossroads_tavern"}, set(), "crossroads_tavern")
        self.assertTrue(png)

    def test_render_layer_map_grows_the_canvas_for_a_heavily_explored_layer(self):
        """
        Confirmed visually (2026-08-09): rendering every real location in
        the underground layer (43 total) on the fixed base canvas
        crammed labels into unreadable overlap. The canvas now scales up
        with node count -- this locks that fix in as a real regression
        test rather than something only ever re-caught by eyeballing a
        screenshot again.
        """
        import io
        import map_render
        from PIL import Image
        underground = bot.CAMPAIGN["locations"]["underground"]
        all_ids = set(underground.keys())
        png = map_render.render_layer_map("underground", underground, all_ids, set(), next(iter(all_ids)))
        image = Image.open(io.BytesIO(png))
        self.assertGreater(image.size[0], map_render.CANVAS_WIDTH)
        self.assertLessEqual(image.size[0], map_render._MAX_CANVAS_WIDTH)

    def test_assign_label_sides_alternates_within_the_same_horizontal_band(self):
        """
        The actual overlap-reduction logic, tested directly on plain
        coordinates rather than through rendered pixels: three nodes on
        the same row (y within the ~30px band tolerance) must not all
        get the same label side, or their labels would still collide
        same as before this fix.
        """
        import map_render
        positions = {"a": (100, 200), "b": (200, 202), "c": (300, 198)}
        sides = map_render._assign_label_sides(positions)
        self.assertEqual(set(sides.keys()), {"a", "b", "c"})
        self.assertIn("above", sides.values())
        self.assertIn("below", sides.values())

    def test_stonearch_bridge_to_weeping_well_renders_south(self):
        """
        Real live bug (2026-08-09, Coffee, Development-topic screenshot
        + follow-up): standing at Stonearch Bridge, "to the south of me
        is supposed to be the weeping well but on the map it doesn't
        show that." Root cause: campaign.json's real, structured
        `directions` field ({"south": "the_weeping_well", ...}, present
        on 73/82 real locations) was never read at all -- the layout
        was pure undirected physics. Now seeded from real compass data;
        this asserts the exact reported relationship actually holds in
        the rendered pixel layout, not just that the two nodes exist.
        """
        import map_render
        surface = bot.CAMPAIGN["locations"]["surface"]
        visited = {"crossroads_tavern", "stonearch_bridge", "the_weeping_well", "greymoor_downs"}
        visited_here, revealed_here, edges, unexplored = map_render._visible_nodes_and_edges(surface, visited, set())
        bidi = map_render._bidirectional_directions(surface, set(visited_here))
        raw = map_render._compute_layout(visited_here, edges, bidi)
        bridge_y = raw["stonearch_bridge"][1]
        well_y = raw["the_weeping_well"][1]
        self.assertGreater(well_y, bridge_y, "the Weeping Well must render BELOW (south of) Stonearch Bridge on the canvas y-axis")

    def test_bidirectional_directions_infers_the_logical_reverse(self):
        """
        Real campaign.json data often only declares a direction from
        ONE side (stonearch_bridge says south is the_weeping_well, but
        the_weeping_well doesn't necessarily declare north back) --
        this must fill in the real, logically-implied reverse rather
        than leaving the graph only half-navigable by the BFS seed.
        """
        import map_render
        surface = bot.CAMPAIGN["locations"]["surface"]
        bidi = map_render._bidirectional_directions(surface, {"stonearch_bridge", "the_weeping_well"})
        self.assertEqual(bidi["stonearch_bridge"].get("south"), "the_weeping_well")
        self.assertEqual(bidi["the_weeping_well"].get("north"), "stonearch_bridge")

    def test_disconnected_components_never_collapse_onto_the_same_point(self):
        """
        Real bug found while testing the directional layout: a node
        the directional BFS never reaches (real campaign.json shape --
        e.g. a sub-dungeon area only linked in via descends_to, a
        separate field this module never treats as an in-layer
        connection) used to fall back to a hardcoded (0.0, 0.0), and
        with MULTIPLE such disconnected components all landing on that
        exact same point, the repulsion force between them is
        mathematically zero (dx=dy=0 makes the push direction
        undefined) -- they could never separate for the rest of the
        relaxation. Confirmed against a real worst case: every location
        in the underground layer at once, which includes several real,
        genuinely disconnected-within-this-layer sub-clusters.
        """
        import map_render
        underground = bot.CAMPAIGN["locations"]["underground"]
        visited = set(underground.keys())
        visited_here, revealed_here, edges, unexplored = map_render._visible_nodes_and_edges(underground, visited, set())
        bidi = map_render._bidirectional_directions(underground, set(visited_here))
        raw = map_render._compute_layout(visited_here, edges, bidi)
        import itertools
        collapsed = [
            (a, b) for a, b in itertools.combinations(visited_here, 2)
            if ((raw[a][0] - raw[b][0]) ** 2 + (raw[a][1] - raw[b][1]) ** 2) ** 0.5 < 1.0
        ]
        self.assertEqual(collapsed, [], f"nodes landed on the exact same point: {collapsed}")

    def test_disconnected_components_stay_near_the_main_cluster(self):
        """
        Real bug (2026-08-09, found visually re-rendering a small layer
        right after fixing the (0,0)-collapse bug above): removing the
        old hard position clamp let a genuinely disconnected node (no
        real connections/directions to anything else visited) drift
        arbitrarily far from the rest of the graph over 400 relaxation
        iterations -- nothing bounded it but a mild centroid pull. That
        blew up _normalize_to_canvas's bounding box and squeezed the
        real, connected chain into a sliver of the rendered map.
        _contain_disconnected_components rigidly translates (never
        reshapes) any non-main component back within a bounded distance
        of the main component's centroid -- confirm it actually holds
        on a small, sparse graph shaped like the real one that exposed
        this (a 5-room chain plus one real, truly isolated room).
        """
        import map_render
        node_ids = ["room_1", "room_2", "room_3", "room_4", "room_5", "loner"]
        edges = [("room_1", "room_2"), ("room_2", "room_3"), ("room_3", "room_4"), ("room_4", "room_5")]
        bidi = {
            "room_1": {"south": "room_2"}, "room_2": {"north": "room_1", "south": "room_3"},
            "room_3": {"north": "room_2", "south": "room_4"}, "room_4": {"north": "room_3", "south": "room_5"},
            "room_5": {"north": "room_4"}, "loner": {},
        }
        raw = map_render._compute_layout(node_ids, edges, bidi)
        main_ids = ["room_1", "room_2", "room_3", "room_4", "room_5"]
        main_cx = sum(raw[nid][0] for nid in main_ids) / len(main_ids)
        main_cy = sum(raw[nid][1] for nid in main_ids) / len(main_ids)
        dist = ((raw["loner"][0] - main_cx) ** 2 + (raw["loner"][1] - main_cy) ** 2) ** 0.5
        k = (map_render._LAYOUT_AREA ** 2 / len(node_ids)) ** 0.5
        self.assertLessEqual(dist, k * 6.0 + 1.0, f"isolated node drifted too far from the main cluster: {dist}")

    def test_floor_levels_reflects_real_up_down_chains(self):
        """
        Real live request (2026-08-09, Coffee: "F3 - F2 - F1 - B1 - B2
        - B3 for floors and basements in dungeons"). Grounded entirely
        in real "up"/"down" entries -- a chain of 3 real rooms each one
        real step below the last must read as levels 0, -1, -2 (i.e.
        the map would label them B1 and B2), never invented for a
        location with no real up/down relationship to anything else.
        """
        import map_render
        bidi = {
            "room_a": {"down": "room_b"},
            "room_b": {"up": "room_a", "down": "room_c"},
            "room_c": {"up": "room_b"},
            "unrelated_room": {},
        }
        levels = map_render._floor_levels(bidi)
        self.assertEqual(levels["room_a"], 0)
        self.assertEqual(levels["room_b"], -1)
        self.assertEqual(levels["room_c"], -2)
        self.assertNotIn("unrelated_room", levels)

    def test_legend_lines_each_fit_the_narrow_canvas_width(self):
        """
        Real bug (2026-08-09, found visually re-rendering a small layer
        right after adding the F1-B3 floor badges): the base legend
        line and the new floor-badge clause used to be concatenated
        into ONE draw.text() call with no width check at all (only the
        separate "revealed but not visited" line ever checked width) --
        on a narrow, un-scaled 900px canvas the combined text ran well
        past the edge and got clipped mid-word. They're now separate,
        independently width-checked lines; confirm each one (with a
        real floor-badge clause AND a real "revealed" clause both
        present, the worst case) actually fits under the same
        int(width / 8.5) character budget render_layer_map itself uses.
        """
        import map_render
        width = map_render.CANVAS_WIDTH
        line_limit = int(width / 8.5)
        base_line = "red ring = where you are  •  dot = visited  •  gold +N = unexplored paths from there"
        floor_line = "blue F/B = floor above/below the chain's entry point"
        self.assertLessEqual(len(base_line), line_limit)
        self.assertLessEqual(len(floor_line), line_limit)

    def test_character_layer_content_groups_by_layer_and_skips_empty_ones(self):
        import sessions
        sessions.end_session(-989)
        user_id = 900940
        make_basic_character(user_id, "MapLayerTester", chat_id=-989, current_location="crossroads_tavern")
        # A real underground location, visited -- so both surface (from
        # character creation's own starting location) and underground
        # should show up; sky (never touched) must not.
        db.update_character(
            user_id, -989, visited_locations=["crossroads_tavern", "sunken_root_caverns"],
        )
        character = db.get_character(user_id, -989)
        content = bot._character_layer_content(character)
        self.assertIn("surface", content)
        self.assertIn("underground", content)
        self.assertNotIn("sky", content)
        self.assertIn("crossroads_tavern", content["surface"][0])
        self.assertIn("sunken_root_caverns", content["underground"][0])

    def test_map_layer_keyboard_offers_a_button_only_for_other_layers_with_content(self):
        """
        Task #11, per Coffee: "if u have above and below locations have
        button available." No button for the layer already being shown;
        exactly one button per OTHER layer that has real content.
        """
        import sessions
        sessions.end_session(-988)
        user_id = 900941
        make_basic_character(user_id, "MapButtonTester", chat_id=-988, current_location="crossroads_tavern")
        db.update_character(user_id, -988, visited_locations=["crossroads_tavern", "sunken_root_caverns"])
        character = db.get_character(user_id, -988)

        keyboard = bot._map_layer_keyboard(character, "surface")
        self.assertIsNotNone(keyboard)
        all_callback_data = [btn.callback_data for row in keyboard.inline_keyboard for btn in row]
        self.assertEqual(all_callback_data, ["map|underground"])

        # Only ever visited surface -- no other layer has content, so no keyboard at all.
        db.update_character(user_id, -988, visited_locations=["crossroads_tavern"])
        character = db.get_character(user_id, -988)
        self.assertIsNone(bot._map_layer_keyboard(character, "surface"))

    async def test_visual_map_sends_a_real_labeled_map_not_ai_art(self):
        import sessions
        sessions.end_session(-987)
        user_id = 900942
        make_basic_character(user_id, "VisualMapTester", chat_id=-987, current_location="crossroads_tavern")
        db.update_character(user_id, -987, visited_locations=["crossroads_tavern", "market_row"])

        sink = []
        update = FakeUpdate(user_id, "show me the map", sink, chat_id=-987)
        await bot._do_show_visual_map(update)

        self.assertEqual(len(update.effective_chat.sent_photos), 1)
        photo = update.effective_chat.sent_photos[0]
        import io
        from PIL import Image
        image = Image.open(io.BytesIO(photo["photo"]))
        self.assertEqual(image.format, "PNG")
        self.assertIn("explored world", photo["caption"].lower())

    async def test_visual_map_button_switches_to_the_requested_layer(self):
        # FakeCallbackUpdate always uses chat_id -999 (no override param) -- character must live there too.
        import sessions
        sessions.end_session(-999)
        user_id = 900943
        make_basic_character(user_id, "MapSwitchTester", current_location="crossroads_tavern")
        db.update_character(user_id, -999, visited_locations=["crossroads_tavern", "sunken_root_caverns"])

        sink = []
        update = FakeCallbackUpdate(user_id, "map|underground", sink)
        await bot.map_menu_callback(update, DummyContext())

        self.assertEqual(len(update.effective_chat.sent_photos), 1)
        sessions.end_session(-999)

    async def test_visual_map_says_so_when_nothing_explored_yet(self):
        import sessions
        sessions.end_session(-985)
        user_id = 900944
        make_basic_character(user_id, "FreshMapTester", chat_id=-985, current_location="crossroads_tavern")
        db.update_character(user_id, -985, visited_locations=[])

        sink = []
        update = FakeUpdate(user_id, "show me the map", sink, chat_id=-985)
        await bot._do_show_visual_map(update)
        self.assertTrue(any("haven't explored" in s.lower() for s in sink), sink)
        self.assertEqual(len(update.effective_chat.sent_photos), 0)

    # -- Battle-menu formation submenu only showed the tapper's own row
    #    (2026-08-09, Coffee, dev-topic screenshot: "I wanted to show
    #    all of my party that is in battle so I can actively move
    #    their formation in this menu. It only lets me see mine, not
    #    the other two members in battle.") -------------------------
    async def test_formation_menu_lists_every_real_party_member_in_the_fight(self):
        from unittest.mock import patch
        import sessions
        sessions.end_session(-999)
        leader_id = 900550
        member_id = 900551
        make_basic_character(leader_id, "FormLeader", current_location="crossroads_tavern")
        make_basic_character(member_id, "FormAlly", current_location="crossroads_tavern")
        party_id = db.create_party(leader_id, -999)
        db.update_character(leader_id, -999, formation_row="back")
        db.update_character(member_id, -999, party_id=party_id, formation_row="front")
        leader = db.get_character(leader_id, -999)
        member = db.get_character(member_id, -999)

        def fake_get(url, timeout=None):
            return SimpleNamespace(status_code=200, headers={"content-type": "image/jpeg"})

        sink = []
        # Real 2026-08-01 formation feature means initiative order is
        # random -- if the enemy or companion wins it, _do_start_combat
        # can resolve a real AI turn (and a real Ollama narration call)
        # before ever returning here. Patched out for speed/determinism,
        # same as the enemy-banter tests -- this test is about the menu
        # wiring, not narration.
        with patch("bot._get_combat_eligible_party_members", return_value=[leader, member]), \
             patch("bot.requests.get", side_effect=fake_get), \
             patch("bot.narrate_action", return_value="A blow lands."):
            await bot._do_start_combat(FakeUpdate(leader_id, "fight a goblin", sink), monster_key="goblin", count=1)

        session = sessions.get_session_for_user(-999, leader_id)
        self.assertIsNotNone(session)
        session.current_turn_index = session.turn_order.index(leader_id)

        sink2 = []
        await bot.battle_menu_callback(FakeCallbackUpdate(leader_id, "bm|formation", sink2), DummyContext())
        combined = "\n".join(sink2)
        self.assertIn("FormLeader", combined)
        self.assertIn("FormAlly", combined)
        self.assertIn("setrow|front|FormLeader", combined)
        self.assertIn("setrow|back|FormAlly", combined)
        sessions.end_session(-999)

    async def test_setrow_button_repositions_the_named_target_not_the_tapper(self):
        # Real note: FakeCallbackUpdate/FakeUpdate/make_basic_character
        # all default to chat_id -999 with no override, so (like every
        # other test in this file) this stays on -999 rather than
        # trying to isolate onto a different chat -- sessions.end_
        # session(-999) at the top defensively clears any session a
        # PRIOR -999 test left behind (e.g. on its own failure, before
        # reaching its own cleanup call), same as every other test here.
        from unittest.mock import patch
        import sessions
        sessions.end_session(-999)
        leader_id = 900552
        member_id = 900553
        make_basic_character(leader_id, "RepoLeader", current_location="crossroads_tavern")
        make_basic_character(member_id, "RepoAlly", current_location="crossroads_tavern")
        party_id = db.create_party(leader_id, -999)
        db.update_character(member_id, -999, party_id=party_id, formation_row="front")
        leader = db.get_character(leader_id, -999)
        member = db.get_character(member_id, -999)

        def fake_get(url, timeout=None):
            return SimpleNamespace(status_code=200, headers={"content-type": "image/jpeg"})

        sink = []
        with patch("bot._get_combat_eligible_party_members", return_value=[leader, member]), \
             patch("bot.requests.get", side_effect=fake_get), \
             patch("bot.narrate_action", return_value="A blow lands."):
            await bot._do_start_combat(FakeUpdate(leader_id, "fight a goblin", sink), monster_key="goblin", count=1)

        session = sessions.get_session_for_user(-999, leader_id)
        self.assertIsNotNone(session)
        session.current_turn_index = session.turn_order.index(leader_id)

        sink2 = []
        await bot.battle_menu_callback(
            FakeCallbackUpdate(leader_id, "bm|setrow|back|RepoAlly", sink2), DummyContext(),
        )
        updated_member = next(p for p in session.participants if p["telegram_user_id"] == member_id)
        self.assertEqual(updated_member["formation_row"], "back", "the NAMED target (RepoAlly) should have moved")
        updated_leader = next(p for p in session.participants if p["telegram_user_id"] == leader_id)
        self.assertEqual(updated_leader.get("formation_row", "front"), "front", "the tapper must be untouched")
        sessions.end_session(-999)

    async def test_startup_chat_stub_can_actually_send_a_photo(self):
        """
        Real live gap, caught 2026-08-09 right after a restart resumed
        a combat session mid-spider-turn: bot._StartupChatStub (used to
        resolve a restored session's already-pending AI turn at startup,
        when there's no real incoming Update to hang a chat object off
        of) only ever implemented send_message -- _maybe_send_monster_
        image/_maybe_send_battle_formation_image's real send_photo call
        failed with a plain AttributeError (caught and logged, never
        fatal, but the player restoring mid-fight silently lost the art
        a normal turn would have shown). Confirms send_photo now
        delegates to application.bot.send_photo, same pattern
        send_message already used.
        """
        calls = []

        class FakeAppBot:
            async def send_photo(self, chat_id, photo, **kwargs):
                calls.append((chat_id, photo, kwargs))
                return SimpleNamespace(message_id=1)

        stub = bot._StartupChatStub(FakeAppBot(), chat_id=-999)
        await stub.send_photo(b"fake-png-bytes", caption="A monster!")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], -999)
        self.assertEqual(calls[0][1], b"fake-png-bytes")
        self.assertEqual(calls[0][2].get("caption"), "A monster!")


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
        db.update_character(resting_id, -999, is_inactive=1)

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

    async def test_two_simultaneous_fights_resolve_independently_through_real_handlers(self):
        """
        Full-stack version of FastRegressionTests'
        test_multi_fight_sessions_do_not_cross_contaminate -- proves a
        REAL attack dispatched through bot.py's actual handler (real
        Ollama narration, not a mock) only ever touches the acting
        player's own fight, never an unrelated second fight sharing the
        same chat. This is the actual end-to-end proof Phase 1 exists
        to deliver: two parties fighting at once in one Telegram chat,
        confusing no one.
        """
        import sessions
        for s in list(sessions.get_sessions_in_chat(-999)):
            sessions.end_session(-999, s)

        player_a, player_b = 900510, 900511
        char_a = make_basic_character(player_a, "RealFightA", current_location="crossroads_tavern")
        char_b = make_basic_character(player_b, "RealFightB", current_location="crossroads_tavern")
        enemy_a = {
            "telegram_user_id": -2_500_080, "name": "Real Goblin A", "dexterity": 10, "strength": 10,
            "armor_class": 12, "hp_current": 200, "hp_max": 200, "proficiency_bonus": 2,
            "is_ai": 1, "xp_reward": 10, "monster_key": "goblin",
            "resistances": [], "vulnerabilities": [], "immunities": [],
        }
        enemy_b = {**enemy_a, "telegram_user_id": -2_500_081, "name": "Real Goblin B"}

        session_a = sessions.start_session(
            -999, [char_a, enemy_a], {player_a: "party", enemy_a["telegram_user_id"]: "enemy"},
        )
        session_b = sessions.start_session(
            -999, [char_b, enemy_b], {player_b: "party", enemy_b["telegram_user_id"]: "enemy"},
        )

        b_round_before = session_b.round_number
        b_enemy_hp_before = next(
            p for p in session_b.participants if p["telegram_user_id"] == enemy_b["telegram_user_id"]
        )["hp_current"]

        sink = []
        await bot.adventure_master_handler(FakeUpdate(player_a, "I attack the goblin", sink), DummyContext())

        session_b_after = sessions.get_session_for_user(-999, player_b)
        self.assertIs(session_b_after, session_b)
        self.assertEqual(session_b_after.round_number, b_round_before)
        b_enemy_hp_after = next(
            p for p in session_b_after.participants if p["telegram_user_id"] == enemy_b["telegram_user_id"]
        )["hp_current"]
        self.assertEqual(b_enemy_hp_after, b_enemy_hp_before)

        sessions.end_session(-999, session_a)
        sessions.end_session(-999, session_b)

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

        db.update_character(user_id, -999, current_location="whispering_wood")
        board_quests = board_quests_module.get_or_generate_board_quests(bot.CAMPAIGN, "whispering_wood", -999)
        self.assertTrue(board_quests, "expected a real board quest to generate here")
        named_quest = board_quests[0]

        sink.clear()
        text = f"Accept the quest, {named_quest['title'].lower()}"
        await bot.adventure_master_handler(FakeUpdate(user_id, text, sink), DummyContext())
        combined = " ".join(sink)
        self.assertIn(named_quest["title"], combined)
        self.assertNotIn("Sarah's Safer Crossing", combined)

        character = db.get_character(user_id, -999)
        self.assertNotIn("seras_safer_crossing", character["active_quests"])

    async def test_accepting_a_self_location_quest_completes_it_immediately_not_a_soft_lock(self):
        """
        Real live bug (2026-08-06, Coffee via dev-topic screenshot: "This
        quest isn't working for me... I've done everything in this area
        and I haven't been able to complete it like I have been with my
        other two characters"). Root cause: completion for a
        reach_location-trigger quest was ONLY ever checked from _do_move's
        "arrived at a new location" event, but _offerable_quest_at_location
        only ever offers a story quest while the player is ALREADY
        standing at quest['location']. For quests whose 'location' and
        trigger.location are the SAME place (the_hollow_stump here,
        confirmed via campaign.json to also affect the_wrong_color and
        the_hush_stage1_signs), the player is by construction already at
        the trigger location the instant they accept, and would never
        "arrive" there again -- a real, permanent soft-lock, confirmed
        live: Coffee's character "Pan" accepted the_hollow_stump while
        standing in Hollow Stump Shrine and never completed it, while his
        other two characters (who apparently wandered off and back)
        completed it fine. Fixed by checking reach_location completion
        immediately after accept, not just after a later move. Needs a
        real board-quest-generation call at this location (which may hit
        live Ollama for branching narration), hence SlowLiveTests.
        """
        bot.setup_default_npcs()
        user_id = 990001
        make_basic_character(user_id, "ReproPan", current_location="hollow_stump_shrine")
        db.complete_quest(user_id, -999, "welcome_to_the_crossroads")

        sink = []
        await bot.adventure_master_handler(FakeUpdate(user_id, "I accept the quest", sink), DummyContext())

        character = db.get_character(user_id, -999)
        self.assertIn("the_hollow_stump", character["completed_quests"])
        self.assertNotIn("the_hollow_stump", character["active_quests"])
        combined = " ".join(sink)
        self.assertIn("Quest complete", combined)

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
        db.update_character(player_id, -999, level=5)
        player = db.get_character(player_id, -999)
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
        db.update_character(player_id, -999, level=2)
        player = db.get_character(player_id, -999)
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
        self.assertEqual(db.get_feature_uses(player_id, -999, "wild_shape"), 1)

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
