# Changelog

All notable changes to Pandora MMO are documented here.

## [1.27.68] — Using an item/scroll ON a named companion no longer gets swallowed as chat

Real live bug (Coffee, mid-fight: "Use a scroll of revivify on Wren"
produced an unrelated ambient "Wren says a line about her garden" reply
instead of any real result). The scroll-of-anything → cast_spell
classification rule already existed (fixed once before for a different
companion's name), but it ran *after* the deterministic keyword
fallback's generic "message names a known NPC/companion → talk to them"
check — so naming who the item was FOR defeated the very rule meant to
route it correctly. Moved the scroll check ahead of that generic check,
matching how `give_item`/`recruit_npc` were already special-cased for
the identical reason. Confirmed the underlying revive logic itself was
already correct (a companion reduced to 0 HP has been marked genuinely
dead in the database since the 2026-07-24 fix) — this was purely a
misclassification bug, not a revival-logic one.

## [1.27.67] — Multiple simultaneous fights, same chat, no cross-contamination

Phase 1 of the multi-tenant scaling plan: `sessions.py` no longer keeps
one combat session per chat — it's now keyed by a real `session_id`, with
a per-user index (`get_session_for_user`) resolving which specific fight
a player is actually in, and a per-session lock so two unrelated fights
in the same chat never block or bleed into each other. Before this,
starting a second fight while any other player's fight was still active
anywhere in the same chat was flatly impossible — the old design
unconditionally overwrote whatever fight was already running. Every
combat-adjacent handler in `bot.py` (attack, flee, spells, class
features, use-item, formations, fast-travel/rest/switch-character combat
gates, the join-an-in-progress-fight flow, the owner-only stuck-combat
override, `/redo`) now resolves the *acting player's own* fight rather
than "whatever fight this chat happens to have." Also fixed a real
latent bug caught during this rewrite: the startup snapshot-restore loop
was iterating the session dict as if it were still keyed by chat_id,
which would have misdirected restored-fight announcements and locks the
moment two sessions were ever restored from the same snapshot at once.
Verified with two independent parties fighting simultaneously in the
same chat, through the real handlers, with real Ollama narration —
zero shared state, and a player already fighting can't be double-drafted
into a second fight.

## [1.27.66] — Fast-travel now brings your AI companions with you

Real bug found investigating Coffee's report ("It's not letting me
send to wren, who is in my party"): on-foot travel has always moved
real AI companions along with the party, but fast-travel (waypoint
warp) never got the same fix — warping anywhere silently stranded
every AI companion at the old location. Confirmed live: Ravenloft
fast-traveled to Market Row while Wren Hollowbrook stayed behind at
the Sunken Root Caverns, so "give X to Wren" correctly (if
confusingly) said she wasn't there — she genuinely wasn't. Fast-travel
now brings every real AI companion along, same as walking does.

## [1.27.65] — Equip from the battle menu now offers your whole party

Real live report, caught testing the new battle menu the moment it
shipped: "when i clicked equip it only showed my character and no one
else." `_do_equip_item` has always supported equipping gear for
another present party member — the new battle-menu button just never
offered anyone but the caller. Tapping Equip now shows an "⚡ Auto
Equip Party" option plus every present party member (self included);
picking someone shows THEIR carried gear, not yours.

## [1.27.64] — Battle formations mid-fight, and AI companions actually earn combat XP now

Per Coffee: "let the party use formations to move forward and pull
back in battle... it shudnt cost them a turn... this can be very
useful in boss battles and defending players." Front/back row can now
be changed in real time, mid-fight, by anyone — free-text ("pull
back", "cover me", "fall back", "push up", "hold the line", "pull Zara
back") or a button, and it takes effect immediately for the fight
already in progress, not just the next one. The old flat "Run" button
is now a "⚙️ More" submenu (Run, Give, Formation, Equip) so the
primary Fight/Skills/Items row stays clean — every action in that
submenu is a genuinely free, no-turn-cost utility, confirmed by
reading each one.

Also fixed a real, significant bug found while investigating Coffee's
live report ("They didn't get experience for being in battle" — Wren
Hollowbrook, a real, present, actively-fighting AI companion, had 0 XP
after a genuine combat win): AI companions were being excluded from
combat XP entirely, even when they were actually in the fight —
contradicting this game's own design (quest-reward sharing already
correctly included them). A present, fighting AI companion now earns
the same real combat XP share as a human party member. An absent
companion (not in this specific fight) still correctly gets nothing —
that rule is unchanged.

## [1.27.63] — New AI companions join at the party's level, and "look around" now shows enemy difficulty

Per Coffee: "when we get a new AI party member to the party, make
their level when joining the party's avg lvl, so players stay
balanced with the party." A freshly recruited companion (or an
existing one invited into a new party) now joins at the real average
level of the party they're entering — recomputed from scratch with
the same formulas a genuine level-up uses (HP, proficiency bonus, XP),
never just a bigger number with stale level-1 stats behind it.
One-directional: a companion already ABOVE the party's average keeps
their own higher level rather than getting nerfed down.

Also, per Coffee: "show the lv of the enemies when we look in areas —
it will give us an idea if we're over our heads or not. If it's a
boss show it as '???'." "Look around" now shows a real inferred level
per monster present (grounded in the same real 5E medium-encounter XP
math the game already uses to scale fights) — a boss shows "???"
instead, same spoiler-avoidance convention as the locked chapter list.

Two real bugs turned up while testing these: a companion's "joins your
party!" message was listing every active character in the entire
game, not just their actual party, whenever more than one real party
existed at once; and "look around" was showing a monster's raw
internal key (e.g. "colosseum_champion") instead of its real display
name.

## [1.27.62] — A quieter thread through the post-story dungeons

A small, deliberate addition to the six grind dungeons that open up
after the main story (Arc 5-10) — no new mechanics, no spoilers here.
Nothing that changes how any of these fights play, and nothing a
first-time player needs to notice. Attentive players might.

## [1.27.61] — Active AI companions now actually show up to fight

Real live report: Coffee had Grask Emberscale — a real, active,
non-benched party member — sit out a fight entirely because he'd
wandered to a different location under the living-world system.
"I want the AI players that are in the party to be in the battles —
that's the point of picking the active party members for combats."

Fixed at the real choke point (`_get_real_party_combatants`): an AI
companion's own location was never a deliberate choice to skip a
fight the way a real human's is — a human genuinely has to be standing
here (can't be teleported into a fight they never walked into), but
an AI companion in your active roster now rushes to your location the
moment combat starts, regardless of where they'd wandered off to.
Being in the active roster (not benched, not resting) is what
actually decides who fights, matching what "active party" was always
supposed to mean. Benching still works exactly as before — a benched
companion still sits out no matter where they are. The fight's opening
message now says who rushed in, so this is visible instead of a
silent database write.

## [1.27.60] — Battle formations: front row tanks, back row is safer

Per Coffee: "character placement has an effect in battle." Every
character (and now every enemy too) has a real front/back row. Front
row is the enemy's primary target; back row is genuinely safer — a
real AC/evade bonus (`BACK_ROW_AC_BONUS`, +2 by default) applied right
where every other AC bonus in this game already lives, plus a lower
chance of being targeted at all while anyone's still standing up
front. Nobody's ever fully safe — there's still a real, if smaller,
chance the enemy targets the back row directly (`FRONT_ROW_TARGET_
CHANCE`, 80% front by default), and once the front row is wiped, the
back row becomes the only target left, same as it would in a real
fight.

This runs both directions: enemies target the party by row, and AI
party members/companions target enemies by row too — a handful of
real monsters (goblin shamans, and any future caster/ranged archetype)
default to their own back row, so a smart player can choose to focus
the real threat instead of whichever monster the game happens to
list first. Every fight's opening message now spells out both
formations by name so there's never any guessing who's where.

Fully player-customizable: "move Zara to the back row" / "put me up
front" / a real ✅ Front / 🔮 Back toggle button on the Party menu for
yourself and every real party member. No hard restriction on what a
front-row character can carry or cast — row only changes targeting
odds and AC, so a melee character stuck up front (or a caster who
wants to hang back) never has an action blocked outright.

## [1.27.59] — Items now have real descriptions, and you can finally examine what you're carrying

Found from a real Development-topic screenshot: "Look at the tattered
underground chart" — an item Coffee actually owned — got "doesn't spot
anything like that here," every time, regardless of phrasing. Root
cause: examining something only ever checked the current location's
objects and monsters, never the character's own inventory at all, so a
carried item could never be examined, full stop.

Fixed: `_do_examine` now also checks your real inventory (via items.py's
own name-matching) before giving up. All 87 items in the catalog now
have a real `description` — and wherever an item actually heals or
deals damage, the description states the exact mechanic: real dice
notation and damage type (e.g. "Heals 1d1+99 HP (100 flat)", "Deals
8d6 fire damage (Dexterity save for half)"), not just flavor text.

## [1.27.58] — Pick who's in the fight: party bench + a real active-combat cap

Per Coffee: parties can grow past a comfortable battle size, so pick
who's actually fighting. A real party member can now be **benched**
out of the next fight without leaving the party — "bench Zara" / a new
✅/⬜ toggle button on the Party menu, "unbench Zara" / "bring Zara
back" to bring them back in. Benching doesn't touch location or
resting; a benched member can be standing right next to the fight,
just sitting it out.

A party's active (non-benched) fighting roster is now hard-capped at
`PARTY_ACTIVE_COMBAT_CAP` (6, env-configurable, independent of
`PARTY_MAX_MEMBERS` which still caps total party membership) —
enforced at the one real choke point, `_get_real_party_combatants`, so
this holds regardless of button state or party size. Un-benching
refuses once the active roster is already full, with the same "your
party is already full" style message invites already use.

Benched members still share fully in the party's progress: they fall
into the existing "absent party member" reward path
(`INACTIVE_PARTY_XP_SHARE`, 50% of XP and gold) already shipped for
anyone off resting or elsewhere — no new reward logic needed, benching
just adds a third reason a real party member can land in that bucket.

## [1.27.57] — Paralysis can no longer permanently soft-lock a boss fight

Found by the full beginning-to-end playthrough simulation: `paralyzed`
is the one on_hit_condition used exclusively by bosses (goblin_boss,
the_waking_ember, the_unbegun, the_unasked), and this engine has never
tracked a duration for any condition — so a single paralyzing hit
against a solo player used to be permanent for the rest of that fight.
The game's own stalemate-safety mechanism would then correctly detect
"the party can never act again" and end combat in a no-win, no-loss
draw... which meant the boss was never actually defeated, no matter how
many times the fight was retried, since the exact same risk was there
every single attempt. This is very likely why an old prior-session
playthrough log shows the identical symptom against this same boss.

Paralyzed now works the way real 5E "paralyzed until a save succeeds"
effects do: a Constitution saving throw (same fixed DC 13 this game
already uses for every other check, not a new invented number) at the
start of the paralyzed creature's own turn. Success breaks the
condition and lets them act normally that same turn; failure skips the
turn as before. The other, unrelated stalemate-safety check that used
to treat "currently paralyzed" as an automatic permanent incapacitation
(written back when that was actually true) has been corrected too —
otherwise it would end the fight before the save attempt ever got a
chance to run, which is exactly how a real regression test caught this
during verification, before it ever shipped.

Verified with a dedicated real-dice test (failed save keeps the
condition, successful save frees and hands back the turn, and a real,
unforced loop confirming paralysis is no longer an unbreakable dead
end) plus the full 285-test regression suite, green.

## [1.27.56] — Softened the other two very-early monsters the same way

Follow-up to 1.27.55's goblin fix: the same "damage tuned for the
monster's intended level, catastrophic against a fresh level-1
character" problem also applied to the Whispering Wood's wolf and the
Sunken Root Caverns/Goblin Warrens' goblin shaman — both had a flat
damage bonus high enough to guarantee-drop a level-1 character on any
connecting hit. Softened both the same way (wolf's damage_bonus 10->1,
goblin shaman's 14->4), keeping their relative danger ordering (goblin
shaman still hits hardest, matching its higher intended tier) while
removing the guaranteed-instant-knockout math. Confirmed live before
shipping; full 285-test regression suite green.

## [1.27.55] — Real human-style playthrough testing: a boss-fight bug, environment targeting, and a lethal starter fight

Ran a full, real (non-mocked) simulated playthrough — real dice, real
DB, real Ollama calls for both intent classification and narration —
the same way a human actually plays, to hunt for issues no unit test
in isolation would catch. It found a real one, plus feedback prompted
two more fixes:

**Fixed: "fight the goblin boss" was fighting a plain goblin.**
`_dispatch_intent`'s own `start_combat` resolution did a naive
first-substring-match scan over every monster in the game, and since
"goblin boss" contains the bare substring "goblin", it locked onto the
wrong, non-boss monster every time — silently making `clear_the_warrens`
uncompletable and the entire new Boss Enrage/environment/cutscene
system from 1.27.54 unreachable by the most obvious phrasing a player
would type. The exact same bug was already found and fixed in a sibling
code path (`_do_attack`'s auto-start-combat) back on 2026-07-23, but
that fix never made it to this second path. Now reuses the same
proven, location-scoped, longest-name-first matcher both paths should
always have shared.

**Interactive combat environments: no more guessing a magic phrase.**
Per feedback, "use the environment" was too vague — a player had no way
to know a room even had one, or what to say. Now, when a fight starts
in a room with a real environmental hazard, it's clearly narrated as
plain scenery up front (e.g. "The sagging tunnel supports — old timber
groaning under the weight of everything above"), and you can act on it
by naming the object directly, the same way you'd attack a monster —
"I attack the tunnel supports," "I hit the cliff" — no meta-phrase
required (the old generic phrasing still works too).

**Fixed: a level-1 character's very first fight was unsurvivable.** The
Whispering Wood's wandering goblin — the destination of the story's own
opening quest — had a minimum possible hit of 13 damage against a real
level-1 character's actual max HP of 12 or less: every class, on any
connecting hit, was guaranteed to go down, with zero chance of a lucky
roll saving them. Confirmed live, then softened (its flat damage bonus
dropped from +12 to +2) so the very first fight in the game is still a
real, dangerous encounter — just no longer a coin a player can never
win.

**Hardened: XP awards can no longer lower a level.** `db.add_xp`
recomputes level from total XP with no floor against the character's
current level — found no live path that can actually desync the two,
but the very next XP award after any such desync (even 1 XP) would
otherwise have silently demoted the character. Added a floor so gaining
XP can never reduce a level.

## [1.27.54] — Boss Enrage, interactive combat environments, and epic boss cutscenes

A real playthrough simulation right after the last balance pass
confirmed even dungeon bosses were dying in 1 round for a sliver of a
player's HP — the numbers were correct, but nothing was actually
dangerous. Three real, interconnected additions fix that and make
battles genuinely interactive:

**Boss Enrage.** Any boss dropping to 30% HP or below permanently
gains +50% damage for the rest of that fight — a real, one-time
"second phase," not a flat difficulty the whole way through. A fight
that drags past round 12 triggers the same enrage automatically even
above that HP threshold, with a fair warning at round 8 ("dragging
this out further looks dangerous") — real time pressure, never a
silent gotcha.

**Interactive combat environments.** 9 of the game's 10 boss rooms
(every real boss except the deliberately untouched final "cosmic
wall") now have a real, once-per-fight environmental attack you can
turn against every living enemy at once — the Goblin Warrens' sagging
tunnel supports, a loose stalactite in the Hush Below, a cracked steam
vent in the First City, the crumbling edge of the Unmoored Isle, the
Colosseum's dark-stained sand, and more — each grounded in that room's
own already-written description. Say "use the environment" in combat
to trigger it; timing when to use it is a real strategic choice.

**Epic boss cutscenes.** Every boss fight now gets a real, distinct
AI-narrated entrance the moment it begins, and a real, distinct
AI-narrated defeat line when it falls — separate from the plain
"Combat Begins!"/"has been defeated!" text every other monster gets,
grounded only in the boss's own real name and the real location's own
already-written description.

## [1.27.53] — Every monster now deals a real elemental damage type

Every monster in the game (55 of 56 — one true "cosmic wall" boss is
deliberately left untouched) now has a real, thematic `damage_type`
for its own attacks, grounded in its actual name/theme (goblins and
wolves hit physical; spiders deal poison; goblin shamans deal poison
from their brews; the Hush Below's Shadow Wisp and The Unspoken deal
necrotic/psychic; The Waking Ember deals fire; the wraiths/legionnaires/
watchers of the Hollow Verge deal necrotic; the Wordless Choir's
remnants/wardens deal psychic; the constructs and paradox-guardians of
The Unbegun deal force) — previously every monster attack silently
defaulted to plain physical damage, meaning racial resistances (Dwarf
poison resistance, Dragonborn/Tiefling fire resistance) and any
resistance/vulnerability profile only ever mattered for damage a
player was DEALING, never damage a monster was dealing back. Now both
directions of the damage-type system are real.

## [1.27.52] — A real, full monster/area/gear rebalance, and two live bugs fixed

**The big one: every monster in the game now has real, scaled HP and
damage.** Confirmed live: the first dungeon's own boss (Goblin Boss)
had only 21 HP while a level-5 player now has 150+ — a single hit
one-shot it. Root cause: monster damage had ALWAYS been a flat,
unscaled 1d8 for every monster in the game regardless of its own
stats, and monster HP was never touched when player HP got its 5x
rescale. Fixed both, for all 55 non-special-cased monsters (one true
"cosmic wall" boss is deliberately left untouched): real HP and real,
tier-scaled damage, using Coffee's own level curve (Arc 1 = level
1-10, Arc 2 = 10-25, Arc 3 = 20-40, Arc 4 = 30-60 — "the first
playthrough is designed to get them to the first evolution" — with
Arcs 5-10's existing side content and the already-real rebirth
1/2/3/10 gates carrying the same curve the rest of the way to the
endgame).

**Weapons, spells, cantrips, and healing abilities now scale with your
own level and rebirths too** — the same gap, from the other side:
weapon damage bonuses topped out at +3 game-wide, and spell damage
never scaled past the level it unlocked at, so casters flatlined
around level 9 while everything else in the game kept growing. A real
character's own weapon attacks, spell damage, spell healing, Second
Wind, Lay on Hands, Bardic Inspiration, Divine Smite, and Breath
Weapon all now scale by the same real formula monster stats do —
monsters are unaffected (their own damage is already scaled directly
in the data).

**Two real live bugs fixed:**
- Switching characters (either by typing it or tapping the roster
  menu) could silently drop you from your party — confirmed live,
  traced to the tap-menu path never applying the same party-seat
  carryover fix the typed path already had. Both paths now share one
  fixed helper. (Anyone this hit already had their party seat manually
  restored.)
- A screenshot+caption bug report in the Development topic could be
  silently lost if Telegram's photo-download API hit a transient
  timeout — the whole report handler crashed before ever logging the
  caption. Now retries like every other Telegram call in this game.
  (Also fixed the specific bug that report was about: the "Bait" item
  had no real flavor text at all, so its generated image had nothing
  to ground it in actual fishing bait/worms.)

## [1.27.51] — Party companion dialogue, a real first-dungeon hint fix, bestiary resistances, mythic rebirth narration, and a deeper Arc 3/4

**New: talking to your party, not just a named NPC.** Saying "talk"/
"speak"/"say"/"tell"/"yell"/"shout"/"scream" without addressing anyone
specific now prompts a real line from whichever recruited companion is
actually traveling with you, grounded in your real current location
and story-quest lead — not silence. A specific named NPC (`talk_npc`)
still always takes priority.

**Fixed a real, confirmed "stuck at the start" gap**: the first
dungeon's quest clue ("the goblins under Stonearch Bridge") read like
the entrance was AT the bridge, when it's actually one more hop away
at The Weeping Well, reached by "descending" — a non-obvious action
nothing else called out. The clue now says so directly.

**Bestiary now shows real resistances/vulnerabilities/immunities** for
any monster you've actually fought — the damage-type system has been
fully wired into combat since 2026-07-24, but nothing ever surfaced a
learned monster's real profile to players, so the whole "reward build
diversity" point of the system was invisible until now.

**Rebirth #1-3 each get a distinct, mythic narration beat** (a flash of
deja vu, sharpening into certainty, into "this is a cycle") instead of
the same dry mechanical reset message every time — planted, spoiler-
free foreshadowing that pays off in Arc 14's existing rebirth-10-gated
finale. Rebirth #4+ keeps the plain mechanical line.

**Arc 3 ("What Was Buried") and Arc 4 ("What Waits Above")** — the two
thinnest arcs in the story (1 quest each, versus 3-7 everywhere else —
these are the climax of a first playthrough, not a footnote) each gain
a real new quest: Arc 3 now also includes an already-written, existing
lore/riddle quest at the Sunken Archive that was never actually linked
into the arc, plus a new arrival quest; Arc 4 gains a new arrival quest
for first reaching the Unmoored Isle. Both new quests reuse only
already-existing locations/monsters — no new areas invented.

**Live data fix (already applied, not part of this deploy): existing
characters' max HP was corrected** to match the HP-scaling formula
shipped 2026-07-25 (`EVOLUTION_HP_MULTIPLIER`) — hp_max used to
accumulate incrementally per level-up, so anyone who'd already leveled
before that formula existed was permanently under-scaled relative to
a fresh character of the same level. A new, reusable
`rules/leveling.py: full_hp_max_for()` reconstructs the correct value
from real current facts (class, constitution, level, rebirth count)
alone — verified against real simulated playthroughs before touching
any live data, backed up first, never reduces anyone's HP.

## [1.27.50] — Real generated art for spells, class abilities, defeats, examined objects, and crafted/gathered/enchanted items

Per Coffee's running to-do list ("create images for spells abilities
and items and actions... fill the gaps with appropriate imagery",
extended over several follow-ups to cover both sides of combat, story
interactables, professions, and every class), this game's real
generated-art coverage (Pollinations.ai, same deterministic-per-thing
convention already used for locations/monsters/NPCs) now extends to:

- **Every spell cast** gets its own generated art, grounded in the
  spell's own real damage type/effect.
- **A defeat, on either side of a fight** (a fallen player, companion,
  or monster) now shows real art of the moment, not just text.
- **Examining a story interactable** (e.g. the wide-boled tree in the
  Whispering Wood) now shows a real generated depiction alongside the
  narration.
- **Gathering and crafting** a real material/item (the silverleaf herb
  you actually picked, the potion you actually brewed) now shows that
  exact item's own art — reusing the existing equip-image convention.
  Item art prompts now also fold in the item's own flavor/`note` text,
  so an enchanted or forged item's generated image reflects its real
  customization instead of a generic same-look icon per rarity.
- **Commissioning an Enchanters' Guild enchantment** now shows the
  granted item's real art.
- **Every class ability and racial trait** (Second Wind, Rage, Wild
  Shape, Action Surge, Reckless Attack, Divine Smite, Flurry of Blows,
  Breath Weapon, Channel Divinity, Bardic Inspiration, Lay on Hands,
  Arcane Recovery) now shows a generated depiction of the character
  using it.
- **Enemy/boss attacks**, not just their deaths, now re-show that
  monster's own real art at the moment it strikes (once per turn, not
  per Multiattack swing, to avoid spamming the chat).

All grounded only in real, already-existing game data (name, effect,
damage type, description) — never inventing visual details, same
discipline as every other image call in this game. An image failure
never blocks the real action; it only logs a warning, same as before.

## [1.27.49] — Autonomous AI party can now actually progress through rebirths/puzzles, plus real fixes

**Fixed a live regression from the last deploy**: the previous
version's edit-safety fix (`update.message` → `update.effective_message`)
broke every autonomous AI party member's own turn outright
(`AttributeError: '_AiPlayerUpdate' object has no attribute
'effective_message'`), caught live within minutes. `_AiPlayerUpdate`
(the internal shim that drives AI companions' turns) now sets both,
same as a real Telegram update always does.

**The autonomous AI party can now actually make it through the game.**
Real, confirmed gaps found while investigating "can the AI party reach
10 rebirths on its own": it had zero awareness that rebirthing was
even possible (would just sit at max level forever) and zero awareness
of puzzles (an accepted solve_puzzle quest — and Hollow Verge, Wordless
Choir, The Unbegun, and the true hidden final boss all have one — was
a silent, permanent dead end). Both fixed: the AI now sees a real fact
prompting it to rebirth at max level, and sees the real riddle text
for any puzzle it's accepted, prioritized right after quest-acceptance.
Also: "Places reachable from here" now filters out anything actually
gated (rebirth count, a required item, min level, an undefeated
prerequisite) instead of listing destinations that would silently
reject it — the same real checks `_do_move` itself applies.

**Sarah's pronouns are now real, grounded data** (she/her) instead of
implicit — NPCs can now carry a real `pronouns` field that flows into
their AI dialogue persona, so a model generating her dialogue has an
explicit fact to work from instead of guessing.

**"What's Next" is now a real, separate section**, per live dev-topic
feedback right after it shipped: generated as its own focused
narration call and combined with a real visual divider, instead of
asked for as a trailing paragraph inside the much longer recap.

## [1.27.48] — "What's Next" in the Story So Far screen, with a real chapter image

Per Coffee: "have in the story so far a 'whats next' section with a
clear hint what to do or where to go. dont give story away but be
clear enough they can do it unless its a puzzle then be vague and
ominous but at least hint how we can figure out the puzzle."

The Story So Far screen (📖 in the menu) now ends its narrated recap
with a real, grounded hint about the very next step in your current
chapter -- a real location and a real clue, both already-written game
data, never invented. For a normal quest, it's phrased clearly enough
to act on immediately (where to go, roughly what to do there). For a
puzzle, it stays deliberately vague and ominous -- mood and a nudge
toward HOW you might puzzle it out, never the actual answer. Also now
sends a real generated image alongside the recap, themed to your
current chapter (never the hidden next destination itself, so it can
never visually spoil a secret room before you've found it).

## [1.27.47] — Two real live bugs fixed: inventory screen crash, edited-command crash

**The backpack screen was completely broken.** Caught live in
bot_live_tmp.log right after last version's deploy: any character
with a real usable consumable or craftable recipe hit
`TypeError: can only concatenate tuple (not "list") to tuple` opening
their inventory — `InlineKeyboardMarkup.inline_keyboard` is a real
tuple, but the scroll and give buttons added this session returned
plain lists, and concatenating a tuple to a list raises. Hit
repeatedly by both Coffee and Sheri within minutes of the deploy.
Fixed by normalizing every piece to a list before combining.

**Editing a slash-command message crashed it outright.** Also caught
live: Coffee edited an existing `/msg ...` message, which crashed with
`AttributeError: 'NoneType' object has no attribute 'message_thread_id'`
— `update.message` is genuinely `None` for an edited-message update in
python-telegram-bot; only `update.effective_message` reliably resolves
either way. This exact unsafe pattern (`update.message.message_thread_id`)
was used in 75 places across every slash command in this file, all
fixed at once to the edit-safe accessor.

## [1.27.46] — AI party members now shop for themselves when they actually need to

Per Coffee: "does the AI companion kno to buy items needed for
battle?! they shud be able to take care of themselves." Buying was
already a real, grounded action for AI-controlled party members, but
purely opportunistic — nothing ever connected it to actual need, so an
AI companion could walk past a shop at 0 healing potions and never
think to restock. Now, whenever an AI party member is standing at a
shop, carrying zero of any healing item that shop sells, and can
actually afford the cheapest one, that's surfaced as a real fact
("You aren't carrying any healing items and this shop sells Healing
Potion for 25 gold...") and explicitly prioritized in their decision-
making — right after actual quest progress, ahead of every other
shopping/flavor option. Never fires when they're already carrying a
healing item, can't afford one, or the shop in question doesn't sell
any — this is about a genuine gap, not nagging.

## [1.27.45] — Real HP growth, a true hidden final boss, the whole story wired into cutscenes, and party-management buttons

A big one. In order:

**Real HP growth with rebirths.** Leveling 1-99 now actually grows your
max HP (a real, already-written 5E growth formula was quietly never
connected to the level-up path before this) -- lands around ~3,200 HP
at level 99 with no rebirths. Each rebirth then doubles your current
max HP on top of that (and fully heals you) -- reaches six-figure HP by
around rebirth 5, real "evolution" territory. Potions rescaled to
match: Healing Potion now heals exactly 100, Greater Healing Potion
exactly 1,000, and a new Supreme Healing Potion (500g, Old Maren's)
heals exactly 10,000. Revivify/scroll_revivify's revival amount is now
5% of max HP instead of a flat 1 -- with HP now reaching into the
thousands, a flat 1 HP had gone from "thin" to "statistically zero."

**All 6 remaining world dungeons expanded**, same treatment as last
version's 3 rebirth dungeons: Goblin Warrens, Sunken Root Caverns,
Stonearch Gorge (the underground gorge beneath the bridge), Greymoor
Downs, Whispering Wood, and Glimmerdeep Grotto each gain 2-4 new rooms
with real monster variants and rewards, without touching any existing
room, quest, or the game's other lore-only locations (the Pandora
marker room stays exactly as it was).

**A true hidden final boss.** Past the hollow the Waiting Shape left
behind, and now also sealed beneath the Lonely Cairn's own buried
marker, something the game has never announced exists: The Unasked,
100,000,000 HP, gated behind 10 rebirths AND having genuinely defeated
every other secret final boss in the game (the Waiting Shape, the
Unrepeating, the Unbegun) -- not reachable by grinding rebirths alone.
Ties every one of this game's secret endings together into one real
answer, with its own achievement and a distinct completion moment
unlike anything else in the game.

**The whole story now has real cutscenes.** Every dungeon and boss
built this session -- the 6 world areas, the 3 rebirth dungeons, and
the true final boss -- is now threaded into the same real story-arc
system that already bookended the base game's 4 chapters: a genuine
AI-narrated opening the moment you take on each area's first quest, and
a chapter-complete beat when you finish it. 14 real chapters now, start
to finish, one continuous line.

**New party-management buttons**, per Coffee:
- 🎯 Auto Level-Up Party: auto-spends every banked ability point (to
  each character's primary stat) and unlocks every affordable
  skill-tree upgrade across your WHOLE real party at once -- including
  AI companions, who'd otherwise never get either spent. Also works by
  just saying "auto level up the party."
- 🤝 Give buttons on the backpack screen: tap an item, tap a party
  member, done -- no more needing to type the exact "give X to Y"
  phrasing.
- Per-companion Level Up / Unlock Skill buttons on the Party Sheets
  screen: make the SAME real ability-score choice for an AI companion
  (or any party member) that you could already make for yourself,
  instead of only ever being able to auto-apply it.

## [1.27.44] — All 3 rebirth dungeons expanded (per Coffee: "bigger expansion")

Each of the 3 rebirth-gated dungeons (Hollow Verge, The Wordless Choir,
The Unbegun) just about doubles in size: 2 new required rooms spliced
into each dungeon's main sequential path (using real monster variants —
tougher named versions of that dungeon's existing creatures, not
invented from scratch — so the extra content stays grounded in what
each place already is), plus 1 new optional side-branch room per
dungeon with its own weaker variant monster and bonus loot, entirely
skippable and never blocking the main path to the boss.

9 new monster variants, 9 new quests, and 6 new items across the 3
dungeons. Every existing node, quest, and boss fight in all 3 dungeons
is completely unchanged — this only adds new rooms in between and
alongside what was already there, re-pointing the sequential
story-gates through the new middle rooms rather than touching the
start or end of any dungeon.

## [1.27.43] — The Unbegun: the true final boss, and the real ending

The third and final rebirth-gated dungeon completes the set — and this
one is the real ending. Where the waiting shape stood (this game's
previous final boss, still a genuine, satisfying conclusion for a
first-time player) is now just a hollow — and past it, for anyone
who's rebirthed 3 times AND already defeated the waiting shape, lies
The First Wait: something that was already here before there was a
shape to wait, before the island, before the story anyone was ever
told about this place.

Four new chambers, four new monsters (a watcher that guards what the
waiting shape stood on, an impossible turning stair, a loom whose
threads all lead back to where they started, and — at the very center
— **The Unbegun**, by far the strongest thing in the game), a puzzle
about cycles and endings, a full quest chain, and 2 new items — The
Unbegun's Crown is now the single most powerful piece of equipment in
Pandora MMO.

Defeating The Unbegun doesn't play like every other quest completion —
it gets its own distinct message and its own new achievement
(Cycle-Breaker), so reaching this specific moment actually feels
different from anything else in the game. Everything before this
remains exactly as conclusive as it already was for anyone not chasing
it — this is genuinely optional, deep post-game content, not a
retcon of the existing ending.

## [1.27.42] — The Wordless Choir: the second rebirth-gated dungeon

The second of 3 planned post-rebirth dungeons is live: **The Wordless
Choir**, found by pressing on past The Unrepeating — the secret boss
already waiting in the depths beneath The First City — but only for a
character who has rebirthed at least twice. Four connected chambers (a
threshold guarded by a lingering echo, a hall of ancient silent
machinery, a throat-shaped chamber with a real riddle to answer, and
the source of an impossible, wordless hum at the very center), four
new monsters with their own real damage-type resistances/
vulnerabilities — including a return of the real `silenced` condition,
fittingly, on the ones guarding a place built around silence — a full
quest chain, and 2 new items, including the game's new most powerful
piece of equipment so far.

This one builds directly on content that was already there rather than
bolting on somewhere new: The First City's deepest chamber already had
a real secret final boss (The Unrepeating) with nothing beyond it —
now there is.

## [1.27.41] — The Hollow Verge: the first rebirth-gated dungeon, plus two real bug fixes

The first of 3 planned post-rebirth dungeons is live: **The Hollow
Verge**, reachable north from the Far End of the Span (past Stonearch
Bridge) — but only for a character who has rebirthed at least once.
Everyone else finds the way there simply doesn't resolve yet. Four
connected areas (a haunted threshold, a bonefield, a sealed cairn with
a real riddle to solve, and an inner sanctum), four new monsters with
real damage-type resistances/vulnerabilities (a Verge Wraith resistant
to physical damage but weak to radiant, a Bone Legionnaire weak to
fire, a Cairn Watcher resistant to necrotic, and the dungeon's own
boss — The Verge Warden, resistant to both physical and necrotic, and
by far the toughest thing in the game so far), a full quest chain
ending in a real capstone reward, and 2 new items earned only by
clearing it.

Also fixed two real bugs found while building this:
- **Monster resistances/vulnerabilities were silently never applied in
  real combat.** The damage-type system (shipped earlier) fully
  supported a monster template setting `resistances`/`vulnerabilities`/
  `immunities`, but the actual enemy-building code in combat never
  copied those fields onto the real fight participant — so even a
  monster that set them would have had them silently do nothing. No
  monster happened to use them yet, so this never showed up until now.
  Real damage-type combat math against The Hollow Verge's new monsters
  confirms this now works correctly (halved/doubled damage as
  expected).
- **The "Run" (and some other) battle buttons could silently do
  nothing.** Found live in Development (2026-07-25): tapping "Run" hit
  a `telegram.error.BadRequest("Message is not modified")` from
  Telegram's side (the button's own visual update was a no-op) and
  that error aborted the ENTIRE button handler before fleeing ever
  actually ran — so the tap just did nothing, with no explanation.
  Every battle-menu button (Fight, Skills, Items, Run, and their
  submenus) shared this same silent-failure risk. Fixed the same way
  an earlier "stale button" bug was fixed: the button's own visual
  update failing no longer blocks the real action underneath it.

## [1.27.40] — Switching characters now carries your party seat with you

Real confusion from Development ("The AI member arent showing up in
my party?!... why are they out of the party now"): switching which
of your own characters is active used to leave your OLD character
still occupying your party seat while the new one had none at all —
so "my AI party isn't showing up" really meant "my other character
is still the one in it." Switching characters now moves your party
seat to the character you're switching TO (and clears it from the
one you're leaving), keeping the party's size exactly the same.
Never overrides a character that already has its own separate party.

## [1.27.39] — Fix: recruited AI companions could get permanently stuck away from you

Real bug from Development ("The AI member arent showing up in my
party?!"): once an AI companion (e.g. Pip Thistledown) had been
recruited once by ANYONE, "invite [name] to the party" for any other
character checked only "does a companion with this name exist,
active, anywhere in the game" — never whether they were actually in
THAT character's own party — so it always replied "already traveling
with you" even when the companion was in a different party (or none
at all), with no way to actually bring them along. Now correctly
checks the requester's own party specifically; if the companion is
elsewhere, they're really moved into the new party instead of hitting
a dead end.

## [1.27.38] — Real mechanical hooks for all 11 "utility" subclasses

Closes the honesty gap from the subclass system's first pass: every
class's second ("utility") archetype now has a genuine mechanical
bonus instead of being sheet-only flavor. Thief gets +3 on steal
attempts (stacks with the Thieves' Guild's own bonus), Life gets +2
extra healing on top of Disciple of Life, and Totem Warrior extends
Rage's damage resistance to spell damage too, not just weapon hits.
The other 8 (Battle Master, Devotion, Beast Master, Wild Magic, Great
Old One, Lore, Land, Open Hand) each get a real +2 on a
class-appropriate ability check, applied automatically anywhere that
check already happens — skill checks, gathering, shoving.

## [1.27.37] — Fix: scrolls had no button, anywhere

Real bug from Development ("No button for scrolls?", shown from a
real backpack listing with a Scroll of Revivify and zero way to tap
it): scrolls were never included in either the backpack's item
buttons or the character sheet's spell buttons, even though using
one has always worked via typed text. Both screens now show a real
"📜 Spell Name (scroll)" button per carried scroll, using the exact
same cast dispatch as a known spell — skipped only when you already
know that spell outright, so it doesn't duplicate.

## [1.27.36] — New achievement: Master of a Trade

Reaching Master rank (the top practiced bonus, 15 uses) in any of
the 7 real professions now earns a real achievement and title ("the
Master Artisan") — the first achievement tied to profession
progress.

## [1.27.35] — Professions overview + fix: Zara/Bram had no traits or features

New "🛠️ Professions" screen (menu button or say "check my professions")
shows real progress in all 7 professions at once — rank title
(Novice/Apprentice/Adept/Master), bonus, and use count — with your
class's favored trade marked.

Also fixes a real, long-standing bug: Zara Windrift and Bram Ashfield
(the original autonomous AI party) were the only characters in the
whole game stored with lowercase race/class ("elf"/"ranger" instead
of "Elf"/"Ranger"), so every exact-match lookup against them —
racial traits, class features, and the new profession-affinity line —
silently came back empty. Fixed at the source and corrected on the 2
existing live rows.

## [1.27.34] — Fix: Party Sheets button showed nothing for real parties

Two real bugs stacked here. Concatenating every party member's full
sheet into one message blew straight through Telegram's real
4096-character limit for anything past 2-3 members, so the whole
reply silently failed to send for any real full party — confirmed
live, `BadRequest('Message is too long')` on every attempt. On top of
that, the lookup silently dropped any party member whose owner had
since switched to a different character (same bug class already
fixed for shrine revival). Now sends one message per member (no
length ceiling) and includes every real party member, switched-away
ones included.

## [1.27.33] — Weekly and monthly missions, separate from the storyline

Every location's quest board now offers weekly and monthly missions
alongside the existing daily ones — same real objective mechanic
(defeat/gather), just bigger scale (much higher counts) and bigger
rewards (6x for weekly, 25x for monthly), with expiry windows that
actually match (7 days / 30 days instead of 24h). Reward XP already
scales further with rebirth for free, same as every other XP source.

## [1.27.32] — Silver Wardens' Colosseum echo-trials

A real, repeatable grind path parallel to rebirth: Silver Wardens
members at The Colosseum can now challenge "echoes" — magical mirror-
copies of monsters they've genuinely already fought (their own
bestiary, never an invented threat). Each trial victory raises your
echo trial tier permanently, scaling future echoes' HP/AC and XP
reward higher, and from tier 3 onward an echo has learned to resist
one real damage type, forcing real build adaptation instead of just
bigger numbers.

## [1.27.31] — 2 more guilds: The Forge Guild, The Enchanters' Guild

Two brand new guilds, each with their own real Telegram topic: The
Forge Guild (Fighter/Paladin/Barbarian/Ranger/Monk — a crafting-focus
alternative to Silver Wardens) grants members +10% weapon damage. The
Enchanters' Guild (Wizard/Sorcerer/Warlock — a crafting-focus
alternative to The Arcane Circle) lets members commission a real
enchanted item once per rest: a 1d20 + their own spellcasting ability
modifier decides which of 3 new real, equippable items (Band of
Ember, Sigil of the Deep, Crown of the Unmoored) they receive. Same
class+subclass gating and permanent membership as every other guild.

## [1.27.30] — Fix: party members no longer scatter away from the leader

Real bug from Development, reported twice: a recruited/autonomous AI
companion had no way of knowing where its human party leader actually
was, so it would just as happily wander off exploring on its own as
stay together — "the party's scattered all over the place" / "what
happened to all the party members? I'm the only one here." Companions
now snap back to their active party leader's location the moment
they've drifted apart, instead of relying on the AI to decide to stay
together on its own. Only while the leader is actually active — a
resting/away leader doesn't drag their party around with them.

Also: The Arcane Circle can now learn 2 real exclusive spells
(Starfall Lance, Voidcall — stronger than any other spell in the
game) found nowhere else, gated on real Circle membership and
character level.

## [1.27.29] — Class profession affinity + 5 real guilds, class+subclass gated

Every class now has a real "home" profession (e.g. Fighter →
Blacksmithing, Wizard → Alchemy, Ranger → Lumberjacking) granting a
genuine +2 ability-check bonus when crafting/gathering in it, shown
on the character sheet. All 7 professions are covered by at least one
class; subclass choice never changes it, since every subclass belongs
to one fixed base class.

Guilds are now genuinely restricted and meaningful: The Arcane Circle
(Wizard/Sorcerer/Warlock), The Silver Wardens (Fighter/Paladin/
Barbarian/Ranger/Monk), 2 brand new guilds — The Thieves' Guild
(Rogue/Bard) and The Faith Circle (Cleric/Druid) — plus the still-open
Adventurers' Guild. Joining any of the 4 restricted guilds now also
requires having chosen a real subclass first. Guild membership is a
real, permanent commitment now — once you're in, you can't leave or
switch to another guild. Hybrid characters (rebirth's dual-class
system) qualify for a guild on EITHER of their two classes' terms.

Real new membership benefits: Arcane Circle members deal +15% spell
damage; Thieves' Guild members get +3 on every steal attempt; Faith
Circle members heal +3 extra on every heal spell cast. Also added a
"🗝️ Steal Something" button to the shop menu.

## [1.27.28] — Four more profession recipes: blacksmithing's full curve + Scroll of Cure Wounds

Blacksmithing now covers 6 real tiers instead of 3: Rusty Dagger,
Shortsword, and Wooden Shield at the easy end, Longsword in the
middle, Chain Shirt and a new Chain Mail capstone at the top —
filling in real weapon/armor items that already existed in the shop
but had no recipe. Alchemy also gains Scroll of Cure Wounds (the
other common-tier scroll, same bracket as Magic Missile's). The
rarer scrolls (Fireball, Revivify, Lightning Bolt, Invisibility,
Summoning) stay real finds/purchases only, deliberately not
craftable.

## [1.27.27] — Two more profession recipes: Greater Healing Potion, Rations

Alchemy gets a real top-tier recipe (Greater Healing Potion, harder
than the base potion) and cooking gets a real easy one (Rations,
no fire needed, just more of the catch) — both items already existed
and were buyable, but had no recipe until now.

## [1.27.26] — Passive party HP regen + fix: standalone AI companions could die permanently

The whole party (any real player or AI companion, alive and not
currently in a fight) now slowly recovers HP on its own over real
time — 1 HP/minute base, doubled in safe locations, and boosted
further by regen-flagged equipped gear (Amulet of Health so far).
This stacks on top of, not instead of, the existing rest mechanic.
Spell- and consumable-granted regen boosts are a planned follow-up,
not built yet.

Also fixes a real bug reported from Development: an AI companion not
in anyone's real party (e.g. Bram Ashfield) who died in battle had
no possible path back — shrine revival and Revivify both require a
real party member to act, and a dead character can't act or move
itself there. Standalone AI companions now auto-recover after a few
hours; any companion still in a real human party keeps needing an
actual revival, same as before.

## [1.27.25] — Quest/mission rewards now shared with the whole party

Completing a story quest, board quest, or branching quest choice no
longer only rewards whoever turned it in — every other real party
member physically with you (including recruited/autonomous AI
companions) now gets the same full XP and gold, so the whole party
levels together. Party members off elsewhere still get a 50% share of
both, same convention combat XP already used for absent members.
Also: two new blacksmithing recipes (Rusty Dagger, easy; Chain Shirt,
hard) round out blacksmithing's difficulty curve alongside the
existing Longsword.

## [1.27.24] — Fix: party revival at the shrine now costs a flat 100

Praying for the whole fallen party at once was accidentally charging
100 gold *per* fallen member instead of one flat 100 gold total —
fixed on both the free-text prayer and the "Revive All" button.

## [1.27.23] — Real subclass choice for all 12 classes

Every class now has two genuine 5E subclasses to choose from (e.g.
Barbarian's Berserker/Totem Warrior, Fighter's Champion/Battle Master,
Rogue's Assassin/Thief), picked with "choose the path of X" — no
rebirth required. One real archetype per class grants a genuine +20%
weapon damage bonus; the other is a real, valid, sheet-showing pick
with no mechanical bonus wired up yet (honest about the gap, not
faked). Shows on your character sheet either way.

## [1.27.22] — Real subclass choice (Wizard pilot)

Wizards can now really specialize: "choose the school of evocation"
(or any real school of magic) picks a genuine Arcane Tradition,
dealing 20% more damage with spells of that school — grounded in the
real school every spell already carries. Shows on your sheet.
Available from your very first playthrough, no rebirth needed. Other
classes don't have a real subclass choice built yet (honest, not
faked) — more to come.

## [1.27.21] — Crafting splits into real named professions

Crafting is no longer one shared "crafting" skill — alchemy (potions,
antitoxin, scrolls), cooking, and a new blacksmithing (forge a real
longsword from iron ore) now each level up independently, the same
way herbalism/mining/fishing/lumberjacking already do. Your backpack
screen now shows a real tap-to-craft button for anything you actually
have the materials for.

## [1.27.20] — Some water in the world isn't just water

Added a real, undisclosed effect to certain water sources in the
world — the game will never tell you which ones, or confirm anything
happened either way. You'll have to find out for yourself.

## [1.27.19] — Pray at the shrine to revive your whole party at once

Praying at the shrine with more than one fallen party member and no
specific name now revives everyone in one offering instead of asking
which single person you meant — a real "Revive All" button was added
alongside the individual revive buttons too. Naming someone specific
still targets just them. Also added a real "Auto Equip Party" button
next to Leave Party on the party screen, for the same party-wide
auto-equip command added last release.

## [1.27.18] — Real damage resistances, sequential dungeon progression, and shrine improvements

A lot in this one:

- **Damage types & resistances are now real.** Weapons and spells carry
  a real damage type (physical, fire, cold, poison, radiant, and more),
  and racial resistances that were always described but never
  mechanically real — Dwarves resisting poison, Dragonborn and
  Tieflings resisting fire — now actually apply, halving that damage.
  Reaching deeper into rebirth also permanently earns back some of what
  a resistance would otherwise block, shown on your sheet as "magic
  penetration" — the more you've been reborn, the harder it is for
  anything's resistance to wall you out.
- **Certain deeper areas now require earning your way in.** Several of
  the wilder, deeper branches of the map (parts of the Whispering
  Wood, Greymoor Downs, the Sunken Root Caverns and what it connects
  to, and the First City) won't let you press on farther until you've
  actually fought through what's already there — no more strolling
  past real danger untested.
- **Shrine offerings go further.** Reviving a fallen party member at
  the shrine now also blesses the rest of the present party to full
  HP, and the offering itself is far cheaper than before. If nobody
  needs reviving, you can instead offer a cheaper blessing — a bit of
  holy water — to heal the whole party to full.
- Recruited companions now actually equip the gear they carry instead
  of fighting with their bare stats the whole time. "Auto equip the
  party" now gears up everyone at once, not just yourself or one
  named companion.
- Tent/Cabin/House prices are 100/1,000/10,000 gold now, a much
  steeper jump between tiers than before.
- Character sheets now show your racial resistances and magic
  penetration, when you have either.

## [1.27.17] — Fixed revival items/potions silently reviving the wrong character

Fixed a serious follow-on to the previous fix: reviving or healing a
party member who wasn't their owner's currently-active character (the
shrine, Tent, Cabin, House, or a plain healing potion) could report
success while actually updating a completely different, already-alive
character instead — the real target was never touched. Every one of
these now writes to the exact character being revived or healed, not
whichever character their owner happens to have active.

## [1.27.16] — Fixed a dead party member becoming permanently unrevivable after switching characters

Fixed a real bug: a player who died and then switched to play a
different character in the meantime (an intended option while dead)
made their fallen character invisible to every revival method —
praying at the shrine, items, everything — since it was no longer
their "active" character. Party lookups for reviving someone now
correctly include every real party member regardless of which
character their owner currently has active.

## [1.27.15] — Praying at the shrine is easier, plus new tap-to-revive buttons

Fixed: praying at the shrine without naming who in that exact same
message used to just ask you to name someone — it now automatically
figures out who you mean when there's only one fallen party member.
Also added real tap-to-revive buttons right at the shrine: looking
around there now shows a "Pray at the Shrine" button, and tapping it
lists anyone fallen with their own one-tap revive option.

## [1.27.14] — Fixed a fallen companion not being recognized by revival features

Fixed a real bug where a companion who died in a fight didn't actually
register as dead afterward — so no revival item, scroll, or the shrine
prayer could ever find and bring them back. Their death is now properly
recorded the moment they fall. Also broadened praying at the shrine to
recognize a lot more natural phrasing ("Pray to the shrine", "I pray at
the Hollow Stump Shrine") instead of requiring one exact wording.

## [1.27.13] — Fixed a rare hard-freeze when a solo fighter got paralyzed

Fixed a real bug where a solo player who got paralyzed mid-fight could
end up stuck forever, eventually forcing combat to abort with an ugly
"stuck in a loop" message instead of a real resolution. This now
resolves cleanly as a stalemate the moment it happens.

## [1.27.12] — Fixed "use the scroll..." style phrasing sometimes being misread

Fixed: using an item or scroll ("Use the scroll of X on Y", "Use my
potion") without starting the sentence with "I" could occasionally be
misread as an unrelated action. This phrasing is now recognized
reliably every time, and scrolls specifically are routed to the right
place so they're found and consumed correctly.

## [1.27.11] — Companions now stay with your party when you travel, and other party fixes

Fixed a real bug where a companion could get left behind whenever you
moved to a new location, even though they were genuinely part of your
formed party — the party could end up scattered across the map over
time. Absent party members' share of battle XP earned while away is
now 50% (up from 10%). Added a one-tap "Party Sheets" button to the
menu showing full HP/level/XP details for everyone in your party at
once.

New ways to bring a fallen party member back: a few new consumable
items now fully heal and revive, and there's a real place you can
visit to pray and leave an offering for a fallen companion instead.

## [1.27.10] — Fixed attacks sometimes hitting the wrong enemy in mixed fights

Naming a specific enemy in a fight with multiple similarly-named
opponents (e.g. two different goblin-type enemies at once) could
mistakenly target the wrong one, since the shorter name was checked
first. Now checks the most specific name match, so "attack the
goblin boss" reliably hits the boss instead of a regular goblin
standing nearby.

## [1.27.9] — Fixed combat rounds occasionally taking a very long time in multi-enemy fights

Fights against several enemies at once (especially ones with a
boss-tier enemy) could, under heavy load, take an unreasonably long
time to resolve a single round while narration caught up. Combat now
keeps its pace bounded even under heavy load — mechanics and outcomes
were never affected, only how long an over-loaded system could make
you wait to see the result.

## [1.27.8] — Attacks now name your weapon, and fixed a mid-combat healing bug

Combat messages now say which weapon an attack actually used (e.g.
"Sarah attacks Giant Spider with their Longsword") instead of just
"attacks", for both real players and companions. Also fixed: using a
healing potion on a party member mid-fight could report "healing 0 HP"
and do nothing, even when that ally was genuinely hurt — the potion
now heals off the ally's real, current combat HP.

## [1.27.7] — Fixed "still thinks I'm in combat" after successfully fleeing

Fixed: "Run from battle" (and similar phrasing) could get misread as
an ordinary comment instead of actually attempting to flee. Also
fixed: resting, switching characters, or deleting a character could
still say "you can't do that in the middle of combat" even after you
successfully fled a fight, if another party member was still in it --
these now only block someone who's actually still a real participant
in that fight, not just "a fight is happening somewhere in this chat."

## [1.27.6] — Reduced background load on shared narration capacity

Purely atmospheric background narration (the "world keeps living while
you're away" ambient line, and the hourly status update's flavor text)
now skips its own narration attempt entirely when the system is
already under heavy load, since nobody is actually waiting on either
one in real time. Frees up shared capacity for narration a real player
(or an active companion) is actually waiting on.

## [1.27.5] — Fixed solo combat drafting in unrelated companions, and talking to an NPC misrouting

Fixed: starting a fight while playing solo could pull in an unrelated
AI party member who simply happened to be standing at the same spot,
even though they were never actually part of your party. Combat allies
are now scoped to your real party, not just "whoever's nearby."

Fixed: talking to an NPC with a longer message could get misrouted as
messaging a party member instead, if the message happened to contain
the phrase "tell me" anywhere in it (a completely ordinary way to
phrase a question). Also hardened NPC name matching against a garbled
name extraction so a real NPC is found even when extra words get
attached to it.

## [1.27.4] — Two more real bugs caught by an internal end-to-end story run

Fixed: attacking a named boss-type monster at a location that also has
weaker versions of the same creature could start a fight against the
wrong (weaker) one instead, since the match wasn't preferring the more
specific name. Some story quests tied to defeating that exact monster
could never complete as a result.

Fixed: a bare "accept the quest" while standing at a real story
location could silently accept an unrelated recruited companion's own
personal errand instead, with no sign anything unexpected happened --
now the location you're actually standing at wins; naming a companion
quest by name still reaches it. Also renamed one quest that
accidentally shared its title with a different, later one in the same
storyline.

## [1.27.3] — Fixed a companion's personal quest being offered to the wrong player

Fixed: a player could accept a quest and get a completely unrelated
companion's personal storyline instead -- one that was never actually
in their own party at all. A companion's own quest was being matched
against every active companion in the whole game rather than just the
accepting player's real party, so someone else's recruited companion
could win out. Now scoped correctly to the player's own party.

## [1.27.2] — Fixed "recruit X into my party" failing to find a real companion

Fixed (caught by an internal end-to-end simulation): "I recruit Sarah
into my party" said a real, recruitable companion couldn't be found,
because the name-extraction only handled "recruit X TO my party," not
"INTO." Now recognizes both.

## [1.27.1] — "Look around" gets Shop and Quest Board buttons

Per Coffee: if a place is visibly there when you look around, you
should be able to tap it. Looking around now shows a real Shop button
when the location has one, and a Quest Board button when there's an
unclaimed bounty posted, stacked above the existing travel buttons.

## [1.27.0] — Player marketplace gets real buttons and clearer instructions

Per Coffee: the player marketplace (list items for other players to
buy) was slash-command-only with no buttons at all, unlike the NPC
shop. Checking the market now shows a real Buy button per listing
(tap to purchase, same as the NPC shop already works), and both the
empty and populated market views now clearly explain how to list
something for sale yourself.

## [1.26.7] — Fixed "Shop" and "Look at shop" not opening the shop

Fixed: typing just "Shop" got zero reply, and "Look at shop" only
showed the shopkeeper's portrait instead of what's actually for sale.
Both now open the real shop listing, same as "what's for sale" already did.

## [1.26.6] — Fixed joining a guild by name getting no reply at all

Fixed: saying "join Adventurers' Guild" (or a typo like "I wan to
join...") got zero reply. The natural-language fallback only
recognized "join THE X" or "I want to join X" phrasing, missing the
equally natural bare "join X". Now recognizes any real guild mention
alongside the word "join" in any form.

## [1.26.5] — Fixed replies silently dropping during busy combat

Fixed: players reported not being able to do anything (open the menu,
use waypoints) while an unrelated fight was happening elsewhere in the
same chat. The actual cause wasn't a combat gate -- neither the menu
nor waypoints have one -- it was message volume: heavy send traffic
from an active fight tripped Telegram's own flood control, and the
retry logic gave up after one quick retry, silently dropping several
real replies in the same short burst. Now retries more persistently
and, when Telegram says "try again in N seconds," actually waits that
long instead of guessing.

## [1.26.4] — Fixed location/monster images failing silently, and AI
companions not returning to accept a quest they'd wandered away from

Fixed: location and monster images could fail to send during
background AI-party turns and the hourly world update, due to a
missing method on an internal helper (same shape as the earlier
send_audio fix). These were already caught gracefully, so nothing
crashed, but the images were being silently skipped.

Fixed: an autonomous AI party member could wander away from a quest
board before ever accepting what was offered there, with nothing in
its own situational awareness ever pointing back to it -- it now
notices an unaccepted quest on offer elsewhere and can choose to head
back for it.

## [1.26.3] — Fixed a real fast-travel blocker

Fixed: fast-travel was being blocked for EVERYONE in the game whenever
ANY combat was happening anywhere, even to players who weren't
involved at all. Now only blocks you if you're actually the one
fighting.

## [1.26.2] — Push-button title picker

Setting your title now has real tap-to-choose buttons when you check
your achievements, instead of needing to type the exact title text.

## [1.26.1] — Two more real fixes from active playtesting

- Fixed the deeper cause of the AI party's stall: accepting a quest
  that's actually on offer is now the AI's single highest priority,
  above everything else — it was previously just one option among
  many and the AI kept choosing safer-looking flavor actions instead,
  for days.
- Fixed: "use dragon breath" (a Dragonborn's own racial ability) was
  being misclassified and given the wrong response entirely. Now
  recognized directly.

## [1.26.0] — Autonomous AI party actually progresses now, clearer map

- Fixed: the autonomous AI party was stalling on repetitive flavor
  actions instead of progressing quests, because it was never actually
  told where its own active quests needed it to go. It now knows its
  real quest destinations and prioritizes heading there over
  everything else.
- The map's own notation (current location, visited places,
  connections, unexplored paths) is now explained with a short legend,
  instead of just being used with no explanation.

## [1.25.2] — Three more real fixes from live monitoring

- Fixed: interacting with an object whose name ends in a location
  descriptor (like "the corkboard by the door") now matches correctly
  on the word that actually names the object, instead of only ever
  checking the last word of its name — another real silent-reply cause.
- Fixed: the skill tree screen now clearly says how many more points
  you need when you can't afford an upgrade yet, instead of just
  showing the cost with no explanation for why there's nothing to tap.
- Fixed a real background-loop crash: the autonomous AI party and
  hourly update loop could silently abort mid-turn if voice narration
  was enabled, due to a missing method on an internal helper. Also
  hardened that same narration path so any future unexpected hiccup
  there can never abort a real game turn again.

## [1.25.1] — Fixed a real silent-reply bug

Fixed: an action naming something real (like carving your initials
into a real object you'd already looked at) that the game couldn't
parse into a specific mechanic used to get silently dropped with no
reply at all — it now falls back to examining the thing instead, so
you always get SOME response when you've named something real.
Ordinary chat between players stays exactly as quiet as before.

## [1.25.0] — Real images for NPCs, monsters, and items

Completing the full "images for the whole game" request: real
generated images now show for NPCs (when you talk to them), monsters
(when a fight starts), and equipped items (when you equip something)
— same consistent image every time for a given NPC/monster/item, same
as locations.

## [1.24.1] — Location images now show every visit

Per player feedback, location images now show every time you visit a
place, not just the first time — still the same consistent image for
a given location every time, just no longer limited to first
discovery.

## [1.24.0] — Real images for locations

The first time you ever visit a place, it now comes with a real
generated image alongside the description — everyone who visits the
same place sees the same consistent depiction, not a different random
image each time. First of several planned image expansions (NPC
portraits, monster art, and item icons are next).

## [1.23.0] — Voice message input, and a second real narration voice

- New: send a voice message in Adventure and it's transcribed (via a
  free cloud speech-to-text service) and played through the exact
  same real game pipeline as typing it — no separate "voice mode,"
  just another way to say what you want to do. The bot echoes back
  what it heard first, since transcription isn't perfect.
- TTS narration (when enabled) now uses two distinct voices instead of
  one — a narrator voice for general narration, and a different voice
  for NPC dialogue lines specifically, so a conversation actually
  sounds like two different speakers.
- TTS no longer reads menu/informational screens aloud — character
  sheet, inventory, quests, shop, market, achievements, bestiary,
  weather, leaderboard, and every other menu stay silent. Only real
  narration and dialogue get voiced.

## [1.22.1] — Push-button menus for rebirth and hybrid classes

Rebirth and hybrid-class picking now have real push-button menu
access too, not just natural language. A Rebirth button appears in the
main menu once you hit level 99, and a Hybrid Class button appears
once you've rebirthed at least once.

## [1.22.0] — Rebirth and hybrid classes

- New: at level 99, say "rebirth" to reset your level and XP back to
  1 — but you keep every stat, item, and point you've earned. Each
  rebirth permanently raises your ability-score cap above the normal
  20, and speeds up all future leveling, so the climb back is
  genuinely faster every time.
- New: after your first rebirth, freely pick any other class as a
  hybrid flavor ("become a hybrid Wizard") for a real, chance-based
  taste of that class's signature ability — a bonus that deepens
  further with each additional rebirth, up to a 3rd tier.

## [1.21.2] — Clearer feedback on button taps that aren't yours

Tapping a button meant for someone else — another player's character-
switch or character-creation button — now gives a clear message
instead of silently doing nothing. Nothing was ever at risk of being
changed by the wrong player; this just makes it obvious when a tap
doesn't apply to you.

## [1.21.1] — Two more natural-language recognition fixes

- Talking to something that isn't a real NPC (a tree, a statue, a
  waystone) used to get silently dropped with no reply at all — it now
  falls back to examining the thing instead.
- Guild-join phrasing now tolerates apostrophe differences (a phone
  keyboard's curly quote) and small spelling typos, instead of only
  matching an exact name.

## [1.21.0] — Weather and night now matter in a fight

- Checking the weather now shows a real 3-day forecast for the region,
  not just today's conditions.
- Steady rain and rolling thunderstorms now make footing genuinely
  slippery — a real chance of disadvantage on attacks for anyone
  fighting out in it.
- Hostile monsters are more dangerous after dark — real advantage on
  their attacks at Night, never affecting players or party companions.
- Both effects are read straight off the same deterministic weather
  and clock system `/weather` already used, so the forecast and the
  in-combat effects are always in sync — never invented independently.

## [1.20.1] — Shops now have real sections

Every shop now opens with a real flavor description of the stall
itself, then lists its stock grouped into clear labeled sections —
Weapons, Armor & Shields, Tools, Consumables, Scrolls, Magic Items,
Maps — instead of one flat list.

## [1.20.0] — Level cap raised to 99

- Characters can now level all the way to 99, a real house extension
  since 5E itself stops at 20 — HP, skill points, and Ability Score
  Improvements (still capped at 20 per stat, same as before) all keep
  growing the whole way, so dedicated players have a genuine long-term
  goal in maxing out every stat.
- Proficiency bonus intentionally stays capped at +6 past level 20 —
  letting it keep climbing would eventually make this game's fixed
  DC-13 skill checks a guaranteed success, which isn't the goal here.
- Fix: the procedural magic-item generator (used for rare/legendary
  loot) never tagged weapon/armor category on what it rolled, so a
  generated item could silently fall back to the wrong proficiency
  category. Fixed, and it can generate magic shields now too.

## [1.19.0] — Weapon/armor proficiency, a visual map, and dev-topic fixes

- **Real weapon and armor proficiency.** Every class now has a genuine
  proficiency list — using a weapon or wearing armor/a shield outside
  it has a real mechanical cost (no proficiency bonus on the attack
  roll, or disadvantage), instead of no consequence at all.
- **`/visual_map`** generates a real map image grounded in exactly the
  places you've actually explored — fog of war applies here too.
- **Fix:** `/msg` failed with a confusing error when messaging a real
  human player by @username — it only ever recognized AI party
  members. Now works for both.
- **New:** a Shovel tool boosts bait-gathering quantity via a dice
  roll, the same way Shears already do for herbalism.
- Joining an in-progress battle and unlocking a skill-tree upgrade now
  post to the Main topic too.

## [1.18.1] — Fix: "replay intro" as plain text

`/replay_intro` worked as a slash command but typing "replay intro" in
plain English silently did nothing — fixed, both now work identically.

## [1.18.0] — Join battles mid-fight, a much bigger menu, and tap-to-travel

- **Join an in-progress fight.** If a battle breaks out somewhere and
  you're elsewhere, you're never blocked from gathering, moving, or
  anything else in the meantime — and if you want in, just travel to
  where it's happening and join.
- **Story So Far now has a chapter menu** — tap any chapter you've
  actually reached to rewatch its opening cutscene, right from that
  screen.
- **A much bigger main menu**: Party, Achievements, Market, Bestiary,
  Weather, and Leaderboard are all one tap away now, alongside
  everything already there.
- **Real party management buttons** — invite, accept, and leave your
  party with a tap instead of typing, right from the new Party screen.
- **Tap-to-travel on "look around."** Every place you can already see
  from where you're standing now shows up as a real button — no more
  typing the name to walk over.

## [1.17.0] — PvP, a player marketplace, the Colosseum, real portraits, and more secrets

- **PvP**: `/duel <name>` challenges a real player at your location — they
  have to `/accept_duel` before anything happens, and it never works in
  a safe location. A real fight, same rules as any other.
- **Player marketplace**: `/sell_market <qty> <price> <item>` lists
  something for sale at a fixed price (never an auction), `/market`
  browses current listings, `/buy_market <#>` buys one outright.
- **The Colosseum**: a new location off the Crossroads Tavern with a
  real, repeatable challenger waiting inside — come back as often as
  you like.
- **Character portraits**: every new character now gets a real,
  generated portrait image alongside their creation summary.
- A few more real secrets exist in the world now — deliberately not
  detailed here.

## [1.16.1] — Skill tree: the remaining six classes

- Barbarian, Paladin, Wizard, Monk, Ranger, and Druid now each have a
  real, class-flavored skill-tree upgrade too, same as the first six —
  all 12 classes are covered now. Tap "Skill Tree" in the menu or use
  `/skilltree` to see yours.

## [1.16.0] — A real skill tree

- Every level gained now banks a real, spendable Skill Point (see it on
  your sheet or with `/skilltree`).
- Fighter, Rogue, Warlock, Sorcerer, Cleric, and Bard each have a real,
  class-flavored upgrade to unlock — each one meaningfully strengthens
  that class's signature ability, not just a flat stat bump. Tap
  "Skill Tree" in the menu, or use `/skilltree`, to spend your points.
- The remaining six classes are getting their own upgrades next, using
  this same system.

## [1.15.1] — /msg for AI party members, clearer multi-attacks, and a fix

- **`/msg <name> <message>`** lets you tell an AI-controlled party member
  something — in or out of combat — without it ever counting as
  anyone's turn. Telling a companion to retreat mid-fight now actually
  makes them attempt a real escape on their own next turn instead of
  just attacking again.
- **Multi-attacks are called out clearly now.** If you (or an AI party
  member) have more than one attack this turn — Extra Attack, Action
  Surge, Flurry of Blows — a plain "N attacks this turn!" line appears
  before the sequence starts.
- **Fix:** Druid's Wild Shape (a real, working command for a while now)
  was never actually listed on the character sheet — it is now.

## [1.15.0] — Alignment, dice mini-games, and a more sensory world

- **Alignment.** Every character now has a real 9-box alignment
  (Lawful/Neutral/Chaotic x Good/Neutral/Evil), shown on the character
  sheet. Set yours with `/alignment` (e.g. "/alignment chaotic good"),
  or let it emerge on its own — helping or hurting a faction now
  genuinely nudges you toward or away from Good/Evil based on that
  faction's own real leanings.
- **Dice mini-games.** `/dice_game` plays your class's own themed die
  (Wizards and Sorcerers draw a d4, Rogues roll a d6, and so on up
  through a d12) for a small XP reward, free to play and levelable —
  and `/fortune` spins a universal d100 Fortune's Wheel open to every
  class.
- **A more sensory world.** Looking around near the Crossroads Tavern
  now describes what's actually visible toward the Whispering Wood and
  Stonearch Bridge — real sight lines, not just a list of names — the
  first pass of a wider pass still to come.

## [1.14.1] — Dev-topic feedback batch

- Character sheet now shows XP remaining to next level right on the
  Level/XP line, not just in the level-up menu.
- New `/replay_intro` command rewatches your current chapter's opening
  cutscene, in case you missed it (e.g. right at character creation).
- Main menu's Waypoints button lets you tap any place you've already
  discovered to fast-travel there, instead of typing the name.
- Tapping an item in a shop now asks how many (1/5/10) via buttons
  instead of always buying one.
- AI-controlled party members will no longer start a fight against a
  story boss entirely on their own — a real human has to be there too.

## [1.14.0] — Story cutscenes, roster menu, and real secrets to find

- **Story cutscenes.** Accepting the very first quest of a new chapter
  now opens with a real AI-narrated cutscene, a bookend to the existing
  "chapter complete" closing beat — grounded strictly in that chapter's
  own real title and description, never inventing new plot.
- **Menu upgrades.** The main `/menu` now has a "Switch Character" row
  that opens your full roster with tap-to-switch buttons, and that
  roster screen now also has a "Create New Character" button, so
  starting an additional character no longer requires typing.
- **Real secrets exist in the world now** — deliberately not detailed
  here. Some things are worth finding on your own.

## [1.13.3] — Real tap-buttons for character creation

Per Coffee: "Add push buttons to the character creation sequence and
anywhere else users must make choices... The players really like those
push buttons." Race, class, dice-preference (physical dice yes/no),
pronouns (He/Him, She/Her, They/Them, Skip), and the auto-assign
ability-score shortcut now all show real tap buttons alongside their
prompts -- free text still works exactly as before for every one of
them (typing a race/class name, "yes"/"no", etc.), and a stale tap from
a step you've already moved past is a harmless no-op. Both paths
(button and text) drive the exact same underlying creation logic, so
there's no divergent behavior between them.

## [1.13.2] — Two real shop-browsing gaps fixed

- Live-caught (Coffee, dev-topic screenshot, 2026-07-19): "Look at
  wares available for purchase" matched the bare "buy"/"purchase"
  keyword trigger before ever reaching the real shop-listing action,
  giving an unhelpful "not sure what item you mean" instead of actually
  showing the shop. Fixed with a browse-only check before that trigger
  (same shape as the existing "where can i buy" precedent) plus adding
  "wares" itself, which was simply never in the trigger list.
- Dev-topic feedback (Coffee): "If a player wants to look at stalls and
  shopfronts in market row - please give a description and then open
  the shop." Examining a generic collective ("the stalls", "the
  shopfronts") at a location with a real shop now gives a short flavor
  line and then opens it -- examining the specific abandoned/shuttered
  stall by name still just describes it, since that one's deliberately
  not the working shop.

## [1.13.1] — Menu polish: Level Up only when it matters, tap-to-switch characters

Dev-topic feedback (Coffee):
- The main menu's "Level Up" button now only appears when the
  character actually has ability-score points pending -- "otherwise it
  serves no purpose." The rest of the menu is unchanged.
- The level menu now shows real XP remaining to the next level (or
  "Max level reached" at 20) under the level/XP line.
- Switching characters now has a real tap-to-switch button menu
  ("Your characters", one button per character, marking the currently
  active one) alongside the existing free-text "switch to <name>" --
  both paths dispatch through the same real switch_character logic and
  the same mid-combat lock.

## [1.13.0] — Deeper world + narrated "cutscene" moments for every companion's story

Per Coffee's direction (2026-07-20): the map goes further still (5 new
frontier locations extending the Wood, the Downs, the gorge, the
underground cave chain, and the buried city — world now 40 locations,
up from 16 at the start of this session), each tied narratively to the
same thread: something singular and ancient connects what's underneath
all of them.

The bigger addition: every one of the 6 recruitable companions'
personal-quest **resolutions** (Sarah, Borin, Wren, Pip, Grask, Vesh)
now gets the same longer, weighted narrated "cutscene" moment the
game's 4 story-arc climaxes already got (task #192) — reusing that
exact existing mechanism (any quest tagged `"weight": "climactic"`),
so no new code was needed, just marking these real, existing
personal-story payoffs as the story beats they already were.

## [1.12.1] — Fixed "Switch to my character X" never matching

Live-caught (Coffee, 2026-07-19, dev-topic screenshot): "Switch to my
character Elduinn" replied "Couldn't find one of your characters by
that name" while listing "Elduinn" as a valid character in the very
same message. Root cause: the keyword-fallback trigger for switching
characters only strips the literal "switch to " prefix, leaving "my
character elduinn" as the extracted target -- longer than the real
name, so the name-matching check (which only ever looks for the
fragment being equal to, or contained within, the real name -- never
the reverse) never matched. Fixed in two places: more specific trigger
phrases ("switch to my character ", "switch my character to ") are now
checked first in `ai/intent_parser.py` so the filler words are stripped
along with the generic prefix, and `_find_own_character_by_name_
fragment` in `bot.py` now also strips a leading "my character "/
"character " itself as defense in depth for any other caller. Two new
permanent tests added and passing.

## [1.12.0] — World expansion: compass navigation, 19 new areas, real puzzles & treasure

Per Coffee's direction (2026-07-19/20): the world's large outdoor/
underground areas are now much bigger and more explorable, navigated by
a real compass system alongside the existing free-text movement.

- **New "directions" field** on locations (compass word -> location
  id), purely additive alongside the existing "connections" reachability
  list -- every pre-existing location behaves identically to before;
  new areas add real N/S/E/W navigation ("go north", "head south").
  `_do_look` now labels known exits with their compass direction when
  available.
- **19 new locations** (world grows from 16 to 35), expanding Whispering
  Wood, Greymoor Downs, Stonearch Bridge, the underground cave chain
  (Sunken Root Caverns/Goblin Warrens/Glimmerdeep Grotto), and The First
  City into real multi-node areas spanning both the surface and
  underground layers -- while leaving small interior locations (the
  tavern, market row, the shrine) untouched, and leaving the game's two
  climactic boss arenas (The Hush Below, The Unmoored Isle) as focused
  single-room fights rather than diluting them. Every existing quest,
  NPC, and monster placement is unchanged -- purely additive, no existing
  location id, connection, or quest target was renamed or moved.
- **Real secrets to find**: several new lockpickable treasure chests, a
  hidden shortcut door connecting two of the new areas, and two brand
  new riddle puzzles (reusing the same solve-a-riddle mechanic as Old
  Maren's existing strongbox) -- deliberately not detailed here; find
  them in play.
- New gathering opportunities across the expanded map, using the
  existing herbalism/mining/lumberjacking/fishing/bait-gathering skills.
- Verified with a real, complete playthrough test walking a character
  through all four story arcs end-to-end (including the companion-trust
  story gate blocking, then correctly opening once a trusted companion
  and a defeated boss were both real) -- the full story critical path
  is confirmed completable beginning to end on top of the bigger map.

Bundled in the same release (all independently tested):
- Fast-travel now honors `requires_item` and `locked_connections` gates,
  matching on-foot movement -- previously only `min_level` and
  `story_gates` were checked, so a required item or a locked shortcut
  could be silently bypassed by fast-traveling instead of walking.
- Fishing now loses its bait by a real 50/50 chance on every attempt
  (catch or miss alike), not for free forever -- and bait itself can now
  be found in the wild (mud, fungus, rotted wood, loose stones) at a few
  discoverable spots, not only bought.
- "Create character" (no article) and "Create a second character"
  (an extra word breaking the old phrase match) now correctly start
  character creation instead of silently falling through to chat.
- A companion NPC's display name (now "Sarah") is fully consistent
  across code comments, docstrings, and tests -- no game-visible change,
  her in-game name was already correct.
- Fixed a same-session regression: the bait-loss roll change above had
  accidentally nested the board-quest-progress-crediting block inside
  the wrong `if`, so gather-type board quests (except fishing ones)
  stopped crediting progress entirely. Caught by the full regression
  suite before shipping and fixed immediately.

## [1.11.68] — Test-only cleanup: 4 stale permanent tests (no game behavior change)

Running the full FastRegressionTests suite end-to-end (started while
validating v1.11.63, finished ~90 minutes later) surfaced 4 stale
tests, all pre-existing technical debt from earlier feature work this
session, none connected to anything shipped tonight:

- `test_ranger_danger_sense_grants_advantage_on_dex_saves_from_level_2`
  imported a nonexistent `_ranger_danger_sense_advantage` (leading
  underscore) -- the real function is the public
  `ranger_danger_sense_advantage` (no underscore, since bot.py's
  `_do_flee` calls it from outside spells.py).
- `test_the_hush_quest_requires_defeating_the_unspoken` accepted the
  old flat `"the_hush"` quest id, from before Full-storyline Phase 2
  split it into a 3-stage chain earlier this session. Updated to the
  real final-stage id, `the_hush_stage3_the_unspoken`.
- `test_skill_check_prompts_for_manual_roll_and_resumes_with_it`
  checked for capitalized "Roll a d20", but the real prompt wording
  (only the character's own name is capitalized) is lowercase "roll a
  d20".
- `test_support_model_unreachable_fallback_is_specific_not_generic_onboarding`
  checked for older fallback wording ("busy"/"try asking again") that
  task #178 already polished into the current, more specific message
  ("genuinely overloaded"/"ask again, or ping an admin to run /redo").

`test_arcane_circle_join_grants_a_real_scroll` (already fixed in
v1.11.64) and `test_ai_companions_never_learn_monsters_for_the_human`
(confirmed passing in isolation, a shared-DB test-ordering flake in
the full run) needed no further changes. All 5 pass together now. No
game code (bot.py/items.py/ai/*.py/campaigns/*.json) touched -- test
file only, no redeploy needed.

## [1.11.67] — Real "Maren's strongbox" object added + head-noun matching fixed

Live-caught the same session: the same player repeatedly tried to
examine "the strongbox" -- a real quest (`marens_locked_ledger`)
already describes it and its carved riddle in detail, but no actual
interactable object for it ever existed at `market_row`, so every
attempt hit the graceful-but-empty "doesn't spot anything" fallback.
Added `marens_strongbox` as a real interactable, reusing the quest's
own already-written description and riddle text verbatim (no new lore
invented).

That surfaced one more real bug while verifying it: "Old Maren's
locked strongbox" has 3 significant words once "old" is excluded, so
a natural phrasing that only says "strongbox" fell short of the plain
majority-of-words threshold in `_find_interactable`'s word-overlap
fallback, even though "strongbox" IS the object's real head noun --
the exact same head-noun-vs-modifier distinction already fixed in
`items.find_item_mentioned_in_text` for the fishing-gear bug earlier
tonight, mirrored here: a lone head-noun match is now checked first
and treated as confident on its own.

Verified via the full 35-interactable self-match sweep (zero
regressions, including re-confirming the brass-scale and boarded-up-
stall fixes from earlier tonight still hold together) plus 2 new
permanent regression tests.

## [1.11.66] — "a boarded up stall" silently failed to match (hyphen vs. space)

Same session, same live player, same root shape as v1.11.65's fix:
"Look closer at a boarded up stall" (the natural way to type it) never
matched the stored "a boarded-up stall" -- the hyphen genuinely joined
"boarded" and "up" into one unsplittable token in `_find_interactable`'s
word-overlap fallback, so a plain space-separated phrasing could never
reach it. Fixed by splitting on hyphens the same as whitespace before
building the significant-word list, so "boarded-up" now contributes
"boarded" to the overlap count exactly as if the stored name used a
space -- the same fix direction as the existing "wide-boled tree"
punctuation fix, just the opposite character. Verified via the full
34-interactable self-match sweep (zero regressions) plus a new
permanent regression test.

## [1.11.65] — "Look at Maren's brass scale" silently failed to match

Live-caught via a real player's own screenshot ("Why isn't this
working?"): examining a real, described interactable
("Old Maren's brass scale") kept getting "doesn't spot anything like
that here" -- even though that exact object was named in the same
reply's own hint list. Two compounding causes in `_find_interactable`:

- A phone keyboard's smart-quote autocorrect sends a curly apostrophe
  (’) which never equals the straight one (') this game's stored names
  use, so "maren's" from the player's own message could never match
  "Maren's" in the stored name at all -- same single-character-
  collision shape as the existing armour/armor and fishing rod/pole
  fixes, just a punctuation mark this time. Normalized the same way.
- "old" wasn't excluded from the word-overlap fallback's stopword list
  (the NPC-name matcher already excludes it as a filler word for the
  same NPC's name, but this separate function never did) -- a 4-word
  name like "Old Maren's brass scale" needed 3 of 4 words to clear the
  match threshold, and with "maren's" silently failing (the apostrophe
  bug above) AND "old" often left out of a natural shorter phrasing,
  legitimate requests fell short by one word.

Verified via a full 34-interactable self-match sweep across the whole
game (zero regressions) plus a new permanent regression test covering
all 4 real phrasings a player tried live.

## [1.11.64] — Test-only fix: stale guild-join test fixture (no game behavior change)

Running the full regression suite while validating v1.11.63 surfaced
`test_arcane_circle_join_grants_a_real_scroll` failing. Root cause:
task #170 (guild membership requiring proof in real combat, shipped
earlier) added a real `proven_in_combat` requirement to
`eligible_for_guild`, but this pre-existing permanent test's fixture
character was never updated to set that flag, so the join now
correctly gets rejected and the test's own assertion (expecting a
granted scroll) fails. The actual live game behavior was already
correct the whole time -- this is a test-only fix (added
`proven_in_combat=1` to the fixture), not a game code change. No
bot.py/items.py/ai/intent_parser.py touched, no redeploy needed.

## [1.11.63] — "Look at [NPC]'s [object]" wrongly routed to NPC chat instead of examine

Live-caught: "Look at old maren's brass scale" -- a real, described
interactable at her location -- got hijacked to talk_npc purely
because "maren" appears in the text, even though the message clearly
uses an explicit examine verb targeting an OBJECT of hers, not
addressing her directly. The 2026-07-14 filler-word fix (excluding
"the"/"old"/etc. from counting as identifying NPC-name words) solved
a different flavor of this same root problem but not this one, since
"maren" is a genuine, real name-word.

Fixed with a narrow guard on the NPC-name match: when the message also
contains an explicit examine-style verb ("look at", "examine",
"inspect", "read", "touch", "peer at", etc. -- the same verb set the
existing examine detection further down already recognizes), the
NPC-name hijack is skipped so the message falls through to real
examine handling instead, which looks the target up against the
location's actual interactables. Also closed a smaller gap while here:
bare "look at X" (no "the"/"a"/"an") wasn't in the examine trigger
list at all, only the articled forms were.

Verified via a 12-case matrix (the live scale case, several NPC-chat
cases that must keep working, recruit_npc, the shop-browse phrasing,
whole-area "look", and every previously-fixed examine-verb case) plus
the existing permanent regression test for NPC-name filler words --
all pass with zero regressions.

## [1.11.62] — URGENT: fix a regression from v1.11.55 that broke selling a Fishing Pole

Caught live within the hour: v1.11.55's fix for "Buy fishing hooks"
(a required_for-based ambiguity check between Fishing Pole and Bait)
had an unintended side effect -- "Sell 1x fishing rod" and even a
follow-up "Sell raw fish" started failing for the same player, because
the required_for check fired unconditionally per item, manufacturing a
false ambiguity even when Fishing Pole already matched confidently.

Real fix, in `items.find_item_mentioned_in_text`:
- "fishing rod" is now normalized to "fishing pole" up front, the same
  single-word real-collision treatment already used for armour/armor
  -- "rod" is the everyday word for this exact tool and no other item
  in this game uses "rod" at all.
- The word-overlap fallback now distinguishes a match on an item's
  HEAD noun (e.g. "pole" in "Fishing Pole", "potion" in "Healing
  Potion") -- a strong, confident match that wins outright -- from a
  match on any other word (a modifier, like the bare "fishing" in
  "fishing hooks") -- a weak match that still goes through the
  required_for ambiguity check, exactly as intended.

Verified via a new permanent regression test covering all 5 real
cases (buy fishing hooks / buy bait / sell fishing rod / sell fishing
pole / sell bait) plus a full self-match sweep across every item in
the game (zero regressions) and the pre-existing axe/potions/armour
cases.

## [1.11.61] — "Browse shops" silently fell through to chat

Live-caught while monitoring a real player's session: "Browse shops"
(right after arriving at Market Row) got silently classified as chat
-- the `list_shop` keyword-fallback trigger list already covered
"browse the shop" but not the bare plural "browse shops" or "browse
shop" (no "the"), so it fell all the way past the fast keyword path
to the AI model, which guessed chat instead. Added "browse shop",
"browse shops", and "browse the shops" to the trigger list. Verified
via a direct case matrix with zero regressions on the existing
list_shop phrasings plus a handful of unrelated actions (check_quests,
buy, sell).

## [1.11.60] — Rogue gets Cunning Action (task #91, reachable right now)

Continuing the task #91 class-features audit, but prioritizing what's
actually reachable over speculative level 5+ content: a real level 2
Rogue is already playing (Laurienna), so Cunning Action (real 5E,
level 2+: Disengage/Dash/Hide as a bonus action) is live-relevant
today, unlike most of this game's remaining gaps.

Translated as the one piece with an exact, already-existing hook:
Disengage means fleeing a fight doesn't provoke the opportunity
attacks every other class's flee attempt does (see 2026-07-13's
opportunity-attack block in `_do_flee`) -- a level 2+ Rogue's flee now
skips that block entirely and gets a real acknowledgment line instead
of silently mattering nothing. Dash/Hide have no clean translation
given this engine's lack of a positioning/movement system, so this is
scoped to the one piece that does. Verified via 2 real tests: a level
2 Rogue's flee shows no opportunity attacks, a level 1 Rogue's still
does.

## [1.11.59] — Druid gets Wild Shape (task #91: Druid had ZERO class features)

Auditing task #91 (class features level 2-10+ for all 12 classes)
found Druid was the one class with NOTHING here at all beyond ordinary
spellcasting -- every other class had at least one real mechanical
feature already (Fighter's Second Wind/Action Surge, Barbarian's Rage,
Cleric's Channel Divinity, Wizard's Arcane Recovery, Monk's Flurry of
Blows, Paladin's Divine Smite, Sorcerer's Metamagic, Warlock's Pact
Magic, Rogue's Sneak Attack/Uncanny Dodge, Ranger's Natural Explorer,
Bard's Jack of All Trades/Expertise/Song of Rest).

Wild Shape (level 2+, 2 uses per rest, same convention as Rage): a new
`wild_shaped` flag on the live combat participant dict grants bonus
claw/bite damage (`wild_shape_damage_bonus`, scaled by level exactly
like Rage's own bonus) and real temporary HP (`wild_shape_temp_hp`,
the same `temp_hp` mechanic Dark One's Blessing already uses) --
simplified from real 5E's actual separate beast statblock, since this
engine has no such system for ANY class (Rage doesn't reroll as a bear
either). The real tradeoff carries over honestly: a Wild Shaped Druid
can't cast spells until they shift back, enforced in `_do_cast_spell`
the same way the existing `silenced` condition already blocks casting.
Recognized via natural language ("I wild shape", "shift into a beast",
etc.) same as every other class ability. Verified via 2 real tests
(one confirming the damage-bonus level scaling matches Rage's own
curve, one driving the actual `_do_wild_shape`/`resolve_attack`/
`_do_cast_spell` handlers end-to-end through a real combat session).

## [1.11.58] — Wren's and Pip's resolutions ship, completing Full-storyline Phase 3

The last two companion resolution quests: Wren's "Roots Worth
Trusting" (offered once "What the Roots Are Hiding" is done, triggered
by returning to the Hollow Stump Shrine -- whether she finally tells
the party what the shrine's roots are really drawing on), and Pip's
"The Story He's Already In" (offered once "A Story Worth Telling" is
done, triggered by returning to Stonearch Bridge -- whether he
realizes the party's story is the one he's been stalling three days
for). Both banded by real `npc_relationships.affinity` the same way
as Grask/Vesh/Sera/Borin. Verified via a combined real throwaway
fixture test driving `bot._complete_quest_and_announce` for both
companions across all 3 trust bands each (6 runs total).

This completes the full-storyline plan's Phase 3 -- all 6 recruitable
companions (Grask, Vesh, Sera, Borin, Wren, Pip) now have a real,
trust-driven resolution arc, each read off actual player behavior
stored in the database, never invented by AI narration. Phase 4 (a
one-off live quest-progress reset for real players, so everyone
experiences this new content fresh) stays deliberately deferred until
scope is confirmed -- not part of this release.

## [1.11.57] — Borin's companion arc gets a real trust-banded resolution

Continuing the full-storyline plan's Phase 3: Borin now has a second
quest, "The Watch He Keeps" (`borins_resolution`), offered once his
existing vouching quest is done, triggered by returning to Market Row
-- the exact spot he's been doing penance on this whole time. Whether
he decides his penance is actually paid depends on the real trust
he's built with the party.

`QUEST_COMPANION_RESOLUTIONS` now also covers `borins_resolution` ->
`borin_ironjaw`, same real-affinity banding as Grask/Vesh/Sera: low
(<=-20, resolved_estranged, back to standing his post), mid (-19..39,
resolved_distant, grudgingly vouches but keeps watching anyway), high
(>=40, resolved_loyal, finally steps off the street for good). Verified
via a real throwaway fixture test driving `bot._complete_quest_and_
announce` through all 3 bands end-to-end.

## [1.11.56] — Sera's companion arc gets a real trust-banded resolution

Continuing the full-storyline plan's Phase 3: Sera (Sarah) now has a
second quest, "Worth Traveling With" (`seras_resolution`), offered
once her existing personal quest ("Sarah's Safer Crossing") is done,
triggered by returning to the Stonearch Bridge -- the exact spot she
first sized up the party at, matching her ranger's "still deciding
whether this is the group worth staying for" arc.

`QUEST_COMPANION_RESOLUTIONS` (Grask/Vesh's pattern) now also covers
`seras_resolution` -> `sera_wanderer`, banded by the same real
`npc_relationships.affinity` thresholds: low (<=-20, resolved_estranged,
still eyeing the road out of town), mid (-19..39, resolved_distant,
sticks around but noncommittal), high (>=40, resolved_loyal, stops
watching the horizon and commits to the party). Verified via a real
throwaway fixture test driving `bot._complete_quest_and_announce`
through all 3 bands end-to-end, confirming both the written
`resolution` state and a real narration note land correctly for each.

## [1.11.55] — "Buy fishing hooks" silently bought the wrong item

Live-caught while monitoring a real player's session: "Buy fishing
hooks" (not a real item -- this game only sells a Fishing Pole and
Bait) got silently resolved to a purchase of the Fishing Pole and
charged for it, with no indication anything was off. Root cause:
`items.find_item_mentioned_in_text`'s generic category-word fallback
(added for "buy an axe"/"buy two potions" -- task #111) matches on ANY
single overlapping word from an item's name, and "fishing" is a
name-word of "Fishing Pole" -- the fallback confidently treated that
lone modifier as a full match even though the sentence's other real
noun ("hooks") didn't match anything at all.

Fixed by also checking each item's existing `required_for` tag (e.g.
both `fishing_pole` and `bait` are tagged `required_for: "fishing"`)
in that same fallback: a bare category word like "fishing" now
correctly matches BOTH tools this shop stocks for it, making the
match ambiguous (falls through to "Not sure what item you mean --
try naming it more directly.") instead of silently guessing one.
Only fires when the direct name-word check didn't already resolve a
match, so it can only ever add ambiguity to a previously-wrong single
match -- verified zero regressions on every item in the game
self-matching its own name, plus the specific previously-fixed cases
("buy an axe", "buy two potions from Grimsby", "Equip my armour").

## [1.11.54] — Vesh's companion arc gets a real trust-banded resolution

Continuing the full-storyline plan's Phase 3: Vesh now has a second
quest, "Back Down Into the Glow" (`veshs_resolution`), offered once her
existing personal quest is done, triggered by returning to
`glimmerdeep_grotto` -- the exact place her own quest text already
established she's spent this whole time trying to forget (the
"early-and-finite" companion pacing mode the narrative-craft memo
called out for her specifically).

`QUEST_COMPANION_RESOLUTIONS` (added for Grask last version) now
supports a second shape: `"banded"`, reading the real
`npc_relationships.affinity` column at completion time and picking one
of 3 real outcomes — low (≤ -20, `resolved_estranged`), mid (-19..39,
`resolved_distant`), or high (≥ 40, `resolved_loyal`) — the exact
banding the memo's research settled on. Verified end-to-end across all
three trust bands: each writes the correct persistent resolution state
and surfaces its own distinct in-character note.

## [1.11.53] — Grask's held-back companion arc resolves at the Arc 2→3 turn

Continuing the full-storyline plan's Phase 2: Grask, the companion
withheld per the narrative-craft memo's finding (his own goal — leaving
the warrens behind for good — resolves cleanly with no remaining hook
into the deeper mystery, unlike Borin's or Wren's arcs), now has a real
second quest, "Grask's Choice" (`grasks_resolution`), offered once his
existing "Grask's Freedom" personal quest is done. Completing it (by
reaching `the_first_city` — the same place the Arc 2→3 `story_gates`
trust check already gates) writes a real, persistent
`db.resolve_companion` call ("resolved_loyal") via a new small
`QUEST_COMPANION_RESOLUTIONS` mapping in `_complete_quest_and_announce`,
extensible to the other five companions in a later pass rather than a
one-off special case. Verified end-to-end: his two quests correctly
sequence in order, and completing the resolution quest writes the real
resolution state and surfaces a clear "has made their choice" note.

## [1.11.52] — Full-storyline Phase 2 (Into the Hush, 3-stage chain) + "open" verb fix

Phase 2 of the full multi-path storyline plan: Arc 2's climax quest,
"Into the Hush," is now a real 3-stage chain instead of one single
defeat-the-boss trigger, reusing the clue text already written for it
(no new lore invented, just gated properly):
- Stage 1, "Into the Hush" (reach `the_hush_below`) — the descent itself.
- Stage 2, "What Watches in the Dark" (defeat `shadow_wisp`) — the
  lesser threat already listed at that location has to be dealt with
  first.
- Stage 3, "Into the Hush" (defeat `the_unspoken`, `weight: climactic`)
  — the real confrontation, now getting the deeper AI-narrated pass
  from Phase 1's `narrate_chapter_climax`.

`_offerable_quest_at_location`, `_check_story_gate`, `_chapter_complete_note`,
and every other place that reads campaign quests are all fully generic
over quest id and location, so this chain needed zero other code
changes — verified end-to-end with a real throwaway test: stage1 →
stage2 → stage3 cascade correctly, the level-up and chapter-complete
notes both fire, and the Arc 2→3 `story_gates` check correctly
recognizes `the_unspoken` as defeated via the new quest id.

Live-caught bug, same monitoring session (Sugar): "Try opening the
barrel with a chalk symbol" fell all the way to `chat` — neither
`ai/intent_parser.py`'s two `examine` trigger lists included any form
of "open" at all, the same "verb not covered" gap this file has hit
many times before (touch/peer/read/observe, all added the same way).
Added `open`/`opening`/`opened` to the examine matcher, explicitly
excluding "force open"/"break down"/"smash" (those already route to a
real strength check elsewhere — forcing something open is a genuinely
different intent from just looking inside something unobstructed).
Also closed the same pre-existing gap for `list_shop` ("open the
shop"). Verified against a full case matrix with no regressions on
check_inventory/check_sheet/skill_check.

## [1.11.51] — Guild membership requires vetting, not an instant join (task #170)

Per Coffee's backlog design item: joining a guild previously just
checked level/class and let you straight in. Guild membership now also
requires `proven_in_combat` -- reusing the exact same real proof every
guild's ongoing quests already demand of members (winning a real
fight, see `guilds.GUILD_QUESTS`) rather than inventing a guild-specific
new mechanic. The flag is set the first time a real (non-AI) party
member wins any real fight (`_award_victory_xp`), with a one-line note
in that fight's victory summary ("proved themselves in real combat —
eligible to join a guild now"). `eligible_for_guild` (used both by the
actual join flow and by check_sheet's "guilds you could join" hint)
now gates on this the same way it already gates on level/class.
Verified end-to-end: an unproven character is rejected with a clear
reason, a real combat win sets the flag and surfaces the note, and the
now-proven character successfully joins afterward.

## [1.11.50] — "Check for quests" misclassified as check_sheet (live-caught, Sugar)

Live-caught while monitoring a real player's session: "Check for quests
in cellar" fell through every `check_quests` keyword trigger in
`ai/intent_parser.py`'s `_keyword_fallback` (the literal "check
quest"/"check my quest" phrases require no word in between, but this
has "for" between "check" and "quests") all the way to the model, which
misread it as `check_sheet` -- same recurring "check ... quest(s)" gap
class this file has hit before (tasks #92, #99, #109, #151, #154,
#161, #185), just with a different filler word. Added a tolerant regex
(`check ... quest(s)`, up to 3 filler words) matching the same shape as
the existing "any ... quest" fix. Verified against a set of phrasings
that should and shouldn't match check_quests -- no regressions on
check_inventory/check_sheet.

## [1.11.49] — Manual dice rolls auto-roll after 1 minute + real "examine" matching fix

Per Coffee's direct instruction ("give the user 1 minute to roll - if
not then auto-roll"): a manual-dice-mode roll prompt (attack, skill
check, shove, flee, gather, steal) previously waited indefinitely for
the player's reported number. Every prompt now also names the specific
character who needs to roll and says the 1-minute window out loud
("you have 1 minute, or I'll roll for you"), per Coffee's follow-up
("make sure to say what user has to roll the dice so its clear"). A
new `_maybe_auto_roll_pending_dice` check (runs every 60s idle-loop
tick) auto-resolves any prompt still unanswered after
`DICE_ROLL_AUTO_TIMEOUT_SECONDS` (60s), rolling a real d20 in place of
the manual value and posting a clear "didn't roll in time — auto-rolling"
notice before the result. This also bounds task #186's restart-survival
gap: a pending roll can now only ever be silently lost for at most one
idle-loop tick after a restart, not forever.

Live-caught bug, same conversation (Coffee: "a player is looking at the
wide-boled tree and its not working"): `_find_interactable`'s
word-overlap fallback split an interactable's stored name on whitespace
without stripping punctuation, so a word immediately followed by a
comma in the name (e.g. "an ancient, wide-boled tree" -> "ancient,")
could never satisfy its own `\bword\b` regex match — a trailing
comma-to-space transition has no word boundary at all. That silently
dropped "ancient" from the overlap count entirely, so both a typo'd
attempt ("wide-boiled") and even a perfectly normal shortening ("the
ancient tree") fell one word short of the required >50% threshold and
matched nothing. Fixed by stripping punctuation per word before
matching; verified this doesn't regress any of the other 34
interactables across the whole campaign (each still self-matches its
own exact name).

## [1.11.48] — Stale button taps no longer crash the callback (task #194)

Live-caught via monitoring, 2026-07-19: Coffee tapped an old "Accept:
Gather Silverleaf Herb" quest-board button and hit an unhandled
`telegram.error.BadRequest` ("Query is too old and response timeout
expired or query id is invalid") from `quest_menu_callback`'s bare
`await query.answer()` — the global error handler caught it (no crash),
but the callback aborted before its real dispatch logic ever ran, so the
tap silently did nothing. Grep found the exact same unguarded pattern at
all 8 callback-query handlers in the game (battle menu, shop, spell,
quest, item, menu, equip, level). New shared helper `_safe_answer(query)`
catches `TelegramError`, logs it, and lets the handler continue instead
of aborting; all 8 call sites now use it. Verified via a real throwaway
test with a callback query whose `.answer()` always raises — confirmed
`menu_callback` and `quest_menu_callback` both complete normally instead
of crashing.

## [1.11.47] — Full character-sheet menu system, full-storyline Phase 1, + 2 live bug fixes

A complete, navigable menu system replacing the old flavor-only
character-sheet buttons, per Coffee's direct request ("I want a usable
menu system linked to it... show things in menu that are needed for the
game — story-so-far - quests - inventory - equip character - make it a
complete menu system... leveling up... include it in the menu — can be
used in adventure and support topics"):
- New `/menu` command (works from both Adventure and Support) and a
  "📖 Menu" root screen linking to six real sections — Character Sheet,
  Story So Far, Quests, Inventory, Equip Gear, and Level Up — every
  section loops back to the root via its own Menu button, forming a real
  navigable loop rather than dead-end screens.
- Spell and item buttons now use a real two-step target picker (self, or
  any other real party member physically present) before casting/using —
  "give them options so if they want to use a potion or charm a player,
  they can select it, pick a target, and then execute it," per Coffee.
- "Story So Far" is a real AI-narrated novel-style recap
  (`narrate_story_so_far`), strictly grounded in the character's actual
  completed chapters/current chapter/completed quests — never invented —
  plus a deterministic chapter list (completed/current/locked).
- Quest and chapter completions now also post to the Main topic (📜 per
  quest, a separate more-prominent 🌟 for a full chapter/arc completion),
  matching Coffee's "when characters complete a quest or mission it
  should be posted to main also" request.
- A small independent chance (8%) for combat loot to include a real,
  buyable map item, on top of the existing gear-loot roll.

Phase 1 of the full multi-path storyline plan (companion trust/branching
resolutions, the Arc 2→3 mid-story turn), per Coffee's narrative-craft
research and direct "build the FULL storyline" request:
- `npc_relationships` gains a `resolution` column (`db.resolve_companion`/
  `get_companion_resolution`) — reuses the existing `affinity`/
  `memory_events` columns as the trust variable and flags list, no new
  parallel state.
- A new `story_gates` third gate type on location connections
  (`_do_move` and `_do_fast_travel`, alongside the existing `min_level`/
  `locked_connections` gates): `the_hush_below` → `the_first_city` now
  requires `the_unspoken` defeated AND at least one recruited
  companion's trust ≥ 40, instead of only a level check.
- Chapter-climax quests (`clear_the_warrens`, `the_first_city_quest`,
  `the_unmoored_isle_quest`) now get a real AI-narrated flourish at a
  deeper `story_mode` pass (`ai.dm_agent.narrate_chapter_climax`), on
  top of the deterministic reward text.

Two real live bugs reported directly by Coffee, fixed and verified via
throwaway tests:
- **Level-up notifications never fired outside combat.** Combat XP
  (`_award_victory_xp`) always compared before/after level and posted
  to Main, but every non-combat XP source — story quest rewards, board
  quest turn-ins, branching quest choices, login streak bonuses, and a
  board-quest-completed-during-combat side case — called `db.add_xp`
  directly with no level-up check at all. New shared helper
  `_award_xp_and_announce_level_up` wraps `db.add_xp` with the same
  check/announcement combat already had; all 5 non-combat XP call sites
  now use it.
- **Completing a gather board quest never consumed the gathered
  material.** `_do_gather` already added the real item to the player's
  backpack; turning the quest in (`_check_board_quest_turnin`) never
  removed it, so gathered materials stayed in inventory forever after
  being "delivered." Also fixed the asymmetric branching "retrieve a
  moral cost" quest archetype (`_do_resolve_quest_choice`): "keep it"
  correctly never consumed the material (by design), but "leave it be"
  didn't consume it either, when it should.

## [1.11.46] — Manual-dice-roll prompts now retry on a transient timeout (task #187)

Real live incident, 2026-07-19: a `telegram.error.TimedOut` (transient
network ConnectTimeout) hit while `_do_gather` tried to send its "Roll
a d20" prompt — caught by the global error handler so the process
didn't crash, but the prompt itself had no retry. Root cause: all six
`_PENDING_DICE_ROLLS` prompt sites (attack, skill_check, shove, flee,
gather, steal) called `update.effective_chat.send_message(...)`
directly instead of `_safe_send`, which exists specifically to retry
once on exactly this failure — the fix already rolled out to every
other narration/reply site in the game, just missed at these six.
Switched all six to `_safe_send`. Verified live through the real
`_do_gather`/`_do_steal` functions that the prompt still sends and the
pending-roll state still records correctly after the change (mechanical
fix, same message text/thread_id at each site — no behavior change
beyond the added retry).

Filed as task #186 (not fixed here, needs a design decision): a
related but separate bug found investigating this same incident —
`_PENDING_DICE_ROLLS` is a bare in-memory dict, so a bot restart while
a roll is pending silently drops it; the player's next roll then gets
misclassified as ordinary chat with no reply. Reproduced live the same
day this was found.

## [1.11.45] — Buyable maps: real, partial-reveal, never a spoiler (task #141, first half)

Per Coffee's backlog: "buyable and secret discoverable maps (partial-
reveal, never a full spoiler)." This ships the buyable half — the
discoverable (loot/quest-reward) half is a natural follow-up using the
same item, not yet wired to any specific quest/board reward, so task
#141 stays open.

Two new real items.py items (type `"map"`): Weathered Surface Map (35
gold, reveals 3 surface locations) and Tattered Underground Chart (60
gold, reveals 3 underground locations) — both sold at Vane's
Curiosities (The Arcane Nook), a natural home thematically for
unlabeled oddities of unclear origin. "Use"/"read" a map (routed
through the same `_do_use_item` dispatch a potion/scroll already uses)
reveals up to its `reveals_count` real, randomly-chosen location NAMES
from its one real `reveals_layer` that this character hasn't already
visited or revealed — a genuine partial reveal, never the whole layer,
and never a location already known. Grounded entirely in real
campaign.json location data, same principle as every other fog-of-war
feature in this game.

New `map_revealed_locations` character field (own DB migration,
`db.py`) — deliberately kept SEPARATE from `visited_locations`, never
merged into it: `_do_show_map` renders these with their own marker
(❓, "not yet visited") and explicitly withholds their connections,
since that's the actual spoiler a revealed-but-unvisited location must
never leak — a revealed location still has to be physically walked to
before its real connections unlock. Verified live through the real
`_do_buy`/`_do_use_item`/`_do_show_map` functions end-to-end (throwaway
test): bought a real map from the real shop (gold deducted correctly),
used it (revealed exactly 3 real not-already-known surface locations,
correctly excluded the already-visited starting location), confirmed
the map render shows both the visited location (with connections) and
the revealed-only ones (name only, no connections, correctly marked),
then bought and used a second copy and confirmed no duplicate reveals
across the two uses.

## [1.11.44] — World events: rare world-boss spawns broadcast server-wide (task #75)

Per Coffee's backlog: a rare, real world event rather than pure ambient
flavor. `_maybe_spawn_world_boss` runs in the existing background world
tick alongside the heartbeat/hourly-update checks — gated on BOTH a
minimum real-time gap (6 hours) since the last spawn AND a low random
roll on top of that, so it stays rare and non-clockwork, and never
spawns a second boss while one is still unresolved. Restricted to a new
`WORLD_BOSS_MONSTER_KEYS` allowlist (currently just Goblin Boss) —
deliberately excludes the unique arc-climax bosses (The Unspoken, The
Waking Ember, The Waiting Shape), which are quest-bound story beats,
not something that should randomly appear mid-story or get farmed.
Grounded in real campaign.json data throughout: `_find_location_for_
monster` looks up the boss's own real native location rather than
inventing one, and the broadcast goes to Main (server-wide, per the
task's own framing) via the same `_ChatOnlyUpdate`/`_safe_send` pattern
the existing world-heartbeat already uses.

State is persisted via `db.get_setting`/`set_setting` (a JSON blob
under `"active_world_boss"`, plus `"world_boss_last_spawn_at"`) rather
than an in-memory dict, so an active world boss survives a bot restart
— same reasoning as task #159's persisted-pending-state work. Players
fight it through the exact same real `_do_start_combat`/attack path any
other monster already uses (no new combat-trigger code needed — the
boss is already a real native encounter at its location). Defeating it
is detected inside `_award_victory_xp` (checking the defeated enemy's
real `monster_key` against the persisted active-boss state), clears
the persisted state so a new one can eventually spawn, and hands out a
real flat bonus (100 gold each, on top of the normal XP/loot every
monster kill already awards) — folded into the existing `level_up_notes`
list every one of this function's 5 call sites already loops over and
posts to Main, so no call-site changes were needed anywhere.

Verified live through the real functions end-to-end (throwaway test):
forced the spawn roll to confirm it persists state and broadcasts to
Main (thread_id=None, not Adventure) exactly once — a second spawn
attempt while one was still active correctly produced no second
broadcast — then started a real combat session against the spawned
Goblin Boss, killed it, and confirmed the real `_award_victory_xp` path
cleared the persisted state, awarded the bonus gold, and produced the
"World Boss Defeated!" note (gold math confirmed exactly: starting gold
+ the always-random loot-sale gold + the flat 100 world-boss bonus).

## [1.11.43] — Inline damage-die roll now honored in physical-dice mode (extends task #177)

Task #177 already let a physical-dice player declare their attack roll
inline ("attack goblin, i rolled 15") in the same message as the
action. This extends the same convention to the weapon's DAMAGE die:
"attack goblin, i rolled 15 to hit and 6 for damage" now substitutes
that real physical roll into the attack's main damage die
(`rules/dice.py`'s `roll_damage` gained a `forced_roll` param, clamped
to the die's real range) instead of always auto-rolling it — same "the
player's own roll is always genuinely used, never discarded" principle
`forced_roll` already established for the attack roll itself. On a
crit or Savage Attacks (multiple dice), only substitutes into the
FIRST die; the rest still roll normally, same as a real tabletop player
would only have one physical die for their weapon. Purely additive —
free text with no damage declaration still auto-rolls exactly as
before. New `_extract_combined_damage_roll` regex in bot.py, wired
into `_do_attack`'s existing `forced_roll` plumbing. Verified live
through the real `_do_attack`/`resolve_attack` path (throwaway test):
a forced attack roll of 20 (crit) with damage forced to 1 correctly
substituted into the first of the two crit dice, with the second die
and STR modifier rolling in naturally around it — HP dropped by
exactly the resulting total, turn order advanced correctly.

## [1.11.42] — Shop/spell/quest-board browsing gets real tap-to-act buttons (task #176)

Per Coffee's request: extend the RPG-style battle-menu button pattern
(shipped v1.11.28 for combat) to three out-of-combat browsing surfaces,
purely additive alongside the existing free-text commands — every
button dispatches through the SAME real handler the equivalent typed
sentence already uses, never duplicated logic, same as the combat menu.

- **Shop** (`_do_list_shop`): one button per real item actually in a
  shop's stock (`_shop_keyboard`, grounded in the same items.py lookup
  the listing text itself uses). Tapping re-resolves the shop from the
  tapper's OWN current location and calls the real `_do_buy` — never
  trusts anything about the shop from the button data itself.
- **Spells** (`_do_check_sheet`, own sheet only): one button per a
  character's real `known_spells` (`_spell_keyboard`) — never shown on
  someone else's sheet, since nobody can tap a button to cast another
  player's spells. Tapping calls the real `_do_cast_spell` with no
  target, identical to typing "cast X" with nobody named — a targeted
  spell fails the same honest way it already does without a stated
  target, no new behavior invented for the button path.
- **Quest board** (`_do_check_quests`): one "Accept" button per quest
  actually postable right now — both the location's story-quest offer
  and every not-yet-taken board quest (`_quest_board_keyboard`), using
  each quest's own stable `quest_id`/`board_quest_id` as callback data
  (never the title text itself, which can be long or punctuated) and
  resolving back to the real title before calling the real
  `_do_accept_quest` — same dispatch path the existing "say which
  choice you want" free-text flow already uses.

New callback-data namespaces (`shop|`, `spell|`, `quest|`), each with
its own `CallbackQueryHandler`, so none of these can ever collide with
the existing combat battle menu's `bm|` prefix or each other. Verified
live through the real handlers end-to-end (throwaway test): tapping a
shop button actually spent gold via the real purchase path, tapping a
quest button actually wrote a real `active_quests` row via the real
accept path, and tapping a spell button dispatched through the real
cast path (correctly declining a target-needing spell with no target
given, same as typing "cast Fire Bolt" alone would).

## [1.11.41] — Boss enemies get a pre-roll "sizing up its target" narration beat (task #167)

Per Coffee's request: for every combat turn, narrate the enemy's
decision-making before the dice roll — who they're sizing up, why
they chose that target — as its own beat, then the existing roll+
outcome narration. Scoped to boss/named enemies only (Coffee's own
call after I flagged the latency tradeoff of doing this for every
regular goblin/wolf too): `_resolve_ai_turns` already picks the
target deterministically (lowest current HP among the living
opposition) before `resolve_attack` ever rolls anything, so this new
`narrate_boss_decision` call (ai/dm_agent.py) just gives voice to a
choice the rules layer already made — never invents a target, and
the roll+outcome narration that follows is completely unchanged.
Fires once per turn (not once per Multiattack swing) via an
`attack_num == 0` guard, and has its own plain-text fallback so a
narration-call hiccup here can never block the actual attack. Verified
live through the real `_resolve_ai_turns` handler with a boss (2
Multiattack swings): the decision beat fired exactly once, before the
first swing, followed by both real attack resolutions.

## [1.11.40] — Skill-check narration (task #165) now states a concrete outcome, not just mood

**Real live report (2026-07-18, trusted dev Sugar's first bug report):**
"Search for wolves" → "Success! (rolled a 14)" followed by pure
atmospheric prose (forest holding its breath, senses sharpening) that
never said what was actually found — she couldn't tell what her
successful check had revealed and had to ask in Development. Root
cause in `ai/dm_agent.py`'s `_skill_check_preamble()`: it required
"vivid, sensory prose" and faithfulness to success/failure, but never
required the narration to land on an actual, stated result.

Fixed in two parts:
- The preamble now requires the narration's first sentence state a
  concrete, actionable outcome, with the atmospheric prose built around
  it rather than saved for last — a first pass that put the payoff at
  the *end* got cut off mid-sentence by this model's output-length cap
  in testing, silently reproducing the exact bug being fixed, so the
  payoff now comes early where a cutoff can't drop it.
- `_do_skill_check` (bot.py) now grounds search/perception-flavored
  ("wisdom" ability) checks in a real fact when possible: if the
  player's search text names a monster actually present at their
  location (`location["monsters"]`, via the same `_find_monster_
  mentioned_in_text` helper task #98's "examine" fix already
  established), that real fact is handed to the narrator to state
  plainly — never inventing a discovery that isn't backed by real game
  data, same ground-truth boundary this project holds everywhere else.
  No grounded fact available (most other skill checks) → narration
  still gets a concrete-but-honest line ("nothing of note turns up")
  rather than inventing specifics.

Verified live through the real `_do_skill_check` handler at The
Whispering Wood (a location with real wolves) — narration now clearly
states the wolf's presence instead of pure mood with zero information.

## [1.11.39] — Mid-fight physical-dice toggle now actually takes effect

**Real live report (2026-07-19, Coffee: "dice mode on doesnt seem to be
working with the battle system"):** confirmed by reading the code path
end-to-end, then reproducing live through the real handlers.
`_do_attack`/`_do_shove`/`_do_flee`/etc. all check `manual_dice_enabled`
on the COMBAT SESSION's in-memory participant dict -- a one-time
snapshot taken from the character's DB row the moment the fight
started (`sessions.start_session` <- `_get_combat_eligible_party_
members`). `_do_toggle_manual_dice`, meanwhile, only ever wrote the new
value to the DB row. So flipping dice mode on (or off) WHILE already in
a fight silently did nothing for that fight -- the toggle only ever
took effect starting with the *next* encounter, with no error and no
indication anything was wrong. Fixed by having the toggle also patch
every active session's copy of that participant in place, so a
mid-fight toggle applies to the very next roll. Verified live through
the real handlers: toggled dice mode on mid-fight, attacked via the
battle menu, got prompted to roll a d20, typed the result, and the
attack resolved correctly with a real hit and real damage -- combat
continued normally into the next round afterward.

Also hardened `tests/helpers.py`'s `use_test_db()` after this fix's own
verification test accidentally overwrote the LIVE bot's real
`sessions_snapshot.json` with fake test-fixture combat data:
`sessions.SNAPSHOT_PATH` is a bare relative filename, resolved against
whatever the current process's cwd happens to be, not anything
`config.DB_PATH` controls -- a throwaway test script run from the same
directory the real bot runs from silently clobbers the production
snapshot the moment it calls `sessions.start_session()`, with zero
warning. `use_test_db()` now also redirects `sessions.SNAPSHOT_PATH` to
a throwaway path and clears the in-memory session/lock dicts, so any
test using this helper is safe from this by default, the same way
`config.DB_PATH` already protects `db.py`.

## [1.11.38] — The v1.11.37 deadlock fix now also checks proactively at startup

v1.11.37's `_try_end_stale_combat` only ran reactively, on the next
attack attempt against a fight with zero living enemies -- but Coffee's
actual stuck fight was ALREADY sitting in that exact state when
v1.11.37 deployed, and he'd already given up and moved on rather than
attacking a visibly-dead wolf again. Confirmed live: the post-deploy
snapshot still showed both wolves at 0 HP, unresolved. Now also checked
once at startup for every restored session, same place and same
`_StartupUpdateStub` pattern the existing AI-turn-resolution startup
check (task #159 follow-up) already uses -- so a restart alone is
enough to un-stick an already-stuck fight, no further player action
required.

## [1.11.37] — Fix a real live combat deadlock (all enemies dead but fight wouldn't end); "who's here" now answers with real location-scoped presence; "any quests available?" now works

**Real live incident (2026-07-19, Coffee: "no enemys left standing... it
stopped"):** confirmed via the session snapshot -- both wolves in the
fight were at 0 HP, but the fight never ended and every further attack
just said "No valid targets remain," forever. Root cause: `remove_defeated()`
(which drops a dead monster from `turn_order`) only ever runs as a
side-effect of the SAME attack that killed it; if that attack's
resolution never finished (the same shutdown-interrupted-handler race
already documented in task #184, just landing on combat state this time
instead of a dropped chat reply), the dead monster stays in `turn_order`
forever. `living_on_side()` correctly shows zero living enemies (it
filters by HP), but `is_combat_over()` checks `turn_order` membership,
not HP, so it never returns true either -- a permanent deadlock with no
legal action to escape it. Fixed with a new `_try_end_stale_combat`
safety net, called at all 5 "No valid targets remain" sites (attack,
shove, breath weapon, damage-spell cast, and the battle menu's Fight
button): it re-runs the defeat cleanup + victory resolution the
interrupted attack should have finished, so a stale already-dead monster
can never strand a fight again. Verified with a real test reproducing
the exact stuck state (0 living enemies, `is_combat_over()` still
False) and confirming it now resolves cleanly (XP, loot, achievements,
session properly ended) instead of deadlocking.

**"Who's here at the market with me?" (task #161)** fell through to
plain chat -- same "words inserted between two required phrase parts"
bug already fixed once for "who's ... with me" on its own, just not
tolerant of "here at the market" landing in between. Broadened to a
tolerant regex, plus bare "who's here"/"anyone here" as their own
trigger. Also fixed what happens once classified: `check_party`
previously only ever answered with the player's global party roster,
never who's physically present at their CURRENT location -- which is
what "who's here" is actually asking. It now answers with a real,
location-scoped list of who else is at the same spot right now.

**"Any quests available?" (task #185)** fell through to plain chat --
found live via topic-activity monitoring right after the combat
incident above. Same recurring gap this action has hit many times
before: every existing check_quests trigger needs a qualifier word
("my"/"board"/"active"/"check"/"show") next to "quest", and "any
quests available" has none of them. Added tolerant "any ... quest(s)"
and "quest(s) ... available" matching alongside the existing list.

## [1.11.36] — Combat messages now name the real spell/ability used, fixing the root cause of a support-agent hallucination (task #171)

**Root cause traced all the way back to the deterministic combat message
itself, not the support agent.** Coffee replied to "Ravenloft hits
Goblin 3 for 5 damage" with /help asking what spell that was; the
support agent invented "fire_bolt" -- but the real spell cast was
Eldritch Blast. Investigation found the actual bug: the combat
resolution line for EVERY hit, spell or plain weapon attack alike, only
ever said "attacks" -- it never named the real spell/ability at all,
even in the deterministic rules-computed block. So when asked which
spell was used, there was no real fact anywhere for the support agent
to ground an answer in, and the model fabricated a plausible-sounding
one instead of admitting it couldn't tell -- the same "no fact given,
model invents one" failure mode already fixed once for class names
(task #153).

Fixed at the source: `_format_combat_result`/`_post_narrated` now take
an optional `action_label`, and `_do_cast_spell`'s damage-spell branch
passes the real spell name through. The resolution line now reads
"**Ravenloft** casts **Eldritch Blast** at **Goblin 3** → **Hits for 8
damage!**" instead of the old generic "attacks" -- both fixing what
players actually see in real time AND giving any later /help-as-reply
question about it a real fact to answer from, rather than needing a
retroactive event-log lookup. Plain weapon attacks are unchanged (no
name was ever being lost there). Verified live via a real test: a cast
Eldritch Blast now shows the true spell name in the resolution block; a
plain attack keeps its existing phrasing untouched.

## [1.11.35] — "Send X to Y" now works as give_item; giving an item to a recruited-NPC party member no longer misfires as talk_npc (task #181)

**"Send woodcutters axe to @ShesAQueen_78" was misclassified as `chat`,
found live via topic-activity monitoring: no item transfer, no reply
about it.** Root cause: the give_item keyword check only triggered on
`"give "`/`"hand "`/`"trade "` + `" to "`, and the no-"to" dative-construction
regex only matched give/hand/trade -- "send" was never a trigger verb
in either, even though "send X to Y" and "send Y a X" are exactly the
same construction. Added "send" as an equal alternative in both checks.

**Found via the real test written for the fix above, NOT part of the
original report:** giving an item to a party member whose name happens
to match a recruited campaign NPC -- "Give the potion to Sera", "Hand
Sera the torch" -- came back as `talk_npc` instead of `give_item`. Same
root cause as several earlier fixes in this file (buy/sell/recruit_npc/
invite_to_party/check_sheet all needed the identical fix already):
give_item's check used to sit AFTER the known-NPC-name matching loop,
so a message naming a real NPC like Sera got swallowed as talk_npc
before give_item ever ran. Moved give_item's two checks (and ask_clue,
which must stay checked first) to before that loop, same fix pattern
already established for every other action this has happened to.
Verified with a real test: 17 cases covering the reported bug, the
newly-found NPC-name-shadowing bug, and a regression sweep of
recruit_npc/invite_to_party/buy/sell/talk_npc/ask_clue/give-idioms, all
passing.

## [1.11.34] — Battle menu now asks who to target; fix a restart landing mid-AI-turn freezing combat forever

**Battle menu didn't ask who a heal/potion/damage-spell targets, per Coffee:
"in the battle menu wen using comsumables it shud ask who u want to use it
on... same with attacks and abilities."** "Fight" already prompted for an
enemy target; casting a damage spell or heal spell, and using a
heal/cure_poison consumable, all used to dispatch immediately with no
target named, so the underlying handlers' own free-text parsing always
fell through to their default (first living enemy for damage, always
self for heal/potions) -- never a real choice. Now shows the same
button-picker pattern Fight already used: a damage spell prompts for
which living enemy when there's more than one; a heal spell or
heal/cure_poison item prompts for which living party member (including
yourself) when there's more than one. Verified with a real test:
casting Magic Missile at a specific goblin only damages that goblin (not
the other), casting Cure Wounds and using a Healing Potion both
correctly land on the chosen ally instead of defaulting to self, and the
potion is consumed from the USER's own inventory, not the target's.

**Real live incident (2026-07-19, Coffee: "the battle seemed to stop"):**
the v1.11.33 restart landed exactly on an AI companion's turn
(Bram Ashfield) mid-fight, and combat just sat there -- confirmed via the
session snapshot, the last event was still the real player's attack from
BEFORE the restart, nothing had advanced since. Root cause:
`_resolve_ai_turns` (which processes AI/downed-player turns) has only
ever run as a side-effect of a human or AI action that just finished
(every action handler calls it right after `advance_turn()`) -- a
startup restore is neither of those, so a restart landing on an
AI-controlled turn had nothing to ever kick it forward again. Fixed by
calling `_resolve_ai_turns` once, unconditionally, for every restored
session right after announcing it in `_on_startup` -- confirmed by
inspection that the function only ever touches `update.effective_chat`,
so a minimal synthetic stand-in (`_StartupUpdateStub`/`_StartupChatStub`,
sending straight through `application.bot`) is enough since there's no
real incoming Update to hang one off at startup time. Safe even when
it's already a real player's turn: it just re-sends that turn's
announcement and battle menu and returns.

## [1.11.33] — Fix Main-topic notifications silently failing every time (task #182)

**Every single `_notify_main_topic` send since v1.11.32 has been silently
failing.** Caught live via the topic-activity monitor: a player's "attack
the wolf" started combat, the Adventure-topic "Combat Begins!" message
sent fine, but the immediately-following Main-topic announcement failed
twice with `BadRequest('Message thread not found')` and was dropped.
Root cause: `config.TOPIC_MAIN_ID` is `1`, and `_notify_main_topic`
passed that literal value as `message_thread_id` -- but Telegram's forum
API only accepts an OMITTED `message_thread_id` (`None`) for the
Main/General topic on outgoing sends, not an explicit numeric id, even
though `1` is the right value for *recognizing* an incoming Main-topic
message (already documented in CLAUDE.md as exactly this asymmetry --
nobody had previously noticed it also applies to sends). This means
level-up notes, quest-accept pings, guild-join confirmations,
combat-start announcements, and real-player-death notices had all been
quietly failing since #172 shipped -- the earlier "verified end-to-end"
tests used a fake chat stub that accepts any `message_thread_id`
without validating it against Telegram's real forum-topic rules, so
they couldn't have caught this.

Fixed with a `_MAIN_TOPIC_SEND` sentinel: `_safe_send`'s `thread_id`
parameter now distinguishes "unspecified, default to Adventure" (`None`,
unchanged) from "explicitly Main, no thread id" (the new sentinel) --
previously both cases would have had to collapse to the same value.
`_notify_main_topic` now passes the sentinel instead of
`config.TOPIC_MAIN_ID`. The two TTS send paths (`_maybe_speak`,
`_speak_via_piper`) had the same latent bug in their own thread-id
fallback logic and are fixed the same way. Verified with a real test
against a fake chat that actually records the `message_thread_id` it
receives: `_notify_main_topic` now sends `None`, plain `_safe_send`
still defaults to Adventure, and explicit thread ids (e.g. Support)
still pass through unchanged.

## [1.11.32] — Main-topic game notifications (task #172) + a real @username intent-parser gap (task #180)

**Main topic now surfaces the big moments, per Coffee: "the main chat can
be used for all game notifications for the player to free up clutter in
the Adventure topic."** A new `_notify_main_topic` helper posts a short
one-line ping to Main -- ALONGSIDE, never instead of, the full narration
that still goes to Adventure as always -- for the 5 events named
explicitly: level up, quest accepted, guild joined, a real player's
death, and entering battle. `_award_victory_xp` now returns
`(summary, level_up_notes)` instead of just a string so its 4 call
sites can post each level-up to Main too. Verified end-to-end with 4
real scenario tests (quest accept, guild join, combat start, and a real
player's 3rd-failed-death-save) -- all correctly posted to Main, with
combat start's narration surviving two real Ollama timeouts via the
existing fallback-template path.

**Real live bug, caught by the topic-activity monitor mid-session:**
Coffee's own message "Give @ShesAQueen_78 a Woodcutters Axe" was
misclassified as `chat` and silently dropped -- AFTER the @username-
targeting work in v1.11.29 already shipped. Root cause: that work fixed
how a recipient gets *resolved* once give_item is dispatched, but
ai/intent_parser.py's give_item *detection* regex (task #164, "give X a
Y" with no "to") only ever matched a capitalized plain word right after
the verb, which can never match an "@username" tag. Fixed by adding
`@\w+` as an explicit alternative in that regex, verified against the
exact failing message, the original capitalized case, both idiom
false-positives it must keep excluding ("give it a hand"/"give it a
try"), the already-working "to"-based @-mention variant, and
`ask_clue`'s "give me a clue" -- all 6 correct.

## [1.11.31] — Combat sessions survive a bot restart (task #159)

The real incident that started this whole line of fixes: a deploy
restart mid-combat (2026-07-18) wiped an active fight outright --
turn order, both goblins' HP, everything -- because sessions.py was
(deliberately, at the time) 100% in-memory with zero persistence.
Coffee: "that was not fair to players." This was the one piece of that
incident that hadn't been fixed yet (the rest -- restart discipline,
hourly-update/combat conflicts, use_item turn-awareness -- shipped in
earlier versions this session).

sessions.py now has a real (best-effort, not a redesign of the live
in-memory system) JSON snapshot/restore: every `start_session`/
`end_session` snapshots immediately, and a new periodic ~60s tick
(piggybacking on the existing idle-check background loop) snapshots
any other in-progress change, so a restart loses at most ~60 seconds
of combat progress instead of the entire encounter. On startup, any
restorable session (skipped if the snapshot is older than 2 hours --
an abandoned fight silently reappearing later would be worse than not
restoring it) is loaded back into memory AND announced in Adventure
("The bot just restarted, but this fight wasn't lost..."), so players
aren't left confused about what happened the way Sugar was last time.

Verified with 2 real tests: a full save/load round-trip confirming
turn order, round number, `sides`, `stabilized_ids`, and even
per-participant conditions all survive intact, plus staleness-cutoff
and missing-file handling; and an end-to-end `_on_startup` test
confirming a restored session gets announced in the correct chat with
the correct current-turn name.

## [1.11.30] — Combined action+roll messages, and Dev-topic stops hallucinating on statements

**Combine an action and its manual dice roll in one message (task #177,
per Coffee's dev-topic screenshot report: "Add support so I can give an
action and then I can say what my dice roll was in the system will
understand that").** Physical-dice mode previously always required two
messages -- the action, then a separate reply once prompted for the
roll. All 6 manual-dice call sites (attack, skill_check, shove, flee,
gather, steal) now also check the action message ITSELF for a real
roll declaration ("Chop for lumber - i rolled a 9", "shove the ogre,
got a 12", "natural 20") via a new `_extract_combined_roll` helper, and
resolve immediately if one's there -- no prompt-and-wait needed.
Deliberately requires an explicit roll WORD near the number (not just
any number in range 1-20, unlike the existing prompt-reply matcher),
so it can't misfire on an unrelated number already in the action text
(a target suffix like "attack goblin 2", a quantity, etc.). Verified
with 9 real unit cases (correct extractions + confirmed non-matches)
and one full end-to-end gather test that resolved directly from a
combined message with no roll prompt.

**Dev topic no longer hallucinates an "answer" to a plain statement.**
Real live bug (dev-topic screenshot, "The Dev topic is acting a little
wonky"): Coffee sent a STATEMENT ("The support topic is incredibly
important and it needs to function as a usable wiki for the players"),
not a question, but development_topic_handler forced it through
ai/dev_agent.py's answer_dev_question anyway, which always tries to
produce SOME answer -- with nothing real to respond to, the model
hallucinated disconnected nonsense ("Configure ai/ for item
application permissions..."). Added a deterministic gate: a message
only reaches the model if it's actually shaped like a question (ends
in "?", or opens with a real question word/ask-phrase) -- a plain
statement/directive now gets a short, honest "📝 Noted — saved for a
live Claude Code session to review" instead. Verified with 4 real
scenarios (a real statement, a real "?"-question, a question-word
message with no "?", and a real directive from this session's own
history) -- only the two real questions ever reach the model.

## [1.11.29] — @username targeting everywhere + Support never dead-ends

Two real dev-topic reports, both fixed:

**@username targeting, extended everywhere (per Coffee: "attack
@tagged_player with my sword... Revive @Tagged_player... use it for
everything we may need to do").** `_do_give_item` got real Telegram
`@username` tagging support 2026-07-17, but every OTHER "name a party
member in free text" spot still only matched a character's in-game
display name. Centralized all of it into one shared helper,
`_match_member_by_name_or_username`, so a single fix now covers every
call site: use_item (heal/cure_poison on `@user`), give_item, equip_item
+ auto-equip ("equip @user with..."), every heal/buff/summon/revivify
spell target, AND `_pick_target` (combat targeting) for the day a real
player ever ends up on an opposing side. "Go with @user to X" already
worked without changes -- move parsing only ever looks for the
destination name and ignores the rest of the sentence. Verified with 6
real scenario tests (use_item, give_item, equip_item, revivify, combat
target-pick, and move-with-extra-text all correctly resolving/ignoring
the `@username` tag).

**Support topic no longer dead-ends a slow question.** Real live bug
(dev-topic screenshot): two genuine player questions ("how do I start a
battle?", "how do I join Ravenloft's quest?") both hit
`answer_support_question`'s old 2-attempt retry limit, got the generic
"couldn't answer in time" apology, and were NEVER actually answered --
no further retry, no interim heads-up, nothing. Per Coffee: "The
support topic ... needs to function as a usable wiki for the players"
+ "if it takes time to process a support message - tell them - then do
it." Fixed both halves: (1) an explicit "⏳ Looking that up now..."
message is now sent immediately, before the potentially multi-minute
Ollama call starts, not just the ambient typing indicator; (2) retries
bumped from 2 to 5 attempts with real backoff (5s/10s/20s/30s), so a
transient busy period on this box's single shared Ollama slot gets
enough real chances to clear before giving up. Verified with 2 real
tests: recovers correctly after simulated transient failures, and still
bounded (5 attempts, not infinite) with a clearer message pointing to
admin `/redo` if the model is genuinely down.

## [1.11.28] — RPG-style battle menu (Fight/Skills/Items/Run)

Coffee's request: "create an RPG style battle menu for battles... allow
them to pick from prompts too... Fight, Skills/Magic/Abilities
(depending on Char), Items (backpack), Run." Every "it's your turn"
message (already showing the HP roster from 1.11.27) now also comes
with tappable Telegram buttons, per his explicit confirmation that this
should be an ADDITIONAL affordance alongside free text, never a
replacement -- typing "attack the goblin" still works exactly as
before, for both human and AI-driven players, dispatching through the
exact same `_do_attack`/`_do_cast_spell`/`_do_use_item`/`_do_flee`
handlers either way (no logic duplicated between the two input paths).

- **Fight**: auto-attacks immediately if there's exactly one living
  enemy; shows a target-picker submenu (with each enemy's name + HP)
  if there's more than one.
- **Skills**: only appears if the acting character actually has real
  known spells -- a martial class with none never sees an empty,
  dead-end menu. Tapping a spell casts it.
- **Items**: only appears if they're actually carrying a real
  consumable. Tapping one uses it.
- **Run**: always available, attempts to flee.
- Every tap is re-verified against the session's actual current turn
  holder before anything happens (same "it's not your turn" boundary
  the text commands already enforce), so nobody can act on someone
  else's turn by tapping their menu.

Verified with 5 real scenario tests: keyboard reflects a real
character's actual spells/inventory; an out-of-turn tap is rejected
without touching any state; a single-enemy Fight attacks directly; a
multi-enemy Fight shows the right target submenu; Run dispatches a
real flee attempt (including through a real Ollama narration timeout,
confirming the fallback-template path still works correctly).

## [1.11.27] — Phrasing gaps (give/eat/armour/join-guild), real combat-turn handling for items, and an HP roster on every turn

Batch of fixes found during continued reverse-playthrough testing plus
two more live reports from Coffee tonight:

- **"Give [person] a [item]" (no "to") fell to silent chat.** Coffee hit
  this live ("Give Laurienna a Woodcutters Axe" — no reply at all).
  Fixed with a capitalized-name check right after give/hand/trade
  (deliberately NOT generic item-name matching, which false-positives
  on idioms like "give it a hand" matching "Bracers of the Steady
  Hand").
- **British "armour" never matched any real armor item** (all are
  American-spelled: "Leather Armor", "Chain Shirt", "Chain Mail") —
  confirmed live a player's "Equip my longbow and armour" silently
  equipped only the longbow. Fixed with a targeted spelling
  normalization.
- **"I want to join the hunt for wolves"/"...join Ravenloft on his
  quest" both misfired as join_guild** — two different real players
  hit this. The trigger now requires an actual guild (the word "guild"
  or a real guild's own name) to be mentioned, not just "i want to
  join" alone.
- **"Eat ration" wasn't recognized as use_item** — broadened from the
  old exact-phrase-only "eat my/the rations" to any real "eat"
  phrasing, using a proper whole-word check (a naive "eat " substring
  would have false-positived on "repeat"/"retreat"/"great"/"defeat",
  all containing "eat" followed by a space).
- **Using/eating an item mid-combat had zero turn awareness** — real
  incident tonight: a player tried to eat mid-fight, it silently did
  nothing (see the "Eat ration" fix above) AND didn't advance their
  turn, stalling the whole encounter until the idle-timeout eventually
  auto-passed it. use_item now blocks out-of-turn the same way attack
  does, and properly advances the turn on success.
- **Turn announcements now show a full HP roster** (per Coffee: "tell
  them the enemies/players still alive with hp like an RPG style
  battle... will help the player know which to target") — every
  "it's your turn" message now lists everyone still standing on both
  sides with current/max HP, defeated participants excluded
  automatically.

Verified: all phrasing fixes tested against both the real cases and
deliberate false-positive probes (idioms, unrelated words containing
"eat"/"armor"-adjacent substrings); use_item's combat-turn logic
verified against 3 real scenarios (out-of-turn rejection, in-turn
consumption+advance, out-of-combat normal behavior) plus the existing
mid-combat poison-cure regression test, all passing with no
regressions.

## [1.11.26] — Hourly ambient update could interrupt active combat mid-fight

Coffee, live: "What is happening here we were in battle?! ... are we
still in it?" -- traced to `_maybe_post_hourly_status_update`, which
fires unconditionally at the top of every real hour regardless of what
players are doing, with no check for an active combat encounter.
Confirmed live it landed mid-fight (two goblins still alive at 2/7 HP
each) and posted unrelated ambient flavor text plus a player-count/
quest-board readout right in the middle of the encounter, making it
look like the fight had just vanished. Fixed with a guard: skips
posting (without marking that hour's update as done, so it retries
every ~60s until the fight actually ends) whenever a real combat
session is active for the group.

(Separately diagnosed, not yet fixed: the actual fight in that
screenshot was independently stalled because a player tried "Eat
ration" instead of attacking on their turn -- using/eating an item
mid-combat doesn't consume a turn or interact with the encounter at
all yet. Tracked as task #168 for a follow-up session.)

## [1.11.25] — Two confirmed blockers on the wolf quest: "wolves" never matched "wolf", and a taken quest silently swapped for the wrong one

Coffee, after Sugar (now a real second trusted dev, added via /add_admin)
couldn't make any progress on "Clear out the Wolves" no matter how she
phrased it: "we have been tryin to get into battles with those damn
wolves for awhile." Found TWO real, independent, confirmed bugs, both
live-reproduced:

**1. "wolf" is not a substring of "wolves".** Every monster-name match
in bot.py (`_do_attack`'s auto-start-combat, the explicit `start_combat`
dispatch, and `_parse_enemy_count`'s plural-hint guess) only ever did a
plain substring check against the monster's singular key. That's fine
for regular plurals ("goblins" contains "goblin" for free), but English
pluralizes "wolf" irregularly (f -> v: wolf -> wolves), so "Attack
wolves using longbow" and "let's fight the wolves" NEVER matched the
one real monster key ("wolf") in this campaign that has this quirk --
combat never auto-started, no matter how the player phrased it. Fixed
with a general `_text_mentions_monster()`/`_monster_plural()` helper
(handles any f/fe-ending monster name, not just this one case) used at
all three call sites.

**2. Accepting an already-taken quest silently substituted a different
one.** Confirmed via the live DB: Coffee accepted "Clear out the
Wolves" at 12:33:59; 50 seconds later Sugar said the exact same "I
accept the quest to clear out the wolves" and got silently signed up
for "A Quiet Word About the Goblins" instead -- the only OTHER quest
still open on that board. `find_board_quest_by_name` correctly only
searches still-available quests, so her named quest (already taken)
correctly found no match -- but the code then blindly defaulted to
"the one other quest still open" instead of telling her hers was
unavailable. Fixed: now checks ALL of today's board quests for a name
match first, so an already-taken/completed quest gets its own honest
"already taken by someone else" / "already been completed" reply
instead of a wrong substitution.

Verified the monster-matching fix with a real unit-level test
(`_text_mentions_monster` against "wolf"/"wolves"/"goblin" cases, all
correct, no regression on real move phrasing). The quest-acceptance fix
was verified by manual trace against the exact live database state
that reproduced Coffee's report (board_quest_id 23 vs 24, accepted_by
mismatch) rather than a live end-to-end run, since Ollama was fully
saturated by real player traffic at the time (load average 8.5+,
llama-server at 164% CPU) -- deliberately didn't add more load on top
of that while Coffee and Sugar were actively mid-session.

## [1.11.24] — Reverse-playthrough phase begins: "go to rest/sleep/bed" fix (task #160)

Per Coffee's direction to commence a reverse-playthrough/testing phase
using varied ways of speaking English (dialects, slang, typos, formal
vs. casual, ESL-style phrasing) so the classifier's real coverage gaps
surface: ran ~40 such phrasings through the real `_keyword_fallback`.
Most "misses" were correct by design -- an unconfident fallback
("chat") is exactly what hands the message to the real Ollama model
for actual language understanding (task #116). One was a genuine bug:
`move_words`' bare `"go to"` substring check ran BEFORE any rest
detection, so "I go to rest now please" / "going to sleep" / "going to
bed" confidently (and wrongly) classified as `move` -- skipping Ollama
entirely and producing a nonsense travel attempt instead of resting.
Fixed with a rest-phrase check ahead of `move_words`, same shadowing
pattern already used for `fast_travel`. Verified against the new
phrasing and the 3 existing move-classification regression tests (no
regression).

## [1.11.23] — Physical dice mode handles advantage/disadvantage itself (task #91) + full slash-command layer (task #122)

Coffee, dev-topic screenshot: "When I do the dice rolls, I only wanna do
the dice. I want you to be able to handle the advantages and
disadvantages. If there is modifiers, you should be adding them
yourself." Modifiers were already handled correctly; advantage/
disadvantage was not -- the manual-dice prompts previously asked the
player to pre-resolve advantage/disadvantage themselves before
reporting one number. Fixed in `rules/dice.py`'s `roll_d20`: when a
physical `forced_roll` is reported and advantage or disadvantage
applies, the game now rolls one internal supplementary d20 and combines
it with the player's real reported roll (max for advantage, min for
disadvantage) -- exactly like a real second physical die would be used
if the player had one on hand. The player's own roll is always
genuinely used, never discarded. All 5 roll-prompt strings (attack,
skill check, gather, shove, flee, steal) had the now-obsolete "(account
for advantage/disadvantage yourself if it applies)" phrasing removed
accordingly. Verified with a real throwaway test exercising both
`roll_d20` directly and through `roll_ability_check`/`roll_attack`.

Also added observability logging around the manual-dice pending-roll
flow (`_PENDING_DICE_ROLLS`) -- both successful resolution and orphaned
bare-number replies with no matching pending state now log via
`logger`, making a repeat of the "I rolled a 9 and didn't get a reply"
report (root-caused this session to a bot restart landing between the
prompt and the reply, wiping in-memory state) traceable in
`bot_live_tmp.log` rather than silent.

**Task #122**: a full slash-command layer -- 9 new commands (`/quests`,
`/party`, `/inventory`, `/shop`, `/buy`, `/sell`, `/cast`, `/rest`,
`/guild`), each delegating to the same real handler logic the
natural-language path already uses. Per Coffee's follow-up request
("alphabetically organize the / commands please they are an incoherent
mess lol" / "keep things clean and professional"), the entire
`CommandHandler` registration block and the `/help` command listing
were both alphabetized and cleaned up. All 9 new commands verified
live through the real handlers.

## [1.11.22] — Real bold text + real player mentions (tasks #118, #158) + two more class features (task #91)

**Major find, task #158**: `bot.py` never set `parse_mode` anywhere in
the live gameplay path, so every single `**bold**` marker in every
narration/achievement/combat/gather message this project has ever sent
rendered as literal double-asterisks to real players -- confirmed by
re-examining a live screenshot mid-session. Fixed properly rather than
just flipping on `parse_mode=Markdown`: this project already got burned
once by that (a bare underscore in dynamic text like `talk_npc` reads
as an unmatched italic marker to Telegram's parser and returns a 400,
which `_safe_send`'s retry logic would then silently swallow -- trading
a cosmetic bug for real message loss). Instead, `_safe_send` now computes
exact Telegram `MessageEntity` objects itself (`_build_message_entities`)
-- `**word**` becomes a real BOLD entity, with correct UTF-16 offsets
(most of this game's emoji are surrogate pairs, so a naive character
count would misplace every entity after one) -- and there is nothing
left for Telegram's own parser to choke on.

**Task #118**, solved by the same mechanism: any real player's character
name mentioned in a message now becomes a genuine Telegram `text_mention`
entity (whole-word matched against every currently-active real player),
so Telegram actually notifies that player and makes their name
tap-able, instead of being plain text nobody gets pinged for.

**Task #91** (ongoing, not claimed complete -- 12 classes × ~10 levels
each is a much bigger audit than one pass): two more real gaps fixed --
Ranger's Danger Sense (already mechanical for spell saves) now also
grants advantage on the one other Dexterity-save-equivalent roll in
this engine, fleeing combat; Warlock's Agonizing Blast (Eldritch
Invocation, fixed-default convention like every other subclass choice
in this game) now adds Charisma modifier to Eldritch Blast damage at
level 2+.

Verified live: bold entities land on the correct UTF-16-correct
offsets past a leading emoji, an unmatched stray `**` is left as
literal text rather than silently dropped, a real player's name
becomes a genuine `text_mention`, whole-word matching avoids
false-positive substring mentions (e.g. "Sarah" inside "Sarahland"),
and the full path through `_safe_send` produces both entity types
correctly on a real message.

## [1.11.21] — Physical dice mode now covers every real player roll (task #157)

Coffee, after the gather fix: "make sure all rolls goto the player for
everything." Full audit of every dice-roll call site in bot.py found
manual_dice_enabled (physical dice mode) was only ever honored by
attacks and generic skill checks. Four more real gaps found and fixed,
all genuine player-only rolls (never a monster's, an AI companion's,
or an opposing combatant's roll — a player can't physically roll for
those):

- **Lockpicking** — used to dispatch straight to the DEX check before
  _do_skill_check's manual-dice branch ever ran, so it always
  auto-rolled even with physical dice on for every other check.
- **Shove** — the attacker's own contested STR check now prompts; the
  target's defensive roll stays internal, same as it already did.
- **Flee** — the fleeing character's own DEX check now prompts.
- **Steal** — the DC-15 theft check now prompts.

Deliberately left alone: the hidden bonus-quantity roll on a maxed-out
gathering skill (never surfaced to the player as a "roll a d20" moment
at all — a background chance mechanic, not a primary decision point).

Verified live: all four now correctly prompt for a manual roll instead
of silently auto-rolling, and resolve correctly once a real result is
given.

## [1.11.20] — Achievement announcements now name who earned them (task #156)

Real bug, reported live by Coffee with a screenshot: in the shared
Adventure topic, "Achievement unlocked: First Steps"/"Battle-Ready"
never said which player earned it, leaving everyone else unable to
tell who it's directed at. `_check_and_award_achievements` (bot.py)
built the announcement without the character's name, unlike the
sibling guild-quest-completion announcement right above it which
already follows this project's actor-naming convention. Now reads
"🏅 **\<name\>** earns achievement: **\<achievement\>**". Verified live by
triggering a real achievement (level-up to 2) and confirming the name
appears.

## [1.11.19] — Fixed resting/waking sending players to the wrong safe location (task #155)

Real bug, reported live by Coffee: he rested in The Whispering Wood
expecting to wake at The Crossroads Tavern, but woke at Market Row
instead. Root cause was in `db.mark_visited()`: revisiting a location
already in `visited_locations` was a silent no-op, so the list's order
only ever reflected each location's FIRST-ever visit, never its most
recent one. `_nearest_safe_waypoint` (bot.py) scans this list in
reverse specifically to find the most-recently-visited safe location
to wake a resting character at — for any character who visited a shop
before their first-ever trip through the tavern (true for most
characters, since the tavern is the starting location and gets
revisited constantly thereafter), the tavern's one early list position
never advanced past the shop's, no matter how many times they'd since
walked back through it. `mark_visited` now moves an already-present
location to the end of the list on every revisit, making the order a
genuine recency order. Verified live by reproducing Coffee's exact
sequence (tavern → market → tavern revisited several times →
whispering wood) — now correctly resolves to the tavern.

## [1.11.18] — Local Piper TTS backend (task #97)

Added a second TTS backend alongside the existing @TextTSBot
integration: `ai/tts_piper.py` synthesizes narration locally via Piper
(a small neural TTS model, confirmed via a live POC to add only ~2.6s
of compute for an 8s line -- no meaningful contention with Ollama's
single generation slot on this CPU-only box) and sends it as a real
audio attachment via `send_audio`, with no third-party bot dependency.
Toggle with "use piper for tts" / "use textbot for tts" in the
Development topic (independent of the existing "turn on/off tts"
switch). Sends WAV rather than OGG/Opus deliberately: Telegram only
renders its compact voice-note bubble for genuine OGG/Opus, and
re-encoding to that format would mean shelling out to ffmpeg -- a
subprocess call this project's security boundary explicitly forbids
adding anywhere in this codebase. The voice model (~60MB) downloads
once on first use into the gitignored `voice_models/` directory rather
than being committed.

Verified live: piper backend produces a real WAV via send_audio,
textbot backend still triggers its `/tts` message unchanged, and
tts_enabled=0 suppresses both.

## [1.11.17] — Two real bugs fixed (tasks #153, #154)

**Fixed: Support hallucinated the player's own class.** A real Warlock
asked if casting Fire Bolt fit their kit and got told "it aligns with
your Wizard class ability" — the yes/no judgment was right, but the
class name was invented even though the correct one was already
handed to the model verbatim, same failure mode as the earlier
XP-hallucination bug just for a class name instead of a number.
`ai/support_agent.py` now deterministically corrects any "your
&lt;other class&gt;" phrasing back to the character's real class after
the model responds, rather than trusting free-form generation with a
fact that has exactly one correct value.

**Fixed: defensive/conditional statements misclassified as an actual
attack.** "Stand on guard in case the wolves attack" contains the bare
word "attack" as a substring, so `ai/intent_parser.py`'s keyword
fallback matched it as a real attack action and could start unwanted
combat. Conditional/hypothetical markers ("in case", "if", "should
they", "in the event") now suppress the attack-word match, falling
through to ordinary silent chat instead — the same as any other
non-actionable flavor text.

## [1.11.16] — Guild quests + real guild-only Telegram topics (task #77)

**New: 3 real Telegram forum topics**, one per guild (Adventurers'
Guild, The Arcane Circle, Silver Wardens), created via
`scripts/create_guild_topics.py`. Each is a genuine members-only
space: the bot only ever engages there with a real member of that
guild (a non-member gets an honest redirect), ordinary chat needs no
processing at all, and "check guild quest" (there or in Adventure)
shows that guild's real daily bounty.

**New: guild quests.** One real, repeatable bounty per guild
(`guilds.GUILD_QUESTS`), claimed once per real calendar day by winning
ANY fight while a member — same day-gating idea as login streaks, not
a new generation/expiry system. Completing it awards real gold + XP
and announces right in the guild's own topic.

Explainer messages are posted and pinned in each guild topic via
`scripts/pin_guild_topic_info.py`, per Coffee's direct request.

## [1.11.15] — Campaign-loading infra (task #56)

`campaign_loader.py` already supported loading any `campaigns/<id>/
campaign.json` and discovering what's on disk, but bot.py hardcoded
`ACTIVE_CAMPAIGN_ID = "default"` directly with no way to point at a
different campaign without editing code, and `discover_campaigns()`
was never actually called anywhere. Now `config.ACTIVE_CAMPAIGN` (.env-
overridable, defaults to "default") drives it, startup fails loudly
and lists what IS available if it names a folder that doesn't exist
(instead of a cryptic error deep in the first handler that touches
CAMPAIGN), and a new `/campaigns` command shows what's discovered and
which is live. Still only one real campaign exists ("default") --
this is the loading infrastructure, not new campaign content.

## [1.11.14] — Weather + day/night cycle (task #84)

New `world_clock.py`: real, deterministic time-of-day (Dawn/Day/Dusk/
Night, from real wall-clock hour) and weather (stable for a whole real
calendar day, per region) -- computed as ground truth, same philosophy
as rules/dice.py, never invented by the AI narrator. Underground
locations honestly report no weather ("just the dark") rather than
inventing subterranean conditions. Shows on "look around" and via a
new direct query ("what's the weather like?" or /weather).

Scoped to the deterministic display layer only this round -- AI
narration prompts (skill checks, combat, examine) aren't yet grounded
in it, since threading `location` through those call sites is a
separate, larger change; worth a follow-up if it's wanted.

## [1.11.13] — Login streak rewards (task #78)

Real consecutive-real-calendar-day tracking, checked from the same
per-message activity checkpoint as presence -- your streak increments
once per real day you play, resets on a missed day, and never double-
counts multiple messages in the same day. Milestones (3/7/14/30 days)
award real gold + XP and announce in Adventure. Shows on your
character sheet. Daily quests already existed via the board-quest
system (one new bounty per location per real day) -- this fills in
just the streak-reward half of the original ask.

## [1.11.12] — Titles & Achievements (task #73)

New `achievements.py` catalog of 10 real achievements, each checked
against data that already existed on the character (level, gold,
known_monsters, completed_quests, board_quests_completed, guild,
equipped gear) -- no new counters invented for this. Unlocked at the
real moment they become true: combat victory, quest/board-quest
turn-in, guild join, or equip -- never on a timer.

Unlocking one grants a real title (`/title <title>` to wear it, or
"set my title to ..."; `/title clear` to remove). `/achievements` (or
"check my achievements") lists what's unlocked and what's still
locked. Active title shows next to your name on your character sheet
and the Hall of Fame leaderboard.

## [1.11.11] — Presence system (task #144) + recruited companions now actually follow you

**New: presence/status (task #144).** Real, derived player status --
🟢 Online / 🌙 Away / 😴 Resting / 🔕 Do Not Disturb -- shown in your
party roster and character sheet. Resting (already a real mechanical
state) always wins; Do Not Disturb is your own explicit choice
(`/donotdisturb`, toggle or `on`/`off`); otherwise online/away uses the
SAME 30-minute threshold that already drives this game's idle warning,
so presence never contradicts what the idle system believes. `/note
<text>` sets a short status line party members can see ("grinding the
mines, back soon"); `/note clear` removes it; bare `/note` shows your
current one.

**New: combat-join nudge.** When a fight starts, real party members who
are online elsewhere (not resting, not DND) are named in the combat
header so they know to come join -- this is what presence actually
feeds, per the original ask.

**Real bug fixed, caught live (Coffee: "if she is recruited she shud
follow the party?"):** recruiting a companion (e.g. Sarah) never
actually attached them to the recruiter's real party_id, so `_do_move`
-- which only ever moved the acting player -- silently left them
standing wherever `create_ai_companion`'s schema default put them
forever. Recruiting now attaches the companion to a real party and
moves them to the recruiter's current location immediately; moving now
brings along any real recruited companion (is_ai, NOT is_autonomous)
sharing that party. The separate hardcoded autonomous AI-played party
still roams entirely on its own, untouched.

## [1.11.10] — NPC rename: Sera -> Sarah

Per Coffee (voice-to-text couldn't reliably say "Sera"): renamed the
recruitable wandering-ranger NPC's display name from "Sera" to "Sarah"
across the campaign data (NPC record, both her quests' titles/
descriptions/clues) and updated her already-recruited live character
row (character_id 24) to match. Internal ID (`sera_wanderer`) is
unchanged, so nothing that references her by ID (quest giver_npc,
relationships) is affected — only the name players see and say.

## [1.11.9] — Hall of Fame, and "examine" now recognizes real monsters

**New: /leaderboard / "Hall of Fame" (task #74), per Coffee.** Real XP
ranking across every player's currently-active character. Combat-only
companions (is_ai=1) are excluded; the autonomous AI-played party
counts the same as any human, since it plays through the exact same
real pipeline.

**Real bug fixed, caught live (Coffee):** "Look at the wolves in the
whispering wood - give me detail about them" answered "doesn't spot
anything like that here" even though wolves are a real, active threat
at that exact location (and the subject of an active board quest).
`examine` only ever checked registered interactable objects, never
real monsters present. Now matches against `location["monsters"]`
too — shows real bestiary stats if already fought, or an honest
"here, but not yet known" line if not, keeping the same fog-of-war
boundary the bestiary already respects. Also handles irregular
plurals (wolf → wolves), which the first attempt at this fix missed
and a real test caught before it shipped.

## [1.11.8] — Pronouns field, "touch X" misclassification, description-flow bug, Pandora AI branding

**New: character pronouns (task #117), per Coffee** ("no gender/pronoun
field — narration guesses pronouns with no real data, can guess
wrong"). Settable at creation or any time after ("set my pronouns to
she/her"); shown on the character sheet when set. Narration now reads
this real fact for any secondary pronoun reference instead of
guessing — defaults to they/them when unset, never invented.

**Real bug fixed, found while building the above:** the description
add-anytime flow's resume path never passed `from_prompt=True`, so
replying to "what would you like your description to be?" with
ordinary text (no literal "description: ..." colon) silently failed
extraction and re-asked the same question forever instead of saving
the answer. Fixed.

**Real misclassification fixed, caught via live gameplay monitoring
(Coffee):** "I touch the tree" wasn't covered by any keyword trigger,
fell through to the model, which picked `look` (whole-area) over
`examine` (the object actually named) — not silent, just the wrong,
generic reply. `touch(ed)?` added to the examine-verb regex.

**Branding (task #138):** the AI pipeline now has a real, consistent
name ("Pandora AI") in the two safe, meta/technical spots where it's
appropriate to say so out loud — `/version` and the Support agent's
"busy right now" fallback — never in Adventure/narrative text, per
this project's design philosophy of never announcing AI-ness
in-fiction.

## [1.11.7] — /help reply-to-narration, and a new /hint command

Per Coffee: **/help used as a reply** to any message now forwards
that message's real text to the Support agent (grounded in this
game's actual items/spells/guilds) and returns its explanation,
instead of the generic reference list. Bare /help is unchanged.

**New: /hint** -- deterministic, no Ollama call, grounded entirely in
the character's current real location data (NPCs present, resource
nodes, interactables, connections, offerable quests, active-quest
clues) -- same fields `_do_look` already reads, reframed as suggested
actions ("Gather Wood... say 'gather wood'") rather than a
description. Never reveals a puzzle's answer or a quest's outcome,
same non-spoiler boundary `_do_ask_clue` already keeps.

Also fixed `scripts/announce_deploy.py`: it silently accepted
unrecognized flags (including `--version`/`--summary`, which don't
exist) and fell through to posting the literal flag text as the
message -- four deploys tonight (v1.11.3-1.11.6) went out broken this
way before it was caught. Now supports `--version`/`--summary` for
real, and any other unrecognized flag errors loudly instead of
silently posting garbage.

## [1.11.6] — Gather-quest turn-in soft-lock, plus a real /help command

**Real bug fixed (task #152):** Coffee reported a completed board quest
("A supply run for Wood") stuck at 4/4 with no reward, saying "I am
already in the whispering wood." Confirmed via the live DB
(progress_count=4, objective_count=4, completed_at=None).
`_do_gather`'s objective-complete message told the player to "return to
X to collect your reward," but the actual hand-out only fires on a
move/arrival event -- and gathering can only ever happen AT the
quest's own location (the crediting match requires it), so a player
who never left never generated an arrival event anywhere. Soft-locked
out of the reward through normal play. Fixed by checking turn-in
immediately after a successful gather, in addition to the existing
move-triggered checks (still correct for a player who leaves and comes
back later). Combat's `defeat_monster` crediting path was checked and
already grants rewards immediately with no such gap. Verified via a
real repro through `_do_gather`: a quest completes and pays out in the
same reply, no move event required.

**New: a real `/help` command (task #121)** -- there wasn't one. Lists
real, grounded example phrasings for movement, NPCs, combat, shops,
gathering/crafting, and quests, plus the handful of real slash
commands, since almost everything in this game is natural language
rather than commands.

## [1.11.5] — "Peer into" fell through to a silent, replyless chat

**Real bug fixed (task #151), found via routine Adventure-topic
monitoring:** "Perr into the face of the wide-boled tree and say
hello" (a typo for "Peer") produced zero reply. Root cause:
`ai/intent_parser.py`'s `examine_verb_match` regex already covered
"peered? at" but not "peer(ed)? into"/"peer(ed)? in" -- the exact
phrasing used -- so it fell through every other classifier branch
(there's no registered NPC named "tree" to reclassify "say hello"
against, since that reclassification only fires for a known NPC) all
the way to the silent `chat` default, which by design never sends a
reply. Extended the regex to also cover "peer(ed)? (at|into|in)", plus
the specific "perr" misspelling directly (its similarity to "peer",
0.75, sits just under the existing fuzzy typo-tolerance threshold of
0.8, and lowering that shared threshold risked new false positives on
the unrelated "accept" typo-tolerance it already covers). Verified via
5 real tests: the exact reported typo, the correctly-spelled phrasing,
the pre-existing "peer at" wording, and confirmed a known NPC mention
still correctly wins over the object match (no regression).

## [1.11.4] — Board quests stuck across a calendar-day boundary

**Real live bugs fixed (tasks #149, #150), reported by Coffee with a
screenshot showing "A supply run for Wood — 0/4" despite having
gathered the right amount.** Confirmed via a read-only query on the
live DB: the quest was accepted the night before (day_key from the
previous calendar day) and was still within its real 24h `expires_at`
window, but every crediting/turn-in call site
(`_do_gather`, combat's `defeat_monster` crediting,
`_check_board_quest_turnin`) matched against
`board_quests_module.get_todays_board_quests(location_id)`, which
filters strictly by *today's* day_key -- so a quest accepted on a
previous day silently dropped out of the set anything could credit
against, even though `check_quests` correctly still showed it as
accepted (a separate, non-day-scoped query). All three call sites now
match against the player's (or, for combat, the location's) real
accepted-quest list instead, so a quest stays creditable for its whole
real 24h window regardless of calendar-day rollover. Added
`db.get_accepted_board_quests_at_location` for the location-scoped
combat case.

**Second bug on the same report:** once a board quest was accepted,
its description disappeared from every view -- `check_quests` and
`format_board_listing` only ever showed a title and a bare progress
fraction, never what to actually do. That's why "read the quest
details for wood" came back empty. Both listings now keep showing the
description after acceptance.

Verified end-to-end through the real handlers (`_do_gather`,
`_do_check_quests`): a quest reconstructed with yesterday's day_key
now gets credited, its description shows up, and
`get_accepted_board_quests_at_location` correctly scopes by location
(excludes completed quests, doesn't leak across locations).

## [1.11.3] — Fix "database is locked" errors under concurrent load

**Real live bug fixed (task #148):** `bot_live_tmp.log` showed 5 real
`OperationalError('database is locked')` occurrences, including one that
crashed an AI companion's autonomous turn outright and two that caused a
~22-second reply delay on an ordinary "Level up"/"my character" message
while the idle-check and world-tick background loops were also hitting
the same lock. Root cause: `db.get_connection()` used a bare
`sqlite3.connect(config.DB_PATH)` — no `timeout=`, no WAL mode — while
the adventure handler, AI-party autonomous turns, idle-check loop, and
world-tick heartbeat all hit the same SQLite file concurrently from
within the same process.

Fixed in two layers: `get_connection()` now opens with `timeout=30.0` and
sets `PRAGMA journal_mode=WAL` (lets readers proceed alongside a writer);
on top of that, a module-level `threading.Lock()` now serializes the
entire connection lifetime, since the real contenders are all
in-process threads/tasks, not separate OS processes, so true
serialization removes the race entirely rather than just reducing its
odds. Verified: an 8-thread/3-second concurrent read+write stress test
against a real WAL-mode DB produced 0 errors (a first pass with only
timeout+WAL still produced 1 error under the same test). Also confirmed
no reentrancy risk — every db.py function that calls another
connection-opening function does so only after its own `with
get_connection()` block has already closed, so the non-reentrant `Lock`
can't self-deadlock.

## [1.11.2] — Multi-item shop/give commands, event-loop blocking fix, unnamed replies

**Real live bug fixed (task #147):** "Buy 10 torches, 1 shears, 1 pickaxe,
1 fishing pole, 5 bait." only ever bought the torches — every item after
the first either silently dropped or, worse, false-fired as an unrelated
action ("1 pickaxe" contains "pick", matching the gather fallback's bare-
"pick" rule meant for "pick the herbs"). `ai/intent_parser.py`'s
`parse_intents` now recognizes a shopping-list-style message (buy, sell,
give, or equip followed by a comma/"and"-separated item list) and keeps
it as ONE action instead of splitting it into several; a new
`_extract_item_list` in `bot.py` resolves every item and its own quantity
from the combined text. `_do_buy`, `_do_sell`, `_do_give_item`, and
`_do_equip_item` all now process every item named, not just the first.
Verified live-reported case exactly, plus single-item purchases
(unaffected) and existing multi-action compound messages (still split
correctly).

**Event-loop blocking fix (task #146):** every message in Adventure was
paying for two synchronous SQLite writes (`db.touch_last_active`,
`db.update_telegram_username`) and a read (`db.is_banned`) directly on
the single-threaded asyncio event loop — under real disk contention this
froze the ENTIRE bot for EVERY player, not just the one who triggered it
(confirmed live: an ordinary "look around" took ~14s post-1.11.0 deploy
with zero `getUpdates` logged during the gap). Wrapped all three in
`asyncio.to_thread` in both `adventure_master_handler` and
`support_topic_handler`.

**Several deterministic replies now name the acting character** (task
#139), matching the same "who does 'you' mean" fix already shipped for
combat/skill-check narration (#107/#115): gathering without the right
tool, "look"/"examine" at an undefined location, a rejected move, buying/
selling (now also correctly attributed per-buyer in a busy shared chat),
and give's "give it to whom?" fallback.

**Test suite fix (task #145):** `test_combat_excludes_characters_at_a_
different_location` and `test_combat_excludes_resting_characters` were
miscategorized under `FastRegressionTests` (whose docstring promises no
Ollama calls) but actually started a real combat session end-to-end,
triggering real combat-start narration — one real run measured this at
290s. Split each into a fast version (tests the location/rest filter
directly, no Ollama) and moved the full end-to-end version to
`SlowLiveTests` where it belongs.

## [1.11.1] — Hotfix: AI companions' autonomous turns were crashing

Caught live within minutes of 1.11.0 shipping: the new
`update_telegram_username` call added for `@username` player-targeting
crashed every AI companion's autonomous turn with `AttributeError` --
the synthetic Update used to route those turns through
`adventure_master_handler` only gives its stand-in user an `.id`, no
`.username` at all. Fixed with a safe `getattr` instead of a bare
attribute access. Verified via a direct reproduction of the AI-player
update path before shipping this fix.

## [1.11.0] — The big speed fix, plus a real admin/moderation system

**The main speed fix.** `ai/intent_parser.py`'s `parse_intent()` was calling
the local Ollama model to classify EVERY message, even though its own
logic already threw the model's answer away whenever the fast,
deterministic keyword classifier already had a confident (non-"chat")
opinion. Every dispatch branch in `bot.py` was audited to confirm the
keyword fallback already sets everything a handler needs — so skipping
the model call for these messages changes nothing about the result,
only the latency: confirmed via direct timing, confidently-classified
actions now resolve in under a hundredth of a second instead of
15–160+ seconds. Genuinely ambiguous messages (where the keyword
classifier has no real opinion) still get the model's help, same as
before. Also capped `num_predict` on every remaining Ollama call
(narration, support, the now-rarer intent-classification fallback) to
bound worst-case generation time, and fixed a real bug this surfaced:
a capped generation can end mid-thought before the model closes its
`<think>` tag, and the old stripping logic required a matching close
tag to work at all — so a truncated response could leak raw internal
reasoning straight to a player. Fixed to strip an unclosed `<think>`
block too.

**New: `/redo`.** Group admins/owner can reply to a message with
`/redo` to re-run it through a fresh, independent model read (not just
the same deterministic keyword pass) — useful right after a
misclassification bug gets fixed, without asking the player to retype
anything.

**New: a real admin/moderation system.** `/add_admin` and
`/remove_admin` (reply to grant/revoke Development-topic access to a
trusted second dev, without making them a full Telegram group admin).
`/ban` and `/unban`. `/report` (any player can flag something straight
to the admins, no auto-detection gate). `/warning` (reply to an
out-of-line message; 3 warnings auto-bans and reports it to
Development for reversal). `/warning_list` and `/ban_list` to see the
current record.

**New: `/map`.** A slash-command shortcut for the existing real
fog-of-war map — shows only where a character has actually been.

**Player targeting now understands @mentions.** `give <item> to
<@username>` resolves a real tagged Telegram user, not just a
character name spelled out.

**A visible "typing…" indicator** now shows during any reply that
involves a real AI call, so players see something's happening instead
of staring at silence during the (normal, documented) 30–160s wait.

**Fixed:** the gather-tool-check message ("You need a Woodcutter's
Axe...") and several other deterministic replies didn't name the
acting character — first of a batch of these being tracked down.

## [1.10.18] — Narration-naming regression fix (from tonight's own 1.10.17 batch)

Caught within the hour: the narration-naming fix shipped in 1.10.17
(#107) told the model to name the acting character, but didn't say
WHICH name to use if another name got mentioned along the way. A
compound message ("Hey Bram, are you here on a quest also?! And then
I chop some lumber" — Bram isn't even a real NPC) got narrated as
"Bram's hands clenched around the chisel..." instead of naming the
real character who rolled the check. The instruction now explicitly
says the acting character's name is whatever's on the "Character:"
line — any other name mentioned within the attempted action itself is
never the narration's subject. Verified live against the exact
reported scenario.

## [1.10.17] — A real shop-browse action, plus a batch of live-reported fixes from tonight's screenshots

**A real "browse the shop" action.** Say "I want to shop", "what's in
the shop", "what do you have for sale", or similar, and get the shop's
actual stocked items and prices — grounded in real data, not invented.
Previously there was no such action at all: "I want to shop" was
silently swallowed as chat, "I want to see the items in the shop" got
guessed by the small local model as a character-sheet lookup with a
garbled, made-up name, and "what do you have for sale" only routed to
`buy` (which needs a specific item already named).

**Fixed:** short-but-unambiguous item words like "axe" (3 letters)
failed to match item names ("Look for a shop to buy an axe" said "not
sure what item you mean" even though the shop sold exactly one axe) —
the matching fallback's word-length guard, meant to exclude trivial
connector words, was excluding real short item nouns too. Now uses an
explicit stopword list plus real word-boundary matching instead of a
blanket length cutoff (also closes a latent risk where a short word
could have matched inside an unrelated longer word).

**Fixed:** "The Tavern Cellar" was mistakenly tagged as a safe rest
location in the campaign data, so a character idle in the cellar
stayed there instead of being redirected to the actual Crossroads
Tavern.

**Fixed:** narration now consistently names the acting character
instead of defaulting to ambiguous "you" framing — genuinely confusing
in a shared multiplayer chat where several characters act in the same
feed. Applies to skill checks (including gathering), combat, and
examine narration.

**Fixed:** check_sheet's "nobody named X" fallback no longer mangles
an unrecognized target through `.title()` (a hallucinated name like
"player's own character" used to come back as the nonsensical "Player'S
Own Character").

**Tuned:** the idle/inactivity warning and auto-rest timers were
doubled (15→30 min warning, 30→60 min until auto-rest) — live feedback
that the old timing felt too fast.

## [1.10.16] — Character sheet: ability scores were missing, pending level-up now flagged at the bottom

Live-reported by Coffee: ability scores (STR/DEX/CON/INT/WIS/CHA) were
only ever shown once, on the one-off sheet printed right after
character creation — every later "check my sheet" (and Support's
character-sheet lookup, and party-sheet views) used a different,
shared formatter that never included them at all. Now shown on every
sheet view. Also, per Coffee's request: a character with unspent
Ability Score Improvement points (banked at levels 4/8/12/16/19) now
gets that flagged as the LAST line of their sheet rather than buried
mid-sheet, so a player who missed the original level-up prompt still
sees it waiting every single time they check their sheet.

## [1.10.15] — Support's "model unreachable" fallback is now specific, not generic onboarding

Live-observed 2026-07-16: when the Support topic's Ollama call fails or
times out (this genuinely happens under real CPU contention on this
box), the fallback used to say "just talk naturally in Adventure... try
'I want to create a character'... check the pinned message" — generic
onboarding tips with no connection to whatever the player actually
asked. Now says the game's local AI is busy and to try asking again in
a moment, which is both accurate and support-specific.

## [1.10.14] — Real skill-check proficiency, 5 more class features, character descriptions, a bestiary, and 3 live-reported misclassification fixes

**Skill checks now apply proficiency bonus, for real.** Every skill
check (`_do_skill_check`) used to roll flat, with no proficiency bonus
applied at all regardless of class — bigger than it sounds, since it
silently made every class equally (un)skilled at everything. A new
`CLASS_SKILL_ABILITIES` table (2 abilities per class, purpose-built for
skill checks rather than reusing the save-proficiency table, which
mismatches badly for classes like Ranger) now grants real proficiency
where it should apply.

**Five more level 2-3+ class features, genuinely mechanical:** Bard's
Jack of All Trades (half proficiency on skills you're not otherwise
proficient in, level 2+), Song of Rest (extra healing for the whole
party when resting near a Bard, level 2+), and Expertise (double
proficiency on your two skill-check abilities, level 3+); Ranger's
Danger Sense (advantage on Dexterity saves, level 2+); Sorcerer's
Metamagic: Empowered Spell (once per rest, automatically rerolls 1s
and 2s on your next damage spell, level 3+).

**Character descriptions.** Add a short backstory/appearance/
personality blurb to your character — asked at creation (skippable),
or any time after by saying something like "I'd like to add a
character description to my player." Shows up on your character
sheet once set.

**A real bestiary.** Say "bestiary" (or "what monsters have I fought")
to see every monster type you've actually fought, with its real stats
(HP, AC, STR/DEX, XP reward, boss/condition tags) — fog-of-war
discovery, same convention as the map: only monsters you've genuinely
encountered in combat show up, nothing pre-revealed.

**Three more live-reported misclassification fixes**, following the
same pattern as every other one this project has fixed the same way —
found via the live activity monitor, reproduced, root-caused, and
regression-tested:
- "What items do you have for sale?" was answered with the ASKER's own
  backpack contents instead of a shop's stock — a bare "what items"
  trigger shadowed the intent before it could reach anything
  shop-aware. Now routes to a real, location-grounded purchase check.
- This build's small local model has a documented bias toward
  guessing "pass_turn" for phrasing it doesn't recognize — confirmed
  again live when ordinary tavern chatter ("I'll take a mug, ale!!!
  how are you doing old buddy?") got the confusing "There's no active
  turn to pass right now" reply. The model's own pass_turn guess is
  never trusted anymore unless the deterministic keyword fallback
  independently agrees — same defensive pattern already used for
  start_combat.
- A compound message repeating the same action type ("Go to the
  crossroads Tavern, and then go to the whispering wood") only ever
  executed the first step and silently dropped the rest, since the
  compound-detection logic required two DIFFERENT action types before
  treating a message as genuinely multi-step. Now requires two REAL
  (non-chat) segments instead, regardless of whether they're the same
  action repeated — confirmed this doesn't regress the original
  false-positive guard ("attack the goblin and the wolf" still
  correctly stays a single action).

## [1.10.13] — Physical dice mode, real ASI level-ups, 4 new class features, gathering tools, and a batch of live-reported bugfixes

**Physical dice mode.** Players can now opt (asked at character
creation, toggleable any time by saying "use my own dice" or "let the
game roll for me") to roll their own physical d20 for the primary roll
of an attack or skill check instead of the game rolling for them — the
game asks, waits indefinitely for a number 1-20, and resolves with
that real roll. Damage and other secondary rolls stay automatic.

**Real player-driven Ability Score Improvements, replacing a silent
auto-apply.** ASI levels (4/8/12/16/19) used to auto-add +2 to a fixed
stat per class with zero player input or even a mention on the level-up
message. Now those points are banked (`pending_asi_points`) and the
player spends them on their own schedule by saying "level up" —
naming a specific ability (capped at +2 per ability per real 5E rule,
any remainder stays pending) or saying "auto"/"do it for me" to apply
them all to the class's traditional primary stat. Shows as a reminder
on the level-up message and the character sheet until spent. Character
creation's ability-score assignment step got the same auto-assign
option — say "assign them automatically"/"do it for me" and the game
picks a sensible spread (primary ability highest, then the class's
other save-proficient ability, then Constitution, then the rest).

**Four new level 2+ class features:** Fighter's Action Surge (double
attacks this turn, once per rest), Barbarian's Reckless Attack
(advantage on your own attacks), Paladin's Divine Smite (spend a spell
slot on a confirmed hit for real bonus radiant damage), and Monk's
Flurry of Blows (spend a ki point for two extra unarmed strikes,
ki pool scaling with level).

**Gathering now requires real tools**, per the profession fantasy
Coffee asked for: fishing needs a fishing pole and bait, lumberjacking
needs a woodcutter's axe, mining needs a pickaxe — all now stocked at
Maren's Wares. Herbalism alone needs no tool, but buying Shears lets a
character gather up to 3 herbs at once (a real dice roll decides how
many), and a separately-tracked proficiency bonus can add a bonus
herbalism roll at any gathering skill once practiced enough.

**Inactive party members now earn a small XP share** (10%) for a
party's victories even when off doing something else, to reward
sticking together as a real party instead of only whoever's actively
fighting.

**Board quests had three real bugs, all fixed together:** a completed
quest permanently occupied its daily slot instead of a fresh one
generating to replace it, completed quests kept showing on the board
listing indefinitely, and completing one was never credited anywhere
on the character — there's now a real "Board quests completed" count
on the sheet and in the quest journal.

**"Look around" now hints at quests available where you're standing**
— it never used to give any sign a location had a story quest tied to
it or a bounty posted, so players had to already know to separately
check. Now it names someone worth talking to, or points at the board.

**A batch of small, live-reported classification bugs, fixed
together:** "Return to X" wasn't recognized as travel; a bare "Check
quests" fell through to silent chat instead of showing the quest
journal; and a typo'd "accepet the quest" silently failed to accept
anything (now tolerant of small single-letter typos on "accept"
specifically). Also fixed: Support's sheet-lookup shortcut swallowing
a genuine question like "what are spell slots used for?" just because
it contained the words "my character sheet."

**Dev-topic video uploads now delete the raw file after extracting
frames**, instead of accumulating indefinitely on a laptop with
limited disk space.

## [1.10.12] — Extra Attack, racial traits, saving throw proficiency, real climax bosses for Arc 2/3, TTS delay fix

Biggest single rules-completeness pass this project has had. Found by
reading the actual 5E rules against what this engine does, the same
"reverse playthrough" audit method as previous sessions.

**Extra Attack.** Confirmed via grep this was completely absent —
arguably THE single biggest DPS feature every 5E martial class gets,
missing from every class (not even mentioned as flavor text). Fighter,
Barbarian, Paladin, Ranger, and Monk now get a real second attack per
turn at level 5; Fighter gets a third at 11, a fourth at 20. Applies to
a human player's own attacks and to AI-controlled party members alike.

**Racial traits were inert for 5 of 6 live characters** — only
Half-Orc's traits were ever mechanically wired; every other race's
signature traits were pure flavor text. Added: Dwarven Resilience
(Dwarves are now immune to the poisoned condition), Fey Ancestry (Elves
and Half-Elves are immune to paralyzed), and Dragonborn's Breath
Weapon — its own trait text called this "a real, usable action" but it
never existed; it's now a real combat action, damage scaling with
level, once per rest, a saving throw for half damage.

**Saving throw proficiency bonus was never applied, anywhere.**
Confirmed every save-based spell (Fireball, Insect Plague, etc.) only
ever added the raw ability modifier, never a proficiency bonus, even
for a class that's proficient in that specific save — the class/save
proficiency table didn't exist yet to look one up in. Added it, plus
Gnome Cunning (advantage on Intelligence/Wisdom/Charisma saves).

**Sneak Attack and Rage were frozen at their level-1 values.** Sneak
Attack always rolled a flat 1d6 regardless of level (real 5E scales to
10d6 by level 20); Barbarian Rage's bonus damage was always a flat +2
(real 5E scales to +4). Both now scale correctly.

**Cleric's Channel Divinity** (level 2+, Turn Undead) and **Ranger's
Natural Explorer** (advantage on tracking/survival checks) — two more
real class/racial features that existed only as flavor text or not at
all, now mechanically real.

**Arc 2 and Arc 3 had no real climax.** Of the campaign's 4 story
arcs, only Arc 1 and Arc 4 ended in an actual fight — "Into the Hush"
and "The First City" both completed just by walking into the location,
despite their own text clearly building to something. Added two new
bosses matching each location's established atmosphere: The Unspoken
(the Hush Below, silences you on a hit) and The Waking Ember (the First
City, drains life to keep its "still, somehow, faintly warm" spire
burning).

**Fixed: the TTS trigger message vanished too fast to actually tap it
and generate the audio** (real feedback from Coffee) — the auto-delete
delay was a flat 2 seconds; bumped to 20.

## [1.10.11] — Rings/amulets/wondrous items, Sera wander-lock fix, auto-equip shadowing fix, dead-message resilience

**Rings, amulets, and wondrous items actually work now (they never
did).** Same "reverse playthrough" pattern as potions/equipment before
them: Ring of Protection, Ring of the Undertow, Amulet of Health, and
Cloak of Elvenkind all had real mechanical fields (`ac_bonus`,
`constitution_set`, `stealth_advantage`) that nothing anywhere ever
read. Added a real `equipped_accessories` slot (a list, not a single
slot like weapon/armor/shield, since 5E genuinely lets you wear
multiple rings at once) — rings/amulets stack their AC bonus, Amulet
of Health sets Constitution to 19 (never lowering a higher score), and
Cloak of Elvenkind grants advantage on Dexterity checks made while
sneaking/hiding. `auto_equip` now also puts on every carried-but-unworn
ring/amulet/wondrous item, not just the single best weapon and armor.

**Fixed: "auto equip my equipment" did nothing.** Real live bug Coffee
reported. `check_inventory`'s "my equipment" keyword trigger was firing
before `auto_equip`'s own check was ever reached. Added an exclusion so
"auto"+"equip" together always routes to auto-equip; "check my
equipment"/"my equipment" alone still correctly shows the inventory.

**Fixed: Sera wasn't at her starting location to be recruited.** Real
live bug Coffee reported ("recruitable shud be location locked and
only move with the party when they recruit them"). Sera is both
`can_wander` and `recruitable` in campaign.json, so the living-world
wander tick was relocating her like any other NPC. Recruitable now
always overrides wanderable — a normal restart puts her back at her
canonical location (the in-memory wander state resets on restart
anyway).

**Fixed: a real player's death message could go missing.** One call
site for "you're dead and can't act" used a raw, non-resilient send
instead of the game's normal retry-safe send helper — a transient
Telegram timeout silently ate the message for a live player. Switched
to the same resilient helper every other message already uses.

## [1.10.10] — Reactions, shields, auto-equip, video support, and two live bug fixes

**Reactions** (per Coffee, "tackle the reactions system next" — the one
item CLAUDE.md explicitly flagged as needing `resolve_attack` to have a
real checkpoint between the attack roll and applying damage, not a
bolt-on): that checkpoint already existed naturally, so both plug in
there. Shield (Wizard/Sorcerer, a real spell that's had an `ac_bonus`
effect nobody ever read) auto-triggers when a hit isn't a crit and
would actually be avoided by the +5 AC, spending a real spell slot.
Uncanny Dodge (Rogue level 5+) halves confirmed damage. Both share one
reaction per round. Opportunity attacks were already implemented
(2026-07-13) — CLAUDE.md just hadn't been updated to say so, fixed
here too. Counterspell isn't implemented and can't meaningfully be yet:
no monster or NPC in this game ever casts a spell, so there's no real
trigger for it.

**Equipment, round 2** (per Coffee): shields are a real, separate 5E
slot (worn in addition to armor, adding their `ac_bonus` on top rather
than replacing it) — `equip_item` now handles all three slots
correctly, including re-adding a shield's bonus if armor is equipped/
changed afterward. New `auto_equip` action ("auto equip my character",
"put on my gear automatically") picks the real best weapon/armor/
shield by actual stats. Both `equip_item` and `auto_equip` can now
target another party member ("equip Sera with the longbow"). New
characters auto-equip their starting gear on creation instead of it
sitting inert. The character sheet now shows both what's equipped and
what's carried-but-not-equipped.

**Video support in the Development topic** (per Coffee, to send the
campaign page by page): videos previously had no handler at all (only
text/photos/documents were), so a video sent before this would have
gotten zero response — same silent-failure gap photos had before
2026-07-11. Downloads the video and extracts a frame every 2 seconds
via `imageio`/`imageio-ffmpeg` (a bundled ffmpeg binary invoked by that
library internally, not by any subprocess call added to this
codebase) so a live Claude Code session can read them as real images,
same as saved screenshots.

**Two real live bugs**, both caught from actual dev-topic reports
during this session: "Write my characters name in the guest book"
(Coffee dropped the apostrophe on "character's") matched
`list_characters`' roster trigger and showed his unrelated character
list instead. And "Glare into the shadows listen, what do I hear?" — a
real Perception-check phrasing — fell through to the small local
model, which misjudged it as `pass_turn`; bare "listen,"/"what do I
hear" had no keyword trigger at all (only "listen for" did), so
nothing could override the model's mistake. Both fixed with permanent
regression tests.

## [1.10.9] — Equipment finally affects combat

Ninth finding from the reverse-playthrough sweep, and the same bug
shape as v1.10.8's potions: `items.py` has had real weapon stats
(`damage_dice`, `ability`) and armor stats (`ac_base`) on Shortsword,
Longsword, Greataxe, Longbow, both daggers, Leather/Chain
Shirt/Chain Mail since they were added — but nothing ever equipped
anything, and every single attack in the game used one hardcoded
default weapon (1d8, Strength) regardless of what was bought or found.
Armor Class never changed after character creation either.

Added a real `equip_item` action ("equip my longsword", "wear the
chain mail", "wield the dagger"): weapons change the attack's damage
die AND ability (a Longbow correctly uses Dexterity, a Greataxe
correctly uses Strength, matching each weapon's real 5E properties);
armor recomputes Armor Class on the spot (`ac_base` + DEX modifier,
same formula character creation already uses). Applies to AI party
companions too, not just human players — anyone with a real inventory
benefits from what they're actually carrying. The character sheet now
shows what's equipped. Verified with a real DB-backed test that
equipping a Greataxe changes the actual damage dice an attack rolls,
and equipping Chain Mail changes actual stored Armor Class.

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
