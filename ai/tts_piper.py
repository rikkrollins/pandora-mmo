"""
Local, self-hosted TTS via Piper (see the 2026-07-18 POC that confirmed
feasibility on this CPU-only box: ~2.6s compute for an 8s narration
line, no meaningful contention with Ollama's single generation slot).

Wired into bot.py's _maybe_speak as an alternative backend to the
existing @TextTSBot integration, selected via
db.get_setting("tts_backend") -- "textbot" (default, unchanged
behavior) or "piper" (this module). Fully local and free: no
third-party bot dependency, no per-message cost, no network call once
a voice is cached.

Deliberately returns raw WAV bytes, not an OGG/Opus conversion -- this
project's CLAUDE.md security boundary is explicit that NOTHING adds a
subprocess call anywhere in this codebase, and shelling out to ffmpeg
for the OGG/Opus re-encode Telegram's real voice-bubble UI wants would
require exactly that. Sent via send_audio (a normal playable audio
attachment) instead of send_voice (the compact voice-note bubble,
which Telegram only renders for genuine OGG/Opus) -- still a real,
functional spoken narration, just a different bubble style.

Voice models (~60MB each) are not committed to git (see voice_models/
in .gitignore) -- each is downloaded once on first use and cached on
disk from here on.

Two real voices (2026-07-22, task #97 "better TTS" follow-up), not
per-individual-NPC ones: this campaign's NPCs (campaign.json) carry no
gender field at all, and guessing one from a name would be inventing a
fact about the character that was never actually written -- the same
"never invent a game fact" rule this project applies to AI narration
generally. What IS a real, always-available distinction is WHO's
voice a line represents: the narrator describing the world, or an NPC
actually speaking (every NPC dialogue line in this game already starts
with the same "💬 **Name:**" marker -- see bot.py's _pick_piper_voice).
That's a real, structural fact already present in the text, not a
guess, so voice selection keys off that instead.
"""
import io
import logging
import wave
from pathlib import Path

import requests

logger = logging.getLogger("pandora_mmo")

VOICE_MODELS_DIR = Path(__file__).parent.parent / "voice_models"
DOWNLOAD_TIMEOUT_SECONDS = 60

# Real, distinct, free Piper voices (rhasspy/piper-voices on Hugging
# Face) -- "narrator" is the exact same en_US-amy-low voice this
# backend already used and already has cached on disk (unchanged, no
# new download for the common case), "npc" is a genuinely different
# voice (British-accented) so dialogue reads distinctly from narration
# without claiming any individual NPC's gender.
VOICES = {
    "narrator": {
        "name": "en_US-amy-low",
        "base_url": "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/low",
    },
    "npc": {
        "name": "en_GB-alan-medium",
        "base_url": "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/alan/medium",
    },
}
DEFAULT_VOICE_KEY = "narrator"

_loaded_voices: dict[str, object] = {}
_failed_voices: set[str] = set()


def _voice_paths(voice_key: str) -> tuple[Path, Path]:
    name = VOICES[voice_key]["name"]
    return VOICE_MODELS_DIR / f"{name}.onnx", VOICE_MODELS_DIR / f"{name}.onnx.json"


def _ensure_voice_downloaded(voice_key: str) -> bool:
    onnx_path, json_path = _voice_paths(voice_key)
    name = VOICES[voice_key]["name"]
    base_url = VOICES[voice_key]["base_url"]
    VOICE_MODELS_DIR.mkdir(exist_ok=True)
    for fname, path in ((f"{name}.onnx", onnx_path), (f"{name}.onnx.json", json_path)):
        if path.exists():
            continue
        try:
            resp = requests.get(f"{base_url}/{fname}", timeout=DOWNLOAD_TIMEOUT_SECONDS)
            resp.raise_for_status()
            path.write_bytes(resp.content)
        except Exception as e:
            logger.warning(f"[tts_piper] failed to download voice file {fname!r}: {e!r}")
            return False
    return True


def _get_voice(voice_key: str):
    """
    Lazily loads and caches each named voice for the lifetime of the
    process, independently -- one voice's failed download/load never
    blocks the other from working. Remembers a failed load
    (_failed_voices) so a broken voice doesn't retry the same
    expensive failure on every single narrated message.
    """
    if voice_key not in VOICES:
        voice_key = DEFAULT_VOICE_KEY
    if voice_key in _loaded_voices:
        return _loaded_voices[voice_key]
    if voice_key in _failed_voices:
        return None
    if not _ensure_voice_downloaded(voice_key):
        _failed_voices.add(voice_key)
        return None
    try:
        from piper import PiperVoice
        onnx_path, json_path = _voice_paths(voice_key)
        voice = PiperVoice.load(str(onnx_path), config_path=str(json_path))
    except Exception as e:
        logger.warning(f"[tts_piper] failed to load voice model {voice_key!r}: {e!r}")
        _failed_voices.add(voice_key)
        return None
    _loaded_voices[voice_key] = voice
    return voice


def synthesize_to_wav_bytes(text: str, voice_key: str = DEFAULT_VOICE_KEY) -> bytes | None:
    """
    Synthesizes `text` to speech locally via Piper and returns WAV
    bytes, using the named voice (see VOICES; falls back to the
    default narrator voice for an unrecognized key). Runs entirely
    synchronously/CPU-bound -- callers on the bot's event loop must
    wrap this in asyncio.to_thread, same convention as every other
    Ollama/AI call in this codebase. Returns None on any failure
    (missing voice, synth error) so a TTS hiccup never breaks the real
    text narration it accompanies.
    """
    voice = _get_voice(voice_key)
    if voice is None:
        return None
    if not text.strip():
        return None

    try:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav_file:
            voice.synthesize_wav(text, wav_file)
        return buf.getvalue()
    except Exception as e:
        logger.warning(f"[tts_piper] synthesis failed: {e!r}")
        return None
