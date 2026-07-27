#!/usr/bin/env python3
"""Tag cached aerial clips as day or night by measuring average luminance.

Apple's modern entries.json doesn't carry a time-of-day field, so we measure
instead: sample a few frames at 25% and 75% of each clip and average the Y
(luma) plane. Night flyovers sit well below YAVG ~60 (0-255); day clips well
above. Writes AERIAL_DIR/timeofday.json — {"<file>.mp4": "day"|"night"} — which
aerial-mode.py uses to build hour-appropriate playlists. Clips missing from the
map (new downloads before a re-run) are treated as fine for any hour.

Idempotent + incremental: already-classified files are skipped unless
--reclassify. Run after fetch-aerials.sh.
"""
from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import sys

AERIAL_DIR = os.environ.get(
    "AERIAL_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "data/hub/aerials"))
NIGHT_THRESHOLD = float(os.environ.get("AERIAL_NIGHT_YAVG", "60"))
TOD_FILE = os.path.join(AERIAL_DIR, "timeofday.json")


def _duration(path: str) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=30).stdout.strip()
        return float(out)
    except (subprocess.TimeoutExpired, ValueError):
        return 0.0


def _yavg_at(path: str, seconds: float) -> float | None:
    """Mean luma of 2 frames starting at `seconds` (signalstats YAVG)."""
    src = path.replace("\\", "\\\\").replace("'", r"\'").replace(":", r"\:")
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-f", "lavfi",
             # format=gray forces 8-bit luma first — the 4K HEVC clips are
             # 10-bit, where raw YAVG comes back on a 0-1023 scale
             "-i", f"movie='{src}':seek_point={seconds},format=gray,signalstats",
             "-show_entries", "frame_tags=lavfi.signalstats.YAVG",
             "-of", "default=noprint_wrappers=1:nokey=1",
             "-read_intervals", "%+#2"],
            capture_output=True, text=True, timeout=120).stdout
    except subprocess.TimeoutExpired:
        return None
    vals = [float(v) for v in re.findall(r"[\d.]+", out)]
    return sum(vals) / len(vals) if vals else None


def classify(path: str) -> str | None:
    dur = _duration(path)
    if not dur:
        return None
    samples = [v for v in (_yavg_at(path, dur * p) for p in (0.25, 0.75))
               if v is not None]
    if not samples:
        return None
    mean = sum(samples) / len(samples)
    tod = "night" if mean < NIGHT_THRESHOLD else "day"
    print(f"  {os.path.basename(path)}: YAVG {mean:.1f} -> {tod}")
    return tod


def main() -> None:
    reclassify = "--reclassify" in sys.argv
    try:
        with open(TOD_FILE) as fh:
            tod_map = json.load(fh)
    except (OSError, ValueError):
        tod_map = {}
    clips = sorted(glob.glob(os.path.join(AERIAL_DIR, "*.mp4")))
    if not clips:
        sys.exit(f"no clips in {AERIAL_DIR}")
    changed = False
    for clip in clips:
        name = os.path.basename(clip)
        if name in tod_map and not reclassify:
            continue
        tod = classify(clip)
        if tod:
            tod_map[name] = tod
            changed = True
    # drop entries for clips that no longer exist
    names = {os.path.basename(c) for c in clips}
    for stale in [k for k in tod_map if k not in names]:
        del tod_map[stale]
        changed = True
    if changed:
        with open(TOD_FILE, "w") as fh:
            json.dump(tod_map, fh, indent=1, sort_keys=True)
    day = sum(1 for v in tod_map.values() if v == "day")
    print(f"{len(tod_map)} classified: {day} day, {len(tod_map) - day} night -> {TOD_FILE}")


if __name__ == "__main__":
    main()
