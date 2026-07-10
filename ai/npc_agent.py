"""
ai/npc_agent.py
NPCs as separate agent contexts, each with their own persona and short
conversation memory. Uses config.DM_NARRATION_MODEL (same narrative
model as dm_agent.py), since dialogue also doesn't need tool calling.
"""
import requests

import config
from ai.text_cleanup import strip_think_tags

# In-memory NPC registry: npc_id -> {"persona": str, "memory": [(role, text), ...]}
_NPCS: dict[str, dict] = {}

MAX_MEMORY_TURNS = 10


def register_npc(npc_id: str, name: str, personality: str, goals: str = "",
                  alignment: str = "", disposition: str = "friendly") -> None:
    """Register a new NPC persona. Call once per NPC at setup time."""
    persona = (
        f"You are {name}, an NPC in a Dungeons & Dragons 5E game. "
        f"Personality: {personality}. "
        + (f"Alignment: {alignment}. " if alignment else "")
        + (f"Goals: {goals}. " if goals else "")
        + "Stay in character at all times. Respond conversationally, in 1-4 sentences."
    )
    _NPCS[npc_id] = {"persona": persona, "memory": [], "disposition": disposition}


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


def _build_prompt(npc_id: str, player_message: str, character_name: str = "the player",
                   memory_facts: list[str] | None = None) -> str:
    npc = _NPCS[npc_id]
    history_lines = []
    for role, text in npc["memory"][-MAX_MEMORY_TURNS:]:
        history_lines.append(f"{role}: {text}")
    history = "\n".join(history_lines) or "(no prior conversation this session)"
    memory_block = _memory_facts_block(memory_facts, character_name)

    return (
        f"{npc['persona']}\n\n"
        f"{memory_block}\n\n"
        f"Conversation so far this session:\n{history}\n\n"
        f"Player: {player_message}\n"
        f"You:"
    )


def talk_to_npc(npc_id: str, player_message: str, character_name: str = "the player",
                memory_facts: list[str] | None = None) -> str:
    """
    Send a player message to a registered NPC and return its in-character
    reply. Falls back to a neutral line if the model is unreachable.
    `memory_facts` (from db.get_relationship) lets the NPC stay
    consistent about this specific player across sessions/restarts, on
    top of the short in-session conversation buffer.
    """
    if npc_id not in _NPCS:
        raise ValueError(f"Unknown NPC id: {npc_id!r}. Call register_npc() first.")

    prompt = _build_prompt(npc_id, player_message, character_name, memory_facts)

    try:
        response = requests.post(
            f"{config.OLLAMA_BASE_URL}/api/generate",
            json={
                "model": config.DM_NARRATION_MODEL,
                "prompt": prompt,
                "stream": False,
            },
            timeout=200,
        )
        response.raise_for_status()
        data = response.json()
        reply = strip_think_tags(data.get("response", ""))
    except (requests.RequestException, ValueError) as e:
        print(f"[npc_agent] NPC call failed, falling back: {e}")
        reply = "..."

    _NPCS[npc_id]["memory"].append(("Player", player_message))
    _NPCS[npc_id]["memory"].append(("You", reply))
    return reply


def generate_ambient_line(npc_id: str, character_name: str, situation: str,
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
    """
    npc = _NPCS[npc_id]
    history_lines = [f"{role}: {text}" for role, text in npc["memory"][-MAX_MEMORY_TURNS:]]
    history = "\n".join(history_lines) or "(no prior conversation this session)"
    memory_block = _memory_facts_block(memory_facts, character_name)

    prompt = (
        f"{npc['persona']}\n\n"
        f"{memory_block}\n\n"
        f"Conversation so far this session:\n{history}\n\n"
        f"{character_name} {situation}. Nobody has spoken to you yet — say or do something "
        f"unprompted, in character, in 1-2 short sentences.\n"
        f"You:"
    )

    try:
        response = requests.post(
            f"{config.OLLAMA_BASE_URL}/api/generate",
            json={"model": config.DM_NARRATION_MODEL, "prompt": prompt, "stream": False},
            timeout=200,
        )
        response.raise_for_status()
        data = response.json()
        line = strip_think_tags(data.get("response", "")).strip()
    except (requests.RequestException, ValueError) as e:
        print(f"[npc_agent] ambient line failed, skipping: {e}")
        return ""

    if line:
        npc["memory"].append(("You", line))
    return line
