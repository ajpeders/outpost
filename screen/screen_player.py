#!/usr/bin/env python3
"""Living-room screen player — controls mpv on the Pi's HDMI (DRM/KMS).

Runs on the Pi HOST (not in Docker) because mpv needs to be DRM master on the
console. The hub (host-networked container) drives it over localhost.

A supervisor thread keeps the current stream alive: if mpv dies while something
should be playing (e.g. jetstream restarts its transcode when you skip media),
it relaunches mpv so playback self-heals.

  POST /play   {url, headers?:{...}, audio_only?:bool}  -> (re)start playback
  POST /stop                                            -> stop playback
  POST /kiosk/restart                                   -> reload idle dashboard
  POST /kiosk/cursor                                    -> reinstall transparent cursor
  GET  /status                                          -> {playing, url}
  GET  /kiosk/status                                    -> kiosk systemd state
  GET  /healthz
"""
from __future__ import annotations

import json
import os
import os.path as osp
import random
import re
import shutil
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

IPC_SOCKET = os.environ.get("SCREEN_MPV_IPC", "/tmp/mpv-ipc")  # for sub toggle etc.
# idle-dashboard kiosk service — stopped while mpv plays (both need DRM master),
# restarted when playback ends.
KIOSK_SERVICE = os.environ.get("SCREEN_KIOSK_SERVICE", "kiosk-screen")
CURSOR_INSTALL = os.environ.get(
    "SCREEN_CURSOR_INSTALL", "/home/alex/livingroom-pi/screen/install-cursor.sh")
# Reclaims the TV's HDMI input for the Pi (CEC active-source) — the Apple TV
# steals the input when it wakes; this switches the TV back to the dashboard.
TV_RECLAIM = os.environ.get(
    "SCREEN_TV_RECLAIM", "/home/alex/livingroom-pi/screen/tv-reclaim.sh")
CEC_DEV = os.environ.get("SCREEN_CEC_DEV", "/dev/cec0")


def _kiosk(action: str) -> None:
    """start | stop the idle-dashboard kiosk (best-effort)."""
    subprocess.run(["systemctl", action, KIOSK_SERVICE],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def _kiosk_status() -> dict:
    active = subprocess.run(
        ["systemctl", "is-active", KIOSK_SERVICE],
        capture_output=True, text=True, check=False).stdout.strip() or "unknown"
    enabled = subprocess.run(
        ["systemctl", "is-enabled", KIOSK_SERVICE],
        capture_output=True, text=True, check=False).stdout.strip() or "unknown"
    return {"service": KIOSK_SERVICE, "active": active, "enabled": enabled}


def _kiosk_restart() -> dict:
    r = subprocess.run(["systemctl", "restart", KIOSK_SERVICE],
                       capture_output=True, text=True, check=False)
    return {"ok": r.returncode == 0, "status": _kiosk_status(),
            "error": (r.stderr or r.stdout).strip()}


def _cursor_repair() -> dict:
    r = subprocess.run(["bash", CURSOR_INSTALL],
                       capture_output=True, text=True, check=False)
    if r.returncode == 0:
        _kiosk("restart")
    return {"ok": r.returncode == 0, "status": _kiosk_status(),
            "output": (r.stdout or r.stderr).strip()}


def _tv_reclaim() -> dict:
    """Switch the TV back to the Pi's HDMI input (the idle dashboard)."""
    r = subprocess.run(["bash", TV_RECLAIM],
                       capture_output=True, text=True, check=False)
    return {"ok": r.returncode == 0, "output": (r.stdout or r.stderr).strip()}


def _tv_input(n: int) -> dict:
    """Switch the TV to HDMI input n via CEC (Set Stream Path to phys addr n.0.0.0).

    Register as a playback device first (the shared /dev/cec0 gets reset to
    "unregistered" by status polling, and an unregistered device's messages are
    ignored), wake the TV, then request the route. Works for whatever device is
    on that HDMI port (the TV does the switching)."""
    pa = f"0x{int(n)}000"
    subprocess.run(["cec-ctl", "-d", CEC_DEV, "--playback"],
                   capture_output=True, check=False)
    subprocess.run(["cec-ctl", "-d", CEC_DEV, "--to", "0", "--image-view-on"],
                   capture_output=True, check=False)
    r = subprocess.run(
        ["cec-ctl", "-d", CEC_DEV, "--to", "0", "--set-stream-path",
         f"phys-addr={pa}"], capture_output=True, text=True, check=False)
    return {"ok": r.returncode == 0, "input": int(n),
            "output": (r.stdout or r.stderr).strip()[:200]}

PORT = int(os.environ.get("SCREEN_PORT", "9595"))
# SMB-mounted media library (direct file play — no transcode)
MEDIA_ROOT = os.environ.get("SCREEN_MEDIA_ROOT", "/mnt/share/media")
VIDEO_EXTS = (".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts", ".webm",
              ".mpg", ".mpeg", ".wmv", ".flv")
# DRM mode index for the HDMI output. 6 = 1920x1080@60 on this TV; native 1080p
# (no upscaling) + gpu-next vsync is smooth for the 1080p streams jetstream/Plex
# produce. `mpv --vo=gpu --gpu-context=drm --drm-mode=help` lists indices.
DRM_MODE = os.environ.get("SCREEN_DRM_MODE", "6")
LOG_FILE = os.environ.get("SCREEN_MPV_LOG", "/tmp/screen-mpv.log")

# --- per-content HDMI mode selection ----------------------------------
_MODES: list[tuple[int, int, int, float]] | None = None  # (index, w, h, refresh)


def _modes() -> list[tuple[int, int, int, float]]:
    """Enumerate the TV's DRM modes (once) so mode choice adapts to the display."""
    global _MODES
    if _MODES is not None:
        return _MODES
    _MODES = []
    try:
        p = subprocess.run(
            ["mpv", "--vo=drm", "--gpu-context=drm", "--drm-mode=help",
             "--frames=0", "--no-config", "/dev/null"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=15)
        for m in re.finditer(r"Mode (\d+):\s*\d+x\d+(i?)\s*\((\d+)x(\d+)@([\d.]+)Hz\)", p.stdout):
            if m.group(2) == "i":
                continue  # skip interlaced
            _MODES.append((int(m.group(1)), int(m.group(3)), int(m.group(4)), float(m.group(5))))
    except Exception:  # noqa: BLE001
        _MODES = []
    return _MODES


def _probe(path: str) -> tuple[int, int, float] | None:
    """(width, height, fps) of a media file via ffprobe, or None."""
    if not shutil.which("ffprobe"):
        return None
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height,avg_frame_rate", "-of", "json", path],
            capture_output=True, text=True, timeout=20).stdout
        s = json.loads(out)["streams"][0]
        num, den = (s.get("avg_frame_rate") or "0/1").split("/")
        fps = float(num) / float(den) if float(den) else 0.0
        return int(s.get("width") or 0), int(s.get("height") or 0), fps
    except Exception:  # noqa: BLE001
        return None


def _pick_mode(w: int, h: int, fps: float) -> str:
    """Best DRM mode: native-ish resolution + a refresh that's an integer
    multiple of the content fps (judder-free). Falls back to DRM_MODE."""
    modes = _modes()
    if not modes:
        return DRM_MODE
    fps = fps or 24.0
    tw, th = (3840, 2160) if (w >= 3000 or h >= 1600) else (1920, 1080)
    cands = [m for m in modes if m[1] == tw and m[2] == th] \
        or [m for m in modes if (m[1], m[2]) == (1920, 1080)] or modes

    def score(refresh: float) -> float:
        return min(abs(refresh - fps * k) / k for k in (1, 2, 3, 4, 5))

    best = min(cands, key=lambda m: (round(score(m[3]), 2), -m[3]))
    return str(best[0])


_lock = threading.RLock()
_proc: subprocess.Popen | None = None
_url: str | None = None
_headers: dict | None = None
_audio_only: bool = False
_profile: str = "live"          # "live" (HLS) or "media" (local file)
_source: str | None = None      # livestream | shuffle | media
_title: str | None = None
_subtitle: str | None = None
_supervise: bool = True         # auto-relaunch on death (live yes, media no)
_stopped: bool = True           # True = intentionally stopped (won't relaunch)
_mode: str = DRM_MODE           # DRM mode index for the current playback


def _build_args(url: str, headers: dict | None, audio_only: bool,
                profile: str = "live", mode: str | None = None) -> list[str]:
    m = mode or DRM_MODE
    if profile == "media":
        # local files are often 4K HEVC 10-bit HDR. The Pi's v3d GPU can't make
        # a GL framebuffer for HDR (gpu-next fails), so no tone-mapping — output
        # the mode picked to match the file (native res + matching refresh) via
        # plain drm + HEVC hardware decode (overlay plane, no GPU), and let the
        # 4K HDR TV do the HDR itself (passthrough). Bigger buffer for SMB reads.
        video = ["--vo=drm", f"--drm-mode={m}", "--hwdec=auto",
                 "--cache=yes", "--cache-secs=15", "--demuxer-readahead-secs=15",
                 # on-demand: load internal + external (.srt) subs, start OFF,
                 # toggle on via the IPC socket (see /sub/cycle).
                 "--sub-auto=fuzzy", "--sid=no", f"--input-ipc-server={IPC_SOCKET}"]
    else:
        # live HLS (H.264): plain drm VO — robust across the transcode restarts
        # jetstream does on skip (gpu-next "export fails" on relaunch). Pi 5 has
        # no H.264 hw decoder so software decode; 1080p is light. SMALL buffer so
        # we stay near the live edge (in sync with the streamer/other viewers) —
        # the network is fast enough not to stutter at a low buffer. jetstream
        # burns its own subs, so don't load a sub track.
        video = ["--vo=drm", f"--drm-mode={m}", "--hwdec=no",
                 "--cache=yes", "--cache-secs=4", "--demuxer-readahead-secs=4",
                 "--hls-bitrate=max", "--sid=no", "--sub-auto=no"]
    args = ["mpv", *video, "--fullscreen",
            # NO --loop: on a live HLS stream it treats the live edge as EOF and
            # jumps back to the start of the segment window (~30s rewind).
            "--keep-open=no",
            "--no-config", "--force-window=yes"]
    if audio_only:
        args = [a for a in args if not a.startswith(("--vo", "--gpu-context", "--drm-mode"))]
        args.append("--no-video")
    if headers:
        args.append("--http-header-fields=" + ",".join(f"{k}: {v}" for k, v in headers.items()))
    args.append(url)
    return args


def _spawn() -> None:
    global _proc
    # Take the display from the idle dashboard (cage/chromium hold DRM master).
    _kiosk("stop")
    # Hard-kill any existing mpv and WAIT for it (and cage) to fully exit + release
    # the DRM master before starting a new one. Racing a dying process for the
    # display is what causes "device busy" / "export failed" -> a black screen.
    subprocess.run(["pkill", "-9", "-x", "mpv"], check=False)
    for _ in range(40):
        if subprocess.run(["pgrep", "-x", "mpv|cage"],
                          stdout=subprocess.DEVNULL).returncode != 0:
            break
        time.sleep(0.1)
    time.sleep(0.5)  # let the kernel release DRM master
    logf = open(LOG_FILE, "w")  # fresh log per spawn, for diagnosis
    _proc = subprocess.Popen(
        _build_args(_url, _headers, _audio_only, _profile, _mode),
        stdout=logf, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, start_new_session=True,
    )


def _play(url: str, headers: dict | None = None, audio_only: bool = False,
          profile: str = "live", supervise: bool = True, mode: str | None = None,
          title: str | None = None, subtitle: str | None = None,
          source: str | None = None) -> None:
    global _url, _headers, _audio_only, _stopped, _profile, _supervise, _mode, _title, _subtitle, _source
    with _lock:
        _url, _headers, _audio_only, _stopped = url, headers, audio_only, False
        _profile, _supervise, _mode = profile, supervise, mode or DRM_MODE
        _title, _subtitle, _source = title, subtitle, source or profile
        _spawn()


def _stop() -> None:
    global _url, _stopped, _proc, _title, _subtitle, _source
    with _lock:
        _stopped, _url, _title, _subtitle, _source = True, None, None, None, None
        subprocess.run(["pkill", "-9", "-x", "mpv"], check=False)
        _proc = None
    _kiosk("start")  # back to the idle dashboard


def _alive() -> bool:
    return _proc is not None and _proc.poll() is None


# --- media library (SMB share) -----------------------------------------
def _safe_media_path(rel: str) -> str:
    """Resolve a relative path under MEDIA_ROOT, blocking traversal."""
    root = osp.realpath(MEDIA_ROOT)
    target = osp.realpath(osp.join(root, rel.lstrip("/")))
    if target != root and not target.startswith(root + os.sep):
        raise ValueError("path outside media root")
    return target


def _media_list(rel: str) -> dict:
    target = _safe_media_path(rel)
    if not osp.isdir(target):
        raise ValueError("not a directory")
    root = osp.realpath(MEDIA_ROOT)
    entries = []
    for name in sorted(os.listdir(target), key=str.lower):
        if name.startswith("."):
            continue
        full = osp.join(target, name)
        relp = osp.relpath(full, root)
        if osp.isdir(full):
            entries.append({"name": name, "type": "dir", "path": relp})
        elif name.lower().endswith(VIDEO_EXTS):
            entries.append({"name": name, "type": "file", "path": relp})
    return {"path": rel, "entries": entries}


def play_media(rel: str, source: str = "media") -> str:
    target = _safe_media_path(rel)
    if not (osp.isfile(target) and target.lower().endswith(VIDEO_EXTS)):
        raise ValueError("not a playable media file")
    info = _probe(target)                       # (w, h, fps) or None
    mode = _pick_mode(*info) if info else DRM_MODE
    title = osp.splitext(osp.basename(target))[0]
    _play(target, profile="media", supervise=False, mode=mode, title=title, source=source)
    return target


def shuffle_media(rel: str = "") -> str:
    """Play a random video from the library (optionally scoped to a subfolder)."""
    base = _safe_media_path(rel)
    root = osp.realpath(MEDIA_ROOT)
    walk_base = base if osp.isdir(base) else root
    files = []
    for dirpath, _dirs, names in os.walk(walk_base):
        for n in names:
            if not n.startswith(".") and n.lower().endswith(VIDEO_EXTS):
                files.append(osp.relpath(osp.join(dirpath, n), root))
    if not files:
        raise ValueError("no media files")
    return play_media(random.choice(files), source="shuffle")


def _ipc(cmd: list) -> dict | None:
    """Send one mpv IPC command and return its reply, or None if the socket
    isn't there (only *media* playback opens it — live HLS doesn't). Skips any
    async event lines mpv emits before the command reply."""
    try:
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(2)
        s.connect(IPC_SOCKET)
        s.sendall((json.dumps({"command": cmd}) + "\n").encode())
        buf = b""
        for _ in range(50):
            try:
                chunk = s.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if "error" in obj:  # command reply (events carry "event" instead)
                    s.close()
                    return obj
        s.close()
    except OSError:
        pass
    return None


def _ipc_prop(name: str):
    """Read an mpv property via IPC, or None if unavailable."""
    r = _ipc(["get_property", name])
    if r and r.get("error") == "success":
        return r.get("data")
    return None


def _sub_cycle() -> bool:
    """Cycle the subtitle track on the current media playback (off → 1 → 2 → off)."""
    return _ipc(["cycle", "sub"]) is not None


def _status() -> dict:
    """Playback status. For media playback (IPC socket open) enrich with the
    transport state the web app's Now-Playing hero needs."""
    playing = (not _stopped) and _url is not None
    st = {
        "playing": playing,
        "url": _url,
        "profile": _profile if playing else None,
        "source": _source if playing else None,
        "title": _title if playing else None,
        "subtitle": _subtitle if playing else None,
    }
    if playing and _profile == "media":
        st["paused"] = bool(_ipc_prop("pause"))
        pos, dur = _ipc_prop("time-pos"), _ipc_prop("duration")
        if pos is not None:
            st["position"] = float(pos)
        if dur is not None:
            st["duration"] = float(dur)
    return st


def _supervisor() -> None:
    """Keep the current playback healthy. For live streams, relaunch mpv if it
    dies (self-heal a skip / transcode restart). For media files, when mpv exits
    (movie ended) mark idle so callers can hand the TV back to the Apple TV."""
    global _stopped, _url, _title, _subtitle, _source
    while True:
        time.sleep(3)
        with _lock:
            if _stopped or _url is None or _alive():
                continue
            if _supervise:
                _spawn()                 # live: relaunch
            else:
                _stopped, _url, _title, _subtitle, _source = True, None, None, None, None
                _kiosk("start")               # -> back to the idle dashboard


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path == "/status":
            self._send(200, _status())
        elif self.path == "/kiosk/status":
            self._send(200, _kiosk_status())
        elif self.path == "/healthz":
            self._send(200, {"ok": True})
        elif self.path.startswith("/media/list"):
            rel = parse_qs(urlparse(self.path).query).get("path", [""])[0]
            try:
                self._send(200, _media_list(rel))
            except (ValueError, OSError) as exc:
                self._send(400, {"error": str(exc)})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("content-length", 0) or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        except ValueError:
            return self._send(400, {"error": "bad json"})
        if self.path == "/play":
            url = str(body.get("url", "")).strip()
            if not url:
                return self._send(422, {"error": "missing url"})
            title = str(body.get("title", "")).strip() or None
            subtitle = str(body.get("subtitle", "")).strip() or None
            source = str(body.get("source", "")).strip() or None
            _play(url, body.get("headers"), bool(body.get("audio_only")),
                  title=title, subtitle=subtitle, source=source)
            self._send(200, {"playing": True, "url": url, "title": title,
                             "subtitle": subtitle, "source": source})
        elif self.path == "/media/play":
            rel = str(body.get("path", "")).strip()
            try:
                target = play_media(rel)
            except (ValueError, OSError) as exc:
                return self._send(400, {"error": str(exc)})
            self._send(200, {"playing": target})
        elif self.path == "/shuffle":
            rel = str(body.get("path", "")).strip()
            try:
                target = shuffle_media(rel)
            except (ValueError, OSError) as exc:
                return self._send(400, {"error": str(exc)})
            self._send(200, {"playing": target})
        elif self.path == "/sub/cycle":
            self._send(200, {"ok": _sub_cycle()})
        elif self.path == "/kiosk/restart":
            result = _kiosk_restart()
            self._send(200 if result["ok"] else 500, result)
        elif self.path == "/kiosk/cursor":
            result = _cursor_repair()
            self._send(200 if result["ok"] else 500, result)
        elif self.path == "/tv/reclaim":
            result = _tv_reclaim()
            self._send(200 if result["ok"] else 500, result)
        elif self.path == "/tv/input":
            try:
                n = int(body.get("n", 0))
            except (TypeError, ValueError):
                n = 0
            if not 1 <= n <= 9:
                return self._send(400, {"error": "n must be 1-9"})
            result = _tv_input(n)
            self._send(200 if result["ok"] else 500, result)
        elif self.path == "/control":
            action = str(body.get("action", ""))
            if action == "pause":
                ok = _ipc(["cycle", "pause"]) is not None
            elif action == "seek":
                try:
                    secs = float(body.get("secs", 0) or 0)
                except (TypeError, ValueError):
                    secs = 0.0
                ok = _ipc(["seek", secs, "relative"]) is not None
            else:
                return self._send(400, {"error": "unknown action"})
            self._send(200, {"ok": ok})
        elif self.path == "/stop":
            _stop()
            self._send(200, {"ok": True})
        else:
            self._send(404, {"error": "not found"})

    def log_message(self, *a):  # silence
        pass


if __name__ == "__main__":
    threading.Thread(target=_supervisor, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
