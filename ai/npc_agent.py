"""
ai/npc_agent.py
NPCs as separate agent contexts, each with their own persona and short
conversation memory. Uses config.DM_NARRATION_MODEL (same narrative
model as dm_agent.py), since dialogue also doesn't need tool calling.
"""
import logging

import requests

import config
from ai.ollama_health import record_timeout
from ai.text_cleanup import strip_think_tags, is_placeholder_text

logger = logging.getLogger("pandora_mmo")

# In-memory NPC registry: npc_id -> {"persona": str, "memory": {(chat_id, telegram_user_id): [(role, text), ...]}}
_NPCS: dict[str, dict] = {}

MAX_MEMORY_TURNS = 10


def _conversation_buffer(npc_id: str, chat_id: int, telegram_user_id: int) -> list:
    """
    Real per-(chat, player) conversation buffer (2026-09-22, fixing a
    real cross-player/cross-tenant leak: this used to be ONE flat list
    per npc_id, shared by literally every real player -- and every
    tenant chat, given the multi-tenant work already shipped -- talking
    to that same NPC, so two different people's conversations could
    blend into each other's prompt context). Lazily created per key,
    same "no upfront allocation" convention as every other per-chat
    dict in this codebase. `telegram_user_id=0` is the real sentinel
    for NPC-to-NPC/world "ambient" lines that aren't about any specific
    player at all (see generate_ambient_line's own callers).
    """
    return _NPCS[npc_id]["memory"].setdefault((chat_id, telegram_user_id), [])


def register_npc(npc_id: str, name: str, personality: str, goals: str = "",
                  alignment: str = "", disposition: str = "friendly",
                  shop_items: list[dict] | None = None, pronouns: str = "") -> None:
    """
    Register a new NPC persona. Call once per NPC at setup time.

    `shop_items` (if this NPC owns a shop — see bot.py's
    setup_default_npcs, which resolves it from the real campaign.json
    shop inventory via items.py) grounds this NPC's dialogue in their
    ACTUAL wares/prices. Without this, confirmed live 2026-07-11: a
    player asking Grimsby "do you have items to sell" or "how can I
    heal" got a different, invented answer each time — the model has no
    way to know what a shopkeeper NPC really sells, so it hallucinated
    plausible-sounding items/prices from general tabletop-RPG knowledge, same
    failure mode support_agent.py was already grounded against (see
    CRITICAL_GROUNDING_RULE there). NPC dialogue needs the same fix.

    `pronouns` (2026-07-25, per Coffee: "Change Sarah's pronoun to
    She/Her"): campaign.json's own real, hand-authored fact for an NPC,
    when set — Sarah's own quest text already consistently wrote her as
    "she", but nothing ever told the MODEL that explicitly, so AI-
    generated dialogue had no real grounding for her pronouns at all,
    same "never invent a fact that should be real data" gap this file's
    shop-grounding fix above already closed once. Empty for any NPC
    that doesn't set one, same as every other optional field here.
    """
    persona = (
        f"You are {name}, an NPC in a 5th-edition-style tabletop RPG. "
        f"Personality: {personality}. "
        + (f"Your pronouns are {pronouns}. " if pronouns else "")
        + (f"Alignment: {alignment}. " if alignment else "")
        + (f"Goals: {goals}. " if goals else "")
        + "Stay in character at all times. Respond conversationally, in 1-4 sentences."
    )
    _NPCS[npc_id] = {
        "persona": persona, "memory": {}, "disposition": disposition,
        "shop_items": shop_items or [],
    }


def _shop_grounding_block(npc_id: str) -> str:
    """
    Real, current wares/prices for an NPC who owns a shop — the ONLY
    accurate answer to "what do you have for sale" or "can you heal me
    with a potion". Empty string for non-merchant NPCs (nothing to
    ground). See register_npc's docstring for why this exists.
    """
    shop_items = _NPCS[npc_id].get("shop_items") or []
    if not shop_items:
        return ""
    lines = [
        "\n\nWhat you ACTUALLY have for sale right now (the ONLY items you can "
        "offer — never invent other wares, prices, or services; if asked for "
        "something not on this list, say honestly that you don't carry it):"
    ]
    for item in shop_items:
        detail = f"- {item['name']} — {item['price']} gold"
        if item.get("note"):
            detail += f" ({item['note']})"
        lines.append(detail)
    lines.append(
        "\nThe only real ways anyone recovers HP or spell slots in this world are "
        "resting (which takes real time, not instant) or a healing item like the "
        "ones above, if you sell one — don't invent other cures, healers, or "
        "magical remedies you don't actually offer."
    )
    return "\n".join(lines)


def _memory_facts_block(memory_facts: list[str] | None, character_name: str) -> str:
    """
    Renders a player's persistent, DB-backed history with this NPC (see
    db.get_relationship's memory_events) as a block the model can use to
    stay consistent across sessions — e.g. remembering a theft, a past
    favor, or a fight — distinct from the short in-memory conversation
    buffer below, which only covers the current back-and-forth.
    """
    if not memory_facts:
        return f"(You have no specific memories of {character_name} yet.)"
    facts = "\n".join(f"- {fact}" for fact in memory_facts)
    return f"What you specifically remember about {character_name}:\n{facts}"


def _build_prompt(npc_id: str, chat_id: int, telegram_user_id: int, player_message: str, character_name: str = "the player",
                   memory_facts: list[str] | None = None, quest_facts: str | None = None,
                   identity_facts: str | None = None) -> str:
    npc = _NPCS[npc_id]
    history_lines = []
    for role, text in _conversation_buffer(npc_id, chat_id, telegram_user_id)[-MAX_MEMORY_TURNS:]:
        history_lines.append(f"{role}: {text}")
    history = "\n".join(history_lines) or "(no prior conversation this session)"
    memory_block = _memory_facts_block(memory_facts, character_name)
    shop_block = _shop_grounding_block(npc_id)
    quest_block = f"\n\n{quest_facts}" if quest_facts else ""
    # Synergy Phase 8 (2026-08-13, per Coffee: NPCs should react to WHO
    # they're actually talking to, not just what quest/history is on
    # file) -- real, grounded class/subclass/guild facts, built fresh
    # per call from the character row (bot.py's _npc_identity_facts),
    # same "compute the fact, hand it to narration as ground truth"
    # convention quest_facts/memory_facts already use. Never a rule
    # dictating HOW the NPC reacts -- just what's true, same as those.
    identity_block = f"\n\n{identity_facts}" if identity_facts else ""

    return (
        f"{npc['persona']}{shop_block}{quest_block}{identity_block}\n\n"
        f"{memory_block}\n\n"
        f"Conversation so far this session:\n{history}\n\n"
        f"Player: {player_message}\n"
        f"You:"
    )


# Real live bug (2026-09-23, dev-bridge screenshot, Coffee: "Is this
# supposed to happen?" -- a player who said "Talk to grimsby" got back
# literally "Grimsby: ..."). Root cause: unlike every other narration
# path in this game (dm_agent.py's own is_placeholder_text call sites
# all fall back to a real plain-text template, e.g. _fallback_
# narration/_fallback_welcome), talk_to_npc's fallback for BOTH a
# request failure AND a degenerate placeholder-shaped model response
# (is_placeholder_text also matches a bare "..." itself, so a model
# reply of literally "..." round-tripped right back to the same "...")
# was a bare ellipsis with zero real words -- a direct reply to
# something the player just said, unlike generate_ambient_line's
# empty-string fallback (an unprompted flourish is allowed to simply
# not happen; a direct reply is not).
_TALK_FALLBACK_REPLY = "just shrugs, not in a talking mood right now."


def talk_to_npc(npc_id: str, chat_id: int, telegram_user_id: int, player_message: str, character_name: str = "the player",
                memory_facts: list[str] | None = None, quest_facts: str | None = None,
                identity_facts: str | None = None) -> str:
    """
    Send a player message to a registered NPC and return its in-character
    reply. Falls back to a neutral line if the model is unreachable.
    `memory_facts` (from db.get_relationship) lets the NPC stay
    consistent about this specific player across sessions/restarts, on
    top of the short in-session conversation buffer. `quest_facts`
    (bot.py builds this fresh per call from real story/board quest data)
    grounds "what's this quest about" / "what's the reward" — confirmed
    live 2026-07-11: asking Grimsby about his own quest got a vague
    non-answer, since nothing here ever told the model what quest, if
    any, this NPC is actually connected to, same failure mode already
    fixed for shop inventory. `identity_facts` (Synergy Phase 8,
    2026-08-13) grounds WHO is actually talking -- real class/subclass/
    guild facts, same "compute then hand to narration" convention.

    `chat_id`/`telegram_user_id` (2026-09-22, real cross-player/cross-
    tenant conversation leak fix): the short-term conversation buffer
    is now scoped per (chat, player), not shared globally by every real
    person talking to this NPC -- see _conversation_buffer's own
    docstring.
    """
    if npc_id not in _NPCS:
        raise ValueError(f"Unknown NPC id: {npc_id!r}. Call register_npc() first.")

    prompt = _build_prompt(npc_id, chat_id, telegram_user_id, player_message, character_name, memory_facts, quest_facts, identity_facts)

    try:
        response = requests.post(
            f"{config.OLLAMA_BASE_URL}/api/generate",
            json={
                "model": config.DM_NARRATION_MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {"num_thread": config.OLLAMA_NUM_THREAD},
            },
            timeout=200,
        )
        response.raise_for_status()
        data = response.json()
        reply = strip_think_tags(data.get("response", ""))
        if is_placeholder_text(reply):
            reply = _TALK_FALLBACK_REPLY
    except (requests.RequestException, ValueError) as e:
        record_timeout()
        logger.error(f"[npc_agent] NPC call failed, falling back: {e}")
        reply = _TALK_FALLBACK_REPLY

    buffer = _conversation_buffer(npc_id, chat_id, telegram_user_id)
    buffer.append(("Player", player_message))
    buffer.append(("You", reply))
    return reply


def generate_ambient_line(npc_id: str, chat_id: int, telegram_user_id: int, character_name: str, situation: str,
                           memory_facts: list[str] | None = None) -> str:
    """
    An UNPROMPTED in-character line — the NPC noticing/reacting to a
    player's arrival on their own, not replying to anything the player
    said. `situation` describes what's happening in plain terms (e.g.
    "notices Aldric arrive" or "sizes Aldric up, about to attack") so the
    line is grounded in a real, already-decided event, not invented by
    the model. `memory_facts` lets a returning player be greeted
    differently than a stranger — a defeated bandit's crew recognizing
    the person who beat them, a shopkeeper who hasn't forgotten a theft.
    Falls back to silence (empty string) if the model is unreachable —
    an ambient flourish is allowed to simply not happen.

    `telegram_user_id` (2026-09-22, same cross-player leak fix as
    talk_to_npc): pass the real arriving player's id when this line is
    actually reacting to one specific person; pass `0` for a pure
    NPC-to-NPC/world "ambient" line that isn't about any specific
    player at all (see `_conversation_buffer`'s own docstring).
    """
    npc = _NPCS[npc_id]
    history_lines = [f"{role}: {text}" for role, text in _conversation_buffer(npc_id, chat_id, telegram_user_id)[-MAX_MEMORY_TURNS:]]
    history = "\n".join(history_lines) or "(no prior conversation this session)"
    memory_block = _memory_facts_block(memory_facts, character_name)
    shop_block = _shop_grounding_block(npc_id)

    prompt = (
        f"{npc['persona']}{shop_block}\n\n"
        f"{memory_block}\n\n"
        f"Conversation so far this session:\n{history}\n\n"
        f"{character_name} {situation}. Nobody has spoken to you yet — say or do something "
        f"unprompted, in character, in 1-2 short sentences.\n"
        f"You:"
    )

    try:
        response = requests.post(
            f"{config.OLLAMA_BASE_URL}/api/generate",
            json={"model": config.DM_NARRATION_MODEL, "prompt": prompt, "stream": False,
                  "options": {"num_thread": config.OLLAMA_NUM_THREAD}},
            timeout=200,
        )
        response.raise_for_status()
        data = response.json()
        line = strip_think_tags(data.get("response", "")).strip()
        if is_placeholder_text(line):
            line = ""
    except (requests.RequestException, ValueError) as e:
        record_timeout()
        logger.error(f"[npc_agent] ambient line failed, skipping: {e}")
        return ""

    if line:
        _conversation_buffer(npc_id, chat_id, telegram_user_id).append(("You", line))
    return line
