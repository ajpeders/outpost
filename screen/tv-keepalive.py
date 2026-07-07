#!/usr/bin/env python3
"""Keep the living-room dashboard alive when the Apple TV drags the TV off.

The Apple TV, when it sleeps (idle timeout, or music ends), makes the TV drop to
standby — which kills the Pi's aerial dashboard on its own HDMI input. Annoying.

There's no clean CEC signal for "the ATV did it" vs "the user pressed the TV's
power button", so we correlate the two power states:

  - poll the Apple TV power and the TV power
  - when the ATV goes On -> Off (it just slept) and the TV drops to standby right
    after, the ATV caused it -> wake the TV back up and reclaim the Pi input
  - if the TV goes straight back to standby after we wake it, BACK OFF for a while
    (the user genuinely wants it off — e.g. they hit the TV remote's power)

Only acts while the dashboard/screensaver service is running (that's the whole
point — keep *the dashboard* on; if it's stopped, leave the TV alone).
"""
import json
import os
import subprocess
import time
import urllib.request

ATV_URL = os.environ.get("KEEPALIVE_ATV_URL", "http://localhost:8010")
CEC_URL = os.environ.get("KEEPALIVE_CEC_URL", "http://localhost:8020")
RECLAIM = os.environ.get("SCREEN_TV_RECLAIM",
                         "/home/alex/livingroom-pi/screen/tv-reclaim.sh")
POLL = int(os.environ.get("KEEPALIVE_POLL", "6"))            # seconds between polls
CORRELATE = int(os.environ.get("KEEPALIVE_CORRELATE", "30"))  # ATV-sleep -> TV-off window
BACKOFF = int(os.environ.get("KEEPALIVE_BACKOFF", "300"))     # stand-down after user override
DASHBOARD_SERVICES = ("aerial-screen", "kiosk-screen")


def _get(url: str) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return json.load(r)
    except Exception:
        return {}


def atv_power() -> str:
    return str(_get(f"{ATV_URL}/api/state").get("power", "")).lower()   # 'on' | 'off'


def tv_power() -> str:
    return str(_get(f"{CEC_URL}/api/status").get("tv_power", "")).lower()  # 'on' | 'standby'


def dashboard_active() -> bool:
    r = subprocess.run(["systemctl", "is-active", *DASHBOARD_SERVICES],
                       capture_output=True, text=True, check=False)
    return "active" in r.stdout.split()


def main() -> None:
    prev_atv = atv_power()
    atv_slept_at = 0.0
    backoff_until = 0.0

    while True:
        time.sleep(POLL)
        atv = atv_power()
        if prev_atv == "on" and atv == "off":
            atv_slept_at = time.time()      # the Apple TV just went to sleep
        if atv:
            prev_atv = atv

        now = time.time()
        if now < backoff_until or not dashboard_active():
            continue
        if tv_power() != "standby":
            continue
        # TV is off. Only treat it as "the ATV did it" if the ATV slept recently.
        if now - atv_slept_at > CORRELATE:
            continue

        subprocess.run(["bash", RECLAIM], capture_output=True, check=False)
        atv_slept_at = 0.0
        time.sleep(8)
        if tv_power() == "standby":
            # We woke it and it went right back off -> the user wants it off.
            backoff_until = time.time() + BACKOFF


if __name__ == "__main__":
    main()
