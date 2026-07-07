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
import os
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


def _first_text(data, keys: tuple[str, ...]):
    if isinstance(data, str):
        return data.strip() or None
    if not isinstance(data, dict):
        return None
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _first_title(data):
    if isinstance(data, str):
        return data.strip() or None
    if not isinstance(data, dict):
        return None
    for key in ("title", "name", "media", "file", "now_playing", "nowPlaying"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
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
_plex_queue: asyncio.Task | None = None
# Which HDMI source we last drove the TV to. The Pi's HDMI shows either mpv or
# the idle dashboard, so "pi" is the resting state; AirPlay/handoff flips it to
# "appletv". Lets the UI highlight the active input (CEC can't report it here).
_active_input: str = "pi"


@asynccontextmanager
async def lifespan(app: FastAPI):
    global client, scheduler, plex_client
    client = httpx.AsyncClient(timeout=30.0)
    plex_client = plexmod.PlexClient(client=client)
    scheduler = alarms.AlarmScheduler(
        client, ATV_URL, CEC_URL,
        os.environ.get("HUB_SELF_URL", "http://localhost:8080"))
    scheduler.start()
    yield
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
    scene; the engine reads the JSON live on each /run so edits don't need a rebuild."""
    return {name: meta.get("label", name)
            for name, meta in scenesmod.load_scenes().items()}


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


async def _play_queue(tracks: list[dict], volume=None):
    for t in tracks:
        if not t.get("url"):
            continue
        await client.post(f"{ATV_URL}/api/stream", json={"url": t["url"], "volume": volume})
        volume = None  # only set volume on the first track
        await asyncio.sleep(max(1, (t.get("duration") or 0) / 1000))


@app.post("/api/plex/play/playlist")
async def plex_play_playlist(body: dict):
    global _plex_queue
    tracks = await plex_client.playlist_tracks(body["ratingKey"])
    if _plex_queue:
        _plex_queue.cancel()
    _plex_queue = asyncio.create_task(_play_queue(tracks, body.get("volume")))
    return {"queued": len(tracks)}


@app.post("/api/plex/stop")
async def plex_stop():
    global _plex_queue
    if _plex_queue:
        _plex_queue.cancel()
        _plex_queue = None
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
    """Transport control for the Pi's mpv playback. body: {action, secs?}.
    action = pause (toggle) | seek (relative, secs). Only works for on-demand
    media (live HLS has no IPC socket)."""
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


@app.post("/api/screen/livestream")
async def screen_livestream():
    """Play the jetstream broadcast on the Pi's HDMI (native HDR10, works today).
    The /hls path authenticates via the `lt` cookie, so use the base URL and drop
    any `?t=` (which only works on jetstream's viewer routes, not /hls)."""
    global _active_input
    if not JETSTREAM_URL:
        raise HTTPException(status_code=503, detail="JETSTREAM_URL not configured")
    url = JETSTREAM_URL.split("?", 1)[0]
    now_playing = await _jetstream_now_playing() or {"title": "Jetstream livestream"}
    title = now_playing["title"]
    await client.post(f"{SCREEN_URL}/play",
                      json={"url": url, "headers": _jetstream_headers(),
                            "title": title, "subtitle": now_playing.get("subtitle"),
                            "source": "livestream"})
    _active_input = "pi"
    try:  # switch the TV to the Pi's HDMI input
        await client.post(f"{CEC_URL}/api/tv/on")
        await client.post(f"{CEC_URL}/api/source/active")
    except httpx.RequestError:
        pass
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
    await client.post(f"{ATV_URL}/api/play_url", json={"url": JETSTREAM_URL})
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
    r = await client.post(f"{SCREEN_URL}/media/play", json={"path": body.get("path", "")})
    if r.is_success:
        _active_input = "pi"
        try:  # switch the TV to the Pi's HDMI input
            await client.post(f"{CEC_URL}/api/tv/on")
            await client.post(f"{CEC_URL}/api/source/active")
        except httpx.RequestError:
            pass
    return Response(content=r.content, status_code=r.status_code, media_type="application/json")


@app.post("/api/media/shuffle")
async def media_shuffle(body: dict | None = None):
    """Play a random file from the SMB library (optionally scoped to {path})."""
    global _active_input
    body = body or {}
    r = await client.post(f"{SCREEN_URL}/shuffle", json={"path": body.get("path", "")})
    if r.is_success:
        _active_input = "pi"
        try:  # switch the TV to the Pi's HDMI input
            await client.post(f"{CEC_URL}/api/tv/on")
            await client.post(f"{CEC_URL}/api/source/active")
        except httpx.RequestError:
            pass
    return Response(content=r.content, status_code=r.status_code, media_type="application/json")


@app.post("/api/media/subtitles")
async def media_subtitles():
    """Cycle the subtitle track on the current on-demand playback (off/1/2/…)."""
    r = await client.post(f"{SCREEN_URL}/sub/cycle")
    return Response(content=r.content, status_code=r.status_code, media_type="application/json")


# --- TV input switcher (CEC) ---
@app.get("/api/input/status")
async def input_status():
    """Which HDMI source we last drove the TV to (for highlighting the switcher)."""
    return {"active": _active_input}


@app.post("/api/input/appletv")
async def input_appletv():
    """Switch the TV to the Apple TV.

    The Pi can explicitly make itself active over CEC, but cannot directly make
    the Apple TV active. Release the Pi's active-source claim first, then wake
    the Apple TV so its HDMI-CEC handoff has a clear path to take over.
    """
    global _active_input
    errors = [
        err for err in (
            await _post_required(f"{CEC_URL}/api/tv/on", "TV power"),
            await _post_required(f"{CEC_URL}/api/source/release", "Pi input release"),
            await _post_required(f"{ATV_URL}/api/power/on", "Apple TV wake"),
        )
        if err
    ]
    if errors:
        return JSONResponse(status_code=502, content={"ok": False, "error": "; ".join(errors)})
    _active_input = "appletv"
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
    return {"ok": True, "input": "pi"}


@app.post("/api/apple-music")
async def apple_music():
    """One-tap: wake the Apple TV and start Apple Music (same sequence as the
    appletv_music alarm). CEC turns the TV on → power_on resumes the last session
    → launch Music → play (idempotent, always ends Playing)."""
    global _active_input
    await client.post(f"{CEC_URL}/api/tv/on")
    await client.post(f"{ATV_URL}/api/power/on")   # wake + resume last playback
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
