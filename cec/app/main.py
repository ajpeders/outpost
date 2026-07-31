"""FastAPI control surface for the TV over HDMI-CEC (runs on the Pi).

  GET  /healthz
  GET  /api/status              — adapter present? TV power? bus devices
  GET  /api/tv/power            — TV power status (on/standby/unknown)
  POST /api/tv/on               — power the TV on
  POST /api/tv/off              — TV to standby
  POST /api/tv/volume/{dir}     — up | down | mute
  POST /api/source/active       — switch TV to the Pi's HDMI input
  POST /api/source/release      — hand the input back
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from . import cec

app = FastAPI(title="livingroom-cec")


@app.exception_handler(cec.CECError)
async def _cec_error(_req, exc: cec.CECError):
    return JSONResponse(status_code=503, content={"error": str(exc)})


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/api/status")
async def status():
    return await cec.status()


@app.post("/api/status/invalidate")
async def status_invalidate():
    """Force the next /api/status to re-probe (the caller drove the TV by
    some other route, e.g. the screen player's host-side cec-ctl)."""
    cec.invalidate_status()
    return {"ok": True}


@app.get("/api/tv/power")
async def tv_power():
    return {"power": await cec.tv_power()}


@app.post("/api/tv/on")
async def tv_on():
    await cec.tv_on()
    return {"ok": True}


@app.post("/api/tv/off")
async def tv_off():
    await cec.tv_off()
    return {"ok": True}


@app.post("/api/tv/volume/{direction}")
async def tv_volume(direction: str):
    try:
        await cec.volume(direction)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"ok": True, "volume": direction}


@app.post("/api/source/active")
async def source_active():
    await cec.make_active_source()
    return {"ok": True}


@app.post("/api/source/release")
async def source_release():
    await cec.release_source()
    return {"ok": True}
