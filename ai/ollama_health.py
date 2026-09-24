"""
ai/ollama_health.py
Real, observed Ollama health tracking (2026-09-24, found live: a
trivial "Say exactly: Hello, world." diagnostic call took 4+ minutes
and never finished, confirming genuine slowness independent of any
narration prompt's own complexity -- traced to real hypervisor CPU
steal time on this VPS, measured directly via `vmstat` at 16-30%
during the incident, not something os.getloadavg() reflects at all
(bot._ollama_congested's load-average check stayed comfortably under
its own threshold the entire time this was happening).

Rather than trying to detect the many possible ROOT causes (steal
time, thermal throttling, a genuinely oversized prompt, model
misbehavior) separately, this tracks the one thing that actually
matters: did a real recent call to Ollama actually fail/time out.
Every ai/*.py call site's own `except (requests.RequestException,
ValueError)` handler already exists purely to fall back gracefully --
this module gives them a one-line way to also report that failure so
OTHER discretionary callers (bot._ollama_congested, called before an
AI companion's own autonomous turn) can back off for a while too,
instead of learning about the same slowdown the hard way, 200 more
seconds later, one at a time.
"""
import time

_last_timeout_at: float | None = None

# How long a single observed timeout/failure keeps the system flagged
# as "recently congested" -- long enough to skip a few AI-party ticks
# (AI_PARTY_TICK_INTERVAL_SECONDS is 900s/15min) without waiting for a
# whole new failure every single time, short enough that a real,
# one-off blip doesn't block AI companions for the rest of the day.
_CONGESTION_WINDOW_SECONDS = 600.0


def record_timeout() -> None:
    """Call this from any ai/*.py Ollama call site's own except block, right where the failure is already logged."""
    global _last_timeout_at
    _last_timeout_at = time.time()


def recently_congested(window_seconds: float = _CONGESTION_WINDOW_SECONDS) -> bool:
    """True if a real Ollama call has failed/timed out within the last `window_seconds`."""
    return _last_timeout_at is not None and (time.time() - _last_timeout_at) < window_seconds
