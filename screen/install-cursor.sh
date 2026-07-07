#!/bin/bash
# Create a fully-transparent XCursor theme named "transparent" so the wlroots
# (cage) compositor draws an invisible pointer on the idle dashboard. Idempotent;
# run as root (writes under /usr/share/icons). Referenced by kiosk.sh via
# XCURSOR_THEME=transparent.
set -e
THEME=/usr/share/icons/transparent
mkdir -p "$THEME/cursors"
# Start clean: a stale/broken left_ptr (e.g. a self-referential symlink from an
# older buggy run) makes the python `open(...,"wb")` below fail with ELOOP.
rm -f "$THEME/cursors"/*

cat > "$THEME/index.theme" <<'EOF'
[Icon Theme]
Name=transparent
Comment=Fully transparent cursor (kiosk)
Inherits=core
EOF

# Emit a fully-transparent XCursor file with images at every common nominal size
# so wlroots finds an EXACT size match and never falls back to a built-in cursor
# (a single 1x1 image gets rejected/scaled and the default pointer shows through).
python3 - "$THEME/cursors/left_ptr" <<'PY'
import struct, sys
out = sys.argv[1]
IMG_TYPE = 0xfffd0002
sizes = [16, 24, 32, 48, 64]
ntoc = len(sizes)
toc, chunks, pos = b"", b"", 16 + ntoc * 12
for s in sizes:
    toc += struct.pack("<III", IMG_TYPE, s, pos)          # type, nominal size, offset
    chunk = struct.pack("<IIIIIIIII", 36, IMG_TYPE, s, 1, s, s, 0, 0, 0)
    chunk += struct.pack("<%dI" % (s * s), *([0] * (s * s)))  # all transparent
    chunks += chunk
    pos += len(chunk)
hdr = b"Xcur" + struct.pack("<III", 16, 0x00010000, ntoc)
open(out, "wb").write(hdr + toc + chunks)
PY

# Common cursor names all point at the transparent image (left_ptr is the real
# file written above — it MUST NOT appear in this loop or `ln -sf` clobbers it
# with a self-referential symlink and every alias dies with it).
# Chromium/wlroots can request different aliases depending on hover target,
# resize state, or theme fallback; keep this broad so no built-in cursor leaks.
for name in default arrow top_left_arrow xterm text ibeam \
            hand hand1 hand2 pointer pointing_hand openhand closedhand \
            grab grabbing crosshair tcross dotbox watch wait progress \
            fleur move all-scroll not-allowed no-drop crossed_circle \
            copy alias context-menu cell help question_arrow \
            size_all size_bdiag size_fdiag size_hor size_ver \
            n-resize ne-resize e-resize se-resize s-resize sw-resize w-resize nw-resize \
            ns-resize ew-resize nesw-resize nwse-resize col-resize row-resize \
            vertical-text zoom-in zoom-out; do
  [ "$name" = left_ptr ] && continue   # never overwrite the real cursor file
  ln -sf left_ptr "$THEME/cursors/$name"
done

# cage/wlroots loads the *default* cursor theme, NOT $XCURSOR_THEME, so make the
# system "default" theme inherit ours. /usr/share/icons/default/index.theme is an
# update-alternatives symlink (x-cursor-theme); register + select transparent.
cat > "$THEME/cursor.theme" <<'EOF'
[Icon Theme]
Inherits=transparent
EOF
update-alternatives --install /usr/share/icons/default/index.theme \
  x-cursor-theme "$THEME/cursor.theme" 20000 >/dev/null 2>&1 || true
update-alternatives --set x-cursor-theme "$THEME/cursor.theme" >/dev/null 2>&1 || true

# The real fix: this cage/wlroots-0.18 build IGNORES XCURSOR_THEME (and the
# default alternative above) — it defaults its cursor theme to "Adwaita", loads
# /usr/share/icons/Adwaita/cursors/left_ptr, and parks it at screen center (there
# is no mouse). XCURSOR_PATH *is* honored, so kiosk.sh must list $SHADOW FIRST:
#   export XCURSOR_PATH=/usr/local/share/icons:/usr/share/icons
# We install transparent copies here under EVERY theme name wlroots might pick, so
# whichever it loads resolves to an invisible cursor. Lives under /usr/local so
# apt upgrades of adwaita-icon-theme never overwrite it.
SHADOW=/usr/local/share/icons
for t in Adwaita default transparent; do
  mkdir -p "$SHADOW/$t/cursors"
  cp -af "$THEME/cursors/." "$SHADOW/$t/cursors/"
  cp -f  "$THEME/index.theme" "$SHADOW/$t/index.theme"
done

echo "installed transparent cursor theme at $THEME + shadow in $SHADOW"
echo "NOTE: kiosk.sh must set XCURSOR_PATH=$SHADOW:/usr/share/icons"
