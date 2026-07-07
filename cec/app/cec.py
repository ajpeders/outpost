"""Thin async wrapper around libCEC's `cec-client`.

Every call spawns a one-shot `cec-client -s` that sends a line or two on the CEC
bus and exits. That's plenty for power/volume/source control; a long-lived
listener (for inbound remote/event handling) is a later phase.

TV is CEC logical address 0. The Pi's own source is addressed as "self".
"""
from __future__ import annotations

import asyncio
import os
import re
import time

TV = "0"  # CEC logical address of the TV

# cec-client cold-start is slow (~10s), so cache the status the UI polls.
_STATUS_TTL = 12.0
_status_cache: dict = {"at": 0.0, "data": None}


class CECError(Exception):
    """Surfaced to the API as a 503 (adapter missing, bus timeout, …)."""


# /dev/cec0 can only be opened by ONE cec-client at a time — serialize all
# invocations so concurrent requests can't collide (a collision left a stuck
# process holding the bus, wedging CEC entirely).
_LOCK = asyncio.Lock()


async def _cec(*commands: str, timeout: float = 15.0) -> str:
    """Feed one or more commands to `cec-client -s` and return its output."""
    args = ["cec-client", "-s", "-d", "1"]
    adapter = os.environ.get("CEC_ADAPTER")  # e.g. /dev/cec0 to force it
    if adapter:
        args.append(adapter)

    async with _LOCK:
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError as exc:  # cec-client not installed
            raise CECError("cec-client not found in container") from exc

        payload = ("\n".join(commands) + "\n").encode()
        try:
            out, _ = await asyncio.wait_for(proc.communicate(payload), timeout)
        except asyncio.TimeoutError as exc:
            proc.kill()
            await proc.wait()  # reap so it actually releases /dev/cec0
            raise CECError("cec-client timed out (no CEC adapter / bus?)") from exc

    text = out.decode(errors="replace")
    if "no serial port" in text.lower() or "could not open a connection" in text.lower():
        raise CECError("no CEC adapter — is /dev/cec0 passed into the container?")
    return text


# --- actions -----------------------------------------------------------
async def tv_on() -> None:
    await _cec(f"on {TV}")


async def tv_off() -> None:
    await _cec(f"standby {TV}")


async def volume(direction: str) -> None:
    verb = {"up": "volup", "down": "voldown", "mute": "mute"}.get(direction)
    if verb is None:
        raise ValueError("direction must be up|down|mute")
    await _cec(verb)


async def make_active_source() -> None:
    """Switch the TV to the Pi's HDMI input (announce as active source)."""
    await _cec("as")


async def release_source() -> None:
    """Hand the TV back (inactive source)."""
    await _cec("is")


# --- status ------------------------------------------------------------
async def tv_power() -> str:
    out = await _cec(f"pow {TV}")
    m = re.search(r"power status:\s*(\S+)", out)
    return m.group(1) if m else "unknown"


async def status() -> dict:
    """Cheap TV power probe for the UI — a single `pow` call, cached briefly.
    (Avoids the old scan+pow = two slow cec-client spawns per poll.)"""
    now = time.monotonic()
    cached = _status_cache["data"]
    if cached is not None and now - _status_cache["at"] < _STATUS_TTL:
        return cached

    info: dict = {"adapter": False, "tv_power": None}
    try:
        out = await _cec(f"pow {TV}")
        info["adapter"] = True
        m = re.search(r"power status:\s*(\S+)", out)
        info["tv_power"] = m.group(1) if m else "unknown"
    except CECError as exc:
        info["error"] = str(exc)

    _status_cache["at"] = time.monotonic()
    _status_cache["data"] = info
    return info
