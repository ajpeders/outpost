#!/bin/bash
# Cache the Apple tvOS "aerial" screensaver videos locally for the dashboard.
#
# WHY THIS EXISTS: the aerial screensaver (screen/aerial-mode.py) plays these with
# mpv (Pi-5 hardware decode) on the HDMI and composites the dashboard over them.
# Apple serves the aerials as 1080p H.264 .mov, but the files are NOT "faststart"
# (the moov index atom is at the END), so a player has to grab the tail before it
# can start. We remux each to a faststart MP4 (moov at front); `-c copy` — no
# re-encode, no quality loss. Set RES=720 to instead downscale (re-encode) if a
# clip is too heavy.
#
# Pulls the WHOLE catalogue from Apple's entries.json (every url-1080-H264). Named
# by Apple's stable video id, so it's idempotent: an already-cached, valid MP4 is
# skipped. Re-run any time.
set -u

REPO="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${AERIAL_DIR:-$REPO/data/hub/aerials}"
RES="${RES:-4K}"                       # 4K = url-4K-SDR (HEVC); 1080 = url-1080-H264
MAX="${AERIAL_MAX:-0}"                  # cap number of clips (0 = whole catalogue)
KEY="url-4K-SDR"; [ "$RES" = "1080" ] && KEY="url-1080-H264"
MANIFEST="${AERIAL_MANIFEST:-https://raw.githubusercontent.com/kopiro/xscreensaver-apple-aerial/main/entries.json}"
mkdir -p "$OUT"

echo "fetching manifest: $MANIFEST"
TMP="$(mktemp)"
if ! curl -fsSL "$MANIFEST" -o "$TMP"; then
  echo "manifest fetch failed"; rm -f "$TMP"; exit 1
fi

# emit "name|url" for every unique 1080p H.264 clip; name = Apple's video id stem
mapfile -t CLIPS < <(python3 - "$TMP" "$KEY" "$MAX" <<'PY'
import json, re, sys
d = json.load(open(sys.argv[1])); key = sys.argv[2]; cap = int(sys.argv[3])
seen = set(); out = []
def walk(o):
    if isinstance(o, dict):
        u = o.get(key)
        if u and u not in seen:
            seen.add(u)
            stem = re.sub(r"\.mov$", "", u.rsplit("/", 1)[-1], flags=re.I)
            stem = re.sub(r"[^A-Za-z0-9]+", "-", stem).strip("-").lower()
            out.append((stem, u))
        for v in o.values():
            walk(v)
    elif isinstance(o, list):
        for i in o:
            walk(i)
walk(d)
if cap > 0:
    out = out[:cap]
for stem, u in out:
    print(f"{stem}|{u}")
PY
)
rm -f "$TMP"
echo "manifest has ${#CLIPS[@]} clips"

ok=0; skip=0; fail=0
for entry in "${CLIPS[@]}"; do
  name="${entry%%|*}"; url="${entry#*|}"
  out="$OUT/$name.mp4"
  if [ -s "$out" ] && ffprobe -v error -i "$out" >/dev/null 2>&1; then
    skip=$((skip+1)); continue
  fi
  echo "fetch $name"
  tmp="$out.part"
  if [ "$RES" = "720" ]; then
    ffmpeg -y -loglevel warning -i "$url" -an \
           -vf "scale=-2:720" -c:v libx264 -preset veryfast -crf 23 \
           -movflags +faststart -f mp4 "$tmp"
  else
    ffmpeg -y -loglevel warning -i "$url" -an -c:v copy \
           -movflags +faststart -f mp4 "$tmp"
  fi
  if [ $? -eq 0 ] && ffprobe -v error -i "$tmp" >/dev/null 2>&1; then
    mv -f "$tmp" "$out"; echo "  done $name.mp4 ($(du -h "$out" | cut -f1))"; ok=$((ok+1))
  else
    rm -f "$tmp"; echo "  FAIL $name"; fail=$((fail+1))
  fi
done

echo "----"
echo "aerials: $ok new, $skip cached, $fail failed  ->  $OUT"
echo "total cached clips: $(ls -1 "$OUT"/*.mp4 2>/dev/null | wc -l)"

# tag new clips day/night (luminance) so aerial-mode can match the hour
python3 "$(dirname "$0")/classify-aerials.py" || echo "classify failed (non-fatal)"
