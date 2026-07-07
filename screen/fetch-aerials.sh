#!/bin/bash
# Cache the Apple tvOS "aerial" screensaver videos locally for the dashboard.
#
# WHY THIS EXISTS: the dashboard shows moving aerials behind the clock via an
# HTML5 <video> in the (software-rendered) chromium kiosk. Apple serves these as
# 1080p H.264 .mov, but the files are NOT "faststart" — the moov index atom is at
# the END, so a browser must download the whole ~180MB before it can start. So we
# pull each clip once and remux it to a faststart MP4 (moov at the front), served
# locally by the hub at /aerials/*.mp4. Remux is `-c copy` — no re-encode, no
# quality loss. Set RES=720 to instead downscale (re-encode) if 1080p is too heavy
# for the software compositor.
#
# Idempotent: an already-produced, valid MP4 is skipped. Re-run any time.
set -u

REPO="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${AERIAL_DIR:-$REPO/data/hub/aerials}"   # hub mounts ./data/hub -> /data, so this is /aerials
RES="${RES:-1080}"                            # 1080 = remux/copy; 720 = downscale re-encode
mkdir -p "$OUT"

# name|url  (the 1080p H.264 variants from Apple's entries.json / AerialViews)
CLIPS=(
  "01-hawaii|https://sylvan.apple.com/Videos/comp_H004_C007_PS_v02_SDR_PS_20180925_SDR_2K_AVC.mov"
  "02-new-york|https://sylvan.apple.com/Videos/comp_N008_C003_PS_v01_SDR_PS_20180925_SDR_2K_AVC.mov"
  "03-dubai|https://sylvan.apple.com/Videos/comp_DB_D011_C010_PSNK_DENOISE_v19_SDR_PS_20180914_SDR_2K_AVC.mov"
  "04-hong-kong|https://sylvan.apple.com/Videos/comp_HK_B005_C011_PSNK_v16_SDR_PS_20180914_SDR_2K_AVC.mov"
  "05-london|https://sylvan.apple.com/Videos/comp_L007_C007_PS_v01_SDR_PS_20180925_SDR_2K_AVC.mov"
  "06-greenland|https://sylvan.apple.com/Videos/comp_GL_G004_C010_PSNK_v04_SDR_PS_FINAL_20180709_SDR_2K_AVC.mov"
  "07-new-zealand|https://sylvan.apple.com/Videos/comp_A105_C003_0212CT_FLARE_v10_SDR_PS_FINAL_20180711_SDR_2K_AVC.mov"
  "08-aurora|https://sylvan.apple.com/Videos/comp_GMT314_139M_170NC_NORTH_AMERICA_AURORA__COMP_v22_SDR_20181206_v12CC_SDR_2K_AVC.mov"
  "09-china|https://sylvan.apple.com/Videos/comp_CH_C007_C011_PSNK_v02_SDR_PS_FINAL_20180709_SDR_2K_AVC.mov"
  "10-san-francisco|https://sylvan.apple.com/Videos/comp_A015_C018_0128ZS_v03_SDR_PS_FINAL_20180709__SDR_2K_AVC.mov"
  "11-caribbean|https://sylvan.apple.com/Videos/comp_A108_C001_v09_SDR_FINAL_22062018_SDR_2K_AVC.mov"
  "12-new-york-night|https://sylvan.apple.com/Videos/comp_GMT307_136NC_134K_8277_NY_NIGHT_01_v25_SDR_PS_20180907_SDR_2K_AVC.mov"
)

ok=0; skip=0; fail=0
for entry in "${CLIPS[@]}"; do
  name="${entry%%|*}"; url="${entry#*|}"
  out="$OUT/$name.mp4"
  if [ -s "$out" ] && ffprobe -v error -i "$out" >/dev/null 2>&1; then
    echo "skip  $name.mp4 (already cached)"; skip=$((skip+1)); continue
  fi
  echo "fetch $name  <- $url"
  tmp="$out.part"
  # -f mp4 is required because $tmp ends in .part (ffmpeg can't infer from ext)
  if [ "$RES" = "720" ]; then
    # downscale re-encode (lighter to decode/composite), drop audio
    ffmpeg -y -loglevel warning -i "$url" -an \
           -vf "scale=-2:720" -c:v libx264 -preset veryfast -crf 23 \
           -movflags +faststart -f mp4 "$tmp"
  else
    # native 1080p, no re-encode: just move moov to the front, drop audio
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
ls -1 "$OUT"/*.mp4 2>/dev/null | wc -l | xargs echo "total cached clips:"
