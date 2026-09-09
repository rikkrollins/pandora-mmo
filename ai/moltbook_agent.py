"""
ai/moltbook_agent.py
Decides PandoraMMO_Bot's next social action on Moltbook (the AI-agent
social network), per Coffee's explicit direction (2026-07-11): full
autonomy, no human review before publishing -- his own informed call,
made after the tradeoff (irreversible public posts under the project's
name, no review) was explicitly laid out to him.

Grounded strictly in the REAL feed content actually fetched from
Moltbook and the game's own REAL recent activity -- never invents what
another agent said, never fabricates engagement, never claims a game
milestone that didn't really happen. Output is a strict, easy-to-parse
format (not natural language) since this decision is never routed
through intent_parser's allowlist -- there is no fixed-action safety
net here the way there is for in-game player input, so the parser
itself must be conservative: any response that doesn't cleanly match
the expected format is treated as "skip", never guessed at.
"""
import re

import requests

import config
from ai.text_cleanup import strip_think_tags

SOCIAL_ACTION_PROMPT = """You are PandoraMMO_Bot, an AI agent running a text-based \
5th-edition-style tabletop RPG MMO for a Telegram group, active on Moltbook (a social \
network for AI agents). Decide ONE social action to take right now, based ONLY \
on the real posts and real game activity given below -- never invent what \
another agent said, never claim a game event that isn't listed.

Respond in EXACTLY this format, one of these four lines and nothing else:
SKIP
UPVOTE_POST <post_id>
COMMENT_POST <post_id> :: <your comment text, one or two sentences, genuine and specific to that post>
CREATE_POST <title> :: <content, a few sentences>

Only use CREATE_POST if real recent game activity is listed below worth sharing \
-- don't post just to post. Only use COMMENT_POST or UPVOTE_POST on a post_id \
that is actually listed below. If nothing here is worth engaging with, respond \
SKIP.

Real posts currently in the feed:
{feed_block}

Real recent activity in the game (only use this for CREATE_POST, and only if \
genuinely worth sharing):
{activity_block}

Your action:"""


def _format_feed(feed_posts: list[dict]) -> str:
    if not feed_posts:
        return "(feed is empty right now)"
    lines = []
    for post in feed_posts[:10]:
        post_id = post.get("id") or post.get("post_id") or ""
        title = post.get("title") or ""
        content = (post.get("content") or post.get("body") or "")[:300]
        author_field = post.get("author")
        if isinstance(author_field, dict):
            author = author_field.get("name") or "someone"
        else:
            author = author_field or post.get("agent_name") or post.get("username") or "someone"
        if not post_id:
            continue
        lines.append(f"- id={post_id} by {author}: \"{title}\" -- {content}")
    return "\n".join(lines) if lines else "(feed is empty right now)"


def _format_activity(recent_activity: list[str]) -> str:
    if not recent_activity:
        return "(nothing new worth sharing right now)"
    return "\n".join(f"- {a}" for a in recent_activity[:5])


def decide_social_action(feed_posts: list[dict], recent_activity: list[str]) -> dict:
    """
    Returns one of:
      {"action": "skip"}
      {"action": "upvote_post", "post_id": str}
      {"action": "comment_post", "post_id": str, "content": str}
      {"action": "create_post", "title": str, "content": str}
    Falls back to {"action": "skip"} if Ollama is unreachable OR the
    response doesn't cleanly match the expected format -- never guesses.
    """
    prompt = SOCIAL_ACTION_PROMPT.format(
        feed_block=_format_feed(feed_posts),
        activity_block=_format_activity(recent_activity),
    )
    valid_post_ids = {
        str(p.get("id") or p.get("post_id")) for p in feed_posts if p.get("id") or p.get("post_id")
    }

    try:
        response = requests.post(
            f"{config.OLLAMA_BASE_URL}/api/generate",
            json={"model": config.BUILD_MODEL, "prompt": prompt, "stream": False},
            timeout=200,
        )
        response.raise_for_status()
        data = response.json()
        text = strip_think_tags(data.get("response", "")).strip()
    except (requests.RequestException, ValueError):
        return {"action": "skip"}

    first_line = text.splitlines()[0].strip() if text else ""

    if first_line.upper().startswith("SKIP"):
        return {"action": "skip"}

    m = re.match(r"UPVOTE_POST\s+(\S+)", first_line, re.IGNORECASE)
    if m and m.group(1) in valid_post_ids:
        return {"action": "upvote_post", "post_id": m.group(1)}

    m = re.match(r"COMMENT_POST\s+(\S+)\s*::\s*(.+)", first_line, re.IGNORECASE)
    if m and m.group(1) in valid_post_ids and m.group(2).strip():
        return {"action": "comment_post", "post_id": m.group(1), "content": m.group(2).strip()}

    m = re.match(r"CREATE_POST\s+(.+?)\s*::\s*(.+)", first_line, re.IGNORECASE)
    if m and m.group(1).strip() and m.group(2).strip():
        return {"action": "create_post", "title": m.group(1).strip(), "content": m.group(2).strip()}

    # Anything that doesn't cleanly match one of the four exact formats
    # is treated as skip, never guessed at or partially acted on.
    return {"action": "skip"}
