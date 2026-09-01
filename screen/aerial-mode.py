#!/usr/bin/env python3
"""Ambient aerial screensaver for the Pi's HDMI.

mpv plays the locally-cached Apple aerials (hardware-decoded via the Pi 5 codec)
straight to DRM/KMS, and the dashboard is composited ON TOP as a transparent
overlay. chromium/cage can't paint HTML5 <video> here (software or GPU — a
Wayland video-surface limitation), so instead of a <video> in the page we render
the dashboard to a transparent PNG and hand it to mpv via `overlay-add`,
refreshed once a minute (the clock's seconds are drawn live by aerial-clock.lua).

The renderer is a PERSISTENT headless chromium driven over the DevTools
protocol (--remote-debugging-pipe: CDP over fd 3/4, NUL-delimited JSON — no
websocket, no port). Each cycle is Page.reload -> readyState poll -> screenshot,
~3s and no process storm, vs ~8-15s for the old cold-start-per-minute chromium.
Transparency comes from Emulation.setDefaultBackgroundColorOverride (the
--default-background-color CLI flag kills the target on this chromium build) and
the exact 4K viewport from Emulation.setDeviceMetricsOverride (--window-size
loses 87px to window chrome). If the renderer wedges, it's restarted; if that
fails too, a one-shot cold render keeps the clock moving.

Clips are tagged day/night in AERIAL_DIR/timeofday.json (classify-aerials.py,
run by fetch-aerials.sh) and the playlist follows the hour: day flyovers
between AERIAL_DAY_START..AERIAL_DAY_END, night ones otherwise. Untagged clips
play in either. The playlist is swapped in-place over mpv's IPC when the
period flips.

Runs as aerial-screen.service in place of the cage dashboard kiosk. Stop it
the same way the screen player stops the kiosk (both want DRM master).
"""
from __future__ import annotations

import base64
import glob
import json
import os
import select
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AERIAL_DIR = os.environ.get("AERIAL_DIR", os.path.join(REPO, "data/hub/aerials"))
DASH_URL = os.environ.get(
    "AERIAL_DASH_URL",
    f"http://localhost:8080/dashboard?host={socket.gethostname()}&overlay=1")
DRM_MODE = os.environ.get("AERIAL_DRM_MODE", "3")   # 3 = 3840x2160@29.97 (matches the 30fps clips)
IPC = os.environ.get("AERIAL_IPC", "/tmp/mpv-aerial-mode")
REFRESH = int(os.environ.get("AERIAL_REFRESH", "60"))   # overlay redraw cadence (s)
SETTLE = float(os.environ.get("AERIAL_SETTLE", "2.0"))  # post-load paint time (s)
DAY_START = int(os.environ.get("AERIAL_DAY_START", "7"))   # day clips from this hour...
DAY_END = int(os.environ.get("AERIAL_DAY_END", "19"))      # ...until this hour
# Native 4K. The V3D scans out 4K@30 smoothly (0 dropped frames, ~14% CPU) — but
# ONLY with --profile=fast + --video-sync=display-resample on the mpv line below.
# The DEFAULT GL processing/vsync path drops 20-70% of frames at 4K (visibly
# choppy). Clips are 4K HEVC (url-4K-SDR); overlay rendered + composited at 4K.
W, H = 3840, 2160
PNG, RAW = "/tmp/aerial-ov.png", "/tmp/aerial-ov.bgra"
PLAYLIST = "/tmp/aerials.m3u"
PROFILE = "/tmp/aerial-cdp-profile"
TOD_FILE = os.path.join(AERIAL_DIR, "timeofday.json")

_mpv: subprocess.Popen | None = None
_renderer: "OverlayRenderer | None" = None


def _stop(*_):
    if _renderer is not None:
        _renderer.close()
    if _mpv and _mpv.poll() is None:
        _mpv.terminate()
    sys.exit(0)


# --- time-of-day playlist ----------------------------------------------
def _period(hour: int) -> str:
    return "day" if DAY_START <= hour < DAY_END else "night"


def _clips_for(period: str) -> list[str]:
    """Cached clips matching the period; untagged clips play in either.
    Falls back to everything rather than an empty playlist."""
    clips = sorted(glob.glob(os.path.join(AERIAL_DIR, "*.mp4")))
    try:
        with open(TOD_FILE) as fh:
            tod = json.load(fh)
    except (OSError, ValueError):
        tod = {}
    other = "night" if period == "day" else "day"
    picked = [c for c in clips if tod.get(os.path.basename(c)) != other]
    return picked or clips


def _write_playlist(period: str) -> list[str]:
    clips = _clips_for(period)
    with open(PLAYLIST, "w") as fh:
        fh.write("\n".join(clips) + "\n")
    return clips


# --- persistent overlay renderer (chromium over CDP pipe) ---------------
class SessionGone(Exception):
    """The flat CDP session died (renderer process swap) — re-attach."""


class OverlayRenderer:
    def __init__(self, url: str):
        self.url = url
        self.proc: subprocess.Popen | None = None
        self.rfd = self.wfd = -1
        self.buf = b""
        self.mid = 0
        self.tid: str | None = None
        self.sess: str | None = None

    # -- transport --
    def _send(self, method: str, params: dict | None = None,
              session: str | None = None, timeout: float = 30.0) -> dict:
        self.mid += 1
        msg: dict = {"id": self.mid, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        os.write(self.wfd, json.dumps(msg).encode() + b"\0")
        r = self._wait_id(self.mid, timeout)
        if "error" in r:
            if r["error"].get("code") == -32001:   # session not found
                raise SessionGone(r["error"].get("message", ""))
            raise RuntimeError(f"{method}: {r['error']}")
        return r.get("result", {})

    def _wait_id(self, mid: int, timeout: float) -> dict:
        end = time.time() + timeout
        while True:
            while b"\0" in self.buf:
                raw, self.buf = self.buf.split(b"\0", 1)
                if not raw.strip():
                    continue
                try:
                    m = json.loads(raw)
                except ValueError:
                    continue
                if m.get("id") == mid:
                    return m
            left = end - time.time()
            if left <= 0:
                raise TimeoutError(f"cdp timeout waiting for #{mid}")
            ready, _, _ = select.select([self.rfd], [], [], left)
            if not ready:
                raise TimeoutError(f"cdp timeout waiting for #{mid}")
            chunk = os.read(self.rfd, 1 << 20)
            if not chunk:
                raise RuntimeError("cdp pipe closed")
            self.buf += chunk

    # -- lifecycle --
    def start(self) -> None:
        self.close()
        # a stale Singleton lock from a killed chromium deadlocks the next one.
        # Match chromium specifically — a bare profile-name pattern would kill
        # ANY process whose command line mentions it (e.g. an admin's shell).
        subprocess.run(["pkill", "-9", "-f", "chromium.*aerial-cdp-profile"],
                       check=False)
        subprocess.run(f"rm -f {PROFILE}/Singleton*", shell=True, check=False)
        a_r, a_w = os.pipe()   # we write a_w -> chromium reads fd 3
        b_r, b_w = os.pipe()   # chromium writes fd 4 -> we read b_r
        os.set_inheritable(a_r, True)
        os.set_inheritable(b_w, True)
        self.proc = subprocess.Popen(
            ["chromium", "--headless=new", "--no-sandbox", "--disable-gpu",
             "--remote-debugging-pipe", "--no-first-run",
             f"--user-data-dir={PROFILE}", f"--window-size={W},{H}"],
            preexec_fn=lambda: (os.dup2(a_r, 3), os.dup2(b_w, 4)),
            close_fds=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.close(a_r)
        os.close(b_w)
        self.rfd, self.wfd = b_r, a_w
        self.buf = b""
        self.tid = self._send("Target.createTarget", {"url": self.url})["targetId"]
        self.sess = None

    def close(self) -> None:
        for fd in (self.rfd, self.wfd):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self.rfd = self.wfd = -1
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None

    def _attach(self) -> None:
        self.sess = self._send("Target.attachToTarget",
                               {"targetId": self.tid, "flatten": True})["sessionId"]
        # transparent page + exact 4K viewport (the CLI flags for these either
        # crash the target or lose height to window chrome)
        self._send("Emulation.setDefaultBackgroundColorOverride",
                   {"color": {"r": 0, "g": 0, "b": 0, "a": 0}}, session=self.sess)
        self._send("Emulation.setDeviceMetricsOverride",
                   {"width": W, "height": H, "deviceScaleFactor": 1,
                    "mobile": False}, session=self.sess)

    def _session_send(self, method: str, params: dict | None = None,
                      timeout: float = 30.0) -> dict:
        """Send within the page session, re-attaching once if the session died
        (cross-process navigation swaps kill flat sessions; targetId survives)."""
        if self.sess is None:
            self._attach()
        try:
            return self._send(method, params, session=self.sess, timeout=timeout)
        except SessionGone:
            self._attach()
            return self._send(method, params, session=self.sess, timeout=timeout)

    def _ready(self, timeout: float = 15.0) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            r = self._session_send(
                "Runtime.evaluate",
                {"expression": "location.pathname + '|' + document.readyState",
                 "returnByValue": True})
            if r.get("result", {}).get("value", "").endswith("|complete") and \
                    "/dashboard" in str(r.get("result", {}).get("value")):
                return True
            time.sleep(0.25)
        return False

    def _painted(self, timeout: float = 3.0) -> bool:
        """Wait until the dashboard has actually drawn its content.

        This replaced a flat `sleep(SETTLE)`: the fixed 2s wait was most of a
        3s render, and it was both too long on a warm renderer and a guess on
        a cold one. Poll the two fields that arrive last — the clock (local,
        instant) and the weather temperature (a hub fetch) — so we capture as
        soon as there is something worth capturing."""
        end = time.time() + timeout
        expr = ("(((document.getElementById('time')||{}).textContent||'').length > 3) && "
                "(['','–','-'].indexOf((((document.getElementById('wx-temp')||{})"
                ".textContent)||'').trim()) === -1)")
        while time.time() < end:
            r = self._session_send("Runtime.evaluate",
                                   {"expression": expr, "returnByValue": True})
            if r.get("result", {}).get("value") is True:
                return True
            time.sleep(0.1)
        return False

    def render(self, reload: bool = True) -> bytes | None:
        if reload:
            self._session_send("Page.reload")
        self._ready()
        if not self._painted():
            time.sleep(SETTLE)   # content never showed up: fall back to the old wait
        data = self._session_send("Page.captureScreenshot", {"format": "png"},
                                  timeout=30)["data"]
        return base64.b64decode(data)


def _render_cold() -> bool:
    """One-shot fallback: cold chromium --screenshot (the old slow path)."""
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
    return r.returncode == 0 and os.path.exists(PNG)


def _render_overlay() -> bool:
    """Produce PNG via the persistent renderer (restarting it if it wedges),
    falling back to a cold one-shot render so the clock never sticks."""
    global _renderer
    png = None
    t0 = time.time()
    cold = _renderer is None or _renderer.proc is None or _renderer.proc.poll() is not None
    for attempt in (1, 2):
        try:
            if _renderer is None or _renderer.proc is None or _renderer.proc.poll() is not None:
                if _renderer is None:
                    _renderer = OverlayRenderer(DASH_URL)
                _renderer.start()
                png = _renderer.render(reload=False)
            else:
                png = _renderer.render()
            break
        except Exception:  # noqa: BLE001 - wedge -> restart once, then cold
            try:
                _renderer.close()
            except Exception:  # noqa: BLE001
                pass
            if attempt == 2:
                png = None
    if png:
        with open(PNG, "wb") as fh:
            fh.write(png)
    elif not _render_cold():
        return False
    t_png = time.time()
    r = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", PNG,
         # belt-and-braces exact size: overlay-add needs exactly WxH (stride W*4)
         "-vf", f"crop='min(iw,{W})':'min(ih,{H})':0:0,pad={W}:{H}:0:0:black@0",
         "-f", "rawvideo", "-pix_fmt", "bgra", RAW],
        capture_output=True)
    print(f"aerial: overlay render {'cold' if cold else 'warm'} "
          f"{t_png - t0:.1f}s + bgra {time.time() - t_png:.1f}s", flush=True)
    return r.returncode == 0


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


def _ipc_send(sock, command: list) -> None:
    sock.sendall(json.dumps({"command": command}).encode() + b"\n")
    sock.recv(600)


def main() -> None:
    global _mpv
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    period = _period(datetime.now().hour)
    clips = _write_playlist(period)
    if not clips:
        sys.exit(f"aerial-mode: no cached clips in {AERIAL_DIR} "
                 f"(run screen/fetch-aerials.sh first)")

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
         f"--playlist={PLAYLIST}"])

    sock = _connect_ipc()
    if sock is None:
        _stop()

    while _mpv.poll() is None:
        now_period = _period(datetime.now().hour)
        if now_period != period:      # day/night flip -> swap the playlist live
            period = now_period
            _write_playlist(period)
            try:
                _ipc_send(sock, ["loadlist", PLAYLIST, "replace"])
                _ipc_send(sock, ["playlist-shuffle"])
            except OSError:
                sock = _connect_ipc(5) or sock
        if _render_overlay():
            try:
                _ipc_send(sock, ["overlay-add", 0, 0, 0, RAW, 0, "bgra",
                                 W, H, W * 4])
            except OSError:
                sock = _connect_ipc(5) or sock
        time.sleep(REFRESH)
    _stop()


if __name__ == "__main__":
    main()
