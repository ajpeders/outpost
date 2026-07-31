"""FastAPI control surface for a single Apple TV.

Endpoints
  GET  /healthz              — liveness (always 200 once the app is up)
  GET  /api/status           — reachability + pairing probe (never errors)
  GET  /api/state            — now-playing + power state
  GET  /api/apps             — installed apps [{name, identifier}]
  POST /api/command/{name}   — remote button (up/down/select/menu/play_pause/…)
  POST /api/launch/{bundle}  — launch app by bundle id
  POST /api/power/{on|off}   — turn the screen on/off
  GET  /                     — minimal web remote
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response

from .atv import ATVError, ATVManager, REMOTE_COMMANDS

ADDRESS = os.environ.get("ATV_ADDRESS", "")
NAME = os.environ.get("ATV_NAME", "Apple TV")
STORAGE = os.environ.get("ATV_STORAGE", "/data/pyatv.conf")

manager: ATVManager


@asynccontextmanager
async def lifespan(app: FastAPI):
    global manager
    if not ADDRESS:
        raise RuntimeError("ATV_ADDRESS is required")
    manager = ATVManager(ADDRESS, NAME, STORAGE)
    yield
    await manager.close()


app = FastAPI(title="appletv", lifespan=lifespan)


@app.exception_handler(ATVError)
async def _atv_error(_req, exc: ATVError):
    return JSONResponse(status_code=503, content={"error": str(exc)})


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/api/status")
async def status():
    return await manager.status()


@app.post("/api/pair/begin")
async def pair_begin():
    """Kick off Companion pairing; the Apple TV displays a PIN."""
    return await manager.pair_begin("companion")


@app.post("/api/pair/{protocol}/begin")
async def pair_begin_proto(protocol: str):
    """Pair a specific protocol: companion | airplay | raop (last two = audio)."""
    return await manager.pair_begin(protocol)


@app.post("/api/pair/finish")
async def pair_finish(body: dict):
    pin = str(body.get("pin", "")).strip()
    if not pin:
        raise HTTPException(status_code=422, detail="missing pin")
    return await manager.pair_finish(pin)


@app.post("/api/pair/cancel")
async def pair_cancel():
    await manager.pair_cancel()
    return {"ok": True}


@app.get("/api/state")
async def state():
    return await manager.now_playing()


@app.get("/api/artwork")
async def artwork():
    """Now-playing artwork as raw image bytes (404 when nothing has art)."""
    art = await manager.artwork()
    if art is None:
        raise HTTPException(status_code=404, detail="no artwork")
    return Response(content=art[0], media_type=art[1],
                    headers={"cache-control": "no-cache"})


@app.get("/api/apps")
async def apps():
    return await manager.app_list()


@app.get("/api/commands")
async def commands():
    return sorted(REMOTE_COMMANDS)


@app.post("/api/command/{name}")
async def command(name: str):
    try:
        await manager.command(name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"ok": True, "command": name}


@app.post("/api/launch/{bundle}")
async def launch(bundle: str, select: bool = True):
    # select=True (default) opens the app; pass ?select=false to only focus it.
    await manager.open_app(bundle, press_select=select)
    return {"ok": True, "launched": bundle}


@app.post("/api/open_url")
async def open_url(body: dict):
    """Hand a URL to tvOS (Companion open-URL). Deep links like
    plex://… let an app jump straight to content without any
    remote-control or client-advertisement plumbing."""
    url = (body.get("url") or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="url required")
    await manager.launch_app(url)
    return {"ok": True, "opened": url}


@app.post("/api/power/{action}")
async def power(action: str):
    if action == "on":
        await manager.turn_on()
    elif action == "off":
        await manager.turn_off()
    else:
        raise HTTPException(status_code=404, detail="use on|off")
    return {"ok": True, "power": action}


@app.post("/api/stream")
async def stream(body: dict):
    """AirPlay-stream an audio URL. body: {url, volume?}. Needs airplay/raop paired."""
    url = str(body.get("url", "")).strip()
    if not url:
        raise HTTPException(status_code=422, detail="missing url")
    vol = body.get("volume")
    return await manager.stream_url(url, float(vol) if vol is not None else None)


@app.post("/api/stream/stop")
async def stream_stop():
    await manager.stop_stream()
    return {"ok": True}


@app.post("/api/play_url")
async def play_url(body: dict):
    """AirPlay a VIDEO url (mp4/HLS) on the Apple TV. body: {url}."""
    url = str(body.get("url", "")).strip()
    if not url:
        raise HTTPException(status_code=422, detail="missing url")
    return await manager.play_url(url)


@app.post("/api/volume")
async def volume(body: dict):
    level = body.get("level")
    if level is None:
        raise HTTPException(status_code=422, detail="missing level (0-100)")
    await manager.set_volume(float(level))
    return {"ok": True, "level": level}


@app.get("/", response_class=HTMLResponse)
async def index():
    return INDEX_HTML


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>Apple TV Remote</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  body { margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
         background:#0b0b0d; color:#f2f2f7; display:flex; flex-direction:column;
         align-items:center; min-height:100vh; padding:24px 16px; gap:20px; }
  h1 { font-size:15px; font-weight:600; opacity:.6; margin:0; letter-spacing:.3px; }
  #now { text-align:center; min-height:40px; font-size:14px; opacity:.85; }
  #now .title { font-weight:600; font-size:16px; }
  #now .sub { opacity:.6; }
  .pad { display:grid; grid-template-columns:repeat(3,76px); grid-template-rows:repeat(3,76px);
         gap:0; background:#1c1c1e; border-radius:50%; overflow:hidden; }
  .pad button { border:none; background:transparent; color:#f2f2f7; font-size:20px;
                cursor:pointer; transition:background .1s; }
  .pad button:active { background:#3a3a3c; }
  .pad .select { grid-column:2; grid-row:2; background:#2c2c2e; border-radius:50%;
                 font-size:13px; font-weight:600; }
  .row { display:flex; gap:12px; flex-wrap:wrap; justify-content:center; max-width:280px; }
  .row button, .wide button { background:#1c1c1e; color:#f2f2f7; border:none;
      border-radius:14px; padding:14px 18px; font-size:15px; cursor:pointer; min-width:64px; }
  .row button:active { background:#3a3a3c; }
  .muted { opacity:.5; font-size:12px; }
</style>
</head>
<body>
  <h1 id="dev">Apple TV</h1>
  <div id="now"><span class="muted">loading…</span></div>

  <div class="pad">
    <span></span>
    <button data-cmd="up">▲</button>
    <span></span>
    <button data-cmd="left">◀</button>
    <button class="select" data-cmd="select">OK</button>
    <button data-cmd="right">▶</button>
    <span></span>
    <button data-cmd="down">▼</button>
    <span></span>
  </div>

  <div class="row">
    <button data-cmd="menu">Menu</button>
    <button data-cmd="home">◉</button>
    <button data-cmd="play_pause">⏯</button>
  </div>
  <div class="row">
    <button data-cmd="skip_backward">⏪</button>
    <button data-cmd="skip_forward">⏩</button>
    <button data-cmd="volume_down">Vol −</button>
    <button data-cmd="volume_up">Vol +</button>
  </div>

  <div id="status" class="muted"></div>

<script>
async function send(cmd) {
  const r = await fetch('/api/command/' + cmd, {method:'POST'});
  if (!r.ok) { const e = await r.json().catch(()=>({})); flash(e.error || ('error: '+cmd)); }
}
function flash(msg){ const s=document.getElementById('status'); s.textContent=msg;
  setTimeout(()=>{ if(s.textContent===msg) s.textContent=''; }, 2500); }
document.querySelectorAll('[data-cmd]').forEach(b =>
  b.addEventListener('click', () => send(b.dataset.cmd)));

async function refresh() {
  try {
    const st = await (await fetch('/api/status')).json();
    document.getElementById('dev').textContent = st.name || 'Apple TV';
    if (!st.reachable) { setNow('<span class="muted">asleep / unreachable</span>'); return; }
    if (!st.paired)    { setNow('<span class="muted">not paired — see README</span>'); return; }
    const s = await (await fetch('/api/state')).json();
    if (s.title) {
      const sub = s.subtitle || [s.artist, s.app].filter(Boolean).join(' · ');
      setNow('<div class="title">'+esc(s.title)+'</div><div class="sub">'+
             esc(sub)+'</div>');
    } else {
      setNow('<span class="muted">'+esc(s.app || s.device_state || 'idle')+'</span>');
    }
  } catch (e) { /* keep last */ }
}
function setNow(h){ document.getElementById('now').innerHTML = h; }
function esc(s){ return (s||'').replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
refresh(); setInterval(refresh, 3000);
</script>
</body>
</html>"""
