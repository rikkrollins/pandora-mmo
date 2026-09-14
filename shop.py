"""
shop.py
Buy/sell transactions between a character and a shop. All state changes
(gold, inventory) go through db.py so they're persisted immediately —
there's no separate in-memory shop state to get out of sync.
"""
import db
import items
from guilds import shop_discount_for_guild
from rules.leveling import rebirth_power_multiplier


def buy_item(telegram_user_id: int, chat_id: int, shop_data: dict, item_id: str, quantity: int = 1) -> tuple[bool, str]:
    """
    Attempt to buy `quantity` of item_id from a shop. Returns (success, message).
    Applies the character's guild discount, if any. A real, persistent
    consequence: a shopkeeper who caught this player stealing before
    (db.set_banned_by_npc) refuses to do business with them at all.
    """
    owner_npc = shop_data.get("owner_npc")
    if owner_npc and db.is_banned_by_npc(telegram_user_id, chat_id, owner_npc):
        return False, "The shopkeeper won't sell you anything — not after what you did last time."

    if item_id not in shop_data["inventory"]:
        return False, f"This shop doesn't carry {item_id.replace('_', ' ')}."

    item = items.get_item(item_id)
    if item is None:
        return False, f"Unknown item: {item_id}."

    character = db.get_character(telegram_user_id, chat_id)
    if character is None:
        return False, "You don't have a character yet."

    discount = shop_discount_for_guild(character.get("guild"))
    unit_price = item.get("price", 0)
    # Real request (2026-08-21, per Coffee, tiered spirit-summon
    # scrolls: "when we evolve let them evolve too and make them more
    # expensive") -- only items explicitly flagged rebirth_scales_price
    # get pricier as the buyer's own rebirth_count climbs, via the same
    # 1.5^rebirth_count curve (rebirth_power_multiplier) every other
    # rebirth-scaled number in this game already follows. Everything
    # else keeps its flat authored price, unchanged.
    if item.get("rebirth_scales_price"):
        unit_price = int(unit_price * rebirth_power_multiplier(character.get("rebirth_count", 0)))
    total_price = round(unit_price * quantity * (1 - discount))

    if character["gold"] < total_price:
        return False, (
            f"You need {total_price} gold for {quantity}x {item['name']}, "
            f"but you only have {character['gold']}."
        )

    db.update_character(telegram_user_id, chat_id, gold=character["gold"] - total_price)
    db.add_item(telegram_user_id, chat_id, item_id, quantity)

    discount_note = f" ({int(discount*100)}% guild discount applied)" if discount else ""
    return True, f"Bought {quantity}x {item['name']} for {total_price} gold{discount_note}."


def sell_item(telegram_user_id: int, chat_id: int, item_id: str, quantity: int = 1) -> tuple[bool, str]:
    """
    Sell `quantity` of item_id from the character's backpack for half its
    listed price (standard 5E convention). Quest items can't be sold.
    """
    item = items.get_item(item_id)
    if item is None:
        return False, f"Unknown item: {item_id}."

    if not items.is_sellable(item_id):
        return False, f"{item['name']} can't be sold."

    character = db.get_character(telegram_user_id, chat_id)
    if character is None:
        return False, "You don't have a character yet."

    have = character["inventory"].get(item_id, 0)
    if have < quantity:
        return False, f"You only have {have}x {item['name']}."

    # Real bug found and fixed (2026-09-14, proactive audit): this
    # always sold back at 50% of the item's flat, static catalog
    # price, even for a `rebirth_scales_price` item (see buy_item's own
    # real rebirth_power_multiplier scaling above) -- a rebirth-5+
    # player who bought a Scroll of the Elder Spirit at its real
    # inflated price got shortchanged selling it back, refunded 50% of
    # the UNSCALED base price instead of 50% of what they actually
    # paid. Never a dupe/profit vector either way (50% sell-back always
    # stays a real loss versus the matching buy price, scaled or not),
    # just a real fairness gap for a rebirth player specifically.
    unit_price = item.get("price", 0)
    if item.get("rebirth_scales_price"):
        unit_price = int(unit_price * rebirth_power_multiplier(character.get("rebirth_count", 0)))
    sell_price = round(unit_price * 0.5 * quantity)
    success, updated = db.remove_item(telegram_user_id, chat_id, item_id, quantity)
    if not success:
        return False, "Something went wrong removing the item."

    db.update_character(telegram_user_id, chat_id, gold=updated["gold"] + sell_price)
    return True, f"Sold {quantity}x {item['name']} for {sell_price} gold."
