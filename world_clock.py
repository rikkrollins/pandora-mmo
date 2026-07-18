"""
world_clock.py
Deterministic time-of-day and weather (task #84) -- computed from real
wall-clock time, never invented by the AI narrator. Same ground-truth-
first philosophy as rules/dice.py: this module decides the real fact,
narration only ever describes it.

Weather is stable for a given (real calendar day, region) pair -- it
changes once per real day, and every player sees the same conditions
at the same moment, rather than each narration call rolling its own.
"""
import hashlib
from datetime import datetime, timezone

_TIME_BUCKETS = [
    (5, 8, "Dawn"),
    (8, 18, "Day"),
    (18, 21, "Dusk"),
]

_WEATHER_TYPES = [
    "clear skies", "light clouds", "steady rain",
    "gusting wind", "thick fog", "rolling thunderstorms",
]


def current_time_of_day(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    for start, end, name in _TIME_BUCKETS:
        if start <= now.hour < end:
            return name
    return "Night"


def current_weather(region: str, now: datetime | None = None) -> str | None:
    """
    Returns None for 'underground' -- real weather doesn't reach that
    deep, an honest simplification rather than inventing subterranean
    weather. 'surface' and 'sky' locations get a real, deterministic
    condition, stable for the whole real calendar day.
    """
    if region == "underground":
        return None
    now = now or datetime.now(timezone.utc)
    day_key = now.date().isoformat()
    digest = hashlib.sha256(f"{day_key}:{region}".encode()).hexdigest()
    return _WEATHER_TYPES[int(digest, 16) % len(_WEATHER_TYPES)]


def conditions_line(region: str, now: datetime | None = None) -> str:
    """e.g. 'Day, steady rain' or 'Night' (underground has no weather to report)."""
    time_of_day = current_time_of_day(now)
    weather = current_weather(region, now)
    return f"{time_of_day}, {weather}" if weather else time_of_day
