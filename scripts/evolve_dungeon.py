#!/usr/bin/env python3
"""
scripts/evolve_dungeon.py

Phase 3 of the dungeon toolkit: runs rules/dungeon_evolve.py's real
RNG-driven generator to produce a NEW, harder variant dungeon alongside
an existing one, validates it against rules/dungeon_audit.py's real
checks, and (only with --apply) writes the result into
campaigns/default/campaign.json. Dry-run by default, matching
scripts/build_location_grid.py's own --apply-optional convention --
inspect the printed graph and reroll with a different --seed before
ever touching the real file.

Usage:
    python3 scripts/evolve_dungeon.py wrathflame_vault wrathflame_vault_evolved \
        --layer underground --rebirth-gate 1 --target-band 35,50 --seed 42

    python3 scripts/evolve_dungeon.py goblin_warrens goblin_warrens_evolved \
        --layer underground --rebirth-gate 2 --seed 7 --apply
"""
import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
CAMPAIGN_PATH = REPO_ROOT / "campaigns" / "default" / "campaign.json"

import random  # noqa: E402

from rules import dungeon_audit, dungeon_evolve  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source_dungeon_id")
    parser.add_argument("new_dungeon_id")
    parser.add_argument("--layer", required=True, choices=["surface", "underground", "sky"])
    parser.add_argument("--rebirth-gate", type=int, required=True, help="requires_rebirth_count on the new entrance")
    parser.add_argument("--target-band", default=None, help="MIN,MAX -- required for a dungeon with no owning arc, or the last banded chapter")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--display-name", default=None)
    parser.add_argument("--apply", action="store_true", help="write the result back to campaign.json (default: dry-run, report only)")
    args = parser.parse_args()

    target_band = None
    if args.target_band:
        lo, hi = args.target_band.split(",")
        target_band = (int(lo), int(hi))

    campaign = json.loads(CAMPAIGN_PATH.read_text())
    rng = random.Random(args.seed) if args.seed is not None else random.Random()

    try:
        summary = dungeon_evolve.evolve_dungeon(
            campaign, args.source_dungeon_id, args.new_dungeon_id, args.layer, args.rebirth_gate,
            target_band=target_band, rng=rng, display_name=args.display_name,
        )
    except (ValueError, RuntimeError) as e:
        print(f"evolve_dungeon failed: {e}")
        sys.exit(1)

    print(dungeon_audit.dump_dungeon_graph(campaign, args.new_dungeon_id))
    print()
    print(f"Summary: {summary}")

    checks = dungeon_audit.audit_dungeon(campaign, args.new_dungeon_id)
    print()
    for check_name, failures in checks.items():
        print(f"  [{'FAIL' if failures else ' OK '}] {check_name}")
        for f in failures:
            print(f"         - {f}")

    if not args.apply:
        print("\nDry run only -- no file written. Re-run with --apply once you're happy with this roll.")
        return

    CAMPAIGN_PATH.write_text(json.dumps(campaign, indent=2) + "\n")
    print(f"\nWritten to {CAMPAIGN_PATH}")


if __name__ == "__main__":
    main()
