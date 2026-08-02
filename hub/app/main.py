"""Living-room hub gateway.

Serves the single-page UI and gives it one same-origin API surface:
  /api/atv/*   → proxied to the appletv service
  /api/cec/*   → proxied to the cec service
  /api/alarms  → music-alarm schedule (CRUD) + a tick scheduler that fires them

Runs on the Pi alongside appletv + cec; with host networking the defaults
(localhost:8010 / :8020) just work.
"""
from __future__ import annotations

import asyncio
import datetime
import os
import re
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import alarms
from . import plex as plexmod
from . import scenes as scenesmod

ATV_URL = os.environ.get("ATV_URL", "http://localhost:8010").rstrip("/")
CEC_URL = os.environ.get("CEC_URL", "http://localhost:8020").rstrip("/")
# Which TV HDMI port the Apple TV is on (CEC phys addr n.0.0.0) — lets us force
# the input switch with Set Stream Path instead of waiting for tvOS to assert.
ATV_HDMI_INPUT = int(os.environ.get("ATV_HDMI_INPUT", "3"))
# Pi-side mpv screen player (fallback path; plays video on the Pi's HDMI)
SCREEN_URL = os.environ.get("SCREEN_URL", "http://localhost:9595").rstrip("/")
# jetstream broadcast (HLS from the homelab jetstream service), AirPlayed to the
# Apple TV so its hardware decodes the stream. The Apple TV fetches the URL
# itself and can't send the viewer cookie, so jetstream's nginx grants /hls/ to
# LAN clients without a token — this is just the plain manifest URL.
JETSTREAM_URL = os.environ.get("JETSTREAM_URL", "")
JETSTREAM_TITLE = os.environ.get("JETSTREAM_TITLE", "").strip()
JETSTREAM_TITLE_URL = os.environ.get("JETSTREAM_TITLE_URL", "").strip()
# Viewer-API endpoints on the homelab jetstream. Same `lt` cookie auth as the title
# discovery endpoint (the HLS path itself bypasses auth on the LAN). SKIP_URL is
# optional — when unset the Skip button on the Now-Playing card hides itself so the
# UI never advertises a control that isn't wired.
JETSTREAM_SKIP_URL = os.environ.get("JETSTREAM_SKIP_URL", "").strip()
# Optional cookie for the mpv-on-Pi fallback (/api/screen/livestream). With the
# jetstream LAN bypass in place the Pi reaches /hls tokenless, so this is only
# needed if that bypass is ever removed; empty → no Cookie header.
JETSTREAM_COOKIE = os.environ.get("JETSTREAM_COOKIE", "")  # e.g. "lt=<token>"
STATIC = Path(__file__).parent / "static"


def _jetstream_headers() -> dict:
    return {"Cookie": JETSTREAM_COOKIE} if JETSTREAM_COOKIE else {}


def _clean_text(value) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text.lower() in {"none", "null", "undefined", "n/a"}:
        return None
    return text


def _first_text(data, keys: tuple[str, ...]):
    if isinstance(data, str):
        return _clean_text(data)
    if not isinstance(data, dict):
        return None
    for key in keys:
        text = _clean_text(data.get(key))
        if text:
            return text
    return None


def _first_title(data):
    if isinstance(data, str):
        return _clean_text(data)
    if not isinstance(data, dict):
        return None
    for key in ("title", "name", "media", "file", "now_playing", "nowPlaying"):
        value = data.get(key)
        text = _clean_text(value)
        if text:
            return text
        nested = _first_title(value)
        if nested:
            return nested
    return None


async def _jetstream_now_playing() -> dict | None:
    if JETSTREAM_TITLE:
        return {"title": JETSTREAM_TITLE}
    if not JETSTREAM_TITLE_URL:
        return None
    try:
        r = await client.get(JETSTREAM_TITLE_URL, headers=_jetstream_headers(), timeout=5.0)
        if not r.is_success:
            return None
        ctype = r.headers.get("content-type", "")
        payload = None
        if "json" in ctype:
            payload = r.json()
        else:
            payload = r.text[:200]
        title = _first_title(payload)
        if not title:
            return None
        subtitle = _first_text(payload, (
            "subtitle", "artist", "show", "series", "program", "episode",
            "description", "details", "meta",
        ))
        return {"title": title, "subtitle": subtitle}
    except (httpx.RequestError, ValueError):
        return None


client: httpx.AsyncClient
scheduler: alarms.AlarmScheduler
plex_client: plexmod.PlexClient
# Which HDMI source we last drove the TV to. The Pi's HDMI shows either mpv or
# the idle dashboard, so "pi" is the resting state; AirPlay/handoff flips it to
# "appletv". Lets the UI highlight the active input (CEC can't report it here).
_active_input: str = "pi"

# --- auto-off: after midnight, an idle dashboard shouldn't keep the TV lit ----
AUTO_OFF = os.environ.get("AUTO_OFF", "1") != "0"
AUTO_OFF_START = int(os.environ.get("AUTO_OFF_START_HOUR", "0"))   # from 00:xx
AUTO_OFF_END = int(os.environ.get("AUTO_OFF_END_HOUR", "6"))       # until 06:00
AUTO_OFF_COOLDOWN = int(os.environ.get("AUTO_OFF_COOLDOWN", "3600"))


async def _auto_off_loop() -> None:
    """Between AUTO_OFF_START..AUTO_OFF_END, if the TV is on but only showing
    the idle dashboard (Pi input, nothing playing), stand it down. Requires two
    consecutive positive checks (~10 min of idle) before acting, and backs off
    for an hour afterwards so a user turning the TV back on wins."""
    strikes = 0
    cooldown_until = 0.0
    while True:
        await asyncio.sleep(300)
        if not AUTO_OFF or time.monotonic() < cooldown_until:
            continue
        hour = datetime.datetime.now(alarms._TZ).hour
        in_window = (AUTO_OFF_START <= hour < AUTO_OFF_END) if AUTO_OFF_START < AUTO_OFF_END \
            else (hour >= AUTO_OFF_START or hour < AUTO_OFF_END)
        if not in_window or _active_input != "pi":
            strikes = 0
            continue
        try:
            sc = (await client.get(f"{SCREEN_URL}/status", timeout=5.0)).json()
            cec = (await client.get(f"{CEC_URL}/api/status", timeout=10.0)).json()
        except (httpx.RequestError, ValueError):
            strikes = 0
            continue
        if sc.get("playing") or cec.get("tv_power") != "on":
            strikes = 0
            continue
        strikes += 1
        if strikes < 2:
            continue
        strikes = 0
        cooldown_until = time.monotonic() + AUTO_OFF_COOLDOWN
        try:
            await client.post(f"{CEC_URL}/api/tv/off")
        except httpx.RequestError:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    global client, scheduler, plex_client
    client = httpx.AsyncClient(timeout=30.0)
    plex_client = plexmod.PlexClient(client=client)
    scheduler = alarms.AlarmScheduler(
        client, ATV_URL, CEC_URL,
        os.environ.get("HUB_SELF_URL", "http://localhost:8080"))
    scheduler.start()
    auto_off = asyncio.create_task(_auto_off_loop())
    yield
    auto_off.cancel()
    await scheduler.stop()
    await client.aclose()


app = FastAPI(title="livingroom-hub", lifespan=lifespan)

# Locally-cached Apple aerial videos for the dashboard background (populated by
# screen/fetch-aerials.sh -> ./data/hub/aerials, i.e. /data/aerials in-container).
# StaticFiles serves HTTP range requests so the <video> can seek/stream. Mounted
# only if present so the hub still starts before the cache is built.
AERIALS_DIR = Path(os.environ.get("AERIALS_DIR", "/data/aerials"))
AERIALS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/aerials", StaticFiles(directory=AERIALS_DIR), name="aerials")
# PWA bits (manifest + icons) and any other static assets by URL
app.mount("/static", StaticFiles(directory=STATIC), name="static")


async def _proxy(base: str, path: str, request: Request) -> Response:
    url = f"{base}/api/{path}"
    body = await request.body()
    try:
        r = await client.request(
            request.method, url, content=body or None,
            params=request.query_params,
            headers={"content-type": request.headers.get("content-type", "application/json")},
        )
    except httpx.RequestError as exc:
        return JSONResponse(status_code=502,
                            content={"error": f"{base} unreachable: {exc}"})
    return Response(content=r.content, status_code=r.status_code,
                    media_type=r.headers.get("content-type", "application/json"))


async def _post_required(url: str, label: str, **kwargs) -> str | None:
    """POST to a local control service and return a short error if it fails."""
    try:
        r = await client.post(url, **kwargs)
    except httpx.RequestError as exc:
        return f"{label} unreachable: {exc}"
    if r.is_success:
        return None
    try:
        data = r.json()
        detail = data.get("error") or data.get("detail") or r.text
    except ValueError:
        detail = r.text
    return f"{label} failed ({r.status_code}): {detail}"


@app.api_route("/api/atv/{path:path}", methods=["GET", "POST"])
async def atv_proxy(path: str, request: Request):
    return await _proxy(ATV_URL, path, request)


@app.api_route("/api/cec/{path:path}", methods=["GET", "POST"])
async def cec_proxy(path: str, request: Request):
    return await _proxy(CEC_URL, path, request)


@app.get("/api/alarms")
async def list_alarms():
    return alarms.load_alarms()


@app.post("/api/alarms")
async def add_alarm(body: dict):
    if not body.get("time"):
        return JSONResponse(status_code=422, content={"error": "missing time (HH:MM)"})
    return alarms.create_alarm(body)


@app.put("/api/alarms/{alarm_id}")
async def edit_alarm(alarm_id: str, body: dict):
    a = alarms.update_alarm(alarm_id, body)
    return a or JSONResponse(status_code=404, content={"error": "no such alarm"})


@app.delete("/api/alarms/{alarm_id}")
async def remove_alarm(alarm_id: str):
    return {"ok": alarms.delete_alarm(alarm_id)}


@app.post("/api/alarms/{alarm_id}/toggle")
async def toggle_alarm(alarm_id: str):
    a = alarms.toggle_alarm(alarm_id)
    return a or JSONResponse(status_code=404, content={"error": "no such alarm"})


@app.post("/api/alarms/{alarm_id}/test")
async def test_alarm(alarm_id: str):
    a = alarms.get_alarm(alarm_id)
    if not a:
        return JSONResponse(status_code=404, content={"error": "no such alarm"})
    return await scheduler.fire(a)


# --- Scenes (data-driven; defaults from scenes.py, override at data/hub/scenes.json)
@app.get("/api/scenes")
async def list_scenes():
    """Available scenes as {name: label}. The UI uses this to render one button per
    scene; the engine reads the JSON live on each /run so edits don't need a rebuild.
    Non-dict entries (e.g. a "_comment" string in the JSON) are skipped."""
    return {name: meta.get("label", name)
            for name, meta in scenesmod.load_scenes().items()
            if isinstance(meta, dict)}


@app.post("/api/scenes/{name}/run")
async def run_scene(name: str):
    try:
        return await scenesmod.run_scene(name, client, ATV_URL, CEC_URL)
    except KeyError:
        return JSONResponse(status_code=404, content={"error": f"no such scene: {name}"})


# --- Plex music (browse + AirPlay to the Apple TV) ---
@app.get("/api/plex/sections")
async def plex_sections():
    return await plex_client.music_sections()


@app.get("/api/plex/playlists")
async def plex_playlists():
    return await plex_client.playlists()


@app.get("/api/plex/playlists/{rating_key}/tracks")
async def plex_playlist_tracks(rating_key: str):
    return await plex_client.playlist_tracks(rating_key)


@app.get("/api/plex/browse/{rating_key}")
async def plex_browse(rating_key: str):
    return await plex_client.children(rating_key)


@app.get("/api/plex/search")
async def plex_search(q: str):
    return await plex_client.search(q)


@app.post("/api/plex/play")
async def plex_play(body: dict):
    rating_key = body.get("ratingKey")
    if not rating_key:
        raise HTTPException(422, "missing ratingKey")
    track = await plex_client.resolve_stream_url(rating_key)
    if not track.get("url"):
        raise HTTPException(502, "could not resolve stream url")
    await client.post(f"{ATV_URL}/api/stream",
                      json={"url": track["url"], "volume": body.get("volume")})
    return {"playing": track}


# The playlist queue: one track AirPlays at a time (pyatv streams single
# files), the loop advances on the track's duration — or immediately when
# next/prev pokes the event. State is inspectable via /api/plex/queue.
_queue: dict = {"tracks": [], "index": -1, "task": None, "event": None,
                "jump": None, "label": None}


async def _play_queue(tracks: list[dict], volume=None):
    ev: asyncio.Event = _queue["event"]
    i = 0
    while 0 <= i < len(tracks):
        t = tracks[i]
        _queue["index"] = i
        if not t.get("url"):
            i += 1
            continue
        await client.post(f"{ATV_URL}/api/stream", json={"url": t["url"], "volume": volume})
        volume = None  # only set volume on the first track
        ev.clear()
        try:
            await asyncio.wait_for(ev.wait(), timeout=max(1, (t.get("duration") or 0) / 1000))
        except asyncio.TimeoutError:
            i += 1          # track ran its course
            continue
        i = _queue.get("jump") if _queue.get("jump") is not None else i + 1
        _queue["jump"] = None
        # tear the current stream down before starting the next one
        try:
            await client.post(f"{ATV_URL}/api/stream/stop")
            await asyncio.sleep(0.5)
        except httpx.RequestError:
            pass
    _queue.update(index=-1, task=None, tracks=[], label=None)


def _queue_jump(delta: int) -> dict:
    if not _queue["task"] or _queue["index"] < 0:
        raise HTTPException(409, "no queue playing")
    target = max(0, min(len(_queue["tracks"]) - 1, _queue["index"] + delta))
    _queue["jump"] = target
    _queue["event"].set()
    return {"ok": True, "index": target, "total": len(_queue["tracks"])}


@app.post("/api/plex/play/playlist")
async def plex_play_playlist(body: dict):
    tracks = await plex_client.playlist_tracks(body["ratingKey"])
    if _queue["task"]:
        _queue["task"].cancel()
    _queue.update(tracks=tracks, index=-1, event=asyncio.Event(), jump=None,
                  label=body.get("label"))
    _queue["task"] = asyncio.create_task(_play_queue(tracks, body.get("volume")))
    return {"queued": len(tracks)}


@app.get("/api/plex/queue")
async def plex_queue_status():
    active = bool(_queue["task"]) and _queue["index"] >= 0
    if not active:
        return {"active": False}
    t = _queue["tracks"][_queue["index"]]
    return {"active": True, "index": _queue["index"], "total": len(_queue["tracks"]),
            "label": _queue["label"],
            "track": {k: t.get(k) for k in ("title", "artist", "album", "duration")}}


@app.post("/api/plex/queue/next")
async def plex_queue_next():
    return _queue_jump(+1)


@app.post("/api/plex/queue/prev")
async def plex_queue_prev():
    return _queue_jump(-1)


@app.post("/api/plex/stop")
async def plex_stop():
    if _queue["task"]:
        _queue["task"].cancel()
    _queue.update(tracks=[], index=-1, task=None, jump=None, label=None)
    await client.post(f"{ATV_URL}/api/stream/stop")
    return {"stopped": True}


# --- Pi screen (mpv on the TV via the Pi's HDMI) + livestream ---
@app.get("/api/screen/status")
async def screen_status():
    try:
        r = await client.get(f"{SCREEN_URL}/status")
        return r.json()
    except httpx.RequestError as exc:
        return JSONResponse(status_code=502, content={"error": f"screen player unreachable: {exc}"})


@app.post("/api/screen/play")
async def screen_play(body: dict):
    """Play any URL on the Pi's HDMI. body: {url, headers?, audio_only?}."""
    global _active_input
    r = await client.post(f"{SCREEN_URL}/play", json=body)
    _active_input = "pi"
    return Response(content=r.content, status_code=r.status_code, media_type="application/json")


@app.post("/api/screen/control")
async def screen_control(body: dict):
    """Transport control for the Pi's mpv playback. body: {action, ...}.
    action = pause (toggle) | seek (relative, secs) | chapter (n) | audio |
    volume ({level} absolute 0-130, or {step} relative) | mute (toggle)."""
    try:
        r = await client.post(f"{SCREEN_URL}/control", json=body)
    except httpx.RequestError as exc:
        return JSONResponse(status_code=502, content={"error": f"screen player unreachable: {exc}"})
    return Response(content=r.content, status_code=r.status_code, media_type="application/json")


@app.post("/api/screen/reclaim")
async def screen_reclaim():
    """Switch the TV back to the Pi's HDMI input (the dashboard). The Apple TV
    grabs the input when it wakes; this re-asserts the Pi as CEC active source."""
    try:
        r = await client.post(f"{SCREEN_URL}/tv/reclaim")
    except httpx.RequestError as exc:
        return JSONResponse(status_code=502, content={"error": f"screen player unreachable: {exc}"})
    return Response(content=r.content, status_code=r.status_code, media_type="application/json")


@app.post("/api/screen/stop")
async def screen_stop():
    global _active_input
    await client.post(f"{SCREEN_URL}/stop")
    _active_input = "pi"  # dashboard returns to the Pi's HDMI
    errors = [
        err for err in (
            await _post_required(f"{CEC_URL}/api/tv/on", "TV power"),
            await _post_required(f"{CEC_URL}/api/source/active", "Pi input select"),
        )
        if err
    ]
    if errors:
        return JSONResponse(status_code=502, content={"ok": False, "error": "; ".join(errors)})
    return {"ok": True}


def _wake_tv_to_pi() -> asyncio.Task:
    """Fire-and-forget: wake the TV and claim the Pi input (cec source/active
    is the full register→image-view-on→active-source sequence). Kicked off
    FIRST when starting Pi playback so the panel's multi-second warm-up runs
    concurrently with mpv startup — otherwise the TV wakes late, dwells on
    whatever input it last showed, and only then jumps to the stream."""
    async def run():
        try:
            await client.post(f"{CEC_URL}/api/source/active")
        except httpx.RequestError:
            pass
    return asyncio.create_task(run())


_jetstream_variant: dict = {"at": 0.0, "url": None}
JETSTREAM_VARIANT_TTL = 300


async def _jetstream_play_url() -> str:
    """Resolve JETSTREAM_URL to a single rendition playlist.

    Handing a player the master playlist is slow and fragile: ffmpeg probes
    every rendition before it starts (measured 6.7s vs 0.68s for a direct
    variant), and that negotiation can outlast jetstream's 24-second segment
    window — the first segment it asks for is already deleted, mpv fails to
    open the stream, and the retry costs the better part of a minute before
    a picture appears. Picking the highest-bandwidth rendition ourselves is
    one cheap request. Falls back to the configured URL on any problem, so a
    jetstream change can never leave us with nothing to play."""
    base = JETSTREAM_URL.split("?", 1)[0]
    if time.time() - _jetstream_variant["at"] < JETSTREAM_VARIANT_TTL and _jetstream_variant["url"]:
        return _jetstream_variant["url"]
    try:
        r = await client.get(base, headers=_jetstream_headers(), timeout=5.0)
        r.raise_for_status()
        best, best_bw = None, -1
        lines = r.text.splitlines()
        for i, line in enumerate(lines):
            if not line.startswith("#EXT-X-STREAM-INF"):
                continue
            m = re.search(r"BANDWIDTH=(\d+)", line)
            nxt = next((v.strip() for v in lines[i + 1:] if v.strip()
                        and not v.startswith("#")), None)
            if m and nxt and int(m.group(1)) > best_bw:
                best, best_bw = nxt, int(m.group(1))
        if best:
            url = str(httpx.URL(base).join(best))
            _jetstream_variant.update(at=time.time(), url=url)
            return url
    except (httpx.HTTPError, ValueError):
        pass
    return base                      # not a master playlist, or jetstream is down


@app.post("/api/screen/livestream")
async def screen_livestream():
    """Play the jetstream broadcast on the Pi's HDMI (native HDR10, works today).
    The /hls path authenticates via the `lt` cookie, so use the base URL and drop
    any `?t=` (which only works on jetstream's viewer routes, not /hls)."""
    global _active_input
    if not JETSTREAM_URL:
        raise HTTPException(status_code=503, detail="JETSTREAM_URL not configured")
    url = await _jetstream_play_url()
    wake = _wake_tv_to_pi()          # TV starts warming up immediately
    _active_input = "pi"
    # Title lookup runs concurrently and gets a short budget — a slow homelab
    # answer must not delay the stream actually starting.
    np_task = asyncio.create_task(_jetstream_now_playing())
    try:
        now_playing = await asyncio.wait_for(np_task, 2.5) or {"title": "Jetstream livestream"}
    except (asyncio.TimeoutError, asyncio.CancelledError):
        now_playing = {"title": "Jetstream livestream"}
    title = now_playing["title"]
    await client.post(f"{SCREEN_URL}/play",
                      json={"url": url, "headers": _jetstream_headers(),
                            "title": title, "subtitle": now_playing.get("subtitle"),
                            "source": "livestream"})
    await wake
    return {"ok": True, "playing": "jetstream", "target": "pi",
            "title": title, "subtitle": now_playing.get("subtitle")}


@app.post("/api/jetstream/start")
async def jetstream_start():
    """AirPlay the homelab jetstream broadcast to the Apple TV so its hardware
    decodes the HLS. The Apple TV fetches the URL itself, so any auth must be in
    the URL (jetstream takes `?t=<token>`), not a cookie."""
    if not JETSTREAM_URL:
        raise HTTPException(status_code=503, detail="JETSTREAM_URL not configured")
    global _active_input
    try:  # TV on via CEC; the AirPlay handoff makes the Apple TV the active source
        await client.post(f"{CEC_URL}/api/tv/on")
    except httpx.RequestError:
        pass
    await client.post(f"{ATV_URL}/api/play_url", json={"url": await _jetstream_play_url()})
    _active_input = "appletv"
    return {"ok": True, "playing": "jetstream", "target": "appletv"}


@app.post("/api/jetstream/stop")
async def jetstream_stop():
    """Stop the AirPlay stream on the Apple TV."""
    await client.post(f"{ATV_URL}/api/stream/stop")
    return {"ok": True}


@app.get("/api/jetstream/capabilities")
async def jetstream_capabilities():
    """What jetstream viewer-API features the hub knows how to drive.
    The UI polls this so the Skip button only renders when it's actually wired."""
    return {
        "configured": bool(JETSTREAM_URL),
        "skip": bool(JETSTREAM_SKIP_URL),
    }


@app.post("/api/jetstream/skip")
async def jetstream_skip():
    """Advance the jetstream queue, same way a regular viewer's Skip button does.
    The hub is just a same-LAN proxy: the viewer cookie (JETSTREAM_COOKIE) authenticates
    against the homelab jetstream, which moves to the next clip and feeds the new
    manifest down the HLS pipeline. mpv (or the Apple TV) picks it up automatically
    because both are already subscribed to the live playlist.
    """
    if not JETSTREAM_SKIP_URL:
        raise HTTPException(status_code=503, detail="JETSTREAM_SKIP_URL not configured")
    try:
        r = await client.post(JETSTREAM_SKIP_URL,
                              headers=_jetstream_headers(), timeout=10.0)
    except httpx.RequestError as exc:
        return JSONResponse(status_code=502,
                            content={"error": f"jetstream unreachable: {exc}"})
    return {"ok": r.is_success, "status": r.status_code}


# --- Media library (SMB share, played directly on the Pi) ---
@app.get("/api/media/list")
async def media_list(path: str = ""):
    r = await client.get(f"{SCREEN_URL}/media/list", params={"path": path})
    return Response(content=r.content, status_code=r.status_code, media_type="application/json")


@app.post("/api/media/play")
async def media_play(body: dict):
    global _active_input
    # Wake the TV before (not after) starting playback — worst case a bad path
    # wakes the TV to the dashboard, best case the panel is warm when mpv is.
    wake = _wake_tv_to_pi()
    _active_input = "pi"
    r = await client.post(f"{SCREEN_URL}/media/play", json={"path": body.get("path", "")})
    await wake
    return Response(content=r.content, status_code=r.status_code, media_type="application/json")


@app.post("/api/media/shuffle")
async def media_shuffle(body: dict | None = None):
    """Play a random file from the SMB library (optionally scoped to {path})."""
    global _active_input
    body = body or {}
    wake = _wake_tv_to_pi()
    _active_input = "pi"
    r = await client.post(f"{SCREEN_URL}/shuffle", json={"path": body.get("path", "")})
    await wake
    return Response(content=r.content, status_code=r.status_code, media_type="application/json")


@app.post("/api/media/subtitles")
async def media_subtitles():
    """Cycle the subtitle track on the current on-demand playback (off/1/2/…)."""
    r = await client.post(f"{SCREEN_URL}/sub/cycle")
    return Response(content=r.content, status_code=r.status_code, media_type="application/json")


@app.get("/api/media/resume")
async def media_resume():
    """Continue-watching list (mpv watch-later entries from the screen player)."""
    try:
        r = await client.get(f"{SCREEN_URL}/media/resume")
    except httpx.RequestError as exc:
        return JSONResponse(status_code=502, content={"error": f"screen player unreachable: {exc}"})
    return Response(content=r.content, status_code=r.status_code, media_type="application/json")


@app.post("/api/media/next")
async def media_next():
    """Play the next file beside the current one ("next episode")."""
    try:
        r = await client.post(f"{SCREEN_URL}/media/next")
    except httpx.RequestError as exc:
        return JSONResponse(status_code=502, content={"error": f"screen player unreachable: {exc}"})
    return Response(content=r.content, status_code=r.status_code, media_type="application/json")


@app.post("/api/media/prev")
async def media_prev():
    """Play the previous file beside the current one."""
    try:
        r = await client.post(f"{SCREEN_URL}/media/prev")
    except httpx.RequestError as exc:
        return JSONResponse(status_code=502, content={"error": f"screen player unreachable: {exc}"})
    return Response(content=r.content, status_code=r.status_code, media_type="application/json")


# --- sleep timer -------------------------------------------------------
_sleep: dict = {"task": None, "ends_at": None}


async def _sleep_fire():
    """Stop whatever is playing and put the room to bed. Talks to the screen
    player directly (the hub's own /api/screen/stop would wake the TV back up)."""
    for method, url in (
        ("post", f"{SCREEN_URL}/stop"),          # Pi mpv -> back to dashboard
        ("post", f"{ATV_URL}/api/stream/stop"),  # AirPlay stream, if any
        ("post", f"{ATV_URL}/api/power/off"),
        ("post", f"{CEC_URL}/api/tv/off"),
    ):
        try:
            await getattr(client, method)(url)
        except httpx.RequestError:
            pass


@app.get("/api/sleep-timer")
async def sleep_timer_status():
    if not _sleep["task"] or _sleep["task"].done():
        return {"active": False}
    return {"active": True, "ends_at": _sleep["ends_at"],
            "remaining": max(0, _sleep["ends_at"] - time.time())}


@app.post("/api/sleep-timer")
async def sleep_timer_set(body: dict):
    try:
        minutes = float(body.get("minutes", 0))
    except (TypeError, ValueError):
        minutes = 0
    if not 0 < minutes <= 24 * 60:
        return JSONResponse(status_code=422, content={"error": "minutes must be 1-1440"})
    if _sleep["task"]:
        _sleep["task"].cancel()
    _sleep["ends_at"] = time.time() + minutes * 60

    async def run():
        await asyncio.sleep(minutes * 60)
        await _sleep_fire()

    _sleep["task"] = asyncio.create_task(run())
    return {"ok": True, "minutes": minutes, "ends_at": _sleep["ends_at"]}


@app.delete("/api/sleep-timer")
async def sleep_timer_cancel():
    if _sleep["task"]:
        _sleep["task"].cancel()
        _sleep["task"] = None
    return {"ok": True}


# --- weather (server-side cache; the dashboard + phones hit this) -------
_weather_cache: dict[str, tuple[float, dict]] = {}
_geo_cache: dict = {"at": 0.0, "data": None}
WEATHER_TTL = int(os.environ.get("WEATHER_TTL", "600"))
DEFAULT_LAT = float(os.environ.get("WEATHER_LAT", "39.7392"))
DEFAULT_LON = float(os.environ.get("WEATHER_LON", "-104.9903"))


@app.get("/api/weather")
async def weather(lat: float | None = None, lon: float | None = None):
    """Open-meteo forecast, geolocated by IP when no coords given. One upstream
    call per 10 min for the whole house; overlay renders stay LAN-local."""
    city = ""
    if lat is None or lon is None:
        g = _geo_cache["data"]
        if not g or time.time() - _geo_cache["at"] > 86400:
            try:
                r = await client.get("http://ip-api.com/json/", timeout=5.0)
                g = r.json() if r.is_success else None
            except (httpx.RequestError, ValueError):
                g = None
            if g and g.get("lat"):
                _geo_cache.update(at=time.time(), data=g)
        g = g or {}
        lat = g.get("lat", DEFAULT_LAT)
        lon = g.get("lon", DEFAULT_LON)
        city = g.get("city", "")
    key = f"{round(lat, 2)},{round(lon, 2)}"
    hit = _weather_cache.get(key)
    if hit and time.time() - hit[0] < WEATHER_TTL:
        return hit[1]
    url = (f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
           f"&current=temperature_2m,weather_code"
           f"&daily=weather_code,temperature_2m_max,temperature_2m_min"
           f"&temperature_unit=fahrenheit&timezone=auto&forecast_days=7")
    try:
        r = await client.get(url, timeout=10.0)
        data = r.json()
    except (httpx.RequestError, ValueError):
        if hit:                      # upstream down -> serve stale over nothing
            return {**hit[1], "stale": True}
        return JSONResponse(status_code=502, content={"error": "weather unavailable"})
    payload = {"city": city, "lat": lat, "lon": lon, "data": data}
    _weather_cache[key] = (time.time(), payload)
    return payload


# --- health (one-glance service + system state) -------------------------
@app.get("/api/health")
async def health():
    async def probe(name: str, url: str, timeout: float = 5.0):
        try:
            r = await client.get(url, timeout=timeout)
            return name, (r.json() if r.is_success else {"error": f"HTTP {r.status_code}"}), r.is_success
        except (httpx.RequestError, ValueError) as exc:
            return name, {"error": str(exc)[:120]}, False

    async def probe_plex():
        try:
            secs = await asyncio.wait_for(plex_client.music_sections(), timeout=5.0)
            return "plex", {"sections": len(secs)}, True
        except Exception as exc:  # noqa: BLE001 - health must never throw
            return "plex", {"error": str(exc)[:120]}, False

    results = await asyncio.gather(
        probe("appletv", f"{ATV_URL}/api/status", 8.0),
        probe("cec", f"{CEC_URL}/api/status", 12.0),
        probe("screen", f"{SCREEN_URL}/status"),
        probe_plex(),
    )
    services = {}
    for name, data, ok in results:
        if name == "appletv":  # reachable+paired is the real health, not HTTP 200
            ok = ok and bool(data.get("reachable")) and bool(data.get("paired"))
        if name == "cec":
            ok = ok and bool(data.get("adapter"))
        services[name] = {"ok": ok, **data}

    system: dict = {}
    try:
        du = shutil.disk_usage("/")
        system["disk_used_pct"] = round(100 * du.used / du.total, 1)
        system["disk_free_gb"] = round(du.free / 1e9, 1)
    except OSError:
        pass
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as fh:
            system["cpu_temp_c"] = round(int(fh.read().strip()) / 1000, 1)
    except (OSError, ValueError):
        pass
    try:
        system["load_1m"] = round(os.getloadavg()[0], 2)
    except OSError:
        pass
    try:
        with open("/proc/uptime") as fh:
            system["uptime_days"] = round(float(fh.read().split()[0]) / 86400, 1)
    except (OSError, ValueError):
        pass
    return {"ok": all(s["ok"] for s in services.values()), "services": services,
            "system": system, "sleep_timer": bool(_sleep["task"] and not _sleep["task"].done())}


# --- homelab (Plex server box) status + Plex client-control playback ----
_homelab_cache: dict = {"at": 0.0, "data": None}
HOMELAB_TTL = 20
# System stats over SSH (key + known_hosts mounted read-only at /ssh).
# Unset HOMELAB_SSH to disable the probe entirely.
HOMELAB_SSH = os.environ.get("HOMELAB_SSH", "")
HOMELAB_SSH_KEY = os.environ.get("HOMELAB_SSH_KEY", "/ssh/id_ed25519")
HOMELAB_DISK = os.environ.get("HOMELAB_DISK", "/mnt/storage")  # media pool mount
_HOMELAB_STATS_CMD = (
    "cut -d' ' -f1 /proc/loadavg; nproc; "
    "free -m | awk '/^Mem:/{{print $3, $2}}'; "
    "(df -m {disk} 2>/dev/null || df -m /) | awk 'NR==2{{print $3, $2}}'; "
    "sort -nr /sys/class/thermal/thermal_zone*/temp 2>/dev/null | head -1"
)


async def _homelab_stats():
    """CPU/RAM/disk/temp from the homelab over SSH (one round-trip, ~0.4s,
    shares the endpoint's 20s cache). None when SSH isn't configured."""
    if not HOMELAB_SSH:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            "ssh", "-i", HOMELAB_SSH_KEY,
            "-o", "BatchMode=yes", "-o", "ConnectTimeout=3",
            "-o", f"UserKnownHostsFile={os.path.dirname(HOMELAB_SSH_KEY)}/known_hosts",
            HOMELAB_SSH, _HOMELAB_STATS_CMD.format(disk=HOMELAB_DISK),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=6.0)
        load_s, cores_s, mem_s, disk_s, temp_s = out.decode().splitlines()[:5]
        load, cores = float(load_s), int(cores_s)
        mem_used, mem_total = map(int, mem_s.split())
        disk_used, disk_total = map(int, disk_s.split())
        return {"ok": True,
                "load": load, "cores": cores,
                "cpu_pct": round(100 * load / cores),
                "mem_pct": round(100 * mem_used / mem_total),
                "disk_used_tb": round(disk_used / 1024 / 1024, 1),
                "disk_total_tb": round(disk_total / 1024 / 1024, 1),
                "disk_pct": round(100 * disk_used / disk_total),
                "temp_c": round(int(temp_s) / 1000)}
    except Exception:  # noqa: BLE001 - status must never throw
        return {"ok": False}
# share-relative library prefix -> (Plex section, item type) as mounted on the
# server (/data/movies, /data/tv). type 1 = movie, 4 = episode.
_PLEX_VIDEO_SECTIONS = {"movies": ("1", 1), "tv": ("2", 4)}
_plex_path_cache: dict = {"at": 0.0, "by_path": {}, "by_base": {}}
PLEX_ATV_BUNDLE = "com.plexapp.plex"


async def _plex_json(path: str, **params) -> dict:
    r = await client.get(
        f"{plexmod.PLEX_URL}{path}",
        params={**params, "X-Plex-Token": plexmod.PLEX_TOKEN},
        headers={"Accept": "application/json"},
        timeout=8.0,
    )
    r.raise_for_status()
    return r.json().get("MediaContainer", {})


@app.get("/api/homelab")
async def homelab():
    """One-glance homelab status: host latency, Plex server + active streams,
    and the jetstream live channel. Cached so the TV overlay (re-rendered every
    minute) and phones share one probe."""
    if _homelab_cache["data"] and time.time() - _homelab_cache["at"] < HOMELAB_TTL:
        return _homelab_cache["data"]
    plex_url = httpx.URL(plexmod.PLEX_URL)
    host, port = plex_url.host, plex_url.port or 32400

    async def tcp_latency():
        t0 = time.monotonic()
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=2.0)
            writer.close()
            return round((time.monotonic() - t0) * 1000, 1)
        except (OSError, asyncio.TimeoutError):
            return None

    async def plex_info():
        try:
            root = await _plex_json("/")
            streams = (await _plex_json("/status/sessions")).get("Metadata") or []
            return {
                "ok": True,
                "name": root.get("friendlyName"),
                "version": (root.get("version") or "").split("-")[0],
                "sessions": len(streams),
                "transcodes": sum(1 for m in streams if m.get("TranscodeSession")),
                "titles": [m.get("grandparentTitle") or m.get("title") for m in streams][:3],
            }
        except Exception as exc:  # noqa: BLE001 - status must never throw
            return {"ok": False, "error": str(exc)[:120]}

    async def live_info():
        if not JETSTREAM_TITLE_URL:
            return None
        try:
            r = await client.get(JETSTREAM_TITLE_URL, headers=_jetstream_headers(), timeout=5.0)
            d = r.json()
            return {"ok": True, "playing": bool(d.get("playing")),
                    "title": _clean_text(d.get("title")),
                    "position": d.get("position_seconds"),
                    "duration": d.get("duration_seconds")}
        except (httpx.RequestError, ValueError):
            return {"ok": False}

    latency, plex, live, stats = await asyncio.gather(
        tcp_latency(), plex_info(), live_info(), _homelab_stats())
    data = {"host": host, "up": latency is not None, "latency_ms": latency,
            "plex": plex, "live": live, "stats": stats}
    _homelab_cache.update(at=time.time(), data=data)
    return data


async def _plex_video_lookup(rel_path: str):
    """Map a share-relative library path (movies/…, tv/…) to its Plex item by
    exact Part-file match; basename match is the fallback for copies that live
    outside the indexed trees (e.g. _4k_archive). Returns metadata dict or None."""
    now = time.time()
    if now - _plex_path_cache["at"] > 600 or not _plex_path_cache["by_path"]:
        by_path: dict = {}
        by_base: dict = {}
        by_dir: dict = {}
        for section, item_type in _PLEX_VIDEO_SECTIONS.values():
            mc = await _plex_json(f"/library/sections/{section}/all", type=item_type)
            for m in mc.get("Metadata") or []:
                entry = {
                    "ratingKey": m.get("ratingKey"),
                    "title": m.get("title"),
                    "grandparentTitle": m.get("grandparentTitle"),
                    "year": m.get("year"),
                    "resolution": next((med.get("videoResolution")
                                        for med in m.get("Media") or []), None),
                }
                for med in m.get("Media") or []:
                    for p in med.get("Part") or []:
                        f = p.get("file") or ""
                        if f:
                            by_path[f] = entry
                            by_base[os.path.basename(f)] = entry
                            # movies only: "Title (Year)" folder name — lets an
                            # unindexed copy (e.g. _4k_archive) find the library's
                            # version of the same movie
                            if item_type == 1:
                                by_dir[os.path.basename(os.path.dirname(f))] = entry
        _plex_path_cache.update(at=now, by_path=by_path, by_base=by_base, by_dir=by_dir)
    return (_plex_path_cache["by_path"].get(f"/data/{rel_path}")
            or _plex_path_cache["by_base"].get(os.path.basename(rel_path))
            or _plex_path_cache.get("by_dir", {}).get(os.path.basename(os.path.dirname(rel_path))))


@app.post("/api/plex/play_on_atv")
async def plex_play_on_atv(body: dict):
    """Play a library file on the Apple TV through its Plex app (proper 4K
    HDR/DV hardware path; the Pi caps HDR at 1080p). Flow: resolve the Plex
    item by file path -> TV to ATV input -> launch Plex -> wait for the app to
    advertise as a player -> send playMedia straight to the client."""
    global _active_input
    rel = (body.get("path") or "").strip("/")
    if not rel:
        raise HTTPException(status_code=400, detail="path required")
    try:
        item = await _plex_video_lookup(rel)
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=f"Plex lookup failed: {exc}") from exc
    if not item:
        raise HTTPException(status_code=404, detail="file not in the Plex library")

    # TV over to the Apple TV while Plex spins up (all tolerant of CEC hiccups):
    # force the input with Set Stream Path and wake the ATV concurrently.
    await asyncio.gather(
        client.post(f"{SCREEN_URL}/tv/input", json={"n": ATV_HDMI_INPUT}),
        client.post(f"{ATV_URL}/api/power/on"),
        return_exceptions=True,
    )
    try:
        await client.post(f"{ATV_URL}/api/launch/{PLEX_ATV_BUNDLE}")
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Plex launch failed: {exc}") from exc
    _active_input = "appletv"

    target = None
    for _ in range(12):                      # ~24s for the app to start advertising
        try:
            clients = (await _plex_json("/clients")).get("Server") or []
        except (httpx.HTTPError, ValueError):
            clients = []
        if clients:
            target = clients[0]
            break
        await asyncio.sleep(2)
    if not target:
        raise HTTPException(status_code=504, detail=(
            "Plex app never advertised as a player. On the Apple TV: "
            "Plex app > Settings > Advertise as Player (enable once)."))

    plex_url = httpx.URL(plexmod.PLEX_URL)
    server_id = (await _plex_json("/")).get("machineIdentifier")
    try:
        r = await client.get(
            f"http://{target['address']}:{target['port']}/player/playback/playMedia",
            params={
                "key": f"/library/metadata/{item['ratingKey']}",
                "offset": 0,
                "machineIdentifier": server_id,
                "address": plex_url.host,
                "port": plex_url.port or 32400,
                "protocol": "http",
                "token": plexmod.PLEX_TOKEN,
                "commandID": 1,
                "type": "video",
            },
            headers={"X-Plex-Target-Client-Identifier": target.get("machineIdentifier", ""),
                     "X-Plex-Client-Identifier": "livingroom-hub"},
            timeout=10.0,
        )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"playMedia failed: {exc}") from exc
    if not r.is_success:
        raise HTTPException(status_code=502, detail=f"playMedia HTTP {r.status_code}")
    title = item.get("grandparentTitle") or item.get("title") or rel
    return {"ok": True, "title": title, "resolution": item.get("resolution"),
            "client": target.get("name")}


async def _cec_status_stale() -> None:
    """The screen player drives the TV with host-side cec-ctl, so the cec
    service's cached power state can't know about it. Never fatal."""
    try:
        await client.post(f"{CEC_URL}/api/status/invalidate", timeout=3.0)
    except httpx.RequestError:
        pass


# --- TV input switcher (CEC) ---
@app.get("/api/input/status")
async def input_status():
    """Which HDMI source we last drove the TV to (for highlighting the switcher)."""
    return {"active": _active_input}


@app.post("/api/input/appletv")
async def input_appletv():
    """Switch the TV to the Apple TV.

    Force the TV straight to the Apple TV's HDMI port with Set Stream Path
    (via the screen player's /tv/input, which also wakes the TV) while waking
    the Apple TV in parallel. The old release-and-wait-for-tvOS handoff took
    5-10s and stalled entirely if the Apple TV was already awake.
    """
    global _active_input
    errors = [
        err for err in await asyncio.gather(
            _post_required(f"{SCREEN_URL}/tv/input", "TV input switch",
                           json={"n": ATV_HDMI_INPUT}),
            _post_required(f"{ATV_URL}/api/power/on", "Apple TV wake"),
        )
        if err
    ]
    if errors:
        return JSONResponse(status_code=502, content={"ok": False, "error": "; ".join(errors)})
    _active_input = "appletv"
    await _cec_status_stale()
    return {"ok": True, "input": "appletv"}


@app.post("/api/input/pi")
async def input_pi():
    """Switch the TV to the Pi's HDMI input (the dashboard).

    The CEC service's bare `as` (source/active) does not switch this TV. The
    screen player's /tv/reclaim runs the sequence that does (cec-ctl: register as
    a playback device → image-view-on → active-source with the Pi's real physical
    address). It also wakes the TV, so no separate tv/on is needed.
    """
    global _active_input
    err = await _post_required(f"{SCREEN_URL}/tv/reclaim", "Pi input select")
    if err:
        return JSONResponse(status_code=502, content={"ok": False, "error": err})
    _active_input = "pi"
    await _cec_status_stale()
    return {"ok": True, "input": "pi"}


@app.post("/api/apple-music")
async def apple_music():
    """One-tap: wake the Apple TV and start Apple Music (same sequence as the
    appletv_music alarm). CEC turns the TV on → power_on resumes the last session
    → launch Music → play (idempotent, always ends Playing)."""
    global _active_input
    await asyncio.gather(                           # TV to the ATV input + wake, together
        client.post(f"{SCREEN_URL}/tv/input", json={"n": ATV_HDMI_INPUT}),
        client.post(f"{ATV_URL}/api/power/on"),     # wake + resume last playback
        return_exceptions=True,
    )
    await asyncio.sleep(4)                          # let the TV + ATV wake
    await client.post(f"{ATV_URL}/api/launch/com.apple.TVMusic")
    await asyncio.sleep(2)
    await client.post(f"{ATV_URL}/api/command/play")
    _active_input = "appletv"
    return {"ok": True, "playing": "apple-music"}


@app.post("/api/tv/input/{n}")
async def tv_input(n: int):
    """Switch the TV to a raw HDMI input (CEC Set Stream Path via the screen
    player, which runs cec-ctl on the host)."""
    try:
        r = await client.post(f"{SCREEN_URL}/tv/input", json={"n": n})
    except httpx.RequestError as exc:
        return JSONResponse(status_code=502, content={"error": f"screen player unreachable: {exc}"})
    return Response(content=r.content, status_code=r.status_code, media_type="application/json")


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/dashboard")
async def dashboard():
    """Idle screen shown on the Pi's HDMI (clock / weather / now-playing)."""
    return FileResponse(STATIC / "dashboard.html")


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")
