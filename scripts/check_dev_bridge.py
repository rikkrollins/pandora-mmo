#!/usr/bin/env python3
"""
scripts/check_dev_bridge.py
Lists Development-topic messages (from bot_live_tmp.log's [dev_topic]
lines) that arrived after the last-processed cursor in
.dev_bridge_state.json. Every [dev_topic] line only exists because
bot.py's development_topic_handler already verified the sender is the
group owner before logging it — so anything this prints is a real,
owner-authenticated command, safe to act on.

Usage:
    python3 scripts/check_dev_bridge.py            # list new commands
    python3 scripts/check_dev_bridge.py --mark-processed "2026-07-10 03:45:10,000"
        # advance the cursor once you've handled everything up to (and
        # including) that log timestamp, so it isn't reprocessed next time
"""
import ast
import json
import re
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = REPO_ROOT / ".dev_bridge_state.json"
LOG_PATH = REPO_ROOT / "bot_live_tmp.log"
SCREENSHOTS_DIR = REPO_ROOT / "dev_screenshots"
VIDEOS_DIR = REPO_ROOT / "dev_videos"

LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) \[INFO\] pandora_mmo: "
    r"\[dev_topic\] user=(?P<user>\d+) text=(?P<text>.*)$"
)

# Real live gap (2026-07-16): a screenshot sent to the Development topic
# with a real, actionable caption ("please make narrations say who is
# doing the action") is logged under a DIFFERENT tag (dev_topic_image,
# see bot.py's dev_topic_image_handler) than plain text commands
# (dev_topic) -- this script only ever matched the latter, so an
# image-with-caption request was completely invisible to every
# unattended monitoring cycle. path/caption are logged via Python's
# !r (repr), so ast.literal_eval is the correct inverse parse.
IMAGE_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) \[INFO\] pandora_mmo: "
    r"\[dev_topic_image\] user=(?P<user>\d+) path=(?P<path>.*?) caption=(?P<caption>.*)$"
)

# Same class of gap as the image one above, found 2026-08-08: videos sent
# to the Development topic are logged under yet another tag
# (dev_topic_video, see bot.py's dev_topic_video_handler) that this
# script never matched at all -- a video walkthrough was completely
# invisible to every monitoring cycle, caption or not. Unlike images
# (where no caption means "just decorative"), a video's own extracted
# frames ARE the actionable content even with no caption at all, so
# these always surface.
VIDEO_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) \[INFO\] pandora_mmo: "
    r"\[dev_topic_video\] user=(?P<user>\d+) path=(?P<path>.*?) caption=(?P<caption>.*)$"
)


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"last_processed_at": "", "pending_drafts": []}
    return json.loads(STATE_PATH.read_text())


def load_cursor() -> str:
    return load_state().get("last_processed_at", "")


def _safe_delete(path: Path, allowed_parent: Path) -> None:
    """
    Deletes a file or directory, but only if it genuinely lives under
    `allowed_parent` -- a hard safety rail against a malformed/unexpected
    log-line path ever causing a delete outside dev_screenshots/dev_videos,
    even though every path here originates from this bot's own logging,
    never from unsanitized user input.
    """
    try:
        resolved = path.resolve()
        if allowed_parent.resolve() not in resolved.parents:
            return
        if resolved.is_dir():
            shutil.rmtree(resolved, ignore_errors=True)
        elif resolved.is_file():
            resolved.unlink()
    except OSError:
        pass


def _purge_processed_media(cursor: str) -> None:
    """
    Per Coffee's request (2026-08-08): "make sure all screenshots from
    DEV get purged after the troubleshooting tasks are complete to save
    on file size. same with videos or anything else." Deletes every
    dev_screenshots/dev_videos file whose corresponding log line
    timestamp is <= the newly-advanced cursor -- i.e. anything already
    considered "handled" by this cycle's own workflow. Raw video files
    are usually already gone by this point (dev_topic_video_handler
    deletes them right after frame extraction), but the extracted
    frames directory (video_path + "_frames") is not -- this is what
    actually accumulates on disk, so it's purged here too.
    """
    if not LOG_PATH.exists():
        return
    for line in LOG_PATH.read_text(errors="replace").splitlines():
        m = IMAGE_LINE_RE.match(line)
        if m and m["ts"] <= cursor:
            try:
                path = ast.literal_eval(m["path"])
            except (ValueError, SyntaxError):
                continue
            _safe_delete(REPO_ROOT / path, SCREENSHOTS_DIR)
            continue
        m = VIDEO_LINE_RE.match(line)
        if m and m["ts"] <= cursor:
            try:
                path = ast.literal_eval(m["path"])
            except (ValueError, SyntaxError):
                continue
            video_path = REPO_ROOT / path
            _safe_delete(video_path, VIDEOS_DIR)
            _safe_delete(Path(str(video_path) + "_frames"), VIDEOS_DIR)


def mark_processed(ts: str) -> None:
    """Advances the cursor without disturbing pending_drafts or any other state field, then purges any now-fully-processed screenshots/videos."""
    state = load_state()
    state["last_processed_at"] = ts
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n")
    _purge_processed_media(ts)


def list_new_commands() -> list[dict]:
    cursor = load_cursor()
    if not LOG_PATH.exists():
        return []
    commands = []
    for line in LOG_PATH.read_text(errors="replace").splitlines():
        m = LINE_RE.match(line)
        if m:
            if m["ts"] <= cursor:
                continue
            commands.append({"timestamp": m["ts"], "user": m["user"], "text": m["text"]})
            continue
        m = IMAGE_LINE_RE.match(line)
        if m:
            if m["ts"] <= cursor:
                continue
            try:
                path = ast.literal_eval(m["path"])
                caption = ast.literal_eval(m["caption"])
            except (ValueError, SyntaxError):
                continue
            if not caption:
                continue  # no caption, nothing actionable to surface
            commands.append({
                "timestamp": m["ts"], "user": m["user"],
                "text": f"[image attached at {path}] {caption}",
            })
            continue
        m = VIDEO_LINE_RE.match(line)
        if m:
            if m["ts"] <= cursor:
                continue
            try:
                path = ast.literal_eval(m["path"])
                caption = ast.literal_eval(m["caption"])
            except (ValueError, SyntaxError):
                continue
            frames_dir = f"{path}_frames"
            caption_part = f" {caption}" if caption else " (no caption)"
            commands.append({
                "timestamp": m["ts"], "user": m["user"],
                "text": f"[video frames extracted to {frames_dir}]{caption_part}",
            })
    commands.sort(key=lambda c: c["timestamp"])
    return commands


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--mark-processed":
        mark_processed(sys.argv[2])
        print(f"Cursor advanced to {sys.argv[2]}")
    else:
        for cmd in list_new_commands():
            print(json.dumps(cmd))
