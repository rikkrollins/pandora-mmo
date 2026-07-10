#!/usr/bin/env python3
"""
scripts/generate_item.py
CLI for rules/item_generator.py — rolls a procedural tiered weapon or
armor piece and prints it. Pure dice-and-data, no AI involved.

Usage:
    python3 scripts/generate_item.py                              # random weapon, random tier
    python3 scripts/generate_item.py --type armor                  # random armor, random tier
    python3 scripts/generate_item.py --base longsword --tier rare   # specific base + tier
    python3 scripts/generate_item.py --count 5                      # roll several at once
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rules.item_generator import ARMOR_BASES, TIERS, WEAPON_BASES, generate_item

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Roll a procedural tiered weapon or armor piece.")
    parser.add_argument("--type", choices=["weapon", "armor"], default="weapon")
    parser.add_argument("--base", choices=list(WEAPON_BASES.keys()) + list(ARMOR_BASES.keys()), default=None)
    parser.add_argument("--tier", choices=TIERS, default=None)
    parser.add_argument("--count", type=int, default=1)
    args = parser.parse_args()

    for _ in range(args.count):
        item = generate_item(args.type, base_id=args.base, tier=args.tier)
        print(json.dumps(item, indent=2))
