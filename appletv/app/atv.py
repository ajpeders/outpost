"""Thin async wrapper around pyatv.

Connects to a *known* Apple TV by IP using unicast scanning (so it works from
inside the Docker `web` network without host-mode mDNS). Credentials are read
from a pyatv FileStorage file that is populated once via `atvremote ... pair`.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import pyatv
from pyatv.const import Protocol
from pyatv.interface import DeviceListener
from pyatv.storage.file_storage import FileStorage

_LOGGER = logging.getLogger("appletv")


# --- tvOS 26 AirPlay-video workaround -------------------------------------
# The play command succeeds, but tvOS 26 answers pyatv's follow-up
# /playback-info poll with HTTP 500. pyatv's _wait_for_media_to_end only catches
# RuntimeError/ConnectionLostError, so the HttpError propagates, exits the
# timing_server context, and tears down the AirPlay session — killing the video.
# Patch the wait loop to tolerate the 500 and keep the session alive; we drive
# playback as a long-lived task and end it via stop_stream() instead.
def _patch_airplay_wait() -> None:
    try:
        from pyatv.protocols.airplay import player as _ap_player
    except Exception:  # noqa: BLE001
        return

    async def _tolerant_wait_for_media_to_end(self) -> None:  # noqa: ANN001
        while True:
            try:
                await self.rtsp.connection.get("/playback-info")
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - incl. HttpError 500 on tvOS 26
                pass
            await asyncio.sleep(3)

    _ap_player.AirPlayPlayer._wait_for_media_to_end = _tolerant_wait_for_media_to_end


_patch_airplay_wait()

# Remote-control verbs we expose verbatim; each maps to a coroutine on
# atv.remote_control of the same name.
REMOTE_COMMANDS = {
    "up", "down", "left", "right", "select", "menu", "home", "home_hold",
    "play", "pause", "play_pause", "stop", "next", "previous",
    "skip_forward", "skip_backward", "volume_up", "volume_down",
    "channel_up", "channel_down", "top_menu",
}


class ATVError(Exception):
    """Base error surfaced to the API as a friendly 503."""


class DeviceOffline(ATVError):
    """The Apple TV did not answer a unicast scan (asleep/unreachable)."""


class NotPaired(ATVError):
    """No stored credentials — run the pairing bootstrap first."""


class ATVManager(DeviceListener):
    """Owns a single, lazily-established, auto-reconnecting connection."""

    def __init__(self, address: str, name: str, storage_path: str) -> None:
        self.address = address
        self.name = name
        self.storage_path = storage_path
        self._storage: Optional[FileStorage] = None
        self._atv: Optional[pyatv.interface.AppleTV] = None
        self._lock = asyncio.Lock()
        self._pairing = None  # in-flight PairingHandler between begin() and pin()
        self._stream_task: Optional[asyncio.Task] = None  # active AirPlay stream

    # --- lifecycle -----------------------------------------------------
    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        return asyncio.get_running_loop()

    async def _storage_ready(self) -> FileStorage:
        if self._storage is None:
            storage = FileStorage(self.storage_path, self.loop)
            await storage.load()
            self._storage = storage
        return self._storage

    async def _connect(self) -> pyatv.interface.AppleTV:
        async with self._lock:
            if self._atv is not None:
                return self._atv

            storage = await self._storage_ready()
            confs = await pyatv.scan(
                self.loop, hosts=[self.address], storage=storage
            )
            if not confs:
                raise DeviceOffline(
                    f"{self.name} ({self.address}) did not respond — it may be "
                    f"asleep or on a different network."
                )
            conf = confs[0]

            companion = conf.get_service(Protocol.Companion)
            if companion is None or not companion.credentials:
                raise NotPaired(
                    "No Companion credentials stored. Run the pairing bootstrap "
                    "(see README) to authorize this host."
                )

            atv = await pyatv.connect(conf, self.loop, storage=storage)
            atv.listener = self
            self._atv = atv
            _LOGGER.info("connected to %s (%s)", self.name, self.address)
            return atv

    async def close(self) -> None:
        if self._atv is not None:
            self._atv.close()
            self._atv = None

    # --- pairing (PIN flow, per protocol) -----------------------------
    _PROTOCOLS = {
        "companion": Protocol.Companion,
        "airplay": Protocol.AirPlay,
        "raop": Protocol.RAOP,
    }

    async def pair_begin(self, protocol: str = "companion") -> dict[str, Any]:
        """Start pairing for a protocol — the Apple TV shows a 4-digit PIN.
        companion = remote/apps/power; airplay + raop = AirPlay audio streaming."""
        proto = self._PROTOCOLS.get(protocol.lower())
        if proto is None:
            raise ATVError(f"unknown protocol: {protocol}")
        await self.pair_cancel()
        await self.close()  # avoid contending for the protocol's port

        storage = await self._storage_ready()
        confs = await pyatv.scan(self.loop, hosts=[self.address], storage=storage)
        if not confs:
            raise DeviceOffline(f"{self.name} ({self.address}) did not respond.")

        pairing = await pyatv.pair(confs[0], proto, self.loop, storage=storage)
        await pairing.begin()
        self._pairing = pairing
        return {"pin_required": pairing.device_provides_pin, "protocol": protocol}

    async def pair_finish(self, pin: str) -> dict[str, Any]:
        """Submit the PIN shown on the TV and persist credentials on success."""
        if self._pairing is None:
            raise ATVError("no pairing in progress — start with pair_begin()")
        pairing = self._pairing
        pairing.pin(pin)
        await pairing.finish()
        paired = pairing.has_paired
        await pairing.close()
        self._pairing = None
        if not paired:
            raise ATVError("pairing failed — wrong PIN? try again")
        await (await self._storage_ready()).save()
        return {"paired": True}

    async def pair_cancel(self) -> None:
        if self._pairing is not None:
            try:
                await self._pairing.close()
            finally:
                self._pairing = None

    # DeviceListener callbacks — drop the handle so the next call reconnects.
    def connection_lost(self, exception: Exception) -> None:  # noqa: D401
        _LOGGER.warning("connection lost: %s", exception)
        self._atv = None

    def connection_closed(self) -> None:
        _LOGGER.info("connection closed")
        self._atv = None

    # --- status --------------------------------------------------------
    async def status(self) -> dict[str, Any]:
        """Cheap reachability/pairing probe that never raises."""
        info: dict[str, Any] = {
            "name": self.name,
            "address": self.address,
            "reachable": False,
            "paired": False,
            "connected": self._atv is not None,
        }
        try:
            storage = await self._storage_ready()
            confs = await pyatv.scan(self.loop, hosts=[self.address], storage=storage)
            if confs:
                info["reachable"] = True
                companion = confs[0].get_service(Protocol.Companion)
                info["paired"] = bool(companion and companion.credentials)
        except Exception as exc:  # noqa: BLE001 - status must never throw
            info["error"] = str(exc)
        return info

    # --- actions -------------------------------------------------------
    async def command(self, name: str) -> None:
        if name not in REMOTE_COMMANDS:
            raise ValueError(f"unknown command: {name}")
        atv = await self._connect()
        await getattr(atv.remote_control, name)()

    async def now_playing(self) -> dict[str, Any]:
        atv = await self._connect()
        playing = await atv.metadata.playing()
        # metadata.app / power_state raise NotSupportedError depending on tvOS
        # state — never let that 500 the whole now-playing view.
        try:
            app = atv.metadata.app
        except Exception:  # noqa: BLE001
            app = None
        try:
            power = atv.power.power_state.name
        except Exception:  # noqa: BLE001
            power = None
        return {
            "power": power,
            "app": app.name if app else None,
            "app_id": app.identifier if app else None,
            "device_state": playing.device_state.name,
            "media_type": playing.media_type.name,
            "title": playing.title,
            "artist": playing.artist,
            "album": playing.album,
            "subtitle": " · ".join(
                part for part in (
                    playing.artist,
                    app.name if app else None,
                    playing.album,
                )
                if part
            ) or None,
            "position": playing.position,
            "total_time": playing.total_time,
            "repeat": playing.repeat.name if playing.repeat else None,
            "shuffle": playing.shuffle.name if playing.shuffle else None,
        }

    async def app_list(self) -> list[dict[str, str]]:
        atv = await self._connect()
        # Companion's FetchLaunchableApplications is flaky on some tvOS builds
        # (times out / ProtocolError). Degrade to a friendly 503 instead of 500;
        # launching by known bundle id still works even when listing doesn't.
        try:
            apps = await atv.apps.app_list()
        except Exception as exc:  # noqa: BLE001
            raise ATVError(f"app list unavailable from this Apple TV ({exc})")
        return [{"name": a.name, "identifier": a.identifier} for a in apps]

    async def launch_app(self, bundle_id: str) -> None:
        atv = await self._connect()
        await atv.apps.launch_app(bundle_id)

    async def open_app(self, bundle_id: str, press_select: bool = True) -> None:
        """Open an app. On tvOS 26 Companion's launch only *focuses* the app on
        the home screen, so we follow with a select press to actually open it."""
        atv = await self._connect()
        await atv.apps.launch_app(bundle_id)
        if press_select:
            await asyncio.sleep(1.5)
            await atv.remote_control.select()

    async def turn_on(self) -> None:
        atv = await self._connect()
        await atv.power.turn_on()

    async def turn_off(self) -> None:
        atv = await self._connect()
        await atv.power.turn_off()

    # --- AirPlay audio streaming (for the music alarm) -----------------
    async def set_volume(self, level: float) -> None:
        """Set output volume 0-100 (uses the RAOP/AirPlay audio interface)."""
        atv = await self._connect()
        await atv.audio.set_volume(float(level))

    async def stream_url(self, url: str, volume: Optional[float] = None) -> dict[str, Any]:
        """Start AirPlay-streaming an audio file/stream URL. Returns immediately;
        streaming runs in the background until it ends or stop_stream() is called."""
        atv = await self._connect()
        await self.stop_stream()
        if volume is not None:
            try:
                await atv.audio.set_volume(float(volume))
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("set_volume failed: %s", exc)
        self._stream_task = asyncio.create_task(self._do_stream(atv, url))
        return {"streaming": url}

    async def _do_stream(self, atv, url: str) -> None:
        try:
            await atv.stream.stream_file(url)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("stream ended/failed: %s", exc)

    async def stop_stream(self) -> None:
        task, self._stream_task = self._stream_task, None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    # --- AirPlay VIDEO (movies/shows/livestream) ----------------------
    async def play_url(self, url: str) -> dict[str, Any]:
        """AirPlay a video URL (mp4/HLS) on the Apple TV. play_url holds the
        timing server open for the whole playback, so we run it as a long-lived
        task (reusing _stream_task) and return once the play command is out.
        Stop via stop_stream(). The Apple TV fetches the URL itself, so it must
        be reachable from the TV (auth, if any, embedded in the URL)."""
        atv = await self._connect()
        await self.stop_stream()
        self._stream_task = asyncio.create_task(self._do_play_url(atv, url))
        await asyncio.sleep(3)  # let the play command reach the Apple TV
        return {"playing": url}

    async def _do_play_url(self, atv, url: str) -> None:
        try:
            await atv.stream.play_url(url)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("play_url ended/failed: %s", exc)
