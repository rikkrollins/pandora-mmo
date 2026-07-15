# Changelog

All notable changes to Pandora MMO are documented here.

## [1.10.8] — Potions actually work now (they never did), plus a real cooking recipe

Eighth, and probably the most significant, finding from the reverse-
playthrough sweep: while looking into the crafting-recipes backlog
item, an exhaustive search turned up **zero references anywhere in
this codebase** to ever consuming an item. Healing Potions, Greater
Healing Potions, and Antitoxin have had real `heal_dice`/`effect`
fields since they were added, but no action existed to ever drink or
use one — a player could spend real gold on a potion and it would just
sit in inventory forever. This has apparently been broken since
potions were introduced.

Added a real `use_item` action ("drink the healing potion", "I use my
antitoxin", "quaff the potion"): heal potions roll their `heal_dice`
and apply it (targetable at another party member, same convention as
heal spells), Antitoxin cures the `poisoned` condition mid-combat,
and flavor-only consumables (rations, ale, torches) get an honest
flavor-only response rather than silently failing.

Also, while auditing gatherable materials against real recipes:
`raw_fish` was gatherable but used by zero recipes. Added a real
Cooked Fish recipe (raw_fish + wood, cooking over a fire) producing an
actually-usable food item now that `use_item` exists.

## [1.10.7] — Player-to-player item trading

New feature (backlog item): "give my healing potion to Sera" now
really transfers the item between characters, not just NPC shop
buy/sell. Scoped to whoever's actually active at the giver's own
location — the same "physically present" rule already used for who
can join a fight — not restricted to a formed party, since handing
something to anyone standing in the same room is the natural reading
of "give X to Y". Rejects cleanly if the recipient isn't there or the
giver doesn't actually have the item.

## [1.10.6] — Bosses finally fight like bosses

Seventh finding from the reverse-playthrough sweep: both bosses
(Goblin Boss, The Waiting Shape) fought exactly like a regular monster
with a bigger stat block — the only "special ability" mechanism that
existed (`on_hit_condition`) was already shared by every regular
monster too. Nothing distinguished a boss fight from a normal one
except higher numbers.

- **Multiattack**: every monster flagged `is_boss` now gets 2 attacks
  per turn, the near-universal real-5E boss-tier convention. Handles
  a mid-turn kill or combat ending between attacks correctly.
- **The Waiting Shape's Life Drain**: heals itself for half the damage
  it deals on a hit, capped at its own max HP — a real vampiric/undead
  trope fitting its spectral "waiting shadow" theme, and this
  campaign's first monster with a genuinely unique signature ability
  beyond the shared on-hit-condition system.

Verified with a real combat session and real narration calls (not
just code review): the boss attacked twice in one turn and its HP
rose after landing a hit.

## [1.10.5] — The campaign's four story chapters are finally visible in play

Sixth finding from the reverse-playthrough sweep: `campaign.json`'s
`story_arcs` mapped the whole main questline into four real chapters
("Discovery" → "The Descent" → "What Was Buried" → "What Waits Above",
the same climax added in 1.10.1/1.10.2) with titles, descriptions, and
required levels — but nothing in `bot.py` ever read it. Players had no
way to know they were in a structured story at all.

- Finishing the last quest in a chapter now shows a real "🌟 Chapter
  complete" beat alongside the quest-complete message.
- The quest journal ("check my quests") now opens with the character's
  current chapter title and description, so the story is visible
  throughout, not just at each chapter's end.

## [1.10.4] — Guild membership benefits are finally real, not just a database flag

Fifth finding from the reverse-playthrough sweep: `guilds.py` defined
per-guild `benefits` lists, but nothing in `bot.py`/`rules/combat.py`
ever checked two of the three (the shop discount was already real —
`shop.py`'s `buy_item` applies it correctly). Joining the Arcane
Circle or the Silver Wardens changed nothing except a database column.

- **Arcane Circle's `bonus_spell_scroll`**: joining now grants a real
  Scroll of Magic Missile on the spot.
- **Silver Wardens' `bonus_damage_vs_undead`**: `resolve_attack` now
  adds +2 damage against this campaign's undead-flavored monsters
  (currently Shadow Wisp — the one spectral enemy in the roster; the
  set is easy to extend if more undead are added later).

Also fixed, while hardening the test suite under this session's severe
Ollama contention: `test_tts_trigger_message_gets_cleaned_up` raced a
fixed 2.5s sleep against a 2s background cleanup task and could flake
under heavy load (confirmed passing in isolation, unrelated to any
change here) — now polls up to 15s instead of a single fixed wait.

## [1.10.3] — Spellcasting actually keeps growing past level 6, and Clerics get their Domain back

Fourth finding from the reverse-playthrough sweep: the level-up system
(`spells_unlocked_at_level`) has promised 4th- and 5th-level spells at
character levels 7 and 9 since it was written, but no spell of either
level existed in the whole catalog — every full caster (Wizard,
Sorcerer, Cleric, Druid, Bard, Warlock) hit a wall at level 6 with
literally nothing new to learn for the rest of a 20-level game. Added
11 real 4th/5th-level spells (Ice Storm, Polymorph, Death Ward,
Banishment, Dimension Door, Guardian of Faith, Cone of Cold, Mass Cure
Wounds, Flame Strike, Insect Plague, Hold Monster) and wired each into
the classes that get it in real 5E.

While auditing spell reachability, also found 4 completely orphaned
spells nothing could ever learn: Summon Lesser Spirit (now Ranger's,
also filling a level 2-4 gap in Ranger's own progression), Bless
(Cleric), Detect Magic (Wizard), and Spare the Dying (Cleric cantrip —
this one's been sitting in the catalog unreferenced since it was
added). Added a permanent regression test asserting every spell in the
catalog is reachable by at least one class, so a new spell added
without a class assignment fails a fast test instead of sitting dead
for months.

Separately: Cleric's "Divine Domain" class feature has been flavor
text only since class features were made real. Gave every Cleric a
fixed default of the Life Domain (same fixed-default convention as
Sorcerer's Draconic Bloodline, Warlock's Fiend patron, and Ranger's
Favored Enemy) — Disciple of Life now genuinely adds 2 + the spell's
level whenever a Cleric casts a leveled healing spell, the real 5E
formula.

## [1.10.2] — Seven previously-unobtainable magic items are now real, and puzzle answers work again

Third finding from the reverse-playthrough sweep: an "orphaned items"
cross-reference (every item in `items.py` checked against shop
inventories, quest rewards, NPC kits, resource nodes, and crafting
recipes) found 7 magic items with no acquisition path anywhere in the
campaign — Greater Healing Potion, Silvered Dagger, Scroll of Fireball,
Ring of Protection, Cloak of Elvenkind, Amulet of Health, and Boots of
the Winterlands. Wired the potions/scrolls/dagger into Maren's and
Vane's shop inventories, and the four rings/cloaks/boots/amulet into
existing quest rewards (Clear the Warrens, The Hush, Maren's Locked
Ledger, Vesh's Way Back) so they're earned, not just sold.

Verifying the Locked Ledger reward live surfaced a second, unrelated
real bug: "I have decided the answer to the riddle is a map" — a
completely natural way to answer a puzzle — was misclassified as
`resolve_choice` instead of `answer_puzzle`, because resolve_choice's
"i have decided" trigger (broadened earlier for board-quest branching
choices) is checked first in `_keyword_fallback` and returned before
answer_puzzle's own checks ever ran. Fixed by checking for
unambiguous puzzle-language ("riddle", "puzzle", "the answer to")
before resolve_choice's more generic decision-framing check, so puzzle
answers win regardless of which decision-phrasing happens to open the
sentence. Both the shop/reward wiring and the classification fix were
verified with real handler calls end to end (buy, accept quest, answer
puzzle, reward lands in inventory), plus permanent regression tests
for both.

## [1.10.1] — The Unmoored Isle finally has something waiting for you

Second finding from the reverse-playthrough sweep: The First City and
The Unmoored Isle — the campaign's two deepest, most narratively
"final" locations — had zero monsters. Nothing to actually fight at
the climax, despite the isle's own "waiting_shape" interactable
explicitly foreshadowing a confrontation ("a still shape... 
unmistakably waiting").

Added a real final boss, **The Waiting Shape** (HP 65, AC 17, marked
`is_boss` so it can't be fled from, inflicts frightened on hit) — well
above the previous toughest monster (Goblin Boss, HP 21). "What Waits
Above" now triggers on actually defeating it, not just walking in,
which also fixes a related latent bug: a `reach_location` quest whose
location and trigger point at the same place could never retroactively
complete after accepting while already standing there, since no
retroactive check exists — `defeat_monster` sidesteps that entirely.

Verified live as far as system load reasonably allowed: reaching the
isle, accepting the quest while already present, and combat correctly
engaging the boss all confirmed with real handler calls; the final
defeat-completes-quest link reuses the exact same generic completion
code already proven live for a different quest the same day, plus a
permanent structural regression test.

## [1.10.0] — The Unmoored Isle is finally reachable

First real finding from a systematic reverse-playthrough sweep
(mapping the full location graph before playing it end to end):
**The Unmoored Isle** — a real location with its own quest ("What
Waits Above"), real interactables, and a `requires_item` gate on
`shard_of_dim_light` — had NO connection from anywhere in the entire
campaign. Its own `connections` list was empty, and no other location
listed it as a connection, descends_to, or ascends_to target. The
shard itself was real and obtainable (a reward from "The Wrong Color"
at Glimmerdeep Grotto), so the item-gate half of the design was
genuinely built — it just never got wired to an actual path. This
was completely dead, unreachable content until now.

Fixed by adding `ascends_to: the_unmoored_isle` to The First City (the
campaign's deepest, most narratively "final" location, level 7+) —
`_do_move`'s existing `requires_item` check already handles the gating
correctly once the destination is reachable at all; no new mechanic
needed. Verified live: blocked without the shard, genuinely reachable
with it. Added as a permanent regression test.

## [1.9.2] — TTS coverage audit completed for every action

Finishes the audit started in 1.9.0: every action handler's primary
reply now routes through `_safe_send` (the only place TTS is hooked),
confirmed by script — zero handlers left with no TTS coverage at all.
Covers buying, selling, casting every spell effect type (damage, heal,
resurrect, summon, and the no-mechanical-effect-yet fallback), joining
a guild, fast travel, and gambling. Also fixed `scripts/announce_deploy.py`
permanently: it broke on a bare underscore in the message text twice
live this session (Telegram's Markdown parser reads it as an unmatched
italic marker) — now escapes Markdown special characters automatically
instead of relying on remembering to avoid them.

All 23 fast regression tests pass.

## [1.9.1] — accept_quest now honors the specific quest you actually name

Real regression from the same day's companion-quest feature (1.8.0):
"Accept the quest, a quiet request for wood" — a specific, real,
correctly-classified request naming a real board quest by title — got
silently swallowed into accepting Sera's personal quest instead,
because that shortcut ran unconditionally regardless of what was
actually typed. Fixed: the companion-quest shortcut now only fires
for a generic "I accept the quest" with nothing else named; naming a
different real quest (story or board) that's actually available here
takes that one instead. Verified live end-to-end and added as a
permanent regression test.

## [1.9.0] — Combat now stays where it happens, and TTS coverage was audited game-wide

- **Combat no longer pulls in characters from other locations, or
  characters who are resting.** Per Coffee: "only characters that are
  active and at the same location shud be in the same battle... if a
  character is in the tavern and another is in the whispering woods
  they shud not be able to fight. if a character is inactive they
  shud be excluded from the fight also." `_get_party_members()`
  deliberately returns every active character globally (used
  elsewhere for real party-roster listings) — but both places combat
  actually starts (`_do_start_combat` and the ambient hostile-NPC
  encounter) were reusing that same unfiltered list directly. Added
  `_get_combat_eligible_party_members(location_id)`, filtered to
  characters actually at that location and not resting, wired into
  both. The "Combat Begins! Your party (...)" header text was also
  quietly listing everyone in the whole game regardless — fixed to
  reflect who's actually in the fight.
- **Fixed "Who is in my current party?" answering nothing** — fell
  through to plain chat because "current" inserted between "my" and
  "party" broke the exact-substring trigger, and the fully spelled-out
  "who is in my party" (not just the contraction "who's") was never
  covered either. Broadened with a regex tolerant of a word or two
  between "my" and "party".
- **TTS coverage audit.** Per Coffee, after finding "look around"
  silently skipped TTS: about half of all action handlers never called
  `_safe_send` at all (the only place TTS is hooked), so their primary
  replies always skipped it silently. Fixed look, move, check_party,
  invite/accept/leave party, second wind, rage, bardic inspiration,
  lay on hands, arcane recovery, find merchant, list/switch character,
  and show map. Also fixed the actual root cause of a live crash this
  surfaced: `_ChatOnlyUpdate` (used for background system messages —
  hourly updates, AI party ticks) never returned the sent message
  object at all, so the new TTS-trigger-cleanup code (1.8.3) crashed
  with an unretrieved exception whenever a background message had TTS
  enabled.

All fixes verified with real handler-level tests; 23 fast regression
tests pass.

## [1.8.3] — TTS no longer leaves a visible duplicate message

Per Coffee: "The TTS is giving two messages — one is the original, the
other is the tts... there should only be one message." Triggering
@TextTSBot works by sending it a real `/tts <text>` command message —
Telegram has no way to hand another bot a command invisibly — so that
trigger message sat in the chat as a second, redundant copy of the
narration text right below the real one.

Fixed by capturing the sent trigger message and deleting it ~2 seconds
later, as a background task that doesn't block the real reply's send
path. @TextTSBot already receives its own copy of the update the
instant the message is sent, independent of what happens to it
afterward, so deleting our copy doesn't affect whether it actually
speaks — it just cleans up the visible duplicate. Added as a permanent
regression test.

## [1.8.2] — "What am I carrying" never fails again, plus a permanent regression suite

- Real player report ("Sugar", Support topic): "What am I carrying?"
  fell through to the generic model-backed path — no deterministic
  check existed for it at all — and when Ollama genuinely couldn't be
  reached in time, the player got the raw "couldn't reach the local
  model" fallback for a question with exactly one correct, already-
  known answer. Same reasoning as the existing XP-to-level and active-
  character deterministic answers: added `_deterministic_inventory_answer`
  so this never needs a model call at all.
- **New**: `tests/` now has a real, permanent regression suite
  (`python3 -m unittest tests.test_regression -v`) covering every real
  bug fixed this session — extracted from the many throwaway
  `bot_test_tmp.py` scripts into shared fixtures (`tests/helpers.py`)
  so future fixes don't reinvent the same fake Telegram objects. Split
  into `FastRegressionTests` (no Ollama, ~5 min under current disk
  contention, safe before every deploy) and `SlowLiveTests` (real
  narration calls, run when touching that code directly). All 18 fast
  tests pass.

## [1.8.1] — Every recruitable now has a real personal quest

Direct follow-up to 1.8.0's companion-quest system (Sera's Safer
Crossing) — now all 6 recruitable NPCs have one, each grounded in
their existing established personality/goals text, not invented fresh:

- **Borin Ironjaw** (Market Row): "A Name Worth Vouching For" — wants
  proof against something real in the Goblin Warrens before he'll
  vouch for the party.
- **Wren Hollowbrook** (Hollow Stump Shrine): "What the Roots Are
  Hiding" — sends the party into the Sunken Root Caverns to check what
  changed.
- **Pip Thistledown** (Stonearch Bridge): "A Story Worth Telling" —
  wants proof of something worth singing about from the Whispering
  Wood.
- **Grask Emberscale** (Goblin Warrens): "Grask's Freedom" — tied to
  defeating the goblin boss, the first companion quest using the
  defeat_monster trigger rather than reach_location.
- **Vesh Nightglass** (Glimmerdeep Grotto): "Vesh's Way Back" — wants
  walking back up to The Crossroads Tavern.

Verified live: all 5 recruit-then-narrate-then-accept-then-track flows
confirmed end-to-end. Grask's defeat_monster trigger is verified at
the code level (same generic, already-proven completion-checking loop
the pre-existing "Clear the Goblin Warrens" quest already relies on)
rather than a full live combat replay, given heavy shared-capacity
contention with the live game at the time (Ollama runs a single
concurrent generation slot, so a long test directly competes with real
players' live requests).

## [1.8.0] — Recruitable companions now have real personal quests

Coffee's idea: "each recruitable has a mission or quest they go on with
players" — e.g. recruiting Sera at the starting tavern, she mentions a
real task she'd like help with, and the party can choose to follow it.
Meant to double as an optional, guided path through the story for
players who want one, not just decoration.

- Story quests gained an optional `giver_npc` field (mirrors board
  quests' existing one). A new `_offerable_companion_quest` matches by
  who's ACTUALLY in the party right now, not by location — a
  companion's request travels with the party rather than being tied to
  wherever they were recruited, and never collides with an unrelated
  quest that happens to share that same location.
- Recruiting a companion with a personal quest now narrates their real
  hook immediately, in-character; "I accept the quest" (the same
  phrase as any other quest) takes it on for real, tracked the same as
  any story quest.
- AI-controlled characters (companions and the autonomous party alike)
  see this as a real fact too, so they can choose to accept it on
  their own.
- Sera (met at The Crossroads Tavern) is the first real example:
  "Sera's Safer Crossing," pointing the party toward Stonearch Bridge —
  grounded in her existing established backstory, not invented fresh.
  The remaining recruitables (Borin Ironjaw, Wren Hollowbrook, Pip
  Thistledown, Grask Emberscale, Vesh Nightglass) don't have one yet —
  same "needs real authored content" follow-up as cooking/forging/
  enchanting.
- Verified live end-to-end: recruiting Sera narrates her real quest
  hook, "I accept the quest" takes it on, and it's genuinely tracked
  as an active quest afterward.

## [1.7.7] — Attacking now just works, and Support stopped inventing a companion's stats

- **"I attack the goblin" now works with no fight already running.**
  Per Coffee: "I also tried to attack the goblin, but it didn't work
  either." `_do_attack` required an already-active combat session and
  just refused otherwise, forcing a separate "let's start a fight"
  first even when the intent to fight a specific, present monster was
  completely unambiguous. It now auto-starts combat itself when no
  session exists but the player is naming (or there's exactly one)
  real monster at their current location. Verified live: a real fight
  auto-starts, resolves real rounds, and concludes correctly.
- **Support topic no longer hallucinates a companion's stats.** Real
  screenshot: "Show me my character sheet and Sera's character sheet"
  showed Coffee's own sheet correctly, then OUTRIGHT INVENTED Sera's
  ("mirrors this structure... similar stats based on available data")
  — exactly the kind of fabrication this game's grounding rules exist
  to prevent. Root cause: the lookup only ever found the FIRST "X's
  sheet" mention in a message (re.search, not re.findall) — since "my"
  came first and was excluded, the whole message fell through to the
  model instead. Now finds every name mentioned and shows each one's
  real sheet, never touching the model once any name resolves.

## [1.7.6] — Every gathering skill now has real verb coverage, and Support became a real wiki

Per Coffee's live report and follow-up: "I would like to chop for
lumber" hit pass_turn, then asked to audit every skill so this
class of bug couldn't be hiding elsewhere.

- Audited all 7 real resource nodes across all 4 gathering skills.
  Lumberjacking and fishing had ZERO working verb triggers at all
  ("chop"/"cut down"/"cut wood" and "go fishing"/"catch fish"/word-
  boundary "fish" added); mining only recognized the bare word "mine"
  ("dig for"/"dig up" added).
- `_find_resource_node` only matched a node's exact material id or
  full display name as a substring — broadened to also match a
  significant word against the node's skill name or display-name
  words in either direction (e.g. "chop the timber" now finds the
  wood node via "timber", not just its material id "wood").
- Verified with 12 unit cases (including negative cases — "selfish"/
  "shellfish"/"finish" must NOT trigger gathering just because they
  contain "fish") plus two real handler-level runs (lumberjacking,
  fishing) with real dice rolls and real narration.

- **Support topic is now a much fuller wiki**, per Coffee: "make sure
  support topic is well rounded like an encyclopedia... for players to
  find out anything they need." The command list only ever covered
  the game's early state — gathering/skills, crafting, campfires,
  resting, status conditions, death/revivify, full party sheets
  (including anyone's, not just your own), board vs. story quests,
  and recruit/invite were all missing. Added a real crafting-recipes
  catalog and gathering-skills list alongside the existing item/spell/
  guild/race catalogs, so none of it is invented — grounded the same
  way as everything else Support answers from.

## [1.7.5] — Fixed examine/look-at failing on real, correctly-described objects

Per Coffee's live report (screenshots, 2026-07-14): "when I was looking
at things it didn't register what I was trying to look at even though
I typed it as described."

`_find_interactable` (bot.py) only ever matched an EXACT substring of
the object's stored name — two real, reported failures:
- "Read the guest book on the landing table" didn't match the stored
  name "a guestbook on the landing table" — the leading article and
  the missing space in "guestbook" (one word in the data, naturally
  typed as two) broke the exact match.
- "look at the door down the hall" didn't match "the door at the end
  of the hall" — genuinely different wording for the same object.

Added two fallback tiers, only used when the exact match misses:
article-stripped + compound-word-joined comparison, then a majority
word-overlap match that only fires when there's a single unambiguous
leader (same "don't guess when ambiguous" convention already used for
NPC-name and location-name word matching elsewhere in this codebase).
Verified with unit cases and two real handler-level runs using
Coffee's exact reported phrases against the real campaign data.

## [1.7.4] — Fixed players getting stuck, unable to leave a location

Per Coffee's live report (screenshot, 2026-07-14): stuck in the
Tavern's Upper Rooms — "Go downstairs" and "Leave this area" both got
"There's no active turn to pass right now."

`_do_move` already knew how to resolve generic leave/exit/downstairs/
upstairs phrasing for single-exit locations (its own
`generic_leave_words`) — the bug was upstream: the intent parser's
`move_words` trigger list never routed these phrasings to the move
action in the first place. "leave the " required the literal word
"the" (not "this"/"here"), and there was no bare downstairs/upstairs/
exit trigger at all, so both commands fell all the way through to
pass_turn before `_do_move` ever got a chance to run.

Broadened `move_words` to include "leave this", "leave here", "go
downstairs", "go upstairs", "head downstairs/upstairs", bare
"downstairs"/"upstairs", "exit this", "exit the", "step out", "walk
out". Verified with 9 unit cases (including that "leave the party"
still correctly resolves to leave_party, not move) plus a real
handler-level test — a character actually placed in tavern_upstairs
and given "Go downstairs" now really leaves and lands in The
Crossroads Tavern.

## [1.7.3] — Fixed a severe, near-universal action-misrouting bug

Per Coffee's live report (screenshot, 2026-07-14): "there is obviously
a roadblock on the taking a closer look/observing/look at functions...
this is probably what is stopping the AI from being able to function
further." Investigation found the actual bug was much bigger than
examine specifically.

- **Root cause**: the per-word NPC-name matching added earlier this
  session (so "Say hello to Maren" would match "Old Maren") only
  filtered candidate words by length, not by whether they were actual
  filler words. "Theron **the** Wanderer" and "Kess **the** Bandit"
  both contributed the bare word "the" as a supposedly-distinctive
  match target — meaning almost any message containing "the" (i.e.
  nearly every sentence in English) was misrouted to `talk_npc`
  before it could ever reach any later, more specific check. "Old
  Maren" similarly leaked the common word "old". Fixed with an
  explicit filler-word denylist ("the", "a", "an", "of", "and", "old")
  excluded from per-word matching.
- Separately, broadened `examine` to cover phrasings that were never
  matched at all even without the above bug: "read", "observed"
  (past tense — only the imperative "observe the" was covered, and
  only as part of the whole-area `look` action), "examined",
  "inspected", "searched", "checked out", "looked at", "peered at",
  "glanced at".
- Verified with 24 unit cases (including negative cases — "already",
  "bread", "spread" must not falsely trigger on the substring "read")
  and two real handler-level runs.

This affected every player and every AI-controlled character equally,
since both go through the same shared intent parser — likely the
single highest-impact fix this session.

## [1.7.2] — Recruits and AI party members act on their own initiative

Per Coffee: "make recruitable able to make their own choices... this
goes for AIs also" — e.g. noticing an active party gather quest and a
matching resource node nearby, and just going and gathering it.

- Recruited companions (Sera, and any future recruit) now get
  autonomous turns the same way the separate AI party already did —
  previously they sat completely idle between being directly talked
  to. Personality for these comes from their real campaign.json NPC
  entry, not the hardcoded AI-party roster.
- Any AI-controlled character now sees, as a real fact, when the
  party has an accepted gather-quest bounty that a resource node at
  their CURRENT location can satisfy — even if the quest was posted
  at a different, connected location (e.g. accepted at the tavern,
  fulfilled down in the cellar) — and is nudged to go gather it to
  help finish the quest.
- They also now see their own real skill proficiency at each resource
  node (or a lack of any practice yet), and are told to lean on
  skills they're already good at, but treat an untried/weak one as
  worth practicing rather than avoiding.
- Verified live: with a real accepted "gather silverleaf herb" bounty
  and Sera standing in the tavern cellar with zero herbalism practice,
  a real (unmocked) model call chose "I gather Silverleaf Herb" on
  its own, unprompted.

## [1.7.1] — Character sheets for anyone, not just your own party, in Adventure and Support

Direct follow-up to 1.7.0's named-sheet-lookup fix, per Coffee: "I want
to be able to see character sheets of all players AI, NPC and human...
shud be able to work in adventure and support chat."

- "Show me X's sheet" now finds ANY real character (human or AI), not
  just someone in the asker's own currently-active roster — including
  a player's other, non-active character slot.
- Asking for an un-recruited campaign NPC (Grimsby, Old Maren, etc.)
  now gets honest basic info (role, personality, disposition) instead
  of a "not in your party" rejection or a fabricated stat block — NPCs
  don't have a real 5E sheet until they're actually recruited.
- The same lookup now works in the Support topic, not just Adventure,
  resolved deterministically (no LLM call) the same way the existing
  party-roster and XP questions already are.

## [1.7.0] — Status ailments, real death, gathering professions, TTS, and a full live-bug-fix pass

A large batch — real player-reported bugs fixed live, several new
systems, and infrastructure work, all shipped and deployed the same
session.

### Real bugs found and fixed (several from Coffee's own live reports)
- A gather-quest board bounty gave zero credit if the player already
  held the target material at accept time — now credited immediately,
  and instantly completes/pays out if that's already enough.
- Answering a branching quest's own suggested reply verbatim (e.g. "I
  have decided to 'Keep it and collect the reward'") was silently
  misread as a gather action — "collect" tripped the wrong keyword.
- "Pick a &lt;material&gt;" gather phrasing (beyond the fixed "pick the
  herbs" pattern) fell through to a small model's own bias toward
  guessing `pass_turn` — the real, repeated root cause behind several
  "my quest won't complete" reports.
- The live-set "story mode" command didn't recognize the word
  "narration," only "story mode" literally.
- Accepted board quests never showed up when checking active
  quests — the quest journal only ever looked at story quests.
- Support couldn't answer party questions ("is Sera in my party?",
  "show me my party's sheets") — it was never given the real party
  roster, only the asking player's own character.
- "Show me SERA's character sheet" (a specific named party member, not
  "my sheet") took three attempts to actually fix. First it had nowhere
  to go and fell through to `examine`. Then the fix that added it
  looked right in isolation but never fired in production, twice in a
  row — it was sitting in the intent parser's keyword fallback AFTER
  the known-NPC-name loop, which matched "Sera" first and returned
  `talk_npc` before the sheet check ever ran. The real fix was moving
  the check before that loop, not tweaking its regex. check_sheet now
  supports a named target, looked up among real party members.
- The autonomous AI party generated an ungrounded action ("I ask
  villagers to join our party") because an example in its prompt was
  always shown even when nothing recruitable was actually nearby.

### New systems
- **Status ailments**: blinded, silenced, paralyzed, and frightened,
  each with a real mechanical effect (attack advantage/disadvantage,
  blocked spellcasting, or a fully skipped turn) — not flavor text.
- **Opportunity attacks**: breaking off from a fight isn't
  consequence-free anymore — every living enemy gets one attack roll
  as you flee, which can knock you out instead of letting you escape.
- **Real permanent death + Revivify**: 3 failed death saves is now
  actually final — the character can't act or be moved, and stays
  exactly where they died — reversible only via Revivify (a real
  Cleric spell, or a purchasable Scroll of Revivify).
- **Gathering professions**: Herbalism, Mining, Fishing,
  Lumberjacking, and Crafting each level up independently now (their
  own practiced-use track, not lumped into a shared ability check).
  New Fishing/Lumberjacking resource nodes, Raw Fish and Wood
  materials, and a real "make a campfire" action.
- **Combined multi-action replies**: a compound message ("recruit Sera
  and check my inventory") now sends one combined reply instead of one
  Telegram message per sub-action.
- **Optional TTS narration**: real narration can be read aloud via
  @TextTSBot when enabled (off by default) — plus a new live-settings
  framework (`db.game_settings`) for story mode, AI party pause, and
  Moltbook pause, changeable from the Development topic with no
  redeploy.
- **Full party sheets**: "show me my party's character sheets" now
  gives a real full sheet per member, including AI companions/recruits
  — not just names. The hourly "Players: X active" count now includes
  AI players too, instead of silently only counting humans.
- **8 previously-missing skill-check triggers** added (Arcana, Nature,
  Religion, Animal Handling, Insight, Medicine, Performance, general
  Sleight of Hand) after auditing against all 18 real 5E skills.
- Skill points (the practiced-use bonus system) now only earned on a
  successful roll, not every attempt — and shown directly on the
  character sheet next to each practiced skill.
- Campaign-book PDFs/links can now be dropped in Development for a
  future campaign-loading feature (saved to `campaign_sources/`,
  gitignored — copyrighted content has no business in a public repo).
- `CONFIG.md` — a full reference for every `.env` setting and every
  live Development-topic command.
- Level-gating for the campaign's two hidden endgame locations, and
  "accept the/this quest" phrasing now routes correctly.
- The autonomous AI party's action-generation prompt and `SKILL.md`
  (the integration doc for any AI agent joining the game externally)
  are both now grounded in every capability shipped this pass —
  gathering, crafting, campfires, party sheets, status conditions, and
  real death/resurrection — not just the older action set.

### Known limitation surfaced, not yet fixed
- Equipment doesn't currently affect combat stats at all — weapon
  damage and AC are fixed regardless of what's actually equipped. A
  real, separate architecture gap, flagged rather than silently
  patched over.

## [1.6.0] — Compound messages: one message, multiple actions

Per Coffee's request: a message that clearly asks for several distinct
things in sequence ("recruit Sera, look at the quest board, and leave
the tavern") now actually does all of them in order, instead of
picking one action and silently dropping the rest.

New `ai/intent_parser.py`'s `parse_intents()` sits alongside the
existing single-action `parse_intent()` (left completely unchanged, so
every existing safeguard around model misclassification still applies
exactly as before) and detects genuinely compound messages using only
the free, instant keyword fallback — never an extra Ollama call, so an
ordinary single-action message pays zero latency cost for this.
Deliberately conservative: only splits on strong, explicit separators
(commas, "then", "and then", ";"), and only actually treats a message
as compound if at least two of the resulting pieces independently
resolve to different real actions. A bare "and" joining two nouns in
one action ("attack the goblin and the wolf", "buy a sword and
shield") is left alone and classified as a single action, exactly as
before — the ambiguity between "two nouns, one action" and "two
actions" isn't reliably solvable by keyword splitting alone, so this
only ever activates where the evidence is unambiguous.

`bot.py`'s dispatch was split into `_dispatch_intent()` (one action)
called in a loop, one per detected action, each fully awaited before
the next runs.

## [1.5.0] — Reliability pass: real bug fixes, two more class features, natural-language coverage

### Real bugs found and fixed (all confirmed live, several from Coffee's own screenshots)
- NPC dialogue could crash on a transient network timeout, showing a
  generic "Something went wrong" instead of the NPC's real reply, even
  though the reply had already been generated. Swept the whole
  codebase for the same pattern and fixed 12 spots with the identical
  latent bug, including two inside combat that could have soft-locked
  a fight.
- A single transient send failure was logged and dropped with no
  retry at all — now retries once after a short pause before giving up.
- "buy X from `<NPC>`" (naming a real NPC while trying to shop) was
  misread as just talking to them, with no purchase happening.
- Buying/selling always silently used exactly 1 item regardless of
  what was said — "buy two potions" only ever bought 1. The shop code
  already supported real quantities; this was a pure wiring gap.
- Item matching required the exact catalog name ("Healing Potion") as
  a literal substring — "buy two potions" (the general word, not the
  specific name) matched nothing. Now falls back to a generic category
  word when it unambiguously picks out exactly one candidate.
- Comprehensive sweep of natural-language phrasing across quests,
  inventory, party, character sheet, looking around, gambling, joining
  a guild, resting, fleeing, fast travel, and finding a merchant —
  dozens of everyday phrasings ("what does this place look like",
  "heal me up", "get me out of here", "where can I find supplies",
  "ask for a hint", "check my equipment", and many more) were silently
  falling through to a no-op instead of the game action they clearly meant.

### Class features
Two more of the four remaining subclass-style features now shipped,
both using a fixed-default convention (no in-game subclass-choice
mechanism exists yet, so each class gets the single most iconic
default rather than a half-built choice system):
- **Sorcerer — Draconic Bloodline**: Draconic Resilience grants
  AC = 13 + DEX modifier when unarmored and +1 HP at level 1.
- **Warlock — Otherworldly Patron: The Fiend**: Dark One's Blessing
  grants temporary HP (CHA modifier + level) whenever you reduce a
  hostile creature to 0 HP. Introduced a real `temp_hp` mechanic on
  combat participants that absorbs damage before real HP for anyone
  carrying it, not just Warlocks.

That's 9 of 12 classes with at least one real mechanical feature now
(Cleric, Druid, Ranger remain, all needing a subclass/choice design
conversation first).

### Operational
- The autonomous AI party is paused for now, at Coffee's request,
  while reliability for real human players is the priority ahead of
  eventually opening the game up publicly. Nothing was deleted —
  existing AI companions are untouched, autonomous ticking just stops
  until it's turned back on.

## [1.4.0] — Real class features, complete spells/races, and epic environment-aware narration

### Mechanical class features (previously flavor text only)
Eight real 5E class features are now genuinely usable, not just shown on
the character sheet, backed by a new shared "limited-use resource"
system (resets on a full rest, same gating as HP/spell-slot recovery):
- **Fighter — Second Wind**: bonus-action heal (1d10 + level), once per rest.
- **Barbarian — Rage**: bonus damage and resistance to incoming damage
  for the rest of a fight, 2 uses per rest.
- **Rogue — Sneak Attack**: automatic +1d6 damage once per turn when
  attacking with advantage.
- **Bard — Bardic Inspiration**: an immediate HP boost to an ally,
  uses per rest equal to Charisma modifier.
- **Paladin — Lay on Hands**: heals a real 5-x-level HP pool, once per rest.
- **Wizard — Arcane Recovery**: recovers ceil(level/2) spell slots once
  per rest.
- **Warlock — Pact Magic**: automatic, no command needed — a Warlock's
  spell slots now recover on a much shorter real-time curve (1/8th of
  everyone else's) than their HP, reflecting real 5E's short-rest
  slot recovery vs. everyone else's long-rest recovery.
- **Monk — Unarmored Defense + Martial Arts**: both automatic, no
  command. AC is now 10 + DEX mod + WIS mod instead of a flat
  approximation, and every Monk attack uses DEX instead of STR.

Everything else in `class_features.py` (Divine Domain, Sorcerous
Origin, Favored Enemy, etc.) is still accurate flavor text, not yet
mechanical — this build is honest about that split rather than
half-building any of them.

### Complete spells, cantrips, and races
- Filled out real 5E cantrip counts for every casting class (Wizard,
  Sorcerer, Warlock, Cleric, Druid, Bard) — most previously had only a
  single placeholder cantrip.
- Added real leveled spells so every casting class actually gains new
  spells on level-up through character level 5, not just their two
  starting spells.
- Fixed three real accuracy bugs found along the way: Cleric had
  "Shield" (a Wizard/Sorcerer-only spell), Druid had "Healing Word"
  (never on Druid's real spell list), and Warlock had "Magic Missile"/
  "Burning Hands" (neither is a real Warlock spell — replaced with
  Warlock's actual spell list, including its signature Eldritch Blast
  cantrip). Also fixed Bard previously being granted a 2nd-level spell
  at character creation with no level gate.
- Added the two missing core 5E races, **Gnome** and **Half-Orc** —
  both fully playable with real ability bonuses and traits, including
  two genuinely mechanical Half-Orc traits: **Relentless Endurance**
  (drop to 1 HP instead of 0 once per rest) and **Savage Attacks**
  (an extra weapon damage die on a melee critical hit).
- Filled in two missing traits on existing races: Elf's Trance and
  Dwarf's Dwarven Combat Training.

### Epic, environment-aware narration
Combat and spell narration now gets the real, current location's
description, with explicit instruction to let the physical
surroundings genuinely color the imagery (a lightning spell crashing
down like the wrath of a storm in an open field reads very differently
than the same spell cast in a cramped tavern) — never inventing new
environment details, and never changing who won, lost, or how much
damage was actually dealt.

### Combat loot and quest-of-the-day fixes (carried over from small
live fixes shipped since 1.3.0)
- Combat victories now award real, varied gold from procedurally
  generated loot (previously XP only) — `rules/item_generator.py`
  existed fully built but had no caller anywhere in the game.
- Fixed asking to read the quest board while naming an NPC (e.g.
  "read the quest for Grimsby from the quest board") getting
  misrouted to that NPC's shop dialogue instead of showing the board.
- NPC dialogue and quest board listings now ground themselves in real
  quest rewards/choices instead of vague non-answers or no reward info.
- Fixed two "no character name" inconsistencies: examining or
  gathering with nothing to find now names the acting character, same
  as their own success-path messages already did.
- Dev-topic screenshot uploads now survive a transient network timeout
  on their confirmation reply instead of crashing silently.

## [1.3.0] — Moltbook autonomy, more recruitable companions, and dev tooling

### Moltbook (AI-agent social network)
- **The bot now has full autonomy on Moltbook** — it can genuinely
  post, comment, and upvote on its own, roughly every 30 minutes, one
  action per cycle. Every decision is grounded strictly in the real
  Moltbook feed and this game's own real recent activity — it never
  invents what another agent said and never fabricates game news.
  Anything the model returns that doesn't cleanly match one of the
  expected response formats is treated as "do nothing," never guessed
  at or partially acted on.
- Fixed a real repeat-notification bug: heartbeat updates were
  re-sending the exact same Moltbook activity to the Development topic
  every ~15 minutes indefinitely, because the old suppression check
  only compared the unread count and bypassed itself whenever any
  activity existed at all. It now compares the actual content, so it
  only notifies again when something has genuinely changed.

### More recruitable companions
- Added 5 new recruitable companions with varied races/classes, spread
  across different locations instead of clustering in one spot: a
  Dwarf Paladin (Market Row), a Half-Elf Druid (Hollow Stump Shrine), a
  Halfling Bard (Stonearch Bridge), a Dragonborn Barbarian (Goblin
  Warrens), and a Tiefling Sorcerer (Glimmerdeep Grotto).
- The autonomous AI party can now discover and recruit any of these on
  its own — recruiting was previously invisible to its situation facts
  entirely, and its action prompt now knows about its own inventory and
  known spells too (fixing a latent risk of it "casting" a spell or
  "selling" an item it doesn't actually have).

### Developer tooling
- Screenshots dropped in the Development topic now actually get saved
  (with the caption logged) instead of vanishing without a trace —
  photo messages have no text, so the bot's normal text handler never
  saw them at all before this.

## [1.2.0] — AI party fixes, quest system hardening, and reliable uptime

### AI-companion identity bug (the big one)
- **Fixed a real ID collision that was quietly corrupting the
  autonomous AI party.** `create_ai_companion`'s synthetic ID counter
  used to reset to a hardcoded `-1000` on every process restart. That
  collided a brand-new autonomous party member (Zara Windrift) with an
  already-existing recruited companion NPC (Sera) that had claimed
  `-1000` in an earlier process. The collision silently rebound Sera's
  active-character slot to Zara, which corrupted the AI party's
  round-robin turn order (Bram Ashfield never got a single autonomous
  turn all session) and explained the AI party's confusing behavior —
  it wasn't inventing content, it was grounded in one character's real
  location while its action landed on a different character standing
  somewhere else. The counter now derives from what's actually in the
  database, so this can't recur; Sera's existing data has been
  repaired (her own ID restored, her own active-character slot back,
  autonomous flag reset).

### AI party / autonomous companions
- The AI party's action prompt no longer names real, copyable
  places/objects in its examples — the small local model was parroting
  those exact examples verbatim regardless of whether they were true,
  which is what caused the repeated "can't get there directly" spam.
  Examples now use situational placeholders instead.
- Moving to your own current location now gives a clear "you're
  already at X" reply instead of the generic can't-get-there message.
- The AI party's situation facts now include sub-locations reachable
  via `descends_to`/`ascends_to` (e.g. a tavern cellar), a hand-authored
  story quest on offer at the location (previously only area board
  bounties were mentioned), a pending branching-quest decision and its
  real choice labels, and what a shop actually has for sale — all
  previously missing, all now grounded in real data.
- The prompt now knows "accept the quest" is a real, expected action.
- **AI-controlled party members can now actually complete quests they
  accept.** A leftover exclusion (from before this game had autonomous
  AI players) blocked every `is_ai` character from ever getting credit
  for a monster kill, even for a `defeat_monster` quest they personally
  held — contradicting this project's own design philosophy that an
  AI-driven party member plays under exactly the same rules as a human
  one.

### Bug fixes
- Resting no longer gets interrupted by repeating "I rest" — it used
  to fully wake the character (losing partial healing) before putting
  them right back to sleep.
- Character-creation progress (or any other mid-flow player state) no
  longer gets silently wiped by a routine bot restart — `user_data` now
  persists across restarts.
- Stat-assignment questions ("help me assign my rolled stats") now get
  a real, deterministic answer computed directly from the character's
  actual class and race — highest rolls to that class's real 5E
  priority stats, real racial bonus applied — instead of asking the
  model to reason it out. It was previously giving bad advice (once
  suggesting a full class change) with no real bonus data to work
  from, and even after grounding the prompt with real data, consistently
  exceeded the response timeout. Same "rules decide, AI narrates"
  principle already used for XP-to-level and active-character lookups.
- "Who is my active character" now classifies correctly instead of
  falling through ungrounded.
- Quest-accepted narration now includes the character's name, matching
  the existing convention for other action messages in the shared
  Adventure feed.

### Infrastructure
- The bot is now supervised by a `systemd --user` service
  (`pandora-mmo-bot.service`, enabled for boot/login persistence)
  instead of a manually launched process — restarts are now near-
  instant and the bot's uptime no longer depends on any particular
  Claude Code session staying open.

## [1.1.0] — Living world: lockpicking, crafting, factions, memory, and more

### New gameplay systems
- **Lockpicking** — real chests and doors, gated behind a genuine DC-13
  DEX check (rules/dice.py, same fixed-DC convention as every other
  skill check). Chests grant real loot/gold; locked doors permanently
  open a shortcut once picked. State is in-memory (resets on restart),
  the same deliberate simplification as combat conditions.
- **Gathering & crafting** — resource nodes at specific locations grant
  raw materials on a successful ability check; a new deterministic
  `rules/crafting.py` module resolves real recipes (material check +
  ability roll decide success, never invented by narration). Failed
  crafts don't consume materials, so a bad roll isn't punished twice.
- **Fog-of-war map** — `visited_locations` is now tracked per character
  and persisted to the DB; "show me the map" renders only what's
  actually been explored, with unexplored connections shown as a count
  rather than a name.
- **Character slots** — a telegram user can now own multiple characters
  (new `character_id` primary key + `active_characters` table). Create
  additional characters without deleting existing ones, list your
  roster, switch your active character, or delete one — the oldest
  remaining character becomes active automatically if the active one is
  deleted.
- **Class/race spell grants on level-up** — leveling up now actually
  grants new spells as they're unlocked (`spells.py`'s
  `SPELL_LEVEL_UNLOCK_CHAR_LEVEL` table), not just at character
  creation. Applies to AI companions too, since they level through the
  same `db.add_xp`. Tiefling's Infernal Legacy trait now genuinely
  grants the Thaumaturgy cantrip at creation.
- **Fast-travel waypoints** — any location a character has actually
  visited becomes a warpable waypoint ("warp to the crossroads
  tavern"), skipping the walk. Ordinary on-foot `move` (fully narrated,
  connection-by-connection) is unchanged; this is a pure convenience
  layered on top of the same visited-locations data as the map.

### Living AI world-NPCs
- NPCs now have a real **alignment** and **disposition** (friendly,
  neutral, or hostile), authored per-NPC in campaign.json. Arriving at
  a location with one has a chance of an unprompted, in-character
  ambient reaction — not just a reply to something the player said.
- **Hostile NPCs are real threats** — a hostile NPC encountered this way
  provokes a genuine, dice-resolved combat encounter through the exact
  same combat engine as any other fight; the AI only narrates the
  provocation, never the outcome. A defeated named antagonist doesn't
  respawn.
- **Persistent, per-player NPC memory** — a new `npc_relationships`
  table tracks rapport (`affinity`) and specific remembered events per
  (player, NPC) pair, surviving restarts. NPCs reference this in both
  ordinary conversation and ambient lines, so a shopkeeper genuinely
  remembers what a specific player has done.
- **Real consequences** — stealing is a genuine DC-15 DEX check
  (harder than an ordinary skill check). Getting caught permanently
  bans that player from the shopkeeper's shop (`shop.buy_item` now
  checks this), tanks affinity, and is remembered as a specific event.
- **Factions** — NPCs belong to factions (`campaign.json`'s new
  `factions` catalog) with their own standing per player
  (`faction_standing` table). Defeating a faction member in combat, or
  stealing from one, sours the whole faction's standing with that
  player — and a badly-soured faction can turn even its friendly/
  neutral members hostile toward that specific player.
- **Proficiency growth** — "the more you do something, the better you
  get": repeated real use of an ability in skill checks, lockpicking,
  gathering, or crafting earns a small, capped bonus over time
  (`rules/proficiency.py`), applied only to these non-combat checks,
  never to attack rolls.

### Fixes found via live testing
- The AI intent classifier was unreliable against explicit keyword
  triggers once the action list grew large (e.g. "my characters"
  misread as starting combat, "pick the lock" misread as passing a
  turn) — the keyword fallback's classification is now trusted whenever
  it has any non-default opinion, not just for brand-new action types.
- Locked-door destinations were invisible to `move` entirely (not even
  reachable-but-blocked) because they weren't included in the
  destination search — fixed so a locked connection is a real, nameable
  travel target that reports as blocked until picked.
- A single-lockable location could match the wrong kind of lockable
  (a chest matching "door" phrasing, or vice versa) via an overly
  generic fallback — now kind-specific.

## [1.0.0] — Initial tagged release

### Core game
- Natural-language play in the Adventure topic — no slash commands required
- Character creation: name, race, class, 4d6-drop-lowest ability score rolling
- Real 5E racial ability bonuses and traits (Human, Elf, Dwarf, Halfling,
  Half-Elf, Dragonborn, Tiefling)
- Real 5E level-1 class features for all 12 classes
- Turn-based combat with initiative, real dice-based attack rolls, and a
  visually structured combat log (round/turn announcements, critical
  hit/fumble banners, real roll numbers always shown)
- Death saving throws, fully reconnected: natural 1 = two failures,
  natural 20 = instant revival, 3 successes = stable, 3 failures = death
- Multi-enemy encounters with individually numbered, targetable enemies
- Conditions: prone (via a real contested shove check) and poisoned
  (inflicted by certain monsters), both with real advantage/disadvantage
  mechanical effects
- Non-combat skill checks (sneaking, persuading, climbing, etc.) rolled
  against a real ability modifier and DC
- Spell slots: real 5E level-1 allotments per class, actually spent and
  restored on rest
- Saving throws for area spells (Fireball, Lightning Bolt, Burning Hands)
  — half damage on a successful DEX save, per real 5E rules
- Leveling: real HP growth per level and Ability Score Improvements at
  levels 4/8/12/16/19
- XP awarded on real monster defeats, sourced from actual 5E XP-by-CR
  values
- Backpack, shop (buy/sell), and guild systems
- AI-controlled party companions and recruitable NPCs
- A hidden, spoiler-free campaign storyline across a 3-layer world
  (surface / underground / sky)

### AI & reliability
- Natural-language intent classification with a robust keyword fallback
  when the model is unreachable
- Dungeon Master narration and NPC dialogue via a local Ollama model,
  with dramatic tone calibrated to how good or bad each dice roll was
- `<think>` reasoning tags stripped from all model output before it
  reaches players
- Per-chat locking to prevent concurrent players from corrupting the
  same combat session
- Concurrent update processing so one slow handler can never freeze the
  whole bot
- A universal "cancel" / "stop" / "reset" escape hatch, including
  force-ending a stuck combat session

### Meta
- Development topic restricted to the real Telegram group owner
  (checked live via the Bot API)
- Support topic open to everyone, with an AI assistant grounded in the
  actual item/spell/guild data (never inventing content that doesn't
  exist in this build)
- `/version` and `/changelog` commands

### Known limitations (see SETUP_GUIDE.md for the full list)
- Reactions (Shield, Counterspell, opportunity attacks) are not yet
  implemented — this needs real changes to the combat resolution step,
  not a quick bolt-on.
- Conditions reset when combat ends rather than persisting with full
  duration tracking.
- Rest is a simplified full-heal, not full 5E short/long rest rules.
