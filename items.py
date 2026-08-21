"""
items.py
The master item catalog. Every item players can carry, buy, sell, or
find is defined here as data — adding a new item never requires
touching bot.py or the rules engine.

Item categories: weapon, armor, shield, consumable, scroll, ring,
amulet, wondrous, quest_item, material.
"""
import copy
import re

ITEMS = {
    # --- Weapons ---
    # weapon_category (task #223, per Coffee): "simple" or "martial" --
    # real classification for THIS game's own small weapon catalog, not
    # a byte-for-byte copy of the full 5E weapon table (which has many
    # weapons this game doesn't). See class_features.py's
    # WEAPON_PROFICIENCIES for which classes are proficient with which.
    # damage_type (2026-07-24): defaults to "physical" everywhere it's
    # read (see rules/combat.py's _apply_damage_type_modifier), so every
    # weapon below behaves exactly as before unless it's one of the two
    # that already had a real elemental/material identity in its flavor
    # note -- flametongue_shortsword's fire and silvered_dagger's silver
    # were previously pure flavor text with zero mechanical effect; both
    # are now real.
    "rusty_dagger": {
        "name": "Rusty Dagger", "type": "weapon", "rarity": "common",
        "price": 2, "weight": 1, "damage_dice": "1d4", "ability": "dexterity",
        "weapon_category": "simple", "damage_type": "physical",
        "description": "Deals 1d4 physical damage. Pitted along the blade, the edge still holds well enough to do the job.",
    },
    "shortsword": {
        "name": "Shortsword", "type": "weapon", "rarity": "common",
        "price": 10, "weight": 2, "damage_dice": "1d6", "ability": "dexterity",
        "weapon_category": "simple", "damage_type": "physical",
        "description": "Deals 1d6 physical damage. A plain, honest blade — nothing about it stands out, and nothing about it fails you.",
    },
    "longsword": {
        "name": "Longsword", "type": "weapon", "rarity": "common",
        "price": 15, "weight": 3, "damage_dice": "1d8", "ability": "strength",
        "weapon_category": "martial", "damage_type": "physical",
        "description": "Deals 1d8 physical damage. Well-balanced steel, the kind every armory keeps in stock because it never stops selling.",
    },
    "greataxe": {
        "name": "Greataxe", "type": "weapon", "rarity": "common",
        "price": 30, "weight": 7, "damage_dice": "1d12", "ability": "strength",
        "weapon_category": "martial", "damage_type": "physical",
        "description": "Deals 1d12 physical damage. Heavy enough that swinging it wrong would hurt you as much as anything else.",
    },
    "longbow": {
        "name": "Longbow", "type": "weapon", "rarity": "common",
        "price": 50, "weight": 2, "damage_dice": "1d8", "ability": "dexterity",
        "weapon_category": "martial", "damage_type": "physical",
        "description": "Deals 1d8 physical damage. Strung tight, the wood still flexes like it was cut yesterday.",
    },
    "silvered_dagger": {
        "name": "Silvered Dagger", "type": "weapon", "rarity": "uncommon",
        "price": 75, "weight": 1, "damage_dice": "1d4+1", "ability": "dexterity",
        "note": "Effective against creatures vulnerable to silver.",
        "description": "Deals 1d4+1 silver damage. Effective against creatures vulnerable to silver.",
        "weapon_category": "simple", "damage_type": "silver",
    },
    "flametongue_shortsword": {
        "name": "Flametongue Shortsword", "type": "weapon", "rarity": "rare",
        "price": 0, "weight": 2, "damage_dice": "1d6+2", "ability": "dexterity",
        "note": "Warm to the touch. Wreathes itself in fire when drawn in anger.",
        "description": "Deals 1d6+2 fire damage. Warm to the touch. Wreathes itself in fire when drawn in anger.",
        "weapon_category": "martial", "damage_type": "fire",
    },
    "the_last_word": {
        "name": "The Last Word", "type": "weapon", "rarity": "legendary",
        "price": 0, "weight": 3, "damage_dice": "2d8+3", "ability": "strength",
        "note": "The carving stops repeating itself the instant your hand closes around the hilt.",
        "description": "Deals 2d8+3 physical damage. The carving stops repeating itself the instant your hand closes around the hilt.",
        "weapon_category": "martial", "damage_type": "physical",
    },

    # --- Armor & Shields ---
    # armor_category (task #223): "light"/"medium"/"heavy", or "shield"
    # for the shield itself -- same real-consequence system as
    # weapon_category above (see class_features.py's ARMOR_PROFICIENCIES).
    "leather_armor": {"name": "Leather Armor", "type": "armor", "rarity": "common", "price": 10, "weight": 10, "ac_base": 11, "armor_category": "light", "description": "Boiled and cured stiff enough to turn a glancing blow, soft enough not to slow you down."},
    "chain_shirt": {"name": "Chain Shirt", "type": "armor", "rarity": "common", "price": 50, "weight": 20, "ac_base": 13, "armor_category": "medium", "description": "Rings of iron, close-linked and heavier than they look at a glance."},
    "chain_mail": {"name": "Chain Mail", "type": "armor", "rarity": "uncommon", "price": 75, "weight": 55, "ac_base": 16, "armor_category": "heavy", "description": "A real suit of it, head to knee — the kind of weight you stop noticing after the first hour."},
    "wooden_shield": {"name": "Wooden Shield", "type": "shield", "rarity": "common", "price": 10, "weight": 6, "ac_bonus": 2, "armor_category": "shield", "description": "Banded oak, scarred along the rim from blows that never got any further."},

    # --- Consumables ---
    # Potion healing (2026-07-25, per Coffee's evolution/HP-scaling pass):
    # rescaled from real dice rolls to guaranteed flat amounts (a d1
    # always rolls exactly 1, so "1d1+99" always totals exactly 100 --
    # no new schema needed) to match character HP now reaching into the
    # thousands/hundreds of thousands with rebirths (see rules/leveling.py's
    # EVOLUTION_HP_MULTIPLIER/rebirth_hp_max).
    "healing_potion": {
        "name": "Healing Potion", "type": "consumable", "rarity": "common",
        "price": 25, "weight": 0.5, "effect": "heal", "heal_dice": "1d1+99",
        "description": "Heals 1d1+99 HP (100 flat). A dull red liquid in a corked glass vial. Tastes worse than it works.",
    },
    "greater_healing_potion": {
        "name": "Greater Healing Potion", "type": "consumable", "rarity": "uncommon",
        "price": 100, "weight": 0.5, "effect": "heal", "heal_dice": "1d1+999",
        "description": "Heals 1d1+999 HP (1,000 flat). Thicker and darker than the common brew, and it goes down just as badly.",
    },
    "supreme_healing_potion": {
        "name": "Supreme Healing Potion", "type": "consumable", "rarity": "rare",
        "price": 500, "weight": 0.5, "effect": "heal", "heal_dice": "1d1+9999",
        "note": "Brewed for someone who's rebirthed more times than most people have leveled up.",
        "description": "Heals 1d1+9999 HP (10,000 flat). Brewed for someone who's rebirthed more times than most people have leveled up.",
    },
    "antitoxin": {
        "name": "Antitoxin", "type": "consumable", "rarity": "common",
        "price": 15, "weight": 0.1, "effect": "cure_poison",
        "description": "Cures poison. A thin, bitter tonic. Smells like it's already working before you drink it.",
    },
    # Per Coffee (2026-08-10): spell slots have no in-battle recovery
    # option at all in this game (real rule, see Support's deterministic
    # answer in ai/support_agent.py) -- these give casters a genuine
    # item-based alternative, same tiered Minor/Greater/Supreme pattern
    # as the healing potions above, distilled by The Arcane Circle (the
    # real spellcaster guild, see guilds.py) rather than a generic
    # unbranded "mana potion". restore_spell_slots_amount is read by
    # bot.py's _do_use_item; the top tier sets restore_all instead of a
    # fixed number.
    "spell_tonic": {
        "name": "Spell Tonic", "type": "consumable", "rarity": "common",
        "price": 40, "weight": 0.5, "effect": "restore_spell_slots", "restore_spell_slots_amount": 2,
        "description": "Restores 2 spell slots. A faintly glowing draught the Arcane Circle sells to apprentices who keep running dry mid-lesson.",
    },
    "greater_spell_tonic": {
        "name": "Greater Spell Tonic", "type": "consumable", "rarity": "uncommon",
        "price": 175, "weight": 0.5, "effect": "restore_spell_slots", "restore_spell_slots_amount": 5,
        "description": "Restores 5 spell slots. Brewed stronger, and it shows — the glow doesn't fade until well after the bottle's empty.",
    },
    "supreme_spell_tonic": {
        "name": "Supreme Spell Tonic", "type": "consumable", "rarity": "rare",
        "price": 700, "weight": 0.5, "effect": "restore_spell_slots", "restore_spell_slots_amount": 10,
        "description": "Restores 10 spell slots. The Arcane Circle only distills this for members they actually trust with it.",
    },
    "elixir_of_the_arcane_circle": {
        "name": "Elixir of the Arcane Circle", "type": "consumable", "rarity": "legendary",
        "price": 2500, "weight": 0.5, "effect": "restore_spell_slots", "restore_all": True,
        "note": "Fully restores every spell slot in a single swallow.",
        "description": "Fully restores every spell slot in a single swallow. The Arcane Circle brews vanishingly little of it, and never explains why.",
    },
    # Per Coffee (2026-07-24): "add items like tents and cabins and
    # houses to reviving and healing characters to full" -- a stronger
    # alternative to Revivify (which only restores 1 HP): these fully
    # heal AND revive, tiered by both price and how many party members
    # they reach at once (see effect="heal_and_revive" in _do_use_item).
    # Prices per Coffee, 2026-07-24 ("make tents 100, cabins 1000, and
    # houses 10000"): a steep, deliberate order-of-magnitude jump per
    # tier, not a gradual one.
    "tent": {
        "name": "Tent", "type": "consumable", "rarity": "rare",
        "price": 100, "weight": 10, "effect": "heal_and_revive", "revive_targets": 1,
        "note": "A night under real canvas mends more than a potion ever could.",
        "description": "Fully heals and revives 1 fallen party member to full HP. A night under real canvas mends more than a potion ever could.",
    },
    "cabin": {
        "name": "Cabin", "type": "consumable", "rarity": "rare",
        "price": 1000, "weight": 20, "effect": "heal_and_revive", "revive_targets": 3,
        "note": "Four walls and a hearth -- room enough for the whole party to actually rest.",
        "description": "Fully heals and revives up to 3 fallen party members to full HP. Four walls and a hearth -- room enough for the whole party to actually rest.",
    },
    "house": {
        "name": "House", "type": "consumable", "rarity": "legendary",
        "price": 10000, "weight": 50, "effect": "heal_and_revive", "revive_targets": None,
        "note": "A real roof over everyone's head. Whatever happened out there, it stays outside.",
        "description": "Fully heals and revives the entire party to full HP. A real roof over everyone's head. Whatever happened out there, it stays outside.",
    },
    "rations": {
        "name": "Rations (1 day)", "type": "consumable", "rarity": "common",
        "price": 2, "weight": 2, "effect": "none",
        "description": "Hardtack, dried meat, a little salt. Filling in the way that word technically means.",
    },
    "torch": {"name": "Torch", "type": "consumable", "rarity": "common", "price": 1, "weight": 1, "effect": "light", "description": "Pitch-wrapped wood, ready to catch. Burns longer than it has any right to."},
    "ale": {
        "name": "Mug of Ale", "type": "consumable", "rarity": "common",
        "price": 1, "weight": 1, "effect": "none",
        "note": "Grimsby's own brew. Doesn't heal anything, but it isn't meant to.",
        "description": "Grimsby's own brew. Doesn't heal anything, but it isn't meant to.",
    },

    # --- Scrolls (single-use spells for non-casters — or anyone, once bought) ---
    "scroll_magic_missile": {
        "name": "Scroll of Magic Missile", "type": "scroll", "rarity": "common",
        "price": 30, "weight": 0.1, "spell": "magic_missile",
        "description": "Deals 1d4+1 force damage (always hits). The ink shifts faintly on the page, like it's still deciding where to strike.",
    },
    "scroll_fireball": {
        "name": "Scroll of Fireball", "type": "scroll", "rarity": "rare",
        "price": 300, "weight": 0.1, "spell": "fireball",
        "description": "Deals 8d6 fire damage (Dexterity save for half). Warm to the touch even rolled up. Whoever wrote this one meant it.",
    },
    "scroll_cure_wounds": {
        "name": "Scroll of Cure Wounds", "type": "scroll", "rarity": "common",
        "price": 35, "weight": 0.1, "spell": "cure_wounds",
        "description": "Heals 1d8+2 HP. The handwriting is steadier than most healing scrolls bother to be.",
    },
    "scroll_revivify": {
        "name": "Scroll of Revivify", "type": "scroll", "rarity": "rare",
        "price": 350, "weight": 0.1, "spell": "revivify",
        "note": "Brings a fallen ally back from death itself, at real cost — not to be used lightly.",
        "description": "Revives a fallen ally at 5% of their max HP. Brings a fallen ally back from death itself, at real cost — not to be used lightly.",
    },
    "scroll_shield": {
        "name": "Scroll of Shield", "type": "scroll", "rarity": "uncommon",
        "price": 60, "weight": 0.1, "spell": "shield",
        "description": "A short scroll, meant to be read fast — there's never much time to spare when you need it.",
    },
    "scroll_bless": {
        "name": "Scroll of Bless", "type": "scroll", "rarity": "uncommon",
        "price": 55, "weight": 0.1, "spell": "bless",
        "description": "The kind of scroll a priest presses into your hand and tells you to save.",
    },
    "scroll_invisibility": {
        "name": "Scroll of Invisibility", "type": "scroll", "rarity": "rare",
        "price": 175, "weight": 0.1, "spell": "invisibility",
        "description": "The parchment is faintly hard to look straight at, even before it's read.",
    },
    "scroll_lightning_bolt": {
        "name": "Scroll of Lightning Bolt", "type": "scroll", "rarity": "rare",
        "price": 275, "weight": 0.1, "spell": "lightning_bolt",
        "description": "Deals 8d6 lightning damage (Dexterity save for half). It crackles faintly against your fingers, like it hasn't fully settled since it was written.",
    },
    "scroll_summon_spirit": {
        # Real live question (2026-08-20, Coffee: "do summoning scrolls
        # have anything to do with the remnants? if not we should
        # remove them so it doesnt confuse the players") -- confirmed
        # NOT related: this is the item form of the real, separate
        # "Summon Lesser Spirit" spell (spells.py, effect="summon"),
        # which calls a temporary spirit ally into the current fight --
        # a genuine, working 5E-style conjuration spell, not dead
        # content. ai/intent_parser.py already disambiguates a real
        # bound Remnant's name from this spell correctly, so there was
        # never a code-level collision -- but the old generic name
        # "Scroll of Summoning" gave a player zero hint it's a
        # completely different mechanic from Remnant summoning, so
        # renamed/re-described to make that clear rather than removing
        # a real, functioning item.
        "name": "Scroll of the Lesser Spirit", "type": "scroll", "rarity": "rare",
        "price": 200, "weight": 0.1, "spell": "summon_lesser_spirit",
        "rebirth_scales_price": True,
        "description": "Calls a lesser spirit to fight at your side for the rest of the battle. The final line of the ritual text is written smaller, like the scribe wasn't sure they should include it. The spirit answering this particular ritual is never much stronger than a level-25 fighter, whoever reads it.",
    },
    # Tiers II-IV of the same family (2026-08-21, per Coffee: "create
    # caps so each scroll can only summon up to a certain level... make
    # it so players dont have access to the higher/better scroll unless
    # we find, steal, or buy them at higher level areas"). Each one's
    # own spell (spells.py) carries the real max_summon_level cap this
    # description promises. rebirth_scales_price (shop.buy_item) means
    # the gold cost itself climbs with the buyer's own rebirth_count --
    # the "let them evolve too, more expensive" half of the request.
    "scroll_summon_spirit_ii": {
        "name": "Scroll of the Spirit", "type": "scroll", "rarity": "rare",
        "price": 800, "weight": 0.1, "spell": "summon_spirit",
        "rebirth_scales_price": True,
        "description": "A more complete ritual than the lesser version -- the ink is darker, the folds more deliberate. Calls a real spirit ally, capable up to roughly a level-50 fighter's own strength.",
    },
    "scroll_summon_greater_spirit": {
        "name": "Scroll of the Greater Spirit", "type": "scroll", "rarity": "epic",
        "price": 2500, "weight": 0.1, "spell": "summon_greater_spirit",
        "rebirth_scales_price": True,
        "description": "Not something Ossian Vane's shelves ever carried -- this one turned up on something that used to be dangerous. Calls a genuinely powerful spirit ally, capable up to roughly a level-75 fighter's own strength.",
    },
    "scroll_summon_elder_spirit": {
        "name": "Scroll of the Elder Spirit", "type": "scroll", "rarity": "legendary",
        "price": 6000, "weight": 0.1, "spell": "summon_elder_spirit",
        "rebirth_scales_price": True,
        "description": "The ritual text doesn't read like it was written for a mortal hand. Calls an elder spirit ally at nearly the full strength this kind of summoning allows.",
    },

    # --- Rings, Amulets, Wondrous Items ---
    "ring_of_protection": {
        "name": "Ring of Protection", "type": "ring", "rarity": "rare",
        "price": 0, "weight": 0, "ac_bonus": 1,
        "note": "A faint shimmer surrounds it.",
        "description": "A faint shimmer surrounds it.",
    },
    "amulet_of_health": {
        "name": "Amulet of Health", "type": "amulet", "rarity": "rare",
        "price": 0, "weight": 0, "constitution_set": 19, "regen_bonus": 1,
        "note": "Your vitality feels different the moment you put it on.",
        "description": "Your vitality feels different the moment you put it on.",
    },
    "cloak_of_elvenkind": {
        "name": "Cloak of Elvenkind", "type": "wondrous", "rarity": "uncommon",
        "price": 0, "weight": 1, "stealth_advantage": True,
        "description": "The color of it shifts a little depending on where you're standing, never quite matching what it's actually near.",
    },
    "boots_of_the_winterlands": {
        "name": "Boots of the Winterlands", "type": "wondrous", "rarity": "uncommon",
        "price": 0, "weight": 1, "note": "Cold never seems to trouble the wearer.",
        "description": "Cold never seems to trouble the wearer.",
    },
    "bracers_of_the_steady_hand": {
        "name": "Bracers of the Steady Hand", "type": "wondrous", "rarity": "uncommon",
        "price": 90, "weight": 1, "note": "Your hands never shake, even when the rest of you wants to.",
        "description": "Your hands never shake, even when the rest of you wants to.",
    },
    "lantern_of_true_sight": {
        "name": "Lantern of True Sight", "type": "wondrous", "rarity": "rare",
        "price": 220, "weight": 2, "note": "Its flame burns a color no ordinary fire does, and shows things exactly as they are, not as they'd rather look.",
        "description": "Its flame burns a color no ordinary fire does, and shows things exactly as they are, not as they'd rather look.",
    },
    "ring_of_the_undertow": {
        "name": "Ring of the Undertow", "type": "ring", "rarity": "uncommon",
        "price": 110, "weight": 0, "ac_bonus": 1,
        "note": "Cold as riverwater no matter how long it's worn.",
        "description": "Cold as riverwater no matter how long it's worn.",
    },
    # Enchanters' Guild commissions (2026-07-25 - 2026-08-11): 3 real,
    # equippable rewards, never shop-buyable (price 0, same convention
    # as Amulet of Health/Ring of Protection). Originally granted via a
    # random-roll "commission" mechanic that Coffee later replaced with
    # the real guild tier ladder (rules/crafting.py's ENCHANT_RECIPES) --
    # left in the catalog since existing characters may already own
    # one, just no longer a live drop source.
    "band_of_ember": {
        "name": "Band of Ember", "type": "ring", "rarity": "uncommon",
        "price": 0, "weight": 0, "regen_bonus": 1,
        "note": "Warm to the touch, like a coal that never quite goes out.",
        "description": "Warm to the touch, like a coal that never quite goes out.",
    },
    "sigil_of_the_deep": {
        "name": "Sigil of the Deep", "type": "amulet", "rarity": "rare",
        "price": 0, "weight": 0, "regen_bonus": 2, "ac_bonus": 1,
        "note": "Carved from something that was never meant to see the surface.",
        "description": "Carved from something that was never meant to see the surface.",
    },
    "crown_of_the_unmoored": {
        "name": "Crown of the Unmoored", "type": "wondrous", "rarity": "very_rare",
        "price": 0, "weight": 1, "regen_bonus": 2, "ac_bonus": 2,
        "note": "It doesn't quite sit still on your head, like it's listening for something.",
        "description": "It doesn't quite sit still on your head, like it's listening for something.",
    },

    # Hollow Verge dungeon rewards (2026-07-25, rebirth-1 gated content --
    # see campaign.json's hollow_verge_* locations/quests). Quest-only,
    # never shop-buyable, same price-0 convention as the guild commissions
    # above.
    "verge_ashbound_band": {
        "name": "Verge-Ashbound Band", "type": "ring", "rarity": "rare",
        "price": 0, "weight": 0, "regen_bonus": 2, "ac_bonus": 1,
        "note": "Ash that never finished falling, cooled into a ring shape and left to be found.",
        "description": "Ash that never finished falling, cooled into a ring shape and left to be found.",
    },
    "wardens_reprieve": {
        "name": "Warden's Reprieve", "type": "wondrous", "rarity": "very_rare",
        "price": 0, "weight": 1, "regen_bonus": 3,
        "note": "Whatever waited that long to be defeated leaves behind something that knows how to wait, too.",
        "description": "Whatever waited that long to be defeated leaves behind something that knows how to wait, too.",
    },

    # The Wordless Choir dungeon rewards (2026-07-25, rebirth-2 gated
    # content -- see campaign.json's wordless_choir_* locations/quests).
    # Quest-only, never shop-buyable, same convention as the Hollow
    # Verge rewards above -- a clear step up in power for a deeper gate.
    "echo_bound_signet": {
        "name": "Echo-Bound Signet", "type": "ring", "rarity": "rare",
        "price": 0, "weight": 0, "regen_bonus": 3, "ac_bonus": 1,
        "note": "Whatever it echoes back was never quite what you said.",
        "description": "Whatever it echoes back was never quite what you said.",
    },
    "the_undertones_hum": {
        "name": "The Undertone's Hum", "type": "wondrous", "rarity": "legendary",
        "price": 0, "weight": 1, "regen_bonus": 4, "ac_bonus": 2,
        "note": "It doesn't make a sound. You just stop, for a moment, being able to not hear it.",
        "description": "It doesn't make a sound. You just stop, for a moment, being able to not hear it.",
    },

    # The Unbegun dungeon rewards (2026-07-25, rebirth-3 gated content,
    # the true final boss -- see campaign.json's unmoored_isle_* /
    # the_unbegun_reckoning). Quest-only, never shop-buyable. The
    # Unbegun's Crown is deliberately the strongest item in the game.
    "hollow_watchers_eye": {
        "name": "Hollow Watcher's Eye", "type": "amulet", "rarity": "rare",
        "price": 0, "weight": 0, "regen_bonus": 3, "ac_bonus": 2,
        "note": "It sees exactly as well with its eyes closed.",
        "description": "It sees exactly as well with its eyes closed.",
    },
    "the_unbegun_crown": {
        "name": "The Unbegun's Crown", "type": "wondrous", "rarity": "legendary",
        "price": 0, "weight": 1, "regen_bonus": 5, "ac_bonus": 3,
        "note": "Worn by whatever was here before there was a story to tell about it.",
        "description": "Worn by whatever was here before there was a story to tell about it.",
    },

    # Hollow Verge expansion rewards (2026-07-25, per Coffee: "bigger
    # expansion" -- new mid-path/side-branch nodes added to the
    # existing rebirth-1 dungeon). Quest-only, same convention as every
    # other dungeon reward.
    "bound_wraiths_chain": {
        "name": "Bound Wraith's Chain", "type": "ring", "rarity": "rare",
        "price": 0, "weight": 0, "regen_bonus": 2,
        "note": "It was never really what was holding the wraith in place.",
        "description": "It was never really what was holding the wraith in place.",
    },
    "forgotten_nooks_trinket": {
        "name": "Forgotten Nook's Trinket", "type": "amulet", "rarity": "uncommon",
        "price": 0, "weight": 0, "ac_bonus": 1,
        "note": "Small enough to have been overlooked. That's probably why it survived.",
        "description": "Small enough to have been overlooked. That's probably why it survived.",
    },

    # Wordless Choir expansion rewards (2026-07-25, same "bigger
    # expansion" pass as the Hollow Verge rewards above).
    "resonance_bound_ring": {
        "name": "Resonance-Bound Ring", "type": "ring", "rarity": "rare",
        "price": 0, "weight": 0, "regen_bonus": 3, "ac_bonus": 1,
        "note": "It answers back half a second after you put it on. It has been for a while now.",
        "description": "It answers back half a second after you put it on. It has been for a while now.",
    },
    "unscheduled_chord_charm": {
        "name": "Unscheduled Chord Charm", "type": "amulet", "rarity": "uncommon",
        "price": 0, "weight": 0, "ac_bonus": 1,
        "note": "Hums the start of a tune it never finishes.",
        "description": "Hums the start of a tune it never finishes.",
    },

    # The Unbegun expansion rewards (2026-07-25, same "bigger expansion"
    # pass as the other two dungeons above).
    "frayed_edge_band": {
        "name": "Frayed Edge Band", "type": "ring", "rarity": "very_rare",
        "price": 0, "weight": 0, "regen_bonus": 4, "ac_bonus": 2,
        "note": "Every thread it's woven from stops at the exact same point. None of them were ever going to finish.",
        "description": "Every thread it's woven from stops at the exact same point. None of them were ever going to finish.",
    },
    "disconnected_steps_charm": {
        "name": "Disconnected Step's Charm", "type": "amulet", "rarity": "uncommon",
        "price": 0, "weight": 0, "ac_bonus": 1,
        "note": "Doesn't lead anywhere the rest of your gear goes. Useful anyway.",
        "description": "Doesn't lead anywhere the rest of your gear goes. Useful anyway.",
    },

    # Goblin Warrens expansion rewards (2026-07-25, same "bigger
    # expansion" pass, extended to the wider world per Coffee: "do this
    # for every dungeon in the game" -- this is an early-game area, so
    # rewards stay proportionately modest next to the rebirth-dungeon
    # gear above).
    "deep_larders_charm": {
        "name": "Deep Larder's Charm", "type": "amulet", "rarity": "uncommon",
        "price": 0, "weight": 0, "regen_bonus": 1,
        "note": "Smells faintly of everything it was stacked next to.",
        "description": "Smells faintly of everything it was stacked next to.",
    },
    "collapsed_tunnels_keepsake": {
        "name": "Collapsed Tunnel's Keepsake", "type": "amulet", "rarity": "common",
        "price": 0, "weight": 0, "ac_bonus": 1,
        "note": "Whoever it belonged to before the cave-in never came back for it.",
        "description": "Whoever it belonged to before the cave-in never came back for it.",
    },

    # Sunken Root Caverns expansion rewards (2026-07-25, same pass).
    "hollow_wellsprings_charm": {
        "name": "Hollow Wellspring's Charm", "type": "amulet", "rarity": "uncommon",
        "price": 0, "weight": 0, "regen_bonus": 1,
        "note": "Always feels faintly damp, no matter how long it's been out of the water.",
        "description": "Always feels faintly damp, no matter how long it's been out of the water.",
    },
    "side_pools_trinket": {
        "name": "Side Pool's Trinket", "type": "amulet", "rarity": "common",
        "price": 0, "weight": 0, "ac_bonus": 1,
        "note": "Small enough that whatever was guarding it barely noticed it was gone.",
        "description": "Small enough that whatever was guarding it barely noticed it was gone.",
    },

    # Stonearch Bridge (gorge) expansion rewards (2026-07-25, same pass).
    "deep_currents_charm": {
        "name": "Deep Current's Charm", "type": "amulet", "rarity": "uncommon",
        "price": 0, "weight": 0, "regen_bonus": 1,
        "note": "Always feels like it's being pulled gently in one direction.",
        "description": "Always feels like it's being pulled gently in one direction.",
    },
    "undertows_band": {
        "name": "Undertow's Band", "type": "ring", "rarity": "rare",
        "price": 0, "weight": 0, "regen_bonus": 1, "ac_bonus": 1,
        "note": "Everything that's ever been dragged down here eventually stops fighting the pull. This didn't.",
        "description": "Everything that's ever been dragged down here eventually stops fighting the pull. This didn't.",
    },
    "silked_nooks_keepsake": {
        "name": "Silked Nook's Keepsake", "type": "amulet", "rarity": "common",
        "price": 0, "weight": 0, "ac_bonus": 1,
        "note": "Still faintly sticky. Best not to think about why.",
        "description": "Still faintly sticky. Best not to think about why.",
    },

    # Greymoor Downs expansion rewards (2026-07-25, same pass -- the
    # Lonely Cairn / Below the Cairn lore location was deliberately
    # left untouched, no reward items added there).
    "vantage_belows_charm": {
        "name": "Beneath-the-Vantage Charm", "type": "amulet", "rarity": "uncommon",
        "price": 0, "weight": 0, "regen_bonus": 1,
        "note": "Still smells faintly of wind, even indoors.",
        "description": "Still smells faintly of wind, even indoors.",
    },
    "tower_cellars_keepsake": {
        "name": "Tower Cellar's Keepsake", "type": "amulet", "rarity": "common",
        "price": 0, "weight": 0, "ac_bonus": 1,
        "note": "Whatever it was keeping safe down there, it isn't anymore.",
        "description": "Whatever it was keeping safe down there, it isn't anymore.",
    },
    "barrow_depths_band": {
        "name": "Barrow Depths Band", "type": "ring", "rarity": "uncommon",
        "price": 0, "weight": 0, "ac_bonus": 1,
        "note": "The cold campsite up top was always bait. This was never meant to be found.",
        "description": "The cold campsite up top was always bait. This was never meant to be found.",
    },

    # Whispering Wood expansion rewards (2026-07-25, same pass -- the
    # Mossy Creek, Elder Glen, and Root Hollow gathering/lore spots were
    # deliberately left untouched, no combat or rewards added there).
    "stray_dens_keepsake": {
        "name": "Stray Den's Keepsake", "type": "amulet", "rarity": "common",
        "price": 0, "weight": 0, "ac_bonus": 1,
        "note": "Pushed out of the main pack's territory, same as whatever carried it here.",
        "description": "Pushed out of the main pack's territory, same as whatever carried it here.",
    },
    "root_wroughts_charm": {
        "name": "Root-Wrought Charm", "type": "amulet", "rarity": "uncommon",
        "price": 0, "weight": 0, "regen_bonus": 1,
        "note": "Every join is the same angle. Nothing about it grew.",
        "description": "Every join is the same angle. Nothing about it grew.",
    },
    "deep_root_wardens_band": {
        "name": "Deep Root Warden's Band", "type": "ring", "rarity": "rare",
        "price": 0, "weight": 0, "ac_bonus": 1, "regen_bonus": 1,
        "note": "Built for exactly one chamber, and nowhere else -- and it still remembers which one.",
        "description": "Built for exactly one chamber, and nowhere else -- and it still remembers which one.",
    },

    # Glimmerdeep Grotto expansion rewards (2026-07-25, same pass --
    # the last of the 6 areas Coffee asked to expand this pass).
    "buried_glows_band": {
        "name": "Buried Glow's Band", "type": "ring", "rarity": "rare",
        "price": 0, "weight": 0, "ac_bonus": 1, "regen_bonus": 1,
        "note": "Glows exactly like the crystals up above. It isn't one.",
        "description": "Glows exactly like the crystals up above. It isn't one.",
    },
    "dim_hollows_keepsake": {
        "name": "Dim Hollow's Keepsake", "type": "amulet", "rarity": "common",
        "price": 0, "weight": 0, "ac_bonus": 1,
        "note": "Just enough light left in it to see by.",
        "description": "Just enough light left in it to see by.",
    },

    # The true hidden final boss (2026-07-25, per Coffee: an FF6/FF7-
    # style ultimate secret superboss, gated on 10 rebirths AND having
    # already defeated every other secret final boss in the game). The
    # single strongest item that exists -- upgraded to real mythic tier
    # (2026-08-02, magic item system Phase 6) with real mechanical
    # effects to match its own "single strongest item" billing, rather
    # than leaving it a plain legendary-tier stat stick the moment
    # actual mythic gear started existing. Hand-authored, never rolled
    # -- same shape a generated mythic item's own affixes produce
    # (ignores_resistance/immunities), set by hand instead, so combat
    # code treats both identically. equip_requirement is almost
    # decorative here (you can only ever receive this by having already
    # beaten the exact content it also gates on), but kept for
    # consistency with every other mythic item.
    "pandoras_answer": {
        "name": "Pandora's Answer", "type": "wondrous", "rarity": "mythic",
        "price": 0, "weight": 0, "regen_bonus": 10, "ac_bonus": 5,
        "ignores_resistance": True, "immunities": ["necrotic", "psychic"],
        "equip_requirement": {"any_of": [{"kind": "completed_quest", "value": "the_unaskeds_reckoning"}]},
        "note": "Not a weapon. Not really armor, either. Just an answer, finally given to whoever was willing to ask the question one more time than everyone before them.",
        "description": "Not a weapon. Not really armor, either. Just an answer, finally given to whoever was willing to ask the question one more time than everyone before them.",
    },

    # --- Maps (task #141: buyable/discoverable, partial-reveal only --
    # see bot.py's _do_use_item "map" branch and _do_show_map's
    # revealed-but-unvisited rendering. reveals_layer/reveals_count are
    # what makes this a genuine partial reveal: only that many random
    # not-yet-visited/revealed location NAMES from that one layer, never
    # the whole map, never connection details -- those stay a real
    # spoiler you still have to walk to and see for yourself.) ---
    "weathered_surface_map": {
        "name": "Weathered Surface Map", "type": "map", "rarity": "uncommon",
        "price": 35, "weight": 0.2, "reveals_layer": "surface", "reveals_count": 3,
        "description": "Water-stained at the corners, marked in a hand that clearly knew this ground well.",
    },
    "tattered_underground_chart": {
        "name": "Tattered Underground Chart", "type": "map", "rarity": "rare",
        "price": 60, "weight": 0.2, "reveals_layer": "underground", "reveals_count": 3,
        "description": "Torn along one edge, the surviving ink tracing passages that never see daylight.",
    },

    # --- Recipe books (2026-08-11, per Coffee: real, buyable reference
    # items teaching a profession's real level-1 recipes -- "make basic,
    # make tiered books that will be available in the game as the
    # players progress. also buy basic Crafting book in the market. Only
    # show Lv 1 items tho (players must join guild for advanced
    # recipies)"). "teaches_profession" is read by bot._do_read_recipe_
    # book, which builds the actual recipe list live from rules.crafting.
    # RECIPES -- never ADVANCED_RECIPES/ENCHANT_RECIPES, so a book can
    # never leak guild-ladder content. Not consumed on use (see
    # bot._do_use_item's "book" branch) -- a reference book is reused. ---
    "cook_book_basic": {
        "name": "Cook Book", "type": "book", "rarity": "common",
        "price": 15, "weight": 1, "teaches_profession": "cooking",
        "description": "Grease-stained, dog-eared, and clearly used by someone who actually cooked from it.",
    },
    "herbalism_guide_basic": {
        "name": "Herbalism Guide", "type": "book", "rarity": "common",
        "price": 20, "weight": 1, "teaches_profession": "alchemy",
        "description": "Pressed leaves still mark a few of its pages, decades after whoever put them there.",
    },
    "crafting_book_basic": {
        "name": "Crafting Book", "type": "book", "rarity": "common",
        "price": 15, "weight": 1, "teaches_profession": "blacksmithing",
        "description": "Soot-smudged and warped from heat, like it's spent as much time by the forge as in a bag.",
    },

    # --- Quest items (never sellable, never have a price) ---
    "waterlogged_journal": {
        "name": "Waterlogged Journal", "type": "quest_item", "rarity": "unique",
        "price": 0, "weight": 0.5,
        "description": "Most of the pages are ruined, swollen and stuck together. A few words still survive here and there.",
    },
    "shard_of_dim_light": {
        "name": "Shard of Dim Light", "type": "quest_item", "rarity": "unique",
        "price": 0, "weight": 0.1,
        "description": "A sliver of something that glows just barely enough to notice, and no brighter no matter how you turn it.",
    },
    "brass_key_no_lock": {
        "name": "Brass Key That Fits No Lock", "type": "quest_item", "rarity": "unique",
        "price": 0, "weight": 0.1,
        "description": "Worn smooth from use, though nothing you've found yet has ever turned for it.",
    },

    # --- Crafting / trade materials ---
    "iron_ore": {"name": "Iron Ore", "type": "material", "rarity": "common", "price": 5, "weight": 2, "description": "A rough, heavy chunk, veined with metal not yet worth calling ore-grade."},
    "moonpetal": {"name": "Moonpetal Flower", "type": "material", "rarity": "uncommon", "price": 20, "weight": 0.05, "description": "Pale petals that seem to hold on to whatever light they last caught."},
    "silverleaf_herb": {"name": "Silverleaf Herb", "type": "material", "rarity": "common", "price": 4, "weight": 0.1, "description": "Thin, pale leaves with a faint metallic sheen along the edges."},
    "sulfur_dust": {"name": "Sulfur Dust", "type": "material", "rarity": "common", "price": 6, "weight": 0.1, "description": "A fine yellow powder that smells exactly as bad as you'd expect."},
    "raw_fish": {"name": "Raw Fish", "type": "material", "rarity": "common", "price": 3, "weight": 0.3, "description": "Still cold from the water. Best cooked before eating."},
    "wood": {"name": "Wood", "type": "material", "rarity": "common", "price": 2, "weight": 1, "description": "A few solid, unremarkable logs — good for a fire or a repair."},
    # Per Coffee (2026-08-10): "make crafting Ethers possible but
    # difficult, shud be rare herbs to craft this" -- a genuinely rare
    # material, gathered from exactly one real node (The Glimmering
    # Pool, campaign.json), used only by the advanced spell-tonic
    # recipes in rules/crafting.py.
    "glimmerdeep_moss": {"name": "Glimmerdeep Moss", "type": "material", "rarity": "rare", "price": 60, "weight": 0.05, "description": "A faint, cold light clings to it even out of the water. It never fully dries."},
    # Guild tier-5 capstone material (2026-08-11, per Coffee: cover every
    # guild tier from level 1 to end game, "Human Ascends to God"). Boss
    # drop only -- same GODSHARD_DROP_CHANCE-gated pattern as spell
    # tonics (see bot.py's _award_victory_xp), never sold in any shop.
    # Feeds exactly one recipe each in enchant_godsforged_ward and
    # godsforged_blade (rules/crafting.py), the true end of both guild
    # ladders.
    "godshard": {"name": "Godshard", "type": "material", "rarity": "mythic", "price": 0, "weight": 0.1, "description": "A splinter of something that was never meant to be small enough to hold. It doesn't feel like it belongs to this world's weight."},

    # --- Cooking (2026-07-15: raw_fish was gatherable but had zero
    # recipe using it -- a real cooking recipe below turns it, plus
    # wood for the fire, into an actually usable food item) ---
    "cooked_fish": {
        "name": "Cooked Fish", "type": "consumable", "rarity": "common",
        "price": 8, "weight": 0.3, "effect": "heal", "heal_dice": "1d4+1",
        "description": "Heals 1d4+1 HP. Simple, filling, and still warm off the fire.",
    },

    # --- Gathering tools (2026-07-16, per Coffee): fishing/lumberjacking/
    # mining each require owning the real tool for that trade -- herbalism
    # deliberately doesn't (picking a plant needs no tool), but Shears let
    # an herbalist harvest more than one plant per success. "required_for"
    # is the gathering skill this tool gates (see bot.py's _do_gather);
    # Shears instead uses "boosts_quantity_for" since it's optional, not
    # gating access at all.
    "fishing_pole": {"name": "Fishing Pole", "type": "tool", "rarity": "common", "price": 8, "weight": 2, "required_for": "fishing", "description": "A simple rod and line. Required to fish anywhere."},
    "bait": {"name": "Bait", "type": "tool", "rarity": "common", "price": 2, "weight": 0.1, "required_for": "fishing", "note": "A small tin of squirming earthworms and grubs, dug fresh from damp soil.", "description": "A small tin of squirming earthworms and grubs, dug fresh from damp soil."},
    "woodcutters_axe": {"name": "Woodcutter's Axe", "type": "tool", "rarity": "common", "price": 10, "weight": 4, "required_for": "lumberjacking", "description": "A well-worn axe, single-bladed and heavy in the head. Required to chop wood."},
    "pickaxe": {"name": "Pickaxe", "type": "tool", "rarity": "common", "price": 10, "weight": 5, "required_for": "mining", "description": "Iron-headed and solid. Required to mine ore."},
    "shears": {"name": "Shears", "type": "tool", "rarity": "common", "price": 6, "weight": 0.5, "boosts_quantity_for": "herbalism", "description": "Sharp, spring-hinged blades. Not required to gather herbs, but they let you harvest more per attempt."},
    # Task, per Coffee (2026-07-21): "add shovels to increase the
    # amount of bait we can get?! Have it use a dice roll." Same
    # optional, dice-rolled quantity-boost shape as Shears above, just
    # for bait_gathering instead of herbalism.
    "shovel": {"name": "Shovel", "type": "tool", "rarity": "common", "price": 5, "weight": 3, "boosts_quantity_for": "bait_gathering", "description": "A plain digging shovel. Not required to dig for bait, but it lets you turn up more per attempt."},
}


def get_item(item_id: str) -> dict | None:
    """
    Real per-instance magic items (2026-08-02 magic item system, per
    Coffee: "make loot real"): a generated/crafted item's id (e.g.
    "gi42") is never a key in the static ITEMS dict below -- it's a
    pointer into db.py's item_instances table instead. Falling back here,
    rather than teaching every call site about the new id scheme
    separately, is what lets equip/combat/shop/market/scrolls all keep
    working against a generated item completely unchanged -- they only
    ever call get_item(). Deferred import: db.py imports this module
    (`items as items_module`) at its own load time, so a top-level
    `import db` here would be circular; by the time get_item() is ever
    actually CALLED, both modules are already fully loaded, which is the
    standard way to break this exact kind of cycle without restructuring
    either file.
    """
    item = ITEMS.get(item_id)
    if item is not None:
        # Real live bug (2026-08-14/15, dev-topic screenshot reports: a
        # plain, hand-authored "Silvered Dagger" -- base damage_type
        # "silver" -- landing hits typed "poison" then later "fire" in
        # the SAME fight, on the same weapon). ITEMS.get() used to hand
        # back a LIVE reference into this module's own shared dict --
        # any code anywhere that ever mutated a get_item() result in
        # place (a reasonable thing to assume is safe; a generated
        # item's own materialize_item_instance below already returns a
        # fresh dict every call, so this was the one inconsistent
        # exception) would permanently corrupt that catalog entry for
        # every player, for the rest of the process's life, until
        # restart -- explaining why the exact SAME static id could show
        # two different damage types on different hits. The specific
        # mutating call site was never conclusively found despite an
        # extensive live-simulated hunt (multiple casters/rounds/
        # elemental procs, all clean) -- fixed at the root instead: this
        # now always returns an independent copy, exactly matching
        # materialize_item_instance's existing per-call-fresh-dict
        # behavior for generated items, closing the entire bug CLASS
        # regardless of which caller was ever responsible.
        return copy.deepcopy(item)
    if isinstance(item_id, str) and item_id.startswith("gi") and item_id[2:].isdigit():
        import db
        return db.materialize_item_instance(item_id)
    return None


def is_sellable(item_id: str) -> bool:
    """
    Real live bug (2026-08-02, caught investigating a Development-topic
    request: "make sure players can sell the items to applicable shops
    also if they don't want them"): this used a direct ITEMS.get(...)
    lookup, not get_item()'s fallback -- every generated magic item
    (rules/item_generator.py, a "gi<n>" id) always came back None here,
    so shop.sell_item's is_sellable check silently rejected selling ANY
    generated item at all, unconditionally, with no way to know why. An
    earlier investigation (magic item system Phase 1) concluded this
    function was dead code never called anywhere in bot.py -- wrong;
    shop.sell_item calls it directly via _do_sell.
    """
    item = get_item(item_id)
    if item is None:
        return False
    return item.get("type") != "quest_item" and item.get("price", 0) > 0


def find_item_id_by_name(name_fragment: str) -> str | None:
    """Fuzzy-ish lookup: match on item_id or display name, case-insensitively."""
    lowered = name_fragment.strip().lower()
    for item_id, data in ITEMS.items():
        if lowered == item_id.lower() or lowered == data["name"].lower():
            return item_id
    for item_id, data in ITEMS.items():
        if lowered in item_id.lower() or lowered in data["name"].lower():
            return item_id
    return None


def find_item_mentioned_in_text(text: str, candidate_ids: list[str] | None = None) -> str | None:
    """
    Search for any item's name or id mentioned WITHIN a longer sentence
    (e.g. "I want to buy a healing potion" -> "healing_potion"). Unlike
    find_item_id_by_name, this checks whether the item name appears
    inside the text, not the other way around. If candidate_ids is
    given, only those items are considered (e.g. a shop's stock list).
    """
    # Real live bug (2026-07-18, confirmed live: "Equip my longbow and
    # armour" silently equipped only the longbow): every real armor item
    # in this game ("Leather Armor", "Chain Shirt", "Chain Mail") is
    # named with the American spelling, and neither the full-name nor
    # the word-level fallback match below ever normalized the player's
    # own British spelling ("armour") to match -- confirmed by direct
    # repro. A live player retried with the American spelling 52 seconds
    # later, strongly suggesting they noticed the first attempt silently
    # failed. Normalizing just this one word (the only real-item-name
    # collision this game has) rather than a general British/American
    # dictionary, which would be a much bigger surface for false
    # positives with no other real payoff here.
    #
    # Same treatment for "fishing rod" (2026-07-19, confirmed live: "Sell
    # 1x fishing rod" failed even though the character owned a real
    # Fishing Pole) -- "rod" is the everyday real-world word for this
    # exact tool and this game has no other item using "rod" at all, so
    # normalizing it to "pole" is the same kind of safe, single-word,
    # real-collision fix as armour/armor, not a general synonym engine.
    lowered = text.strip().lower().replace("armour", "armor").replace("fishing rod", "fishing pole")
    search_space = candidate_ids if candidate_ids is not None else list(ITEMS.keys())
    # Check longer names first so "greater healing potion" doesn't get
    # shadowed by a shorter partial match like "healing potion".
    # get_item(), not a direct ITEMS[...] index (2026-08-02 magic item
    # system): a generated item's id (e.g. "gi42") is never a key in the
    # static ITEMS dict -- candidate_ids can genuinely include one the
    # moment a player carries any kept generated loot, and this function
    # is reached from ordinary inventory-scanning free text (equip/sell/
    # give), so it must resolve those too, not just the static catalog.
    ordered = sorted(search_space, key=lambda i: -len(get_item(i)["name"]))
    for item_id in ordered:
        data = get_item(item_id)
        if data["name"].lower() in lowered or item_id.replace("_", " ") in lowered:
            return item_id

    # Fall back to a generic category word (e.g. "potions" for "Healing
    # Potion") when it unambiguously picks out exactly one candidate --
    # confirmed live 2026-07-12 via a real screenshot: "buy two potions
    # from Grimsby" never says the full item name, just the general
    # kind, and this shop only sells one real potion.
    #
    # Real live bug (2026-07-16, Coffee): "Look for a shop to buy an
    # axe" failed ("not sure what item you mean") even though the shop
    # sold exactly one axe (Woodcutter's Axe) -- the word-length >= 4
    # guard here, meant to keep trivial connector words ("of", "the")
    # from causing false matches, ALSO excluded genuinely short,
    # unambiguous item nouns like "axe" (3 letters). Fixed by explicitly
    # excluding a real stopword list instead of using a blanket length
    # cutoff, and by matching whole WORDS of the input (word membership)
    # rather than a bare substring check -- the old substring check
    # could also have matched a short word inside an unrelated longer
    # word (e.g. "ore" inside "before"), a latent risk this removes too.
    stopwords = {"the", "of", "an", "a", "on", "in", "to", "for", "and", "no"}
    # A plain .split() would leave punctuation stuck to words ("axe!!!"
    # never equals "axe") -- \w+(?:'\w+)? keeps a real internal
    # apostrophe (e.g. "woodcutter's") but strips trailing punctuation.
    lowered_words = set(re.findall(r"\w+(?:'\w+)?", lowered))

    # Real live bug (2026-07-19, confirmed live TWICE within the hour):
    # "Buy fishing hooks" silently bought a Fishing Pole -- "hooks" isn't
    # a real item anywhere, but "fishing" alone is a name-word of
    # "Fishing Pole" and word-overlap treated that lone MODIFIER as a
    # confident match. A required_for-based ambiguity check (flagging
    # Bait too, since both tools share required_for="fishing") fixed
    # that, but applying it unconditionally then broke "Sell 1x fishing
    # rod" -- Fishing Pole should resolve confidently there since "rod"
    # is just the everyday synonym for the SAME item's actual head noun
    # ("pole"), already normalized to match above.
    #
    # The real distinction: a match on an item's HEAD noun (the last
    # significant word -- "pole" in "Fishing Pole", "potion" in "Healing
    # Potion", "axe" in "Woodcutter's Axe") is a strong, confident match
    # that should win outright. A match on any OTHER word (a modifier,
    # like the bare "fishing" in "fishing hooks") is weak and should
    # still be checked against required_for for real ambiguity, exactly
    # like the fallback below already does for a bare category word.
    strong_matches = set()
    weak_matches = set()
    for item_id in search_space:
        name_words = [w for w in get_item(item_id)["name"].lower().split()]
        significant_words = [
            w[:-1] if w.endswith("s") else w for w in name_words
            if (w[:-1] if w.endswith("s") else w) not in stopwords
            and len(w[:-1] if w.endswith("s") else w) >= 3
        ]
        if not significant_words:
            continue
        # Check the head word FIRST, independent of its position in the
        # name -- iterating in name order and breaking on the first
        # match (as an earlier version of this fix did) could match a
        # modifier before ever checking the head word even when the
        # head word ALSO appears in the input, misclassifying a strong
        # match as weak.
        head_word = significant_words[-1]
        if head_word in lowered_words or f"{head_word}s" in lowered_words:
            strong_matches.add(item_id)
            continue
        for word in significant_words[:-1]:
            if word in lowered_words or f"{word}s" in lowered_words:
                weak_matches.add(item_id)
                break

    if strong_matches:
        if len(strong_matches) == 1:
            return strong_matches.pop()
        return None

    matches = set(weak_matches)
    for item_id in search_space:
        required_for = get_item(item_id).get("required_for")
        if required_for and (required_for in lowered_words or f"{required_for}s" in lowered_words):
            matches.add(item_id)
    if len(matches) == 1:
        return matches.pop()
    return None
