#!/usr/bin/env bash
# Install the host-side screen services (mpv player + on-TV dashboard).
#
# These run on the Pi HOST (not in Docker) because mpv/chromium need to be DRM
# master on the console. The committed unit files carry a %%REPO%% token instead
# of a hardcoded path; this script substitutes the real checkout location so the
# repo is runnable wherever it's cloned.
#
# Usage (on the Pi):
#   sudo screen/install-services.sh                 # install all units
#   sudo screen/install-services.sh screen-player   # install one
#
# Then enable what you want:
#   sudo systemctl enable --now screen-player
#   sudo systemctl enable --now aerial-screen   # OR kiosk-screen, not both
# mtv-screen is installed but stays stopped until the hub swaps to it
# (POST /api/screen/mtv); the kiosk units Conflicts= each other, so only
# one holds DRM master at a time.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
DEST=/etc/systemd/system

units=("$@")
if [ ${#units[@]} -eq 0 ]; then
  units=(screen-player aerial-screen kiosk-screen mtv-screen tv-keepalive)
fi

for u in "${units[@]}"; do
  src="$HERE/${u}.service"
  [ -f "$src" ] || { echo "no such unit: $u" >&2; exit 1; }
  sed "s#%%REPO%%#${REPO}#g" "$src" > "${DEST}/${u}.service"
  echo "installed ${u}.service -> ${DEST} (REPO=${REPO})"
done

systemctl daemon-reload
echo "done. enable with: sudo systemctl enable --now <unit>"
