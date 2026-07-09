"""
shop.py
Buy/sell transactions between a character and a shop. All state changes
(gold, inventory) go through db.py so they're persisted immediately —
there's no separate in-memory shop state to get out of sync.
"""
import db
import items
from guilds import shop_discount_for_guild


def buy_item(telegram_user_id: int, shop_data: dict, item_id: str, quantity: int = 1) -> tuple[bool, str]:
    """
    Attempt to buy `quantity` of item_id from a shop. Returns (success, message).
    Applies the character's guild discount, if any.
    """
    if item_id not in shop_data["inventory"]:
        return False, f"This shop doesn't carry {item_id.replace('_', ' ')}."

    item = items.get_item(item_id)
    if item is None:
        return False, f"Unknown item: {item_id}."

    character = db.get_character(telegram_user_id)
    if character is None:
        return False, "You don't have a character yet."

    discount = shop_discount_for_guild(character.get("guild"))
    unit_price = item.get("price", 0)
    total_price = round(unit_price * quantity * (1 - discount))

    if character["gold"] < total_price:
        return False, (
            f"You need {total_price} gold for {quantity}x {item['name']}, "
            f"but you only have {character['gold']}."
        )

    db.update_character(telegram_user_id, gold=character["gold"] - total_price)
    db.add_item(telegram_user_id, item_id, quantity)

    discount_note = f" ({int(discount*100)}% guild discount applied)" if discount else ""
    return True, f"Bought {quantity}x {item['name']} for {total_price} gold{discount_note}."


def sell_item(telegram_user_id: int, item_id: str, quantity: int = 1) -> tuple[bool, str]:
    """
    Sell `quantity` of item_id from the character's backpack for half its
    listed price (standard 5E convention). Quest items can't be sold.
    """
    item = items.get_item(item_id)
    if item is None:
        return False, f"Unknown item: {item_id}."

    if not items.is_sellable(item_id):
        return False, f"{item['name']} can't be sold."

    character = db.get_character(telegram_user_id)
    if character is None:
        return False, "You don't have a character yet."

    have = character["inventory"].get(item_id, 0)
    if have < quantity:
        return False, f"You only have {have}x {item['name']}."

    sell_price = round(item.get("price", 0) * 0.5 * quantity)
    success, updated = db.remove_item(telegram_user_id, item_id, quantity)
    if not success:
        return False, "Something went wrong removing the item."

    db.update_character(telegram_user_id, gold=updated["gold"] + sell_price)
    return True, f"Sold {quantity}x {item['name']} for {sell_price} gold."
