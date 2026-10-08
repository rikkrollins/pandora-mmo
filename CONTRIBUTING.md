# Contributing to Pandora MMO

Thanks for considering a contribution. A few ground rules to keep
things easy for everyone:

## Reporting a bug

Open an issue describing what you did, what you expected, and what
actually happened. A log excerpt or a screenshot helps a lot. If it's
something you can reproduce reliably, say how.

## Suggesting a feature

Open an issue. Short description of the idea and why it'd help is
plenty — no template required.

## Submitting a change

1. Fork the repo and branch off `main`.
2. Make your change.
3. **Add a real test.** This project's own discipline (see `CLAUDE.md`)
   is that every change is verified with an actual executed test —
   real dice rolls, real database reads, real simulated handler calls
   — never just "read the code and assume it works." `tests/
   test_regression.py` has hundreds of examples to follow the pattern
   of. Point `DB_PATH` at a throwaway file under `tests/tmp/` — never
   test against a real/production database.
4. Run the relevant tests and confirm they pass before opening a PR.
5. Open a pull request describing what changed and why.

## Content additions (new campaign content, quests, items, etc.)

The whole game world lives in `campaigns/default/campaign.json`,
separate from the Python code — see the "Contributing / Extending"
section of `README.md` and `SETUP_GUIDE.md` for how that's structured.
You don't need to touch a line of Python to add new locations, quests,
or monsters.

## Code style

Match the surrounding code. This project leans toward explaining the
**why** behind a non-obvious decision in a comment (often with the
real bug report or request that drove it), not restating what the
code already says.

## Questions

Open a GitHub Discussion, or join the game itself and ask in the
Development topic (owner-only posting, but readable) — see `README.md`
for the invite link.
