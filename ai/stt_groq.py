"""
ai/stt_groq.py
Voice message transcription via Groq's free Whisper Large-v3-Turbo
endpoint (task #97, 2026-07-22 investigation: "investigate better tts/
stt options that are FREE"). Cloud-hosted, OpenAI-compatible REST API
-- deliberately NOT a local model, since the real blocker on local STT
last time wasn't cost, it was this box being CPU-only and already
running Ollama's single generation slot at load average 8-15 (see
project memory); a second local ML workload would slow down narration
for everyone. Groq's free tier needs no credit card, no local compute,
and covers this game's real scale many times over (2,000 requests/day,
~8 hours of audio/day -- a hobby Telegram group's voice-message volume
isn't going to come close).

Requires GROQ_API_KEY in .env (config.GROQ_API_KEY) -- unset means STT
is simply off (bot.py's voice_message_handler no-ops), same
"missing config disables the feature, doesn't error" convention as
every other optional integration in this codebase.
"""
import logging

import requests

import config

logger = logging.getLogger("pandora_mmo")

GROQ_TRANSCRIPTION_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_STT_MODEL = "whisper-large-v3-turbo"
# Ollama's own calls in this codebase use 200s for the documented real
# 30-160s CPU-bound local latency -- Groq is cloud-hosted, fast
# (real-world transcription completes in a couple of seconds even for
# a multi-minute clip), but a generous timeout still costs nothing on
# a genuine network hiccup and avoids ever cutting off a real answer.
REQUEST_TIMEOUT_SECONDS = 30


def transcribe_voice(audio_bytes: bytes, filename: str = "voice.ogg") -> str | None:
    """
    Sends a real voice-message recording to Groq's Whisper endpoint and
    returns the transcribed text, or None on any failure (no API key
    configured, network error, empty transcription) -- a transcription
    failure should never crash the message pipeline, same "AI hiccup,
    not a lost game fact" convention as every other optional AI call in
    this codebase. Runs synchronously/blocking -- callers on the bot's
    event loop must wrap this in asyncio.to_thread.
    """
    if not config.GROQ_API_KEY:
        return None
    if not audio_bytes:
        return None
    try:
        response = requests.post(
            GROQ_TRANSCRIPTION_URL,
            headers={"Authorization": f"Bearer {config.GROQ_API_KEY}"},
            files={"file": (filename, audio_bytes, "audio/ogg")},
            data={"model": GROQ_STT_MODEL},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        text = (response.json() or {}).get("text", "").strip()
        return text or None
    except Exception as e:
        logger.warning(f"[stt_groq] transcription failed: {e!r}")
        return None
