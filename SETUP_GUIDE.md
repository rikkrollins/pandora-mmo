# Pandora MMO — Setup & Play Guide

A fully natural-language 5th-edition-style tabletop multiplayer game for your Telegram
group. No slash commands required — just talk like you're playing at
a table. Includes an AI Dungeon Master, AI NPCs, AI-controlled party
companions, a backpack and shop system, spellcasting, guilds, a
three-layer world (surface, underground, sky), and a hidden campaign
storyline that unfolds as you explore.

## 1. Install (5–10 minutes)

```bash
unzip pandora_mmo.zip
cd pandora_mmo
cp .env.example .env
```

Edit `.env` and set your real bot token (get one from **@BotFather** in
Telegram if you don't have one):
```
BOT_TOKEN=your-real-token-here
```
Everything else in `.env` is already correct for a group with the
standard Main / Support / Adventure / Development topic IDs — only
change those if your topics use different thread IDs. (You don't
actually have to hunt down and edit these by hand at all, though —
see "Automatic topic setup" below.)

```bash
pip install -r requirements.txt --break-system-packages
python3 bot.py
```

You should see:
```
... BotApplication created successfully. Starting polling...
```

Make sure the bot account is actually added as a **member** of your
Telegram group (Group Settings → Add Member) — creating it in
BotFather alone isn't enough.

### Automatic topic setup

The moment the bot is added to a group as a member, it sends a welcome
message and walks an admin through a one-time setup conversation:
whether to create the Adventure/Support topics for you automatically
(if Telegram's Topics/forum mode is on and the bot has been made an
admin with "Manage Topics" permission), or manual step-by-step
instructions otherwise. It then asks whether this group should run its
own separate **Private World** or join the shared **Public World** at
the official group instead. Under the hood this is the same
`/set_topic <main|support|adventure>` command you can also run
yourself, from inside any real topic, at any time — no `.env` editing
needed for any of it. The one topic this can never set up for a group
other than the official one is **Development**: that's the official
group's own exclusive maintainer channel, not a per-group option.

## 2. How to play

Everything happens in the **Adventure** topic, entirely in plain
English. There are no commands to memorize. Some examples of things
you can just say:

- *"I want to create a character"* — starts character creation
  (name, race, class, roll and assign ability scores). You'll get an
  automatic, in-character welcome describing exactly where you are —
  no guessing what to do next.
- *"Look around"* / *"Where am I?"* — describes your current location
- *"I head to the market"* / *"I descend into the well"* — moves you
  to a connected location
- *"What am I carrying?"* — shows your backpack
- *"Who's in my party?"* — shows exactly who's adventuring with you
  and how many members total
- *"I want to buy a healing potion"* — purchases from a shop, if
  you're standing in one
- *"Sell my rusty dagger"* — sells an item from your backpack
- *"Let's start a fight"* / *"I attack the goblin"* — combat, using
  monsters native to wherever you currently are. Every message shows
  a clear round number and whose turn is next.
- *"Let's fight 3 goblins"* / *"fight some goblins"* — starts a
  multi-enemy encounter; each enemy is numbered (Goblin 1, Goblin 2...)
  so you can target a specific one: *"I attack Goblin 2"*
- *"I try to sneak past the guards"* / *"I attempt to persuade the
  merchant"* / *"I try to lift the fallen beam"* — non-combat skill
  checks, rolled against a real DC using the right ability score
- *"I shove the goblin"* / *"I try to knock it prone"* — a real
  contested Strength check; success knocks the target prone (attacks
  against a prone target get advantage; a prone attacker has
  disadvantage on their own attacks)
- *"I cast fireball at the goblin"* — casts a spell you know
- *"[NPC name], join us"* — recruits an eligible NPC into your party
  as a real, fighting companion
- *"I want to join the Adventurers' Guild"* — joins a guild, if eligible
- *"I rest"* — recovers a fallen character's HP once combat has ended
- *"cancel"* / *"stop"* / *"reset"* — universal escape hatch: backs
  you out of character creation, or force-ends a combat session that
  seems stuck, at any time
- Just talking to an NPC by name (e.g. mentioning **Grimsby** or
  **Old Maren**) — starts an in-character conversation with them

A few optional slash-command shortcuts still exist
(`/newcharacter`, `/startcombat`, `/attack`, `/endturn`, `/sheet`) if
anyone prefers typing a command over a full sentence — both do exactly
the same thing under the hood.

**Development** and **Support** are also fully conversational:
- **Development** — restricted to the Telegram group's actual owner
  (checked live via Telegram's own "creator" role, not a hardcoded ID).
  Ask it anything about troubleshooting, the codebase, or planning new
  features. It's read-only and conversational — it can't run commands
  or edit files on its own.
- **Support** — open to everyone. Ask anything about how to play, what
  an item or spell does, or how any game mechanic works. It's built to
  never give hints, strategy advice, or story spoilers — only explain
  mechanics and interface, honestly and directly.

## 3. AI-controlled party companions

Add an AI-controlled character that plays alongside real players
automatically (acts on its own turn in combat, no player needed):

```bash
python3 -c "
import db
db.init_db()
db.create_ai_companion(
    name='Finn', race='Halfling', char_class='Rogue',
    ability_scores={'strength': 10, 'dexterity': 17, 'constitution': 12,
                     'intelligence': 13, 'wisdom': 10, 'charisma': 14},
    hp_max=8, armor_class=14, gold=100,
    inventory={'shortsword': 1, 'longbow': 1},
)
"
```

Run that once from inside the `pandora_mmo` folder, and the companion
joins every future combat encounter automatically.

## 4. Adding your own custom campaign

The entire world — locations, quests, shops, NPCs, monsters, and the
story arc — lives in one JSON file per campaign, not in code. To add a
new campaign:

1. Create a new folder: `campaigns/my_campaign/`
2. Add `campaigns/my_campaign/campaign.json`, following the same
   structure as `campaigns/default/campaign.json` (locations grouped by
   layer, quests, shops, npcs, monsters, story_arcs)
3. In `bot.py`, change this line near the top:
   ```python
   ACTIVE_CAMPAIGN_ID = "default"
   ```
   to:
   ```python
   ACTIVE_CAMPAIGN_ID = "my_campaign"
   ```
4. Restart the bot.

Items, spells, guilds, and abilities are shared across all campaigns
(defined in `items.py`, `spells.py`, `guilds.py`) — add new ones there
any time; they're plain Python dictionaries.

## 5. What's real vs. what to expect

Everything in the rules engine — dice, combat, damage, healing,
leveling, shop transactions, backpack math, guild eligibility, world
navigation — was actually run and verified against real test scenarios
while building this, not just written and assumed to work.

**One honest limitation:** the AI narration (the Dungeon Master's prose,
NPC dialogue, and the natural-language understanding of what you
type) all depend on your local Ollama model — `lfm2.5-thinking:latest`
handles both understanding what you mean and narration/NPC voice.
This build environment had no live Ollama instance to
test against, so I verified the *fallback* behavior (plain-template
responses if a model call fails) thoroughly, but the actual prose
quality and natural-language accuracy on your specific models is
untested until you run it for real. If the intent recognition ever
misfires on a phrasing, the optional slash commands are always there
as a reliable fallback.

## 6. Known current limitations

- Multiple enemies per fight now work, but targeting is name-based
  (say the enemy's exact label, e.g. "Goblin 2") — there's no menu or
  numbered-list picker.
- The backpack system tracks quantity but not carry weight limits yet.
- "Rest" is a simple full-heal outside combat (including spell slots),
  not full 5E short/long rest rules (hit dice spending, per-rest-type
  recovery differences) — a reasonable stopgap, not a complete
  implementation.
- Two players acting in the same chat at the same moment are correctly
  serialized (no corrupted turn order), and combat correctly resolves
  even in edge cases like a stabilized-but-unconscious player facing
  an enemy with no other target (a real stalemate-detection fix).
- Only two conditions are implemented: **prone** (via shoving) and
  **poisoned** (inflicted by certain monsters, like spiders) — both
  have real mechanical effects on attack rolls (advantage/disadvantage),
  but conditions reset when combat ends rather than persisting via
  more complex duration tracking.
- **Reactions (Shield, Counterspell, opportunity attacks) are not
  implemented.** This isn't an oversight — the combat engine resolves
  an attack roll and its damage as one atomic step, and doing
  reactions properly (deciding whether to react *after* seeing the
  attack roll, without re-rolling or risking a damage-tracking bug)
  needs real architectural changes to that core resolution step. Rather
  than rush it, it's deliberately left for a future, more careful pass.
- Non-combat skill checks use one fixed DC (13) for every situation,
  rather than a DM dynamically picking Easy/Medium/Hard per context —
  a deliberate simplification to keep outcomes fair and predictable
  rather than having the AI invent difficulty numbers on the fly.
- The Development and Support AI assistants are conversational only —
  they can't read live logs, files, or run diagnostics on their own;
  they only know what's in their system prompt plus what you tell them
  in the conversation.
- Lockable chests/doors and defeated named world-NPCs (e.g. a beaten
  bandit captain) are tracked in-memory, not the database — like
  combat conditions, this world state resets on a bot restart rather
  than needing its own schema.
- Ambient world-NPC encounters (friendly small talk, or a hostile NPC
  provoking a fight) are a flat per-arrival chance
  (`bot.AMBIENT_NPC_ENCOUNTER_CHANCE`), not aware of pacing, recent
  encounters, or story context.
- Faction standing and NPC affinity are simple integer scores with a
  few fixed consequence hooks (theft, defeating a faction's member,
  ordinary conversation) — there's no broader simulation of factions
  acting on their own initiative between player actions.

All of this is built to be extended — the data-driven campaign system
in particular means most future content (new areas, monsters, items,
quests) can be added without touching the Python code at all.

Have fun exploring — I won't spoil what you find.
