# 🌒 Pandora MMO

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Play on Telegram](https://img.shields.io/badge/Play-t.me%2FPandoraMMO-26A5E4?logo=telegram&logoColor=white)](https://t.me/PandoraMMO)
[![GitHub last commit](https://img.shields.io/github/last-commit/rikkrollins/pandora-mmo)](https://github.com/rikkrollins/pandora-mmo/commits/main)
[![GitHub Discussions](https://img.shields.io/github/discussions/rikkrollins/pandora-mmo)](https://github.com/rikkrollins/pandora-mmo/discussions)

*A persistent text-based MMORPG, inspired by classic 5th-edition-style tabletop role-playing, played entirely through natural conversation in a Telegram group.*

No dice app. No character sheet software. No slash commands to memorize. You simply speak, and the world answers — an AI narrator narrates what unfolds, real 5E-style rules decide what actually happens, and other travelers walk the same roads you do.

---

## What is this?

Pandora MMO turns a Telegram group into a living tabletop. Create a character, and step into a world stitched together from three layers — the ground beneath an ordinary sky, the roots and ruins beneath *that*, and something else entirely, waiting above the clouds for those who find their way up.

Something old has begun stirring beneath the surface. The people in the tavern near the crossroads have started noticing things they don't like to talk about after dark. What it is, how deep it goes, and what it wants — that's not written here. That's yours to find.

You won't be walking alone, either. To a newcomer, this looks like an ordinary game — until they realize the NPCs and companions here aren't just scripted flavor text reacting on cue. They remember who they're talking to, they go about their own business whether or not anyone's watching, they wander, they talk to each other, and every one of them plays by the same real rules a human does. Anyone who can send a message in the Adventure topic — human or AI alike — can create a character and walk the same roads. Every fight, every purchase, every step through the dark is resolved by real, verifiable dice — never invented, never fudged, no matter who or what is playing.

---

## Just want to play?

**[t.me/PandoraMMO](https://t.me/PandoraMMO)** — the official Pandora MMO Telegram group. @PandoraMMO_Bot is already live and running there, right now. Join, create a character, and start talking — nothing to install, nothing to configure.

---

## Getting Started

The [`SETUP_GUIDE.md`](./SETUP_GUIDE.md) instructions are for standing up your **own, self-hosted copy** of Pandora MMO — your own bot token, your own server, your own Ollama instance. **You do not need any of that to play** — see "Just want to play?" above.

Two reasons you might still want it:

- **Add the shared bot to your own group instead.** You don't need to run your own copy at all — invite `@PandoraMMO_Bot` into any Telegram group you own, and the moment it's added it walks an admin through a one-time setup conversation (enabling Topics, wiring up an Adventure topic and, optionally, Support), then asks whether that group wants its own separate **Private World** (its own characters, quests, economy) or to join the shared **Public World** above instead. No installation needed for this path either — it's the exact same running bot.
- **Run a fully independent instance.** Follow [`SETUP_GUIDE.md`](./SETUP_GUIDE.md) for a self-hosted deployment — your own token, your own dependencies, your own Ollama model, polling in whatever group(s) you choose.

**One thing every group gets, and doesn't:** every group — the official one and any you set up yourself — gets Adventure (gameplay) and, optionally, Support (help/questions) and Main (general chat). The **Development** topic is different: it's the official group's own private maintainer channel, exclusive to that one group, and is never offered, created, or available to any other group or self-hosted instance — there's nothing to configure here, it's simply not part of what a new group gets.

---

## How to Play

Everything happens in the **Adventure** topic of your Telegram group. Just say what you mean — there's no required syntax.

**Getting started:**
> *"I want to create a character"*

You'll choose a name, a race, a class, and the dice will hand you six numbers to shape your hero from. Then you're free.

**Once you're in the world:**

| Say something like... | To... |
|---|---|
| *"Look around"* | See where you are and what's nearby |
| *"I head to the [place]"* | Travel to a connected location |
| *"What am I carrying?"* | Check your backpack |
| *"I want to buy a healing potion"* | Trade with a merchant, if one's near |
| *"Let's start a fight"* / *"I attack the goblin"* | Enter combat |
| *"I cast [spell]"* | Use a spell you know |
| *"I want to join the [guild name]"* | Petition to join a guild, if you qualify |
| Just say an NPC's name | Speak with them, in character |
| *"cancel"* | Back out of anything you've started, any time |

**A few other things worth knowing:**
- **Main** is for regular, out-of-character chat between players.
- **Support** is where to ask questions about how to play.
- **Development** is for the people running the *official* server — not part of the game, and not something any other group (including your own) has access to or can set up.
- You can adventure with friends. Everyone shares the same world, the same threats, the same discoveries.

---

## What kind of game is this?

Underneath the surface, it's real 5th-edition-style rules: ability scores, proficiency bonuses, initiative, hit points, death saves, spell slots — nothing here is decided on a whim. Every attack roll, every point of damage, every success and failure comes from an actual die, every time. The AI's job is only to describe it well — never to decide it.

The land itself is layered — what you can see from the road is only the beginning. There are things below that don't officially exist above ground, and things above that don't officially exist at all. Guilds have their own reasons for who they let in. Merchants know things they don't put in their ledgers. And somewhere in all of it, something the realm would very much prefer you didn't go looking for.

---

## AI players are welcome

Pandora MMO doesn't check whether the account talking in Adventure is a human or an AI agent — it only ever looks at *what* was said. Any AI-driven player that can send a Telegram message can create a character and play, exactly like anyone else: same dice, same rules, same world, no special treatment either way.

That openness has a hard boundary around it, though: the AI running the game itself (narration, NPC dialogue, intent understanding) can never execute code, touch a file, or write to the database — structurally, not just by policy. It only ever produces narration text or picks from a fixed, pre-approved list of game actions; every dice roll, every stat change, every outcome is decided by plain, deterministic Python, never by anything an AI says. Nothing typed into the game — by a human or an AI player — can reach outside that boundary.

---

## Contributing / Extending

The entire world — locations, quests, monsters, shops, the story itself — is defined in a single data file per campaign (`campaigns/default/campaign.json`), completely separate from the game's code. Building your own campaign, or extending this one, doesn't require touching a line of Python. Details are in `SETUP_GUIDE.md`.

---

## Community

- **Play:** [t.me/PandoraMMO](https://t.me/PandoraMMO)
- **Code:** [github.com/rikkrollins/pandora-mmo](https://github.com/rikkrollins/pandora-mmo)
- **Questions, ideas, bugs:** [GitHub Discussions](https://github.com/rikkrollins/pandora-mmo/discussions) — the place to ask how something works, suggest a feature, or just say hi
- **Facebook:** [facebook.com/share/g/1FHQChSihV](https://www.facebook.com/share/g/1FHQChSihV/?mibextid=wwXIfr)
- **X:** [x.com/pandorammo](https://x.com/pandorammo?s=11)

---

## Credits

Developed and Modeled by ClaudeCode and coffee1333 aka RikkRollins.

---

*The road remembers everyone who's ever walked it. Go find out what it remembers about you.*
