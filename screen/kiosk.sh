#!/bin/bash
# Living-room idle dashboard: cage (software/pixman compositor — the Pi's v3d GPU
# can't do the GL a hardware compositor needs) running chromium in kiosk mode on
# the hub's /dashboard page. The screen player stops this service before it plays
# mpv (both need DRM master) and starts it again when playback ends.
set -e
export XDG_RUNTIME_DIR=/run/user/0
export WLR_RENDERER=pixman
# Hide the pointer: wlroots draws its OWN default cursor sprite (the page's
# cursor:none only kicks in once a pointer enters chromium, which never happens
# with no mouse). Point the whole session at a fully-transparent cursor theme
# (created by bin/install-cursor, run on deploy).
export XCURSOR_THEME=transparent
export XCURSOR_SIZE=24
# This cage/wlroots build ignores XCURSOR_THEME and defaults to "Adwaita"; the
# only lever it honors is XCURSOR_PATH. install-cursor.sh puts transparent copies
# of the Adwaita/default themes under /usr/local/share/icons, so list it FIRST.
export XCURSOR_PATH=/usr/local/share/icons:/usr/share/icons
export GTK_CURSOR_THEME=transparent
export GTK_CURSOR_SIZE=24
# Force software cursors: a DRM hardware-cursor plane can show a default sprite
# that ignores the theme; the software path honors our transparent cursor.
export WLR_NO_HARDWARE_CURSORS=1
unset CHROMIUM_USER_FLAGS CHROME_EXTRA_FLAGS
mkdir -p "$XDG_RUNTIME_DIR"; chmod 700 "$XDG_RUNTIME_DIR"

URL="${DASHBOARD_URL:-http://localhost:8080/dashboard}?host=$(hostname)"

# wait for the hub (docker) to be serving before we point chromium at it
for _ in $(seq 1 90); do
  curl -sf "http://localhost:8080/healthz" >/dev/null 2>&1 && break
  sleep 2
done

exec cage -- bash -c "exec dbus-run-session -- chromium \
  --kiosk --ozone-platform=wayland --disable-gpu --no-sandbox --no-first-run \
  --disable-dev-shm-usage --disable-features=Translate --noerrdialogs \
  --check-for-update-interval=31536000 --hide-scrollbars --ash-hide-cursor '$URL'"
