#!/usr/bin/env python3
"""Living-room screen player — controls mpv on the Pi's HDMI (DRM/KMS).

Runs on the Pi HOST (not in Docker) because mpv needs to be DRM master on the
console. The hub (host-networked container) drives it over localhost.

A supervisor thread keeps the current stream alive: if mpv dies while something
should be playing (e.g. jetstream restarts its transcode when you skip media),
it relaunches mpv so playback self-heals.

  POST /play   {url, source?:"mtv", channel?:int, ...}  -> (re)start playback
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
from queue import Empty, Queue
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlparse
from urllib.request import urlopen

_HERE = osp.dirname(osp.abspath(__file__))
IPC_SOCKET = os.environ.get("SCREEN_MPV_IPC", "/tmp/mpv-ipc")  # for sub toggle etc.
# idle-dashboard kiosk service — stopped while mpv plays (both need DRM master),
# restarted when playback ends.
KIOSK_SERVICE = os.environ.get("SCREEN_KIOSK_SERVICE", "kiosk-screen")
CURSOR_INSTALL = os.environ.get(
    "SCREEN_CURSOR_INSTALL", osp.join(_HERE, "install-cursor.sh"))
# Reclaims the TV's HDMI input for the Pi (CEC active-source) — the Apple TV
# steals the input when it wakes; this switches the TV back to the dashboard.
TV_RECLAIM = os.environ.get(
    "SCREEN_TV_RECLAIM", osp.join(_HERE, "tv-reclaim.sh"))
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
_REPO = osp.dirname(osp.dirname(osp.abspath(__file__)))
# mpv resume points ("continue watching") — written on graceful quit, removed by
# mpv itself when a file plays to the end.
WATCH_LATER_DIR = os.environ.get(
    "SCREEN_WATCH_LATER", osp.join(_REPO, "data", "screen", "watch_later"))
# recent shuffle picks, so shuffle doesn't repeat until it has to
SHUFFLE_HISTORY_FILE = os.environ.get(
    "SCREEN_SHUFFLE_HISTORY", osp.join(_REPO, "data", "screen", "shuffle-history.json"))
SHUFFLE_NO_REPEAT = int(os.environ.get("SCREEN_SHUFFLE_NO_REPEAT", "20"))
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


def _probe(path: str) -> tuple[int, int, float, bool] | None:
    """(width, height, fps, is_hdr) of a media file via ffprobe, or None."""
    if not shutil.which("ffprobe"):
        return None
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height,avg_frame_rate,color_transfer", "-of", "json", path],
            capture_output=True, text=True, timeout=20).stdout
        s = json.loads(out)["streams"][0]
        num, den = (s.get("avg_frame_rate") or "0/1").split("/")
        fps = float(num) / float(den) if float(den) else 0.0
        hdr = (s.get("color_transfer") or "") in ("smpte2084", "arib-std-b67")
        return int(s.get("width") or 0), int(s.get("height") or 0), fps, hdr
    except Exception:  # noqa: BLE001
        return None


def _pick_mode(w: int, h: int, fps: float, hdr: bool = False) -> str:
    """Best DRM mode: native-ish resolution + a refresh that's an integer
    multiple of the content fps (judder-free). Falls back to DRM_MODE.

    HDR (PQ/HLG) 4K is output at 1080p: the V3D can composite SDR 4K fine
    (the aerials) but drops ~1/3 of frames on 10-bit HDR at 4K — measured, the
    hw decoder and CPU were idle. Quarter the fragments and it keeps up; the
    TV upscales and still gets the HDR signal (--target-colorspace-hint)."""
    modes = _modes()
    if not modes:
        return DRM_MODE
    fps = fps or 24.0
    want_4k = (w >= 3000 or h >= 1600) and not hdr
    tw, th = (3840, 2160) if want_4k else (1920, 1080)
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
_mtv: dict | None = None         # base/channel and latest schedule response


def _clean_text(value) -> str | None:
    text = str(value or "").strip()
    if not text or text.lower() in {"none", "null", "undefined", "n/a"}:
        return None
    return text


def _build_args(url: str | None, headers: dict | None, audio_only: bool,
                 profile: str = "live", mode: str | None = None) -> list[str]:
    m = mode or DRM_MODE
    if profile == "media":
        # local files are often 4K HEVC. --vo=drm forces drm-copy (frames copied to
        # RAM) which drops ~50% of frames at 4K — visibly choppy. --vo=gpu with
        # zero-copy --hwdec=drm + --profile=fast + display-resample is smooth (the
        # V3D imports the decoder's frames directly), same fix as the aerials.
        # NOT gpu-next (scans out purple on this V3D). --target-colorspace-hint
        # passes HDR metadata through to the TV for HDR files (no-op for SDR).
        # Bigger buffer for SMB reads.
        video = ["--vo=gpu", "--gpu-context=drm", f"--drm-mode={m}",
                 "--hwdec=drm", "--profile=fast", "--video-sync=display-resample",
                 # 4K HDR/DV 10-bit is too heavy for the V3D's full GL path
                 # (~8 dropped frames/s); dumb mode skips the scaler/dither
                 # passes — content plays at native resolution anyway.
                 "--gpu-dumb-mode",
                 "--target-colorspace-hint=yes",
                 "--cache=yes", "--cache-secs=15", "--demuxer-readahead-secs=15",
                 # on-demand: load internal + external (.srt) subs, start OFF,
                 # toggle on via the IPC socket (see /sub/cycle).
                 "--sub-auto=fuzzy", "--sid=no", f"--input-ipc-server={IPC_SOCKET}",
                 # resume support: a graceful quit (see _quit_mpv) saves the
                 # position; replaying the same file picks it back up. mpv
                 # deletes the entry itself when a file finishes normally.
                 "--save-position-on-quit",
                 f"--watch-later-dir={WATCH_LATER_DIR}",
                 "--write-filename-in-watch-later-config",
                 "--watch-later-options=start,sid"]
    elif profile == "mtv":
        video = ["--vo=drm", f"--drm-mode={m}", "--hwdec=no",
                 "--cache=yes", "--cache-secs=4", "--demuxer-readahead-secs=4",
                 "--sid=no", "--sub-auto=no", f"--input-ipc-server={IPC_SOCKET}",
                 "--idle=yes", "--prefetch-playlist=yes",
                 "--osd-font-size=42", "--osd-margin-x=56", "--osd-margin-y=46",
                 "--osd-align-x=left", "--osd-align-y=bottom", "--osd-border-size=2"]
    else:
        # live HLS (H.264): plain drm VO — robust across the transcode restarts
        # jetstream does on skip (gpu-next "export fails" on relaunch). Pi 5 has
        # no H.264 hw decoder so software decode; 1080p is light. SMALL buffer so
        # we stay near the live edge (in sync with the streamer/other viewers) —
        # the network is fast enough not to stutter at a low buffer. jetstream
        # burns its own subs, so don't load a sub track.
        video = ["--vo=drm", f"--drm-mode={m}", "--hwdec=no",
                 "--cache=yes", "--cache-secs=4", "--demuxer-readahead-secs=4",
                 "--hls-bitrate=max", "--sid=no", "--sub-auto=no",
                 # IPC so the supervisor can see video-pts: after a jetstream
                 # discontinuity mpv can wedge with audio running and video
                 # frozen — no errors, process alive, only the pts gives it away
                 f"--input-ipc-server={IPC_SOCKET}"]
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
    if profile != "mtv" and url:
        args.append(url)
    return args


def _quit_mpv() -> None:
    """End the current mpv, gracefully when we can. A graceful IPC quit is what
    lets --save-position-on-quit write the resume point; the pkill afterwards
    is the belt-and-braces cleanup for anything that didn't listen."""
    if _proc is not None and _proc.poll() is None and os.path.exists(IPC_SOCKET):
        _ipc(["quit"])
        for _ in range(20):
            if _proc.poll() is not None:
                break
            time.sleep(0.1)
    subprocess.run(["pkill", "-9", "-x", "mpv"], check=False)


def _spawn() -> None:
    global _proc
    # Take the display from the idle dashboard (cage/chromium hold DRM master).
    _kiosk("stop")
    # End any existing mpv (gracefully, so a movie's position is saved) and WAIT
    # for it (and cage) to fully exit + release the DRM master before starting a
    # new one. Racing a dying process for the display is what causes "device
    # busy" / "export failed" -> a black screen.
    _quit_mpv()
    for _ in range(40):
        if subprocess.run(["pgrep", "-x", "mpv|cage"],
                          stdout=subprocess.DEVNULL).returncode != 0:
            break
        time.sleep(0.1)
    time.sleep(0.5)  # let the kernel release DRM master
    with open(LOG_FILE, "w") as logf:  # fresh log per spawn, for diagnosis
        _proc = subprocess.Popen(
            _build_args(_url, _headers, _audio_only, _profile, _mode),
            stdout=logf, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
    if _profile == "mtv" and _mtv:
        MtvConductor(_proc, dict(_mtv)).start()


def _play(url: str, headers: dict | None = None, audio_only: bool = False,
          profile: str = "live", supervise: bool = True, mode: str | None = None,
          title: str | None = None, subtitle: str | None = None,
          source: str | None = None) -> None:
    global _url, _headers, _audio_only, _stopped, _profile, _supervise, _mode, _title, _subtitle, _source
    global _corrupt_restarts
    _corrupt_restarts = 0
    with _lock:
        _url, _headers, _audio_only, _stopped = url, headers, audio_only, False
        _profile, _supervise, _mode = profile, supervise, mode or DRM_MODE
        _title, _subtitle, _source = title, subtitle, source or profile
        _spawn()


def _stop() -> None:
    global _url, _stopped, _proc, _title, _subtitle, _source, _mtv
    with _lock:
        _stopped, _url, _title, _subtitle, _source = True, None, None, None, None
        _mtv = None
        _quit_mpv()
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


def _pretty_name(real: str, root: str) -> str:
    """Human title for a library file: prefer the folder name ("Hokum (2026)")
    over the release-name file stem; for Season folders, show + episode tag."""
    stem = osp.splitext(osp.basename(real))[0]
    parent = osp.dirname(real)
    if osp.realpath(parent) == root:
        return stem
    pname = osp.basename(parent)
    ep = re.search(r"(?i)s\d{1,2}e\d{1,3}", stem)
    if re.match(r"(?i)(season[ ._-]*\d+|specials)$", pname):
        show = osp.basename(osp.dirname(parent)) or pname
        return f"{show} · {ep.group(0).upper() if ep else pname}"
    return f"{pname} · {ep.group(0).upper()}" if ep else pname


def play_media(rel: str, source: str = "media") -> str:
    target = _safe_media_path(rel)
    if not (osp.isfile(target) and target.lower().endswith(VIDEO_EXTS)):
        raise ValueError("not a playable media file")
    info = _probe(target)                       # (w, h, fps) or None
    mode = _pick_mode(*info) if info else DRM_MODE
    title = _pretty_name(target, osp.realpath(MEDIA_ROOT))
    _play(target, profile="media", supervise=False, mode=mode, title=title, source=source)
    return target


# Walking the SMB share is a network round-trip per directory — cache the file
# list per scope so back-to-back shuffles (and alarm-time shuffles) are instant.
_shuffle_cache: dict[str, tuple[float, list[str]]] = {}
SHUFFLE_CACHE_TTL = int(os.environ.get("SCREEN_SHUFFLE_TTL", "300"))


def _load_shuffle_history() -> list[str]:
    try:
        with open(SHUFFLE_HISTORY_FILE) as fh:
            hist = json.load(fh)
        return hist if isinstance(hist, list) else []
    except (OSError, ValueError):
        return []


def _save_shuffle_history(hist: list[str]) -> None:
    try:
        os.makedirs(osp.dirname(SHUFFLE_HISTORY_FILE), exist_ok=True)
        tmp = SHUFFLE_HISTORY_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(hist, fh)
        os.replace(tmp, SHUFFLE_HISTORY_FILE)
    except OSError:
        pass


def shuffle_media(rel: str = "") -> str:
    """Play a random video from the library (optionally scoped to a subfolder),
    avoiding the last-N picks so shuffle doesn't repeat until it has to."""
    base = _safe_media_path(rel)
    root = osp.realpath(MEDIA_ROOT)
    walk_base = base if osp.isdir(base) else root
    cached = _shuffle_cache.get(walk_base)
    if cached and time.time() - cached[0] < SHUFFLE_CACHE_TTL:
        files = cached[1]
    else:
        files = []
        for dirpath, _dirs, names in os.walk(walk_base):
            for n in names:
                if not n.startswith(".") and n.lower().endswith(VIDEO_EXTS):
                    files.append(osp.relpath(osp.join(dirpath, n), root))
        _shuffle_cache[walk_base] = (time.time(), files)
    if not files:
        raise ValueError("no media files")
    history = _load_shuffle_history()
    fresh = [f for f in files if f not in history]
    if not fresh:          # everything's been played recently — start over
        fresh = files
    pick = random.choice(fresh)
    keep = max(0, min(SHUFFLE_NO_REPEAT, len(files) - 1))
    _save_shuffle_history(([pick] + [h for h in history if h != pick])[:keep])
    return play_media(pick, source="shuffle")


def _resume_list() -> dict:
    """Unfinished library files with a saved position ("continue watching").
    Reads mpv's watch-later entries; the '#' comment line carries the path
    (--write-filename-in-watch-later-config)."""
    root = osp.realpath(MEDIA_ROOT)
    entries = []
    try:
        names = os.listdir(WATCH_LATER_DIR)
    except OSError:
        names = []
    for n in names:
        wl = osp.join(WATCH_LATER_DIR, n)
        try:
            with open(wl) as fh:
                txt = fh.read(4096)
        except OSError:
            continue
        path, start = None, None
        for line in txt.splitlines():
            if line.startswith("#"):
                path = line.lstrip("# ").strip()
            elif line.startswith("start="):
                try:
                    start = float(line[len("start="):])
                except ValueError:
                    pass
        if not path or start is None:
            continue
        real = osp.realpath(path)
        if not (real == root or real.startswith(root + os.sep)) or not osp.isfile(real):
            continue      # gone from the library, or not ours
        entries.append({
            "path": osp.relpath(real, root),
            "name": _pretty_name(real, root),
            "position": start,
            "saved_at": osp.getmtime(wl),
        })
    entries.sort(key=lambda e: e["saved_at"], reverse=True)
    return {"entries": entries[:10]}


def forget_resume(rel: str) -> bool:
    """Drop the saved resume point for a library file (the Continue-watching
    "Cancel" action). Removes every watch-later entry whose '#' comment names
    this file; returns whether anything was removed."""
    real_target = osp.realpath(_safe_media_path(rel))
    try:
        names = os.listdir(WATCH_LATER_DIR)
    except OSError:
        return False
    removed = False
    for n in names:
        wl = osp.join(WATCH_LATER_DIR, n)
        try:
            with open(wl) as fh:
                txt = fh.read(4096)
        except OSError:
            continue
        path = None
        for line in txt.splitlines():
            if line.startswith("#"):
                path = line.lstrip("# ").strip()
                break
        if path and osp.realpath(path) == real_target:
            try:
                os.remove(wl)
                removed = True
            except OSError:
                pass
    return removed


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


class MtvUnavailable(RuntimeError):
    pass


def _mtv_item_url(base: str, data: dict, item: dict) -> str:
    return urljoin(base.rstrip("/") + "/", str(data.get("url_base", "/videos/"))) + \
        quote(str(item["id"]), safe="") + ".mp4"


def mtv_now(base: str, channel: int) -> dict:
    """Return the MTV site's authoritative schedule response."""
    url = base.rstrip("/") + "/admin/api/now?" + urlencode({"ch": channel})
    try:
        with urlopen(url, timeout=5) as response:
            data = json.load(response)
        now, nxt = data["now"], data["next"]
        for item in (now, nxt):
            if not isinstance(item, dict) or not item.get("id"):
                raise ValueError("schedule item has no id")
        now["offset"] = float(now["offset"])
        now["duration"] = float(now["duration"])
        return data
    except HTTPError as exc:
        exc.close()
        raise MtvUnavailable(str(exc)) from exc
    except (URLError, OSError, ValueError, KeyError, TypeError) as exc:
        raise MtvUnavailable(str(exc)) from exc


def _play_mtv(base: str, channel: int) -> dict:
    global _mtv
    data = mtv_now(base, channel)
    now = data["now"]
    url = _mtv_item_url(base, data, now)
    with _lock:
        _mtv = {"base": base.rstrip("/"), "channel": channel, "schedule": data}
        _play(url, profile="mtv", supervise=True, mode=DRM_MODE,
              title=_clean_text(now.get("artist")), subtitle=_clean_text(now.get("song")),
              source="mtv")
    return {"url": url, "title": _title, "subtitle": _subtitle}


class MtvConductor(threading.Thread):
    """Keep one idle mpv process joined to MTV's wall-clock schedule."""

    def __init__(self, proc: subprocess.Popen, state: dict):
        super().__init__(daemon=True, name="mtv-conductor")
        self.proc = proc
        self.state = state
        self.sock: socket.socket | None = None
        self.events: Queue = Queue()
        self.pending: dict[int, Queue] = {}
        self.write_lock = threading.Lock()
        self.next_request = 1
        self.closed = threading.Event()
        self.idle = False
        self.loaded = False
        self.failure_logged = False
        self.correction_pending = False
        self.current_path: str | None = None
        self.current_duration: float | None = None
        self.current_credits: str | None = None
        self.end_credits_shown = False

    def _current(self) -> bool:
        return _proc is self.proc and _profile == "mtv" and not _stopped

    def _reader(self) -> None:
        buf = b""
        try:
            while self._current() and not self.closed.is_set():
                chunk = self.sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    request_id = obj.get("request_id")
                    if request_id in self.pending:
                        self.pending[request_id].put(obj)
                    elif "event" in obj:
                        self.events.put(obj)
        except OSError:
            pass
        finally:
            self.closed.set()
            for reply in list(self.pending.values()):
                reply.put(None)
            # A live mpv with no conductor cannot advance or recover from idle.
            # Ending only this process lets the supervisor rejoin the schedule.
            if self._current() and self.proc.poll() is None:
                self.proc.terminate()

    def command(self, command: list) -> dict | None:
        if self.closed.is_set() or not self.sock:
            return None
        with self.write_lock:
            request_id = self.next_request
            self.next_request += 1
            reply: Queue = Queue()
            self.pending[request_id] = reply
            try:
                self.sock.sendall((json.dumps({"command": command, "request_id": request_id}) + "\n").encode())
                return reply.get(timeout=5)
            except (OSError, Empty):
                return None
            finally:
                self.pending.pop(request_id, None)

    def _load_schedule(self, data: dict, replace: bool) -> None:
        base = self.state["base"]
        now_url = _mtv_item_url(base, data, data["now"])
        next_url = _mtv_item_url(base, data, data["next"])
        if replace:
            self.command(["loadfile", now_url, "replace", -1,
                          f"start={float(data['now']['offset'])}"])
        self.command(["loadfile", next_url, "append", -1, "start=0"])

    def _refresh(self) -> None:
        global _url, _title, _subtitle, _mtv
        path_reply = self.command(["get_property", "path"])
        path = path_reply.get("data") if path_reply else None
        try:
            data = mtv_now(self.state["base"], self.state["channel"])
        except MtvUnavailable as exc:
            if not self.failure_logged:
                _log_event(f"MTV schedule unavailable — keeping queue: {exc}")
                self.failure_logged = True
            return
        self.failure_logged = False
        pos_reply = self.command(["get_property", "time-pos"])
        pos = pos_reply.get("data") if pos_reply else None
        expected = _mtv_item_url(self.state["base"], data, data["now"])
        matched = path == expected and pos is not None and abs(float(pos) - float(data["now"]["offset"])) <= 2
        if not matched:
            if self.correction_pending:
                _log_event("MTV still differs after correction — waiting for next boundary")
                self.correction_pending = False
                return
            self.correction_pending = True
            self._load_schedule(data, replace=True)
            return
        self.correction_pending = False
        # Keep the playing item, but replace its queued successor with the latest schedule.
        self.command(["playlist-clear"])
        self._load_schedule(data, replace=False)
        now = data["now"]
        artist, song = _clean_text(now.get("artist")), _clean_text(now.get("song"))
        with _lock:
            if self._current():
                _url, _title, _subtitle = expected, artist, song
                _mtv = {**self.state, "schedule": data}
        self.current_path = expected
        self.current_duration = float(now["duration"])
        self.current_credits = "\n".join(v for v in (artist, song) if v)
        self.end_credits_shown = False
        if self.current_credits:
            self.command(["show-text", self.current_credits, 8000])

    def _connect(self) -> bool:
        deadline = time.time() + 5
        while self._current() and time.time() < deadline:
            try:
                self.sock = socket.socket(socket.AF_UNIX)
                self.sock.connect(IPC_SOCKET)
                return True
            except OSError:
                if self.sock:
                    self.sock.close()
                time.sleep(0.1)
        return False

    def _handle_event(self, event: dict) -> None:
        if event.get("event") == "file-loaded":
            self.loaded = True
            self.idle = False
            self._refresh()
        elif event.get("event") == "property-change":
            if event.get("name") == "idle-active":
                # mpv starts idle before the conductor's first loadfile. Treating
                # that initial state as a drained playlist issues duplicate loads.
                if self.loaded:
                    self.idle = bool(event.get("data"))
            elif (event.get("name") == "time-pos" and self.current_duration
                  and not self.end_credits_shown and event.get("data") is not None
                  and self.current_duration - float(event["data"]) <= 10):
                if self.current_credits:
                    self.command(["show-text", self.current_credits, 8000])
                self.end_credits_shown = True

    def run(self) -> None:
        if not self._connect():
            _log_event("MTV conductor could not connect to mpv IPC")
            if self._current() and self.proc.poll() is None:
                self.proc.terminate()
            return
        if not self._current():
            self.sock.close()
            return
        threading.Thread(target=self._reader, daemon=True, name="mtv-ipc-reader").start()
        self.command(["observe_property", 1, "time-pos"])
        self.command(["observe_property", 2, "idle-active"])
        self._load_schedule(self.state["schedule"], replace=True)
        next_idle_retry = 0.0
        while self._current() and not self.closed.is_set():
            try:
                event = self.events.get(timeout=1)
            except Empty:
                event = None
            if event:
                self._handle_event(event)
            if self.idle and time.time() >= next_idle_retry:
                next_idle_retry = time.time() + 5
                try:
                    data = mtv_now(self.state["base"], self.state["channel"])
                except MtvUnavailable:
                    continue
                self.state["schedule"] = data
                self._load_schedule(data, replace=True)
        if self.sock:
            self.sock.close()


def _sub_cycle() -> bool:
    """Cycle the subtitle track on the current media playback (off → 1 → 2 → off)."""
    return _ipc(["cycle", "sub"]) is not None


def _neighbor_media(delta: int) -> str:
    """Rel path of the next/previous video beside the currently-playing file —
    "next episode" for a season folder, sorted the same way the browser lists it."""
    with _lock:
        cur = _url if (_profile == "media" and not _stopped) else None
    if not cur:
        raise ValueError("nothing from the library is playing")
    root = osp.realpath(MEDIA_ROOT)
    folder = osp.dirname(cur)
    sibs = sorted((n for n in os.listdir(folder)
                   if not n.startswith(".") and n.lower().endswith(VIDEO_EXTS)),
                  key=str.lower)
    try:
        i = sibs.index(osp.basename(cur))
    except ValueError:
        raise ValueError("current file left the library")
    j = i + delta
    if not 0 <= j < len(sibs):
        raise ValueError("no more files in this folder")
    return osp.relpath(osp.join(folder, sibs[j]), root)


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
    if playing:
        vol = _ipc_prop("volume")       # live streams have a socket too
        if vol is not None:
            st["volume"] = round(float(vol))
            st["muted"] = bool(_ipc_prop("mute"))
    if playing and _profile == "media":
        st["paused"] = bool(_ipc_prop("pause"))
        pos, dur = _ipc_prop("time-pos"), _ipc_prop("duration")
        if pos is not None:
            st["position"] = float(pos)
        if dur is not None:
            st["duration"] = float(dur)
    return st


# h264 decode-corruption signature: joining the jetstream HLS while its
# transcode is (re)starting decodes P-frames against references we never got —
# smeared video, clean audio. A fresh steady-state rejoin is clean (verified),
# so the supervisor watches mpv's log and rejoins once when it sees this.
_CORRUPT_MARKERS = (b"error while decoding", b"Reference", b"unavailable for requested intra")
CORRUPT_ERR_THRESHOLD = int(os.environ.get("SCREEN_CORRUPT_ERRS", "25"))
# The rejoin budget is *rolling*, not per-playback. jetstream starts a new
# ffmpeg run (new fMP4 init segment) at every title change — roughly every
# 20-30 min — and mpv carries the stale init across the discontinuity, so the
# picture corrupts at each boundary and only a rejoin clears it. With a
# lifetime budget, the third title change of the evening exhausted it and left
# a permanently smeared picture on the TV. Allow 3 rejoins per window and let
# the window lapse after a stretch of healthy playback; a genuinely broken
# stream still can't spin forever.
CORRUPT_BUDGET_WINDOW = float(os.environ.get("SCREEN_CORRUPT_WINDOW", "300"))
# Supervisor polls every 3s, so this is how long a wedged picture stays up
# before the rejoin starts. Was 3 (~9s); the buffering guard in the watchdog
# makes 2 (~6s) safe, since a cache stall no longer looks like a freeze.
VIDEO_STALL_READS = int(os.environ.get("SCREEN_VIDEO_STALL_READS", "2"))
_corrupt_restarts = 0     # per user-initiated playback; reset in _play()
_corrupt_window_at = 0.0  # when the current budget window started


def _new_log_errors(pos: int) -> tuple[int, int]:
    """Count new h264-corruption lines in the mpv log since offset pos."""
    try:
        with open(LOG_FILE, "rb") as fh:
            fh.seek(pos)
            data = fh.read()
    except OSError:
        return 0, pos
    n = sum(1 for line in data.replace(b"\r", b"\n").split(b"\n")
            if b"h264:" in line and any(m in line for m in _CORRUPT_MARKERS))
    return n, pos + len(data)


def _rejoin_budget(reason: str) -> None:
    """Spend one rejoin; the first spend of a window starts its clock."""
    global _corrupt_restarts, _corrupt_window_at
    if _corrupt_restarts == 0:
        _corrupt_window_at = time.time()
    _corrupt_restarts += 1
    _log_event(f"rejoin ({reason}) — budget {_corrupt_restarts}/3 this window")


def _log_event(msg: str) -> None:
    """Say what the supervisor did, and why, in the journal.

    Self-healing used to be invisible: the only evidence a rejoin had happened
    was a new mpv PID in the journal, so working out what a stream did overnight
    meant diffing process ids against timestamps. These lines make it greppable
    (`journalctl -u screen-player | grep 'screen:'`)."""
    print(f"screen: {msg} [{_source or 'idle'}: {_title or _url or '-'}]", flush=True)


def _supervisor() -> None:
    """Keep the current playback healthy. For live streams: relaunch mpv if it
    dies (self-heal a skip / transcode restart) — after a settle delay, so we
    don't rejoin mid-churn — and rejoin once if the picture is decode-corrupted.
    For media files, when mpv exits (movie ended) mark idle so callers can hand
    the TV back to the Apple TV."""
    global _stopped, _url, _title, _subtitle, _source, _corrupt_restarts, _corrupt_window_at
    dead_since = 0.0
    last_proc = None
    log_pos = 0
    spawn_errors = 0
    last_video_pts = None
    video_stalls = 0
    budget_warned = False    # so an exhausted budget logs once, not every 3s
    mtv_retry_since = 0.0
    while True:
        time.sleep(3)
        with _lock:
            if _stopped or _url is None:
                dead_since = 0.0
                continue
            if _proc is not last_proc:          # new spawn -> fresh log trackers
                last_proc = _proc
                log_pos = 0
                spawn_errors = 0
                last_video_pts = None
                video_stalls = 0
                mtv_retry_since = 0.0
            if _alive():
                dead_since = 0.0
                if _profile == "live":
                    if (_corrupt_restarts
                            and time.time() - _corrupt_window_at >= CORRUPT_BUDGET_WINDOW):
                        _corrupt_restarts = 0   # healthy for a while -> budget refills
                        budget_warned = False
                        _log_event("healthy — rejoin budget refilled")
                    n, log_pos = _new_log_errors(log_pos)
                    spawn_errors += n
                    if spawn_errors >= CORRUPT_ERR_THRESHOLD:
                        if _corrupt_restarts < 3:
                            _rejoin_budget(f"{spawn_errors} decode errors")
                            _spawn()            # clean rejoin fixes the smear
                            continue
                        if not budget_warned:   # once per exhausted window
                            budget_warned = True
                            _log_event("rejoin budget spent — leaving a corrupt "
                                       "picture up; refills after "
                                       f"{CORRUPT_BUDGET_WINDOW:.0f}s healthy")
                    # video-freeze watchdog: audio keeps playing but the video
                    # frame counter stops (post-discontinuity wedge; video-pts
                    # is unavailable on this profile). This is the failure mode
                    # that actually fires in practice, so detect it fast: a
                    # frozen picture lasts stalls x 3s plus the rejoin.
                    # Buffering stalls the counter too and resolves on its own —
                    # rejoining through one would turn a hiccup into a restart —
                    # so paused-for-cache doesn't count and clears the tally.
                    pts = _ipc_prop("estimated-frame-number")
                    buffering = bool(_ipc_prop("paused-for-cache"))
                    if (pts is not None and pts == last_video_pts
                            and not buffering and not _ipc_prop("pause")):
                        video_stalls += 1
                        if video_stalls >= VIDEO_STALL_READS and _corrupt_restarts < 3:
                            _rejoin_budget(f"video frozen ~{VIDEO_STALL_READS * 3}s, "
                                           "audio still running")
                            video_stalls = 0
                            _spawn()            # video wedged: clean rejoin
                    else:
                        video_stalls = 0
                    last_video_pts = pts
                continue
            if _supervise:
                # died (jetstream transcode restart): wait a few seconds before
                # rejoining so the new run's playlist has settled
                if dead_since == 0.0:
                    dead_since = time.time()
                elif time.time() - dead_since >= 4:
                    if _profile == "mtv" and _mtv:
                        if mtv_retry_since == 0.0:
                            mtv_retry_since = time.time()
                        try:
                            _log_event("mpv exited — rejoining MTV schedule")
                            _play_mtv(_mtv["base"], _mtv["channel"])
                            dead_since = 0.0
                            mtv_retry_since = 0.0
                        except (MtvUnavailable, OSError) as exc:
                            _log_event(f"MTV rejoin failed: {exc}")
                            if time.time() - mtv_retry_since >= 60:
                                _log_event("MTV unavailable for 60s — back to the dashboard")
                                _stop()
                    else:
                        dead_since = 0.0
                        _log_event("mpv exited — relaunching (jetstream run change?)")
                        _spawn()                # live: relaunch
            else:
                _log_event("playback finished — back to the dashboard")
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
        elif self.path == "/media/resume":
            self._send(200, _resume_list())
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
            title = _clean_text(body.get("title"))
            subtitle = _clean_text(body.get("subtitle"))
            source = _clean_text(body.get("source"))
            if source == "mtv":
                try:
                    channel = int(body.get("channel", 1))
                    result = _play_mtv(url, channel)
                except (TypeError, ValueError):
                    return self._send(422, {"error": "channel must be an integer"})
                except MtvUnavailable as exc:
                    return self._send(502, {"error": f"mtv: {exc}"})
                except OSError as exc:
                    _stop()
                    return self._send(502, {"error": f"mtv: could not start mpv: {exc}"})
                self._send(200, {"ok": True, "playing": True,
                                 "profile": "mtv", "source": "mtv", **result})
            else:
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
        elif self.path == "/media/resume/forget":
            rel = str(body.get("path", "")).strip()
            try:
                ok = forget_resume(rel)
            except (ValueError, OSError) as exc:
                return self._send(400, {"error": str(exc)})
            self._send(200, {"ok": ok})
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
            elif action == "chapter":       # skip intros/credits on chaptered files
                try:
                    n = int(body.get("n", 1) or 1)
                except (TypeError, ValueError):
                    n = 1
                ok = _ipc(["add", "chapter", n]) is not None
            elif action == "audio":         # cycle audio track (multi-audio files)
                ok = _ipc(["cycle", "audio"]) is not None
            elif action == "volume":
                # mpv's own (software) volume — instant and absolute, unlike
                # CEC stepping against the TV. Absolute with {"level": 0-130},
                # relative with {"step": ±n}.
                if body.get("level") is not None:
                    try:
                        level = max(0.0, min(130.0, float(body["level"])))
                    except (TypeError, ValueError):
                        return self._send(400, {"error": "level must be a number"})
                    ok = _ipc(["set_property", "volume", level]) is not None
                else:
                    try:
                        step = float(body.get("step", 5) or 5)
                    except (TypeError, ValueError):
                        step = 5.0
                    ok = _ipc(["add", "volume", step]) is not None
                return self._send(200, {"ok": ok, "volume": _ipc_prop("volume"),
                                        "muted": bool(_ipc_prop("mute"))})
            elif action == "mute":
                ok = _ipc(["cycle", "mute"]) is not None
                return self._send(200, {"ok": ok, "volume": _ipc_prop("volume"),
                                        "muted": bool(_ipc_prop("mute"))})
            else:
                return self._send(400, {"error": "unknown action"})
            self._send(200, {"ok": ok})
        elif self.path in ("/media/next", "/media/prev"):
            try:
                rel = _neighbor_media(1 if self.path.endswith("next") else -1)
                target = play_media(rel)
            except (ValueError, OSError) as exc:
                return self._send(409, {"error": str(exc)})
            self._send(200, {"playing": target})
        elif self.path == "/stop":
            _stop()
            self._send(200, {"ok": True})
        else:
            self._send(404, {"error": "not found"})

    def log_message(self, *a):  # silence
        pass


if __name__ == "__main__":
    # Nothing is playing on a fresh start, so bring the idle dashboard up. The
    # unit doesn't do this itself, and without it a bare restart (e.g. after an
    # edit) leaves the TV blank until the next playback ends.
    _kiosk("start")
    threading.Thread(target=_supervisor, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
