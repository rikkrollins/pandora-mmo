#!/usr/bin/env python3
"""
scripts/check_error_log.py
Lists real unhandled exceptions (bot.py's global `_log_unhandled_error`
handler, registered via `application.add_error_handler`) that arrived
after the last-processed cursor in .error_log_state.json. Every one of
these represents an update that crashed a handler somewhere -- without
that global handler, this would have failed completely silently with
no reply to whoever triggered it and no trace at all (see
_log_unhandled_error's own docstring in bot.py).

Unlike check_dev_bridge.py (owner reports) and check_topic_activity.py
(ambient player traffic), this is the one check that doesn't depend on
anyone noticing and describing a symptom first -- it catches a crash
directly from the log, even a quiet one nobody happened to report.

Real occurrences are grouped by their exact exception summary line
(e.g. "TypeError: 'NoneType' object is not subscriptable") so a burst
of N identical crashes (a flaky network blip retried automatically,
the same bug hit by the same player repeatedly) surfaces as ONE finding
with a count, not N duplicate rows -- matching this project's own
"don't force N findings out of one repeated signal" convention.

Each finding also includes `code_location`: the innermost real-code
frame (bot.py/ai/*.py/rules/*.py/db.py -- never a telegram/site-packages
library frame) still on the stack when the exception fired, i.e. where
to start reading in the actual codebase. This is `null` when the crash
never actually left library code (e.g. a raw network error) -- almost
always a sign this specific occurrence isn't a real code bug.

A `telegram.error.*` exception is not automatically noise: most ARE a
transient network hiccup (NetworkError, RetryAfter, "query is too
old") with nothing to fix on our side, but some are a real, fixable
bug wearing telegram's exception class (e.g. `telegram.error.BadRequest:
Can't parse entities...` almost always means OUR OWN outgoing text has
malformed Markdown -- a real narration-formatting bug). This script
deliberately does NOT try to auto-classify real-bug-vs-noise by
exception type -- that judgment call belongs to whoever reviews the
output (a live session, or the monitoring cron's own investigation
step), the same way check_topic_activity.py defers "is this real
signal" to the reviewer rather than guessing in the script itself.

Usage:
    python3 scripts/check_error_log.py            # list new error groups
    python3 scripts/check_error_log.py --mark-processed "2026-08-21 02:36:04,433"
        # advance the cursor once you've reviewed everything up to (and
        # including) that log timestamp, so it isn't reprocessed next time
"""
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = REPO_ROOT / ".error_log_state.json"
LOG_PATH = REPO_ROOT / "bot_live_tmp.log"

TIMESTAMPED_LINE_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} \[")
TRIGGER_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) \[ERROR\] pandora_mmo: "
    r"Unhandled exception while processing update"
)
# Our own real code, never a third-party library frame -- see
# code_location's own docstring note above.
OUR_CODE_FRAME_RE = re.compile(
    r'^  File "[^"]*/pandora_mmo/((?:bot|shop)\.py|ai/[^"]+\.py|rules/[^"]+\.py|db\.py)", '
    r'line (\d+), in (\S+)'
)


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"last_processed_at": ""}
    return json.loads(STATE_PATH.read_text())


def load_cursor() -> str:
    return load_state().get("last_processed_at", "")


def mark_processed(ts: str) -> None:
    state = load_state()
    state["last_processed_at"] = ts
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n")


def _parse_one_traceback(lines: list[str], start: int) -> tuple[int, str | None, str | None]:
    """
    Scans forward from the trigger line (start) through the raw
    traceback text that follows it -- which, unlike every other log
    line, has NO timestamp/level prefix of its own (it's Python's
    default exception formatter output, appended verbatim via
    logger.error(..., exc_info=...)) -- until the next real timestamped
    log line or EOF. Returns (index of that next line, the final
    "ExceptionClass: message" summary line, the innermost our-code
    frame as "path:line in func" or None if the crash never left
    library code).
    """
    i = start + 1
    exception_summary = None
    our_code_frame = None
    while i < len(lines) and not TIMESTAMPED_LINE_RE.match(lines[i]):
        line = lines[i].rstrip("\n")
        m = OUR_CODE_FRAME_RE.match(line)
        if m:
            our_code_frame = f"{m.group(1)}:{m.group(2)} in {m.group(3)}"
        elif line and not line[0].isspace() and not line.startswith(("Traceback", "Exception ignored")):
            exception_summary = line
        i += 1
    return i, exception_summary, our_code_frame


def list_new_errors() -> list[dict]:
    cursor = load_cursor()
    if not LOG_PATH.exists():
        return []
    lines = LOG_PATH.read_text(errors="replace").splitlines(keepends=True)

    groups: dict[str, dict] = {}
    order: list[str] = []
    i = 0
    while i < len(lines):
        m = TRIGGER_RE.match(lines[i])
        if not m:
            i += 1
            continue
        ts = m["ts"]
        next_i, exception_summary, our_code_frame = _parse_one_traceback(lines, i)
        i = next_i
        if ts <= cursor:
            continue
        key = exception_summary or "(no exception text captured -- inspect the log directly at this timestamp)"
        if key not in groups:
            groups[key] = {
                "exception": key, "code_location": our_code_frame,
                "first_seen": ts, "last_seen": ts, "count": 0,
            }
            order.append(key)
        groups[key]["last_seen"] = ts
        groups[key]["count"] += 1
        # Keep the first real code_location we found for this exception
        # text, in case a later occurrence's traceback got truncated by
        # a coincidental log line collision.
        if groups[key]["code_location"] is None and our_code_frame:
            groups[key]["code_location"] = our_code_frame

    return [groups[k] for k in order]


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--mark-processed":
        mark_processed(sys.argv[2])
        print(f"Cursor advanced to {sys.argv[2]}")
    else:
        for group in list_new_errors():
            print(json.dumps(group))
