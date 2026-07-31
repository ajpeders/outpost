"""Thin async wrapper around the kernel CEC API via `cec-ctl` (v4l-utils).

Previously this shelled out to libCEC's `cec-client -s`, which cold-starts in
~5-10s per command — every button press paid that. `cec-ctl` talks straight to
the kernel /dev/cec0 device: register + send + reply is well under a second
(the bus reply itself is ~20ms).

It also fixes the reliability problem: the host side (screen player's
tv-reclaim / input switching) already uses cec-ctl and registers the Pi as a
CEC *playback device*. libCEC kept reconfiguring the shared adapter to
"unregistered", after which the host's active-source messages were ignored by
the TV. Now both sides assert the same playback-device config, so the adapter
state stays consistent no matter who talked last.

TV is CEC logical address 0. Volume goes to CEC_VOLUME_TARGET (default the TV;
set 5 for a soundbar/AVR that implements System Audio Control).
"""
from __future__ import annotations

import asyncio
import os
import re
import time

TV = "0"  # CEC logical address of the TV
DEV = os.environ.get("CEC_ADAPTER", "/dev/cec0")
VOLUME_TARGET = os.environ.get("CEC_VOLUME_TARGET", TV)

# TV power changes only when something on this Pi drives it (or the user picks
# up the TV remote), and every miss costs ~0.8s *and* re-registers the shared
# /dev/cec0 — which is what used to knock input switching out. The UI polls
# status every 10s, so a short TTL meant every poll hit the bus. Our own power
# commands publish the new state into the cache, so the UI still updates
# instantly; only a change made on the TV itself waits out the TTL.
_STATUS_TTL = float(os.environ.get("CEC_STATUS_TTL", "45"))
_status_cache: dict = {"at": 0.0, "data": None}


def _publish_power(state: str) -> None:
    """Record a power state we just caused, so the next status is free."""
    _status_cache["at"] = time.monotonic()
    _status_cache["data"] = {"adapter": True, "tv_power": state}
_phys_addr: str | None = None   # cached "x.y.z.w" (changes only on replug)


class CECError(Exception):
    """Surfaced to the API as a 503 (adapter missing, bus timeout, …)."""


# One CEC transaction at a time — concurrent senders on the same adapter can
# collide and wedge the bus.
_LOCK = asyncio.Lock()


async def _cec_ctl(*args: str, timeout: float = 8.0) -> str:
    """Run one cec-ctl invocation (skipping the shared lock is never worth it)."""
    cmd = ["cec-ctl", "-s", "-d", DEV, *args]
    async with _LOCK:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError as exc:
            raise CECError("cec-ctl not found in container (install v4l-utils)") from exc
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise CECError("cec-ctl timed out (CEC bus stuck?)") from exc

    text = out.decode(errors="replace")
    low = text.lower()
    if "cannot open" in low or "no such file" in low or "failed to open" in low:
        raise CECError(f"no CEC adapter — is {DEV} passed into the container?")
    return text


async def _register() -> None:
    """(Re)assert the Pi as a CEC playback device — idempotent and cheap.
    An unregistered initiator's messages are ignored by the TV."""
    await _cec_ctl("--playback")


async def _physical_address() -> str:
    """The Pi's CEC physical address (which TV HDMI port), e.g. '1.0.0.0'."""
    global _phys_addr
    if _phys_addr:
        return _phys_addr
    out = await _cec_ctl()
    m = re.search(r"Physical Address\s*:\s*([0-9a-fA-F]\.[0-9a-fA-F]\.[0-9a-fA-F]\.[0-9a-fA-F])", out)
    _phys_addr = m.group(1) if m else "1.0.0.0"
    return _phys_addr


# --- actions -----------------------------------------------------------
async def tv_on() -> None:
    await _cec_ctl("--playback", "--to", TV, "--image-view-on")
    _publish_power("on")


async def tv_off() -> None:
    await _cec_ctl("--playback", "--to", TV, "--standby")
    _publish_power("standby")


async def volume(direction: str) -> None:
    cmd = {"up": "volume-up", "down": "volume-down", "mute": "mute"}.get(direction)
    if cmd is None:
        raise ValueError("direction must be up|down|mute")
    await _cec_ctl("--playback", "--to", VOLUME_TARGET,
                   f"--user-control-pressed", f"ui-cmd={cmd}",
                   "--user-control-released")


async def make_active_source() -> None:
    """Switch the TV to the Pi's HDMI input (same sequence as tv-reclaim.sh:
    register → wake → active-source with our real physical address)."""
    pa = await _physical_address()
    await _register()
    await _cec_ctl("--to", TV, "--image-view-on")
    await _cec_ctl("--active-source", f"phys-addr={pa}")
    _publish_power("on")


def invalidate_status() -> None:
    """Drop the cached power state — for when something outside this service
    drove the TV (the screen player's input switch runs cec-ctl on the host)."""
    _status_cache["at"] = 0.0
    _status_cache["data"] = None


async def release_source() -> None:
    """Hand the TV back (inactive source)."""
    pa = await _physical_address()
    await _cec_ctl("--inactive-source", f"phys-addr={pa}")


# --- status ------------------------------------------------------------
def _parse_power(out: str) -> str:
    m = re.search(r"pwr-state:\s*([a-z-]+)", out)
    if not m:
        return "unknown"
    state = m.group(1)
    # normalize transitional states to what the UI already understands
    return {"to-on": "on", "to-standby": "standby"}.get(state, state)


async def tv_power() -> str:
    out = await _cec_ctl("--playback", "--to", TV, "--give-device-power-status")
    return _parse_power(out)


async def status() -> dict:
    """TV power probe for the UI — one fast kernel-CEC query, cached briefly."""
    now = time.monotonic()
    cached = _status_cache["data"]
    if cached is not None and now - _status_cache["at"] < _STATUS_TTL:
        return cached

    info: dict = {"adapter": False, "tv_power": None}
    try:
        out = await _cec_ctl("--playback", "--to", TV, "--give-device-power-status")
        info["adapter"] = True
        info["tv_power"] = _parse_power(out)
    except CECError as exc:
        info["error"] = str(exc)

    _status_cache["at"] = time.monotonic()
    _status_cache["data"] = info
    return info
