#!/usr/bin/env python3
"""Ambient aerial screensaver for the Pi's HDMI.

mpv plays the locally-cached Apple aerials (hardware-decoded via the Pi 5 codec)
straight to DRM/KMS, and the dashboard is composited ON TOP as a transparent
overlay. chromium/cage can't paint HTML5 <video> here (software or GPU — a
Wayland video-surface limitation), so instead of a <video> in the page we render
the dashboard to a transparent PNG with headless chromium and hand it to mpv via
`overlay-add`, refreshed once a minute (the clock's seconds are hidden in overlay
mode). One process owns DRM; no compositor / chromium-transparency gamble.

Run it in place of the cage dashboard kiosk (see aerial-screen.service). Stop it
the same way the screen player stops the kiosk (both want DRM master).
"""
from __future__ import annotations

import glob
import json
import os
import signal
import socket
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AERIAL_DIR = os.environ.get("AERIAL_DIR", os.path.join(REPO, "data/hub/aerials"))
DASH_URL = os.environ.get(
    "AERIAL_DASH_URL", "http://localhost:8080/dashboard?host=pi5&overlay=1")
DRM_MODE = os.environ.get("AERIAL_DRM_MODE", "3")   # 3 = 3840x2160@29.97 (matches the 30fps clips)
IPC = os.environ.get("AERIAL_IPC", "/tmp/mpv-aerial-mode")
REFRESH = int(os.environ.get("AERIAL_REFRESH", "60"))   # overlay redraw cadence (s)
# Native 4K. The V3D scans out 4K@30 smoothly (0 dropped frames, ~14% CPU) — but
# ONLY with --profile=fast + --video-sync=display-resample on the mpv line below.
# The DEFAULT GL processing/vsync path drops 20-70% of frames at 4K (visibly
# choppy). Clips are 4K HEVC (url-4K-SDR); overlay rendered + composited at 4K.
W, H = 3840, 2160
PNG, RAW = "/tmp/aerial-ov.png", "/tmp/aerial-ov.bgra"

_mpv: subprocess.Popen | None = None


def _stop(*_):
    if _mpv and _mpv.poll() is None:
        _mpv.terminate()
    sys.exit(0)


def _connect_ipc(timeout: float = 20.0):
    end = time.time() + timeout
    while time.time() < end:
        if os.path.exists(IPC):
            try:
                s = socket.socket(socket.AF_UNIX)
                s.connect(IPC)
                return s
            except OSError:
                pass
        time.sleep(0.5)
    return None


def _render_overlay() -> bool:
    """Dashboard(overlay) -> transparent PNG -> raw BGRA that mpv can overlay.

    Uses a PERSISTENT chromium profile so the dashboard's localStorage weather
    cache survives between renders — otherwise every render is a cold browser
    that must re-fetch weather (external, slow) within the virtual-time budget,
    and intermittently snapshots before it lands (blank weather/alarm). With the
    cache warm, the page paints weather immediately on load.

    A previous render can hang (a killed chromium leaves the profile's Singleton
    lock, and the next launch deadlocks on it). So: kill any leftover chromium on
    this profile + drop the lock FIRST, and run with a hard timeout — otherwise a
    single hung render freezes this loop forever and the clock gets stuck."""
    subprocess.run(["pkill", "-9", "-f", "aerial-chrome-profile"], check=False)
    subprocess.run("rm -f /tmp/aerial-chrome-profile/Singleton*", shell=True, check=False)
    try:
        r = subprocess.run(
            ["chromium", "--headless=new", "--no-sandbox", "--disable-gpu",
             "--hide-scrollbars", "--default-background-color=00000000",
             "--user-data-dir=/tmp/aerial-chrome-profile", "--no-first-run",
             f"--window-size={W},{H}", "--virtual-time-budget=8000",
             f"--screenshot={PNG}", DASH_URL],
            capture_output=True, timeout=30)
    except subprocess.TimeoutExpired:
        subprocess.run(["pkill", "-9", "-f", "aerial-chrome-profile"], check=False)
        return False
    if r.returncode != 0 or not os.path.exists(PNG):
        return False
    r = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", PNG,
         "-f", "rawvideo", "-pix_fmt", "bgra", RAW],
        capture_output=True)
    return r.returncode == 0


def main() -> None:
    global _mpv
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    clips = sorted(glob.glob(os.path.join(AERIAL_DIR, "*.mp4")))
    if not clips:
        sys.exit(f"aerial-mode: no cached clips in {AERIAL_DIR} "
                 f"(run screen/fetch-aerials.sh first)")
    playlist = "/tmp/aerials.m3u"
    with open(playlist, "w") as fh:
        fh.write("\n".join(clips) + "\n")

    # --vo=gpu (NOT gpu-next — that scans out solid PURPLE on this V3D) with zero-copy
    # --hwdec=drm. --profile=fast + --video-sync=display-resample are REQUIRED for
    # smooth 4K: without them the V3D drops 20-70% of frames; with them it's 0 drops
    # at ~14% CPU. --gpu-context=drm scans out straight to KMS (no compositor).
    vo = os.environ.get("AERIAL_VO", "gpu")
    clock_lua = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "aerial-clock.lua")   # live ticking seconds (OSD)
    _mpv = subprocess.Popen(
        ["mpv", f"--vo={vo}", "--gpu-context=drm", f"--drm-mode={DRM_MODE}",
         "--hwdec=drm", "--profile=fast", "--video-sync=display-resample",
         f"--script={clock_lua}",
         "--loop-playlist=inf", "--shuffle", "--no-audio", "--no-config",
         f"--input-ipc-server={IPC}", "--force-window=yes", "--really-quiet",
         f"--playlist={playlist}"])

    sock = _connect_ipc()
    if sock is None:
        _stop()

    while _mpv.poll() is None:
        if _render_overlay():
            try:
                sock.sendall(json.dumps(
                    {"command": ["overlay-add", 0, 0, 0, RAW, 0, "bgra",
                                 W, H, W * 4]}).encode() + b"\n")
                sock.recv(600)
            except OSError:
                sock = _connect_ipc(5) or sock
        time.sleep(REFRESH)
    _stop()


if __name__ == "__main__":
    main()
