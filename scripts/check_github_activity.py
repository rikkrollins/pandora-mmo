#!/usr/bin/env python3
"""
scripts/check_github_activity.py
Lists real external GitHub activity on this repo (new issues, issue
comments, pull requests, PR comments, stars, forks) that arrived after
the last-processed cursor in .github_activity_state.json -- the same
"print new events since the cursor, advance it once handled" shape as
scripts/check_dev_bridge.py / check_error_log.py / check_topic_
activity.py, so it slots into the same 3-check (now 4-check) monitoring
cron without a new pattern to learn.

Added 2026-10-08 per Coffee: "make something so if there is activity
on github we are notified and can act on it" -- right after the repo
went public and picked up its first real discoverability work (LICENSE,
CONTRIBUTING, issue/PR templates, topics). A public repo with nobody
watching for a first issue/PR/star is a real missed-engagement risk,
the same shape as the dev-bridge/topic-activity gaps this cron already
exists to close.

Uses GitHub's repo Events API (no extra auth scope beyond the existing
GITHUB_TOKEN already in .env). Cursored by each event's own `created_at`
timestamp (same convention check_dev_bridge.py/check_topic_activity.py
already use) -- NOT by event `id`: confirmed live that GitHub's Events
API does not return ids in strict monotonic order (a batch pulled
2026-10-08 had a higher id appear BEFORE several lower ones), so
"highest id seen" would have silently skipped real events. Deliberately filters OUT PushEvent
and ReleaseEvent -- those are this project's own routine work, already
tracked via git log/CHANGELOG.md, and would just be noise here. Only
surfaces events that represent someone ELSE interacting with the repo:
a new issue, a comment on an issue/PR, a new pull request, a new star,
a new fork.

Note: GitHub Discussions are NOT covered by the classic REST Events
API (would need a separate GraphQL poll) -- not wired in here since
Discussions just got enabled and has zero activity yet; revisit if
that becomes a real channel.

Usage:
    python3 scripts/check_github_activity.py            # list new activity
    python3 scripts/check_github_activity.py --mark-processed "2026-10-08T03:45:10Z"
        # advance the cursor to this event's created_at timestamp once
        # everything up to (and including) it has been handled
"""
import json
import os
import sys
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = REPO_ROOT / ".github_activity_state.json"
REPO_SLUG = "rikkrollins/pandora-mmo"

# Only event types that represent someone ELSE engaging with the repo --
# never our own routine commits/releases (PushEvent/ReleaseEvent), which
# are already tracked via git log/CHANGELOG.md and would just be noise.
_RELEVANT_TYPES = {
    "IssuesEvent", "IssueCommentEvent", "PullRequestEvent",
    "PullRequestReviewEvent", "PullRequestReviewCommentEvent",
    "WatchEvent", "ForkEvent",
}


def _read_github_token() -> str | None:
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return None
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line.startswith("GITHUB_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"last_processed_at": ""}
    return json.loads(STATE_PATH.read_text())


def load_cursor() -> str:
    return load_state().get("last_processed_at", "")


def mark_processed(created_at: str) -> None:
    STATE_PATH.write_text(json.dumps({"last_processed_at": created_at}, indent=2) + "\n")


def _summarize(event: dict) -> str | None:
    etype = event["type"]
    actor = event.get("actor", {}).get("login", "someone")
    payload = event.get("payload", {})

    if etype == "IssuesEvent" and payload.get("action") == "opened":
        issue = payload.get("issue", {})
        return f"{actor} opened issue #{issue.get('number')}: {issue.get('title')!r} — {issue.get('html_url')}"
    if etype == "IssueCommentEvent" and payload.get("action") == "created":
        issue = payload.get("issue", {})
        comment = payload.get("comment", {})
        kind = "PR" if "pull_request" in issue else "issue"
        return f"{actor} commented on {kind} #{issue.get('number')} ({issue.get('title')!r}): {comment.get('body', '')[:200]!r} — {comment.get('html_url')}"
    if etype == "PullRequestEvent" and payload.get("action") == "opened":
        pr = payload.get("pull_request", {})
        return f"{actor} opened PR #{pr.get('number')}: {pr.get('title')!r} — {pr.get('html_url')}"
    if etype == "PullRequestReviewEvent" and payload.get("action") == "submitted":
        pr = payload.get("pull_request", {})
        review = payload.get("review", {})
        return f"{actor} reviewed PR #{pr.get('number')} ({review.get('state')}) — {review.get('html_url')}"
    if etype == "PullRequestReviewCommentEvent" and payload.get("action") == "created":
        pr = payload.get("pull_request", {})
        comment = payload.get("comment", {})
        return f"{actor} commented on PR #{pr.get('number')} code: {comment.get('body', '')[:200]!r} — {comment.get('html_url')}"
    if etype == "WatchEvent":
        return f"⭐ {actor} starred the repo"
    if etype == "ForkEvent":
        forkee = payload.get("forkee", {})
        return f"🍴 {actor} forked the repo — {forkee.get('html_url')}"
    return None


def list_new_activity() -> list[dict]:
    token = _read_github_token()
    if not token:
        print("GITHUB_TOKEN not found in .env", file=sys.stderr)
        return []
    cursor = load_cursor()
    resp = requests.get(
        f"https://api.github.com/repos/{REPO_SLUG}/events",
        headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
        params={"per_page": 100},
        timeout=30,
    )
    resp.raise_for_status()
    events = resp.json()

    new_events = []
    for event in events:
        if event["created_at"] <= cursor:
            continue
        if event["type"] not in _RELEVANT_TYPES:
            continue
        summary = _summarize(event)
        if summary is None:
            continue
        new_events.append({"id": event["id"], "created_at": event["created_at"], "summary": summary})

    new_events.sort(key=lambda e: e["created_at"])
    return new_events


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--mark-processed":
        mark_processed(sys.argv[2])
        print(f"Cursor advanced to event id {sys.argv[2]}")
    else:
        for event in list_new_activity():
            print(json.dumps(event))
