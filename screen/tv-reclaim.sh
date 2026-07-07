#!/bin/bash
# Reclaim the TV's HDMI input for the Pi (the idle dashboard).
#
# WHY: the Apple TV and the Pi are on different HDMI inputs. When the Apple TV
# wakes (e.g. a music alarm) it CEC-grabs the TV input and the Pi never gets it
# back — the dashboard keeps rendering but isn't the visible input. The Pi's own
# cec service (container, libcec `cec-client`) sends a bare `as` that does NOT
# switch this TV. cec-ctl (v4l) does, but only when registered as a playback
# device first (the shared /dev/cec0 gets reset to "unregistered" by the
# container's status polling, and an unregistered device's active-source is
# ignored). So: register → wake → assert active-source, with the Pi's real
# physical address read live (which TV HDMI port it's plugged into).
set -u
DEV="${CEC_DEV:-/dev/cec0}"

cec-ctl -d "$DEV" --playback >/dev/null 2>&1        # register as Playback device
sleep 0.5
PA=$(cec-ctl -d "$DEV" 2>/dev/null | grep -i 'Physical Address' | head -1 \
     | grep -oE '[0-9a-fA-F]\.[0-9a-fA-F]\.[0-9a-fA-F]\.[0-9a-fA-F]')
[ -z "$PA" ] && PA="1.0.0.0"                          # sane fallback (HDMI 1)
HEX="0x${PA//./}"

cec-ctl -d "$DEV" --to 0 --image-view-on   >/dev/null 2>&1   # wake the TV
cec-ctl -d "$DEV" --active-source phys-addr="$HEX" >/dev/null 2>&1  # switch to us
echo "reclaimed TV input for the Pi (phys-addr $PA)"
