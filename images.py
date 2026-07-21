"""
images.py
Task #81, per Coffee: "Generate images across the game — free API,
hidden/obfuscated storage, surprise for Coffee too." Pollinations.ai is
the chosen service (Coffee's pick, 2026-07-21): free, no API key or
account needed, a plain HTTP GET against a URL returns a real generated
image. No local storage at all — Telegram fetches the URL itself when
sent as a photo, so there's nothing to store, obfuscate, or leak.
"""
import urllib.parse

POLLINATIONS_BASE = "https://image.pollinations.ai/prompt/"


def generate_image_url(prompt: str, width: int = 512, height: int = 512, seed: int | None = None) -> str:
    """A real Pollinations.ai image URL for this prompt — the image itself is generated server-side on first fetch."""
    encoded = urllib.parse.quote(prompt)
    url = f"{POLLINATIONS_BASE}{encoded}?width={width}&height={height}&nologo=true"
    if seed is not None:
        url += f"&seed={seed}"
    return url
