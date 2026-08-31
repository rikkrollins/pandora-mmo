#!/usr/bin/env python3
"""
scripts/audit_dungeon.py

Runs rules/dungeon_audit.py's real checks against one dungeon (or
every real dungeon in campaign.json) and prints a plain report. Never
writes anything -- read-only, matching the checker's own "diagnose,
don't mutate" role. Reach for this first when investigating a reported
dungeon design issue, or after building/editing a dungeon, before
manual inspection.

Usage:
    python3 scripts/audit_dungeon.py wrathflame_vault
    python3 scripts/audit_dungeon.py --all
    python3 scripts/audit_dungeon.py --graph wrathflame_vault
"""
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
CAMPAIGN_PATH = REPO_ROOT / "campaigns" / "default" / "campaign.json"

from rules import dungeon_audit  # noqa: E402


def _all_dungeon_ids(campaign: dict) -> list[str]:
    # campaign["locations"] is nested one level deep by layer
    # (surface/underground/sky) -- flatten before scanning for dungeon_id.
    ids = set()
    for layer_locations in campaign.get("locations", {}).values():
        for loc in layer_locations.values():
            if loc.get("dungeon_id"):
                ids.add(loc["dungeon_id"])
    return sorted(ids)


def _report_one(campaign: dict, dungeon_id: str) -> bool:
    """Prints one dungeon's report, returns True iff every check passed."""
    results = dungeon_audit.audit_dungeon(campaign, dungeon_id)
    all_passed = True
    print(f"\n=== {dungeon_id} ===")
    for check_name, failures in results.items():
        if failures:
            all_passed = False
            print(f"  [FAIL] {check_name}")
            for f in failures:
                print(f"         - {f}")
        else:
            print(f"  [ OK ] {check_name}")
    return all_passed


def main() -> None:
    args = sys.argv[1:]
    campaign = json.loads(CAMPAIGN_PATH.read_text())

    if "--graph" in args:
        args.remove("--graph")
        if not args:
            print("Usage: python3 scripts/audit_dungeon.py --graph <dungeon_id>")
            sys.exit(1)
        print(dungeon_audit.dump_dungeon_graph(campaign, args[0]))
        return

    if "--all" in args:
        dungeon_ids = _all_dungeon_ids(campaign)
    elif args:
        dungeon_ids = args
    else:
        print(f"Real dungeons in this campaign: {', '.join(_all_dungeon_ids(campaign))}")
        print("\nUsage: python3 scripts/audit_dungeon.py <dungeon_id> [<dungeon_id> ...]")
        print("       python3 scripts/audit_dungeon.py --all")
        print("       python3 scripts/audit_dungeon.py --graph <dungeon_id>")
        sys.exit(1)

    all_passed = True
    for dungeon_id in dungeon_ids:
        if not _report_one(campaign, dungeon_id):
            all_passed = False

    print()
    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
