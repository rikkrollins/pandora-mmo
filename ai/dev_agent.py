"""
ai/dev_agent.py
Conversational assistant for the Development topic. Answers questions
about the codebase, troubleshooting, and how the game is built. This
is intentionally READ-ONLY and conversational — it does not execute
code, edit files, or run shell commands on its own. Giving a chat
interface direct file/code-execution power is a much bigger, riskier
feature (effectively re-building an agentic dev tool inside the game
bot) and isn't something to add quietly; it deserves its own explicit
discussion about sandboxing and permissions before ever being wired in.

Uses config.BUILD_MODEL, since this is a technical/code-adjacent task,
closer to what that model is already used for elsewhere in the project.
"""
import requests

import config
from ai.text_cleanup import strip_think_tags, is_placeholder_text

DEV_SYSTEM_PROMPT = """You are a helpful development assistant for Pandora MMO, \
a Python-based 5th-edition-style tabletop RPG Telegram bot. You help the developer \
troubleshoot issues, understand the codebase, and plan new features. \
Project structure: bot.py (Telegram handlers), db.py (SQLite persistence), \
rules/ (pure dice/combat/leveling logic, no AI), ai/ (intent parsing, NPC \
dialogue, DM narration, this dev assistant), campaigns/ (data-driven world \
content), items.py, spells.py, guilds.py (game content catalogs), \
sessions.py (combat/turn state). You do not have the ability to read live \
files, run commands, or see current logs — only what the developer tells \
you in this conversation. If you need more information to help (like an \
error message or file contents), ask for it directly rather than guessing. \
Keep answers concise and practical."""


def _build_prompt(question: str, recent_context: list[str] | None = None) -> str:
    recent_context = recent_context or []
    history = "\n".join(f"- {c}" for c in recent_context[-10:]) or "(no prior context)"
    return (
        f"{DEV_SYSTEM_PROMPT}\n\n"
        f"Recent conversation:\n{history}\n\n"
        f"Developer: {question}\n"
        f"Assistant:"
    )


def answer_dev_question(question: str, recent_context: list[str] | None = None) -> str:
    """
    Send a development question to the build model and return its reply.
    Falls back to a plain message if Ollama is unreachable.
    """
    prompt = _build_prompt(question, recent_context)

    try:
        response = requests.post(
            f"{config.OLLAMA_BASE_URL}/api/generate",
            json={
                "model": config.BUILD_MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {"num_thread": config.OLLAMA_NUM_THREAD},
            },
            timeout=120,
        )
        response.raise_for_status()
        data = response.json()
        text = strip_think_tags(data.get("response", ""))
        if text and not is_placeholder_text(text):
            return text
    except (requests.RequestException, ValueError) as e:
        print(f"[dev_agent] model call failed: {e}")

    return (
        "I couldn't reach the local model just now, so I can't answer that "
        "one right now — try again in a moment, or check that Ollama is "
        "running (`ollama list`)."
    )
