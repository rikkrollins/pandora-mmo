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
from datetime import datetime, timedelta, timezone

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


# Real hazard/aggression conditions this system feeds into real combat
# math (task, per Coffee, 2026-07-21: "use weather or time of day to
# change factors... raining = wet = slippery... Night time monsters are
# more aggressive"). Kept as real, named, exported constants rather
# than bare strings scattered at each call site, so bot.py's combat
# hooks and this module's own forecast stay in sync automatically if
# the weather list ever changes.
HAZARDOUS_WEATHER = {"steady rain", "rolling thunderstorms"}


def is_hazardous(region: str, now: datetime | None = None) -> bool:
    """True when the real current weather for this region is slippery/dangerous underfoot (rain or storm)."""
    return current_weather(region, now) in HAZARDOUS_WEATHER


def is_night(now: datetime | None = None) -> bool:
    """True during Night specifically (not Dawn/Day/Dusk) -- monsters are more aggressive then (see bot.py's _attack_advantage_disadvantage)."""
    return current_time_of_day(now) == "Night"


def forecast(region: str, days: int = 3, now: datetime | None = None) -> list[tuple[str, str | None]]:
    """
    A real, deterministic multi-day forecast -- reuses the exact same
    (day, region) hash current_weather already computes, just walked
    forward across future real calendar days, so "tomorrow" always
    actually turns out to be whatever this returns (never a separate
    invented guess). Returns (ISO date, weather) pairs, weather None
    for underground the same way current_weather already is.
    """
    now = now or datetime.now(timezone.utc)
    return [
        ((now + timedelta(days=offset)).date().isoformat(), current_weather(region, now + timedelta(days=offset)))
        for offset in range(days)
    ]
