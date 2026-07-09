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


def register_npc(npc_id: str, name: str, personality: str, goals: str = "") -> None:
    """Register a new NPC persona. Call once per NPC at setup time."""
    persona = (
        f"You are {name}, an NPC in a Dungeons & Dragons 5E game. "
        f"Personality: {personality}. "
        + (f"Goals: {goals}. " if goals else "")
        + "Stay in character at all times. Respond conversationally, in 1-4 sentences."
    )
    _NPCS[npc_id] = {"persona": persona, "memory": []}


def _build_prompt(npc_id: str, player_message: str) -> str:
    npc = _NPCS[npc_id]
    history_lines = []
    for role, text in npc["memory"][-MAX_MEMORY_TURNS:]:
        history_lines.append(f"{role}: {text}")
    history = "\n".join(history_lines) or "(no prior conversation)"

    return (
        f"{npc['persona']}\n\n"
        f"Conversation so far:\n{history}\n\n"
        f"Player: {player_message}\n"
        f"You:"
    )


def talk_to_npc(npc_id: str, player_message: str) -> str:
    """
    Send a player message to a registered NPC and return its in-character
    reply. Falls back to a neutral line if the model is unreachable.
    """
    if npc_id not in _NPCS:
        raise ValueError(f"Unknown NPC id: {npc_id!r}. Call register_npc() first.")

    prompt = _build_prompt(npc_id, player_message)

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
