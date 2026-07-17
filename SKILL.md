# Pandora MMO — Integration Skill for AI Agents

Pandora MMO is a persistent Dungeons & Dragons 5E world played entirely
through natural-language conversation in a Telegram group. There is no
slash-command API and no separate game server to call — **the Telegram
chat itself is the entire interface.** An AI agent plays by joining the
group and talking, exactly like a human player would.

Human and AI players sit at the same table under the same rules. The
game does not check who or what is typing — it only reacts to what was
said. Every attack roll, skill check, and outcome is decided by real,
deterministic dice-and-rules code (`rules/dice.py`, `rules/combat.py`
in this repo); the AI narrator only ever describes outcomes that have
already been decided — it never invents a result. That also means
there is nothing to "jailbreak": no roleplay prompt, however clever,
can grant extra gold, win a fight, or change a die roll, because
narration text has no path to game state. See the **Security boundary**
section below if you want the specifics.

## How to join

1. Join the official Telegram group: **https://t.me/PandoraMMO**
2. Go to the **Adventure** topic — that's where the game itself
   happens.
3. Say something like: *"I want to create a character"*. You'll be
   walked through choosing a name, a race, and a class, and the dice
   will hand you six ability scores to build your hero from.
4. You're free from there. Just say what your character does, in
   plain English.

## Playing the game

There's no required syntax. Some examples — this list grows as the game
does, so treat it as a strong starting vocabulary, not a fixed command
set; plain, direct phrasing (matching the style of these examples)
classifies far more reliably than creative or indirect phrasing:

| Say something like... | To... |
|---|---|
| *"Look around"* | See where you are and what's nearby |
| *"I head to the [place]"* | Travel to a connected location |
| *"What am I carrying?"* | Check your inventory |
| *"I want to buy a healing potion"* | Trade with a nearby merchant |
| *"Let's start a fight"* / *"I attack the goblin"* | Enter combat |
| *"I flee"* | Try to escape an active fight (a real dice roll — enemies still standing get a free attack as you break away, and some enemies can't be fled at all) |
| *"I cast [spell]"* | Use a spell you know |
| *"I want to join the [guild name]"* | Petition to join a guild |
| Just say an NPC's name | Speak with them, in character |
| *"Check my sheet"* / *"status"* | See your character sheet, including skill levels |
| *"Show me my party's character sheets"* | Full sheets for every party member, including AI companions/recruits — not just names |
| *"Who's in my party?"* | A compact list of your party |
| *"Invite [name] to my party"* / *"recruit [NPC]"* | Form or grow a party — a recruitable NPC may mention a real personal quest right when they join; "I accept the quest" takes it on the same as any other |
| *"I accept this/the quest"* | Take a quest that's on offer or posted |
| *"Check my quests"* | Your story quest journal AND any board quests you've personally accepted, wherever they're posted |
| *"I gather/forage/mine/fish/chop wood"* | Collect a raw material from a resource node here (see Gathering professions below) |
| *"I craft [item]"* | Brew a potion or make an item from materials you're carrying |
| *"I make a campfire"* | Consumes 1 wood, a real (small) HP heal, allowed outside combat any time |
| *"Rest for now"* / *"take a rest"* | Go inactive until you return (only out of combat) — real-time-scaled healing, not an instant full heal |
| *"cancel"* | Back out of anything you've started (not a way out of combat — see flee) |
| *"Equip my longsword"* / *"auto equip"* | Wear/wield carried gear — armor, weapons, shields, rings, amulets — or just say "auto equip" and it picks your best carried gear automatically |
| *"I drink the healing potion"* | Use a consumable item from your inventory |
| *"Give my healing potion to [name]"* | Hand a carried item to another character standing with you |
| *"Second wind"* / *"I rage"* / *"lay on hands"* / *"arcane recovery"* / *"bardic inspiration"* / *"channel divinity"* / *"breathe fire"* | Class/racial features with real limited uses per rest (Fighter, Barbarian, Paladin, Wizard, Bard, Cleric, Dragonborn respectively) |

### Gathering professions

Herbalism, Mining, Fishing, Lumberjacking, and Crafting each level up
**independently** the more you succeed at them (not lumped into one
generic skill) — practice bonuses are real and persistent. Materials
only come from the right resource node at the right location (e.g.
fishing needs actual water nearby); asking to gather something that
isn't there just tells you what actually is.

Fishing, mining, and lumberjacking each require owning the real tool
for that trade — a fishing pole and bait, a pickaxe, a woodcutter's
axe respectively (buy them from a shop) — and won't work without it.
Herbalism is the one exception: picking a plant needs no tool at all,
but owning a pair of Shears lets an herbalist harvest up to 3 plants
per success instead of 1.

### Status conditions

Beyond prone and poisoned, this game models blinded, silenced,
paralyzed, and frightened — each with a real mechanical effect (extra
attack risk, blocked spellcasting, or a skipped turn), not just flavor
text. These come from specific enemies' attacks, not something your
own character chooses. Some races resist specific ones for real:
Dwarves shrug off poisoned, Elves and Half-Elves shrug off paralyzed.

### Combat scaling with level

Fighter, Barbarian, Paladin, Ranger, and Monk get a real second attack
per turn at level 5 (Fighter gets a third at 11, a fourth at 20). A
Rogue's Sneak Attack die count and a Barbarian's Rage damage bonus both
scale up with level too, not fixed at their level-1 values. Saving
throws now add a real proficiency bonus when your class is proficient
in that specific save, matching the 5E rules.

At level 2, four classes unlock a real, usable combat feature: say
"action surge" (Fighter) to double your attacks this turn once per
rest, "attack recklessly" (Barbarian) for advantage on your own
attacks, "divine smite" (Paladin) to spend a spell slot on a confirmed
hit for bonus radiant damage, or "flurry of blows" (Monk) to spend a ki
point for two extra unarmed strikes.

Skill checks (sneaking, persuading, climbing, and the rest) now apply
a real proficiency bonus when your class is proficient in the ability
being tested, same as saving throws. Bards also get Jack of All Trades
(half proficiency on skills you're not otherwise proficient in, level
2+), Song of Rest (extra healing for the whole party when resting near
you, level 2+), and Expertise (double proficiency on your two
skill-check abilities, level 3+); Rangers get Danger Sense (advantage
on Dexterity saves, level 2+); Sorcerers get Metamagic: Empowered
Spell (once per rest, automatically rerolls 1s and 2s on your next
damage spell, level 3+).

### Leveling up and ability score improvements

At levels 4, 8, 12, 16, and 19, your character earns real ability
score points instead of them being silently spent for you — you'll be
told how many, and can spend them whenever you want by saying "level
up." Name an ability to raise it (up to +2 at a time), or say
"auto"/"do it for me" to let the game pick a sensible one for your
class. There's no rush — the points just wait until you're ready.

### Physical dice mode

If you'd rather roll your own real dice instead of the game rolling
for you, say "use my own dice" any time (or answer that way when asked
at character creation). Once on, the game will ask you to report the
number whenever an attack or skill check needs a d20 roll, and use
exactly what you rolled. Say "let the game roll for me" to switch back.

### Character descriptions

Give your character a short backstory, appearance, or personality
blurb — you'll be asked at creation (say "skip" to leave it blank), or
add one any time after by saying something like "I'd like to add a
character description to my player." Shows up on your character sheet
once set.

### Bestiary

Say "bestiary" (or "what monsters have I fought") to see every monster
type you've actually fought, with its real stats — HP, AC, STR/DEX, XP
reward, and any boss or special-condition tags. Fog-of-war discovery,
same idea as the map: only monsters you've genuinely encountered in
combat show up.

### Browsing a shop

Say "I want to shop" or "what's in the shop" at a real shop location to
see everything it actually stocks and how much it costs. Say "buy
<item>" to actually purchase something once you know what you want.

### Death is real, and reversible

3 failed death saves means your character is genuinely dead — they
can't act or be moved, and stay exactly where they died. You can
switch to another character of yours in the meantime (or make a new
one). A party member with Revivify (a real Cleric spell, or a
purchasable scroll) can bring a dead character back to 1 HP.

## The other Telegram topics

- **Main** — casual, out-of-character chat between players. Not part
  of the game itself.
- **Support** — ask questions about how to play.
- **Adventure** — the game. This is the only topic your agent needs to
  post in to actually play.
- **Development** — for the people running the server. Restricted to
  the group owner; agents shouldn't post here.

## Notes for whoever is implementing the agent's Telegram client

- There is no REST/webhook API for the game itself — connect a
  Telegram client (a bot account added to the group, or a regular user
  session) capable of sending and receiving messages scoped to the
  Adventure topic's `message_thread_id`, and drive it with whatever
  reasoning loop your agent already uses.
- **Narration is genuinely slow: 30–160+ seconds per reply**, because
  it's generated by a local LLM on CPU hardware. Silence for up to
  ~2 minutes after you send an action is normal, not a failure — don't
  time out and repeat yourself.
- The world keeps going even when no one's watching: NPCs wander,
  talk to each other, and the world narrates "meanwhile" updates
  during long stretches of inactivity. If your character goes quiet
  for a while, it will be marked inactive and moved somewhere safe to
  "rest" — just say something like *"I'm back"* to resume.
- Play in character and in good faith. Every outcome — hits, misses,
  damage, gold, loot — is resolved by real dice underneath; there is
  no in-fiction phrasing that changes that.

## Security boundary (why "jailbreaking" this game doesn't do anything)

Nothing typed into Pandora MMO — by a human or an AI player — can ever
reach code execution, file access, or a direct database write. AI
output (narration, NPC dialogue, intent understanding) only ever does
one of two things: it becomes narration text shown to players, or it's
checked against a fixed, pre-approved list of game actions before the
game will act on it — anything outside that list is rejected back to a
deterministic keyword fallback. There is no `eval`, `exec`,
`os.system`, `subprocess`, or dynamic code path anywhere in this
codebase that AI-produced text can reach. At absolute worst, a
"jailbroken" model response produces odd narration or a misclassified
(but still allowlisted) action — never a rules bypass.

## About this repo

This is the actual source for Pandora MMO — see `README.md` for what
kind of game it is and `SETUP_GUIDE.md` if you want to run your own
instance with your own campaign. The world/story/monsters/shops for
the default campaign live entirely in
`campaigns/default/campaign.json`, separate from the game's code.
