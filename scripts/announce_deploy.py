#!/usr/bin/env python3
"""
scripts/announce_deploy.py
Posts a deployment announcement to the Telegram group's Development
topic. Called after pushing and restarting the bot with new code, per
Coffee's standing request (2026-07-09) that every deploy gets posted
there automatically — see CLAUDE.md's Deployment section.

Sends via the Bot API directly (BOT_TOKEN from .env), independent of
the bot's own getUpdates polling loop — this only ever POSTs a message,
so it can't conflict with the live poller.

Usage:
    python3 scripts/announce_deploy.py "v1.1.0 deployed: lockpicking, crafting, ..."
    python3 scripts/announce_deploy.py --warn "~2 minutes"
    python3 scripts/announce_deploy.py --pin "catchy player-facing update text"

--pin (2026-07-17, per Coffee: "write a pinned post... make it catchy
and enticing... make this part of the update/upgrade system") posts a
player-facing, enticing summary to Main and pins it there, replacing
whatever was pinned before -- meant to be run as a normal step of
every real update from now on, not a one-off. Requires the bot to have
"pin messages" admin rights in the group; if it doesn't, this fails
loudly rather than silently doing nothing, so the missing permission
gets noticed immediately.
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

import config
import db

REQUIRED_ENV = "TELEGRAM_CHAT_ID"
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_GITHUB_REPO = "rikkrollins/pandora-mmo"


def _escape_markdown(text: str) -> str:
    """
    Escapes Telegram legacy Markdown's special characters so a message
    describing code (which routinely mentions function_names, snake_case,
    or asterisks) can't accidentally break parsing. Confirmed live
    2026-07-14, twice: a deploy message mentioning "talk_npc" and later
    "accept_quest" each hit a 400 Bad Request from Telegram's Markdown
    parser choking on the bare underscore (an unmatched italic marker) --
    not something the caller should have to remember to avoid every time.
    """
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, f"\\{ch}")
    return text


def _send(text: str) -> None:
    chat_id = getattr(config, "TELEGRAM_CHAT_ID", None)
    if not chat_id:
        raise RuntimeError(
            f"{REQUIRED_ENV} isn't set in .env — add it (the group's chat id, "
            "usually a negative number) before this script can post anything."
        )

    resp = requests.post(
        f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage",
        json={
            "chat_id": chat_id,
            "message_thread_id": config.TOPIC_DEVELOPMENT_ID,
            "text": text,
            "parse_mode": "Markdown",
        },
        timeout=30,
    )
    resp.raise_for_status()


def announce(message: str) -> None:
    _send(f"🚀 **Deploy update**\n{_escape_markdown(message)}")


def warn(eta: str) -> None:
    """Posted before a redeploy — the bot briefly restarts, players shouldn't be caught off guard."""
    _send(f"🛠️ **Update coming soon** — the bot will restart shortly (ETA: {_escape_markdown(eta)}). Back momentarily.")


_PINNED_MESSAGE_ID_SETTING = "pinned_update_message_id"


def pin_update(catchy_text: str) -> None:
    """
    Posts a player-facing, enticing update summary to Main and pins it,
    unpinning whatever this same mechanism pinned last time (tracked in
    game_settings via db.py, same pattern as every other small piece of
    persistent state in this project). Meant to run as a normal part of
    every real update going forward, not a one-off -- see module
    docstring.
    """
    chat_id = getattr(config, "TELEGRAM_CHAT_ID", None)
    if not chat_id:
        raise RuntimeError(
            f"{REQUIRED_ENV} isn't set in .env — add it (the group's chat id, "
            "usually a negative number) before this script can post anything."
        )

    resp = requests.post(
        f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": catchy_text, "parse_mode": "Markdown"},
        timeout=30,
    )
    resp.raise_for_status()
    new_message_id = resp.json()["result"]["message_id"]

    old_message_id = db.get_setting(_PINNED_MESSAGE_ID_SETTING)
    if old_message_id:
        # Best-effort -- an already-deleted or already-unpinned old
        # message shouldn't stop the new one from getting pinned.
        try:
            requests.post(
                f"https://api.telegram.org/bot{config.BOT_TOKEN}/unpinChatMessage",
                json={"chat_id": chat_id, "message_id": int(old_message_id)},
                timeout=30,
            )
        except requests.RequestException:
            pass

    pin_resp = requests.post(
        f"https://api.telegram.org/bot{config.BOT_TOKEN}/pinChatMessage",
        json={"chat_id": chat_id, "message_id": new_message_id, "disable_notification": False},
        timeout=30,
    )
    pin_resp.raise_for_status()
    db.set_setting(_PINNED_MESSAGE_ID_SETTING, str(new_message_id))


def _read_github_token() -> str:
    env_path = os.path.join(_REPO_ROOT, ".env")
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line.startswith("GITHUB_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("GITHUB_TOKEN isn't set in .env — can't publish a GitHub Release.")


def _changelog_section(version: str) -> tuple[str, str] | None:
    """Returns (title, body) for one version's own '## [x.y.z] — title' section in CHANGELOG.md, or None if it doesn't exist yet."""
    changelog_path = os.path.join(_REPO_ROOT, "CHANGELOG.md")
    with open(changelog_path) as f:
        content = f.read()
    pattern = re.compile(r"^## \[(\d+\.\d+\.\d+)\] — (.+)$", re.MULTILINE)
    matches = list(pattern.finditer(content))
    for i, m in enumerate(matches):
        if m.group(1) != version:
            continue
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        return m.group(2), content[start:end].strip()
    return None


def _read_current_version() -> str:
    with open(os.path.join(_REPO_ROOT, "VERSION")) as f:
        return f.read().strip()


def publish_github_release(version: str) -> str:
    """
    Publishes a real GitHub Release for this version, pulling the title
    and body straight from CHANGELOG.md's own section for it. Real,
    recurring lapse (2026-09-06, then again 2026-09-12: 32 then 33
    versions shipped with zero GitHub Releases, both times only caught
    by someone noticing after the fact, the second time because Coffee
    asked directly: "why hasnt the releases on github been updated?")
    -- leaving this as a manual "remember to also run curl" step failed
    twice in a row. Fails loudly (raises) on a real problem -- a
    missing token or a GitHub API error should be noticed immediately.
    A missing CHANGELOG.md section is treated as "nothing to publish
    yet" (returns None) rather than an error, since _auto_publish_
    release_for_current_version below calls this unconditionally on
    EVERY invocation of this script, including --warn calls that can
    legitimately fire before a version bump's CHANGELOG entry exists.
    """
    token = _read_github_token()
    section = _changelog_section(version)
    if section is None:
        raise RuntimeError(f"No CHANGELOG.md section found for version {version} (expected '## [{version}] — ...').")
    title, body = section
    tag = f"v{version}"
    payload = json.dumps({
        "tag_name": tag,
        "target_commitish": "main",
        "name": f"{tag} — {title}",
        "body": body,
        "draft": False,
        "prerelease": False,
    }).encode()
    req = urllib.request.Request(
        f"https://api.github.com/repos/{_GITHUB_REPO}/releases",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "pandora-mmo-announce-deploy",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.load(resp)
            return result.get("html_url", "")
    except urllib.error.HTTPError as e:
        error_body = e.read().decode()
        if e.code == 422 and "already_exists" in error_body:
            return f"(release {tag} already exists, skipped)"
        raise RuntimeError(f"GitHub Release publish failed ({e.code}): {error_body}")


def _auto_publish_release_for_current_version() -> None:
    """
    Self-healing safety net, called unconditionally at the end of EVERY
    invocation of this script regardless of which flag was used. The
    real gap the two GitHub Release lapses above shared: the release
    publish only ever happened from the --version/--summary branch, so
    forgetting that exact flag combo (a plain-message call, a --warn-
    only call, or just running the wrong command) silently skipped it
    again, exactly like a third lapse waiting to happen. This checks
    the live VERSION file directly and (re)publishes its release if
    CHANGELOG.md already has that version's section and GitHub doesn't
    have the release yet -- regardless of what this particular
    invocation was actually for. Deliberately non-fatal: this must
    never take down a time-sensitive --warn or announce call just
    because GitHub's API hiccuped, but it DOES print a loud, impossible
    -to-miss status line either way, since a human (Claude) reads this
    script's stdout every time it runs.
    """
    version = _read_current_version()
    if _changelog_section(version) is None:
        # Normal, not an error: e.g. a --warn posted before this
        # version's CHANGELOG.md entry has been written yet.
        return
    try:
        result = publish_github_release(version)
        print(f"[github-release] v{version}: {result}")
    except Exception as e:
        print(f"[github-release] ⚠️  WARNING: v{version} release publish FAILED: {e}", file=sys.stderr)
        print(f"[github-release] ⚠️  Fix this and re-run: python3 scripts/announce_deploy.py --version {version} --summary \"...\"", file=sys.stderr)


if __name__ == "__main__":
    usage_lines = [
        "Usage: python3 scripts/announce_deploy.py \"message text\"",
        "       python3 scripts/announce_deploy.py --version X --summary \"...\"",
        "       python3 scripts/announce_deploy.py --warn \"ETA text\"",
        "       python3 scripts/announce_deploy.py --pin \"catchy player-facing text\"",
    ]
    if len(sys.argv) < 2:
        print("\n".join(usage_lines), file=sys.stderr)
        sys.exit(1)
    if sys.argv[1] == "--warn":
        warn(sys.argv[2] if len(sys.argv) > 2 else "a couple minutes")
        print("Posted to Development topic.")
    elif sys.argv[1] == "--pin":
        pin_update(sys.argv[2])
        print("Posted and pinned to Main.")
    elif sys.argv[1] == "--version":
        # 2026-07-17: added after this exact interface was called with
        # --version/--summary flags that silently didn't exist -- the
        # old code fell through to the plain-message branch and posted
        # the literal string "--version" to Development four deploys in
        # a row before anyone noticed, since the script still printed
        # "Posted to Development topic." either way. Supporting the
        # interface for real, rather than just documenting it, is the
        # actual fix; an unrecognized flag now errors loudly (below)
        # instead of silently posting garbage.
        try:
            version_idx = sys.argv.index("--version") + 1
            summary_idx = sys.argv.index("--summary") + 1
            version_text = sys.argv[version_idx]
            summary_text = sys.argv[summary_idx]
        except (ValueError, IndexError):
            print("--version requires both --version X and --summary \"...\"", file=sys.stderr)
            sys.exit(1)
        announce(f"v{version_text} deployed\n\n{summary_text}")
        print("Posted to Development topic.")
    elif sys.argv[1].startswith("--"):
        print(f"Unrecognized flag: {sys.argv[1]}", file=sys.stderr)
        print("\n".join(usage_lines), file=sys.stderr)
        sys.exit(1)
    else:
        announce(sys.argv[1])
        print("Posted to Development topic.")

    # Self-healing GitHub Release check -- runs after EVERY successful
    # invocation above, not just --version calls. See
    # _auto_publish_release_for_current_version's own docstring: this
    # is what actually closes the recurring lapse, since it no longer
    # depends on remembering the right flag combination.
    _auto_publish_release_for_current_version()
