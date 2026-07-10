#!/usr/bin/env python3
"""
scripts/rotate_github_token.py
Swaps a new GitHub personal access token into .env's GITHUB_TOKEN line.

GitHub does not allow a token to mint its own replacement — a new PAT
must be created by hand at github.com/settings/tokens. This script only
handles the second half: validating the new token actually works, then
replacing the old one in .env in place (never printing either token).

Usage (token read from stdin, never as a CLI argument, so it never
lands in shell history or a process listing):
    echo "ghp_..." | python3 scripts/rotate_github_token.py
"""
import json
import os
import sys
import urllib.error
import urllib.request

ENV_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")


def validate_token(token: str) -> str:
    """Confirms the token is live and returns the GitHub username it belongs to."""
    req = urllib.request.Request(
        "https://api.github.com/user",
        headers={"Authorization": f"Bearer {token}", "User-Agent": "pandora-mmo-token-rotate"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read()).get("login", "unknown")


def rotate(new_token: str) -> None:
    with open(ENV_PATH, "r", encoding="utf-8") as f:
        lines = f.readlines()

    replaced = False
    for i, line in enumerate(lines):
        if line.startswith("GITHUB_TOKEN="):
            lines[i] = f"GITHUB_TOKEN={new_token}\n"
            replaced = True
            break
    if not replaced:
        lines.append(f"GITHUB_TOKEN={new_token}\n")

    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.writelines(lines)


def main() -> None:
    new_token = sys.stdin.readline().strip()
    if not new_token:
        print("No token provided on stdin.", file=sys.stderr)
        sys.exit(1)

    try:
        username = validate_token(new_token)
    except urllib.error.HTTPError as e:
        print(f"Token validation failed: HTTP {e.code} — not written to .env.", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"Could not reach GitHub to validate the token: {e.reason}", file=sys.stderr)
        sys.exit(1)

    rotate(new_token)
    print(f"GITHUB_TOKEN rotated in .env — validated for GitHub user: {username}")


if __name__ == "__main__":
    main()
