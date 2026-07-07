"""Plex music client for the living-room hub.

Read-only browse + stream-URL resolution against a self-hosted Plex Media
Server. Everything here is plain HTTP GETs with the account token; the only
"write" the hub ever does is POSTing a resolved URL to the appletv service.

The stream URL we hand to pyatv is the *raw Part file* form:

    {PLEX_URL}/library/parts/<id>/<ver>/file.<ext>?X-Plex-Token=<token>

Verified against the homelab server: HEAD returns ``Content-Type: audio/flac``
with ``Accept-Ranges: bytes`` and the real Content-Length, i.e. a plain,
range-able, un-DRM'd audio file — exactly what pyatv ``stream_file`` needs.
See PLEX_INTEGRATION.md for the captured evidence.

pyatv streams ONE file at a time, so a playlist/album is played by the hub
chaining tracks (resolve every Part up-front, then fire them in sequence,
pacing off each track's ``duration``). This module only *resolves*; the
sequencing lives in the hub (see the queue sketch in PLEX_INTEGRATION.md).

Config (env):
    PLEX_URL    e.g. http://192.168.0.176:32400   (reachable from the Pi)
    PLEX_TOKEN  X-Plex-Token for the account
"""
from __future__ import annotations

import os
from typing import Any, Optional
from urllib.parse import urlencode

import httpx

PLEX_URL = os.environ.get("PLEX_URL", "http://192.168.0.176:32400").rstrip("/")
PLEX_TOKEN = os.environ.get("PLEX_TOKEN", "")

# Plex library `type` codes we care about.
TYPE_ARTIST = 8
TYPE_ALBUM = 9
TYPE_TRACK = 10


class PlexError(RuntimeError):
    """Raised when Plex returns a non-2xx or an unusable payload."""


class PlexClient:
    """Thin async wrapper over the Plex HTTP API (music only).

    Pass an existing ``httpx.AsyncClient`` (the hub already owns one) or let it
    create its own. The token is injected into every request as a query param
    and JSON is requested via the Accept header.
    """

    def __init__(
        self,
        base_url: str = PLEX_URL,
        token: str = PLEX_TOKEN,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.base = base_url.rstrip("/")
        self.token = token
        self._client = client
        self._owns_client = client is None

    # --- plumbing ------------------------------------------------------
    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _get(self, path: str, params: Optional[dict] = None) -> dict:
        """GET a Plex endpoint, return the MediaContainer dict."""
        q = {"X-Plex-Token": self.token, **(params or {})}
        try:
            r = await self.client.get(
                f"{self.base}{path}",
                params=q,
                headers={"Accept": "application/json"},
            )
        except httpx.RequestError as exc:
            raise PlexError(f"plex unreachable: {exc}") from exc
        if r.status_code >= 400:
            raise PlexError(f"plex {path} -> {r.status_code}")
        try:
            return r.json().get("MediaContainer", {})
        except ValueError as exc:
            raise PlexError(f"plex {path} returned non-JSON") from exc

    def _token_url(self, key: str) -> str:
        """Absolute, token-carrying URL for a Part key or image key.

        The token is embedded in the query string because pyatv / the Apple TV
        fetch the file with no auth headers of their own.
        """
        sep = "&" if "?" in key else "?"
        return f"{self.base}{key}{sep}{urlencode({'X-Plex-Token': self.token})}"

    # --- browse --------------------------------------------------------
    async def music_sections(self) -> list[dict]:
        """All library sections of type 'artist' (music), simplified."""
        mc = await self._get("/library/sections")
        return [
            {"key": d["key"], "title": d.get("title"), "type": d.get("type")}
            for d in mc.get("Directory", [])
            if d.get("type") == "artist"
        ]

    async def playlists(self) -> list[dict]:
        """Audio playlists (playlistType=audio)."""
        mc = await self._get("/playlists", {"playlistType": "audio"})
        return [
            {
                "ratingKey": p.get("ratingKey"),
                "title": p.get("title"),
                "leafCount": p.get("leafCount"),
                "duration": p.get("duration"),
                "thumb": self._token_url(p["composite"]) if p.get("composite") else None,
            }
            for p in mc.get("Metadata", [])
        ]

    async def playlist_tracks(self, rating_key: str | int) -> list[dict]:
        """Every track in a playlist, each with a resolved stream URL.

        One GET returns the items with Media/Part inline, so this is the
        queue-builder the hub uses to chain a whole playlist.
        """
        mc = await self._get(f"/playlists/{rating_key}/items")
        return [self._track(m) for m in mc.get("Metadata", []) if m.get("type") == "track"]

    async def children(self, rating_key: str | int) -> dict:
        """Generic drill-down: artist -> albums, album -> tracks.

        Returns ``{"type": <child type>, "items": [...]}``. Track children come
        back with resolved stream URLs; container children (albums) don't.
        """
        mc = await self._get(f"/library/metadata/{rating_key}/children")
        items = mc.get("Metadata", [])
        child_type = items[0].get("type") if items else None
        if child_type == "track":
            return {"type": "track", "items": [self._track(m) for m in items]}
        return {
            "type": child_type,
            "items": [
                {
                    "ratingKey": m.get("ratingKey"),
                    "title": m.get("title"),
                    "type": m.get("type"),
                    "year": m.get("year"),
                    "thumb": self._token_url(m["thumb"]) if m.get("thumb") else None,
                }
                for m in items
            ],
        }

    async def search(self, query: str, limit: int = 20) -> dict:
        """Music search via /hubs/search, grouped by artist/album/track."""
        mc = await self._get(
            "/hubs/search",
            {"query": query, "limit": limit, "sectionId": await self._music_section_id()},
        )
        out: dict[str, list] = {"artist": [], "album": [], "track": []}
        for hub in mc.get("Hub", []):
            htype = hub.get("type")
            if htype not in out:
                continue
            for m in hub.get("Metadata", []):
                if htype == "track":
                    out["track"].append(self._track(m))
                else:
                    out[htype].append(
                        {
                            "ratingKey": m.get("ratingKey"),
                            "title": m.get("title"),
                            "type": htype,
                            "parentTitle": m.get("parentTitle"),
                            "thumb": self._token_url(m["thumb"]) if m.get("thumb") else None,
                        }
                    )
        return out

    async def _music_section_id(self) -> Optional[str]:
        secs = await self.music_sections()
        return secs[0]["key"] if secs else None

    # --- resolve -------------------------------------------------------
    async def resolve_stream_url(self, rating_key: str | int) -> dict:
        """Resolve a track ratingKey to its direct, decodable audio URL.

        Returns the full track dict (title/artist/duration/url). Raises
        PlexError if the ratingKey isn't a single playable track.
        """
        mc = await self._get(f"/library/metadata/{rating_key}")
        items = mc.get("Metadata", [])
        if not items or items[0].get("type") != "track":
            raise PlexError(f"ratingKey {rating_key} is not a track")
        return self._track(items[0])

    def _track(self, m: dict) -> dict[str, Any]:
        """Normalise a Plex track Metadata node into the hub's track shape,
        including the raw Part stream URL (the verified working form)."""
        part_key = None
        container = None
        try:
            part = m["Media"][0]["Part"][0]
            part_key = part["key"]                 # /library/parts/<id>/<ver>/file.<ext>
            container = part.get("container")
        except (KeyError, IndexError):
            pass
        return {
            "ratingKey": m.get("ratingKey"),
            "title": m.get("title"),
            "artist": m.get("grandparentTitle"),
            "album": m.get("parentTitle"),
            "duration": m.get("duration"),          # ms — used to pace queue chaining
            "container": container,                 # flac/mp3/... (informational)
            "thumb": self._token_url(m["thumb"]) if m.get("thumb") else None,
            "url": self._token_url(part_key) if part_key else None,
        }
