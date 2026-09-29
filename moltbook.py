"""
moltbook.py
Thin wrappers around Moltbook's real, documented REST API
(moltbook.com/heartbeat.md) for PandoraMMO_Bot's agent profile. Pure
HTTP -- no AI, no decision-making. Every function here calls a real,
documented endpoint; nothing is invented. Deciding WHAT to post/comment/
upvote lives in ai/moltbook_agent.py instead.
"""
import requests

import config

BASE_URL = "https://www.moltbook.com/api/v1"


def _headers() -> dict:
    return {"Authorization": f"Bearer {config.MOLTBOOK_API_KEY}"}


def get_feed(sort: str = "new", limit: int = 15) -> list[dict]:
    resp = requests.get(
        f"{BASE_URL}/feed", headers=_headers(), params={"sort": sort, "limit": limit}, timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, list):
        return data
    return data.get("posts", [])


def get_post_comments(post_id: str, sort: str = "new", limit: int = 35) -> list[dict]:
    resp = requests.get(
        f"{BASE_URL}/posts/{post_id}/comments",
        headers=_headers(), params={"sort": sort, "limit": limit}, timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("comments", data) if isinstance(data, dict) else data


def create_post(submolt_name: str, title: str, content: str) -> dict:
    resp = requests.post(
        f"{BASE_URL}/posts",
        headers=_headers(),
        json={"submolt_name": submolt_name, "title": title, "content": content},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def verify_post(verification_code: str, answer: str) -> dict:
    """
    Real gap found 2026-09-29 (audit): every real create_post response
    carries a `verification` block (verification_code, a garbled math
    word-problem challenge_text, an expires_at ~5 minutes out, and
    explicit instructions to "solve the math problem... send your
    answer to POST /api/v1/verify with the verification_code") that
    this codebase had never once called, in 172 logged real posts/
    comments/upvotes since launch. Not documented in heartbeat.md
    (confirmed via WebFetch) -- only ever seen in the live response
    itself. `answer` per that instruction's own wording: "respond with
    ONLY the number (with 2 decimal places)".
    """
    resp = requests.post(
        f"{BASE_URL}/verify",
        headers=_headers(),
        json={"verification_code": verification_code, "answer": answer},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json() if resp.content else {}


def upvote_post(post_id: str) -> dict:
    resp = requests.post(f"{BASE_URL}/posts/{post_id}/upvote", headers=_headers(), timeout=30)
    resp.raise_for_status()
    return resp.json() if resp.content else {}


def add_comment(post_id: str, content: str, parent_id: str | None = None) -> dict:
    body = {"content": content}
    if parent_id:
        body["parent_id"] = parent_id
    resp = requests.post(f"{BASE_URL}/posts/{post_id}/comments", headers=_headers(), json=body, timeout=30)
    resp.raise_for_status()
    return resp.json()


def upvote_comment(comment_id: str) -> dict:
    resp = requests.post(f"{BASE_URL}/comments/{comment_id}/upvote", headers=_headers(), timeout=30)
    resp.raise_for_status()
    return resp.json() if resp.content else {}


def mark_notifications_read(post_id: str) -> None:
    resp = requests.post(f"{BASE_URL}/notifications/read-by-post/{post_id}", headers=_headers(), timeout=30)
    resp.raise_for_status()
