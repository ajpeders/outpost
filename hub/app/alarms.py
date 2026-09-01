"""Music alarms: persisted schedule + a lightweight tick scheduler.

An alarm fires at HH:MM on selected weekdays and starts a chosen source:
  - "appletv_music" : wake the Apple TV + open Apple Music and press play  (the default)
  - "livestream"    : jetstream broadcast on the Pi's HDMI
  - "file"          : a specific file from the SMB library on the Pi
  - "shuffle"       : a random movie/show from the SMB library on the Pi
  - "airplay"       : AirPlay-stream a URL to the Apple TV        (needs airplay/raop paired)

Apple-TV sources are driven directly (CEC + atv); Pi sources go through the hub's
own endpoints (localhost) so they reuse the input-handoff + active-input logic.

No external scheduler dependency — a 20s tick checks the wall clock (container
local time; set TZ on the hub) and fires once per matching minute.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
from zoneinfo import ZoneInfo

_LOGGER = logging.getLogger("hub.alarms")
ALARMS_FILE = os.environ.get("ALARMS_FILE", "/data/alarms.json")
# Alarm times are wall-clock in this zone (tzdata pip pkg supplies the data).
_TZ = ZoneInfo(os.environ.get("TZ") or "UTC")

# app bundle id for the tvOS Music app (Apple Music)
APPLE_MUSIC_BUNDLE = os.environ.get("APPLE_MUSIC_BUNDLE", "com.apple.TVMusic")

FIELDS = ("label", "time", "days", "enabled", "source", "method",
          "app", "url", "path", "volume")


def _source_of(a: dict) -> str:
    """Resolve an alarm's source, mapping the legacy method field for old alarms."""
    src = a.get("source")
    if src:
        return src
    return "airplay" if a.get("method") == "airplay" else "appletv_music"


# --- persistence -------------------------------------------------------
def load_alarms() -> list[dict]:
    try:
        with open(ALARMS_FILE) as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError):
        return []


def save_alarms(alarms: list[dict]) -> None:
    os.makedirs(os.path.dirname(ALARMS_FILE), exist_ok=True)
    tmp = ALARMS_FILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(alarms, fh, indent=2)
    os.replace(tmp, ALARMS_FILE)


def _next_id(alarms: list[dict]) -> str:
    used = [int(a["id"]) for a in alarms if str(a.get("id", "")).isdigit()]
    return str((max(used) if used else 0) + 1)


# --- CRUD --------------------------------------------------------------
def create_alarm(data: dict) -> dict:
    alarms = load_alarms()
    alarm = {
        "id": _next_id(alarms),
        "label": data.get("label", "Alarm"),
        "time": data["time"],                    # "HH:MM" (24h)
        "days": data.get("days", []),            # [] = every day; 0=Mon..6=Sun
        "enabled": data.get("enabled", True),
        # appletv_music | livestream | file | shuffle | airplay
        "source": _source_of(data),
        "app": data.get("app", ""),              # bundle id override (appletv_music)
        "url": data.get("url", ""),              # stream URL (airplay)
        "path": data.get("path", ""),            # library path (file, or shuffle scope)
        "volume": data.get("volume"),            # 0-100 or None (Apple TV volume)
    }
    alarms.append(alarm)
    save_alarms(alarms)
    return alarm


def update_alarm(alarm_id: str, data: dict) -> dict | None:
    alarms = load_alarms()
    for a in alarms:
        if a["id"] == alarm_id:
            for k in FIELDS:
                if k in data:
                    a[k] = data[k]
            save_alarms(alarms)
            return a
    return None


def delete_alarm(alarm_id: str) -> bool:
    alarms = load_alarms()
    kept = [a for a in alarms if a["id"] != alarm_id]
    save_alarms(kept)
    return len(kept) != len(alarms)


def toggle_alarm(alarm_id: str) -> dict | None:
    alarms = load_alarms()
    for a in alarms:
        if a["id"] == alarm_id:
            a["enabled"] = not a.get("enabled", True)
            save_alarms(alarms)
            return a
    return None


def get_alarm(alarm_id: str) -> dict | None:
    return next((a for a in load_alarms() if a["id"] == alarm_id), None)


# --- scheduler ---------------------------------------------------------
class AlarmScheduler:
    def __init__(self, client, atv_url: str, cec_url: str,
                 hub_url: str = "http://localhost:8080") -> None:
        self.client = client
        self.base = {"atv": atv_url.rstrip("/"), "cec": cec_url.rstrip("/"),
                     "hub": hub_url.rstrip("/")}  # hub = self, for Pi-side sources
        self._task: asyncio.Task | None = None
        self._fired: dict[str, str] = {}  # alarm id -> "YYYYMMDDHHMM" it last fired

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self._check()
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("alarm check failed: %s", exc)
            await asyncio.sleep(20)

    async def _check(self) -> None:
        now = datetime.datetime.now(_TZ)
        minute_key = now.strftime("%Y%m%d%H%M")
        hhmm = now.strftime("%H:%M")
        weekday = now.weekday()  # 0=Mon .. 6=Sun
        for a in load_alarms():
            if not a.get("enabled"):
                continue
            if a.get("time") != hhmm:
                continue
            days = a.get("days") or []
            if days and weekday not in days:
                continue
            if self._fired.get(a["id"]) == minute_key:
                continue
            self._fired[a["id"]] = minute_key
            _LOGGER.info("firing alarm %s (%s)", a["id"], a.get("label"))
            asyncio.create_task(self.fire(a))

    async def _call(self, svc: str, path: str, body: dict | None = None):
        try:
            return await self.client.post(self.base[svc] + path, json=body)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("alarm step %s%s failed: %s", svc, path, exc)
            return None

    async def fire(self, a: dict) -> dict:
        """Run the alarm by source. Pi sources (livestream/file/shuffle) go through
        the hub's own endpoints so they reuse the CEC input-handoff; Apple-TV sources
        wake the ATV (power.turn_on resumes the last music session) and start music."""
        src = _source_of(a)

        if src == "livestream":
            await self._call("hub", "/api/screen/livestream")
        elif src == "file":
            await self._call("hub", "/api/media/play", {"path": a.get("path", "")})
        elif src == "shuffle":
            await self._call("hub", "/api/media/shuffle", {"path": a.get("path", "")})
        elif src == "airplay":
            await self._call("cec", "/api/tv/on")
            await self._call("atv", "/api/power/on")
            await asyncio.sleep(4)
            await self._call("atv", "/api/stream",
                             {"url": a.get("url"), "volume": a.get("volume")})
        else:  # appletv_music (and legacy "app")
            await self._call("cec", "/api/tv/on")
            await self._call("atv", "/api/power/on")  # wake + resume last playback
            await asyncio.sleep(4)  # let the TV + ATV wake before playing
            await self._call("atv", "/api/launch/" + (a.get("app") or APPLE_MUSIC_BUNDLE))
            await asyncio.sleep(2)
            # Use "play" (idempotent), NOT "play_pause": power.turn_on above often
            # resumes playback, and a toggle would then PAUSE it — the alarm would
            # look like it did nothing. "play" always ends up Playing.
            await self._call("atv", "/api/command/play")
            if a.get("volume") is not None:
                await self._call("atv", "/api/volume", {"level": a["volume"]})
        return {"fired": a["id"]}
