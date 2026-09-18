# TV dashboard rework — design

Date: 2026-09-18. Scope: the on-TV ambient screen (`hub/app/static/dashboard.html`),
the mpv seconds overlay (`screen/aerial-clock.lua`), one hub constant, and the
host services that display it. The phone controller (`index.html`) is out of
scope.

## Why

An audit on 2026-09-18 found the rebuilt Pi in a degraded state and the
dashboard layout broken at TV sizes:

- The Pi runs `kiosk-screen` (cage + Chromium, software-rendered at
  3840x2160), not `aerial-screen`. `data/hub/aerials/` is empty, so
  `aerial-screen` cannot start.
- Chromium's GPU process sits at ~96% of one core because the Ken Burns CSS
  animation on a 4K photo is composited in software.
- The Pi host timezone is Europe/London; the hub is America/Denver. The kiosk
  browser renders the clock with host time, so the TV shows 7:09 when it is
  00:09, and next-alarm math is off by the same seven hours.
- The 7-day forecast overflows the right edge at 1080p and 4K. The homelab
  line truncates the live title after a few characters. The clock at 15vw
  dominates; the middle two thirds of the screen are empty.

## Goal

An **info board** over Apple aerials: clock and date, current weather and a
7-day forecast, homelab stats, Plex and livestream status, next alarm, and
Pi now-playing when relevant. Aerial clips remain the visual point; the board
sits in edge bands with the middle open.

## Part 1 — baseline fixes (host-side, no UI code)

Done on the Pi before any UI work, while `curl localhost:9595/status` reports
`"playing": false`.

1. `sudo timedatectl set-timezone America/Denver`.
2. `screen/fetch-aerials.sh` to refill `data/hub/aerials/` (12 clips, runs
   `classify-aerials.py` for `timeofday.json`).
3. Point the screen player at the right idle service **before** switching
   units, so there is no window where a play request stops the wrong one. `screen_player.py`
   defaults `SCREEN_KIOSK_SERVICE` to `kiosk-screen` and the unit does not
   override it, so mpv would restart the wrong unit after playback. Add a
   drop-in on the Pi at
   `/etc/systemd/system/screen-player.service.d/override.conf` containing
   `[Service]` / `Environment=SCREEN_KIOSK_SERVICE=aerial-screen`, then
   `sudo systemctl daemon-reload && sudo systemctl restart screen-player`
   (safe: nothing is playing). The
   repo default stays `kiosk-screen` so other installs are unaffected; HOWTO
   documents the drop-in.
4. `sudo systemctl disable --now kiosk-screen && sudo systemctl enable --now aerial-screen`.

Success: `systemctl is-active aerial-screen` is `active`, `/tmp/aerial-ov.png`
exists and refreshes each minute, the TV clock matches `docker exec hub date`,
`systemctl show screen-player -p Environment` includes
`SCREEN_KIOSK_SERVICE=aerial-screen`, and no Chromium process stays above
20% CPU between renders.

## Part 2 — layout and visual system

`dashboard.html` in overlay mode becomes a fixed three-row page:

```
┌ top band ────────────────────────────────────────────────────────┐
│ 9:41 [:ss] PM                                        66°  ☁      │
│ Friday, September 18                     Cloudy · Denver         │
├ open middle (aerial visible) ────────────────────────────────────┤
│                                                                  │
├ bottom band: one flex row of tiles ──────────────────────────────┤
│ [ Forecast (flex 2) ][ Server (1) ][ Media (1) ][ Alarm (1) ]    │
└──────────────────────────────────────────────────────────────────┘
```

### Top band

- **Left:** clock ~9vw (was 15vw). Order: hours:minutes, a seconds slot,
  AM/PM, all on one baseline. Date beneath.
- **Seconds slot.** The page keeps an empty inline `#sec` element of fixed
  width (three characters, `:SS`, at the seconds font size) in overlay mode. The page never
  fills it in overlay mode; mpv's `aerial-clock.lua` draws the live seconds
  there. To keep the two aligned, implementation measures the `#sec`
  bounding box in the 3840x2160 Playwright render and commits the resulting
  centre-x, top-y and font size as the new `AERIAL_SEC_X/Y/FS` defaults in
  `aerial-clock.lua` (the lua anchors with `\an8`, top-centre, which is why
  centre-x and top-y are the values to commit). The lua file is
  also changed from `DejaVu Sans Mono` to `DejaVu Sans` bold to match the
  page. In non-overlay mode the page fills
  `#sec` itself each second, as today.
- **Right:** icon, current temperature, condition and city.

### Bottom band

- `display: flex; gap: 1.2vw;` inside the same horizontal padding as the
  top band. Forecast tile `flex: 2 1 0`, every other tile `flex: 1 1 0`.
  A hidden tile is `display: none`, so the remaining tiles share the row
  with no gap. This is the single reflow rule; there are no per-count
  grid templates. If the forecast tile is hidden the others still share the
  full width.
- Tiles are dark glass: `rgba(5,7,13,.55)` fill, 1px `rgba(255,255,255,.14)`
  border, radius 1.2vw. No `backdrop-filter` (render cost unverified; out of
  scope).
- **Forecast:** seven equal columns (`display: grid; grid-template-columns:
  repeat(7, 1fr)`); day label, icon, hi and lo. Type sized so seven columns
  fit the tile at 1080p and 4K. Hidden only if there is no weather at all
  (fetch failed and no cache).
- **Server:** five labelled readouts in a row: latency, CPU, RAM, disk, temp.
  CPU is `min(100, cpu_pct)`. Rules from the `/api/homelab` shape:
  - `up: false` → tile shows `<host> offline` and nothing else.
  - `up: true`, `stats` null or `!stats.ok` → tile shows host name and
    latency only.
  - `stats.ok` → all five readouts. Missing individual values show `–`.
  - Endpoint failed and no localStorage copy → tile hidden.
- **Media:** Plex line (`Plex idle` / `Plex N streams`, then up to three
  titles) and livestream line (red dot + title). Rules:
  - Plex line shown when `plex.ok`; hidden when `plex.ok` is false.
  - Live line shown when `live && live.ok && live.playing && live.title`
    (`live` is null when `JETSTREAM_TITLE_URL` is unset).
  - Tile hidden when neither line would show, or when the endpoint failed
    and there is no localStorage copy.
  - Both lines ellipsize inside the tile.
- **Alarm:** next alarm time, relative day, source. Hidden when none; a
  failed `/api/alarms` fetch counts as none, matching today's `catch`.
- **Now playing (Pi):** when `/api/screen/status` reports `playing && url`, a
  now-playing tile (title, subtitle) appears after the alarm tile. Kept by
  user decision; in practice mpv owns the display during playback so this
  is rarely visible.

### Type and colour

- Font stack `"DejaVu Sans", -apple-system, "Segoe UI", system-ui, sans-serif`.
  DejaVu Sans is first because it is the only sans on the Pi (`fc-match
  sans-serif` → DejaVu Sans), so the Pi render, the lua seconds, and any
  Playwright run on a machine with DejaVu installed all agree.
  `font-variant-numeric: tabular-nums` on the clock and readouts.
- All text white; existing text-shadow kept for the open areas.
- Icons: the current inline monochrome SVG set, unchanged.

### Non-overlay mode

Plain browser or a kiosk fallback: static aurora gradient only. The Unsplash
photo rotation, the `<video>` aerial path and the Ken Burns animation are
deleted. `screen/kiosk.sh` passes only `?host=`, so nothing depends on the
removed `?bg=video` branch.

## Part 3 — data and refresh

### Overlay render contract (preserved)

`screen/aerial-mode.py` reloads the page once a minute, then polls
`OverlayRenderer._painted`, which captures as soon as **`#time`** has more
than three characters of text and **`#wx-temp`** is not one of `''`, `–`,
`-`. Both element ids and the `–` sentinel are preserved exactly. The new
layout uses `–` as the universal empty state, which is compatible because
`_painted` only inspects `#wx-temp`.

Consequence: if `/api/weather` fails and there is no localStorage cache,
`_painted` times out after 3 s and the renderer sleeps `SETTLE` (2 s) before capturing.
The Part 5 warm-render budget therefore only applies when weather is
available.

### Fetching

- On load the page fires weather, screen status, alarms and homelab
  concurrently, as it already does today at script evaluation. Each tile
  decides shown/hidden from its own result; there is no combined
  completion point, and `_painted` is unchanged.
- The per-function refresh intervals (weather 15 min, screen 5 s, alarms
  60 s, homelab 60 s) are kept as-is. In overlay mode the page lives about
  60 s between reloads, so the 5 s screen poll fires a dozen times per
  cycle; harmless and unchanged.
- **Homelab is cache-first.** The server and media tiles render immediately
  from the last good `/api/homelab` response in `localStorage`
  (`livingroom-dashboard-homelab-v1`, 10 min max age) and update in place
  when the fresh response arrives. This mirrors the weather cache and keeps
  a 5 s cache-miss SSH probe from stalling the capture. Note: the
  `_render_cold` fallback in `aerial-mode.py` uses a separate Chromium
  profile (`/tmp/aerial-chrome-profile`), so that path will not see this
  cache; acceptable.
- **Hub:** `HOMELAB_TTL` in `hub/app/main.py` goes from 20 s to 120 s. The
  overlay fetches about 63 s apart (60 s sleep plus render), so a 120 s TTL
  serves every other overlay fetch from cache and halves SSH probes. The
  localStorage copy, not the hub TTL, is what protects the capture. Hub
  restart is safe during playback.
- Weather caching is unchanged. The direct `api.open-meteo.com` and
  `ip-api.com` fallback in `weather()` is **removed**: the hub is the only
  thing that serves this page, `/api/weather` already serves stale data on
  upstream failure, and the fallback made the page's behaviour depend on
  the Pi's internet access. `GEO_CACHE_KEY` and `geolocate()` go with it.
  The `?lat`/`?lon` URL passthrough to `/api/weather` is kept.

## Part 4 — error handling

- Every readout has an explicit empty state (`–`).
- Tile show/hide rules are the ones in Part 2; a tile is either fully
  populated per its rule or `display: none`, never blank.
- Any text that can grow (live title, Plex titles, city, alarm label) is
  ellipsized inside its tile with `overflow: hidden; text-overflow:
  ellipsis; white-space: nowrap`.
- Fetch timeouts stay as today (weather 7 s, screen 2.5 s, alarms 3 s,
  homelab 4 s).

## Part 5 — verification

1. Playwright (from the dev box, DejaVu Sans installed) renders
   `/dashboard?overlay=1` against the live hub at 3840x2160 and
   1920x1080. Assert: forecast tile has seven children and
   `scrollWidth <= clientWidth`; the bottom band's right edge equals the
   top band's right edge; no tile's bounding box exceeds the viewport.
2. Fresh browser context (no localStorage). `/api/homelab` and
   `/api/alarms` routed to HTTP 502 via Playwright `route`: assert the
   server, media and alarm tiles are `display: none` and the forecast tile
   spans the full band width.
3. Fresh browser context. `/api/weather` routed to HTTP 502: assert the
   forecast tile is hidden, `#wx-temp` reads `–`, no request left the hub
   origin, and the other tiles span the band.
4. Measure the `#sec` box in the 4K render and confirm the committed
   `AERIAL_SEC_X/Y` defaults are within 2 px of its centre-x and top-y and
   `AERIAL_SEC_FS` within 2 px of its computed font size.
5. On the Pi: from the `aerial-screen` journal, one cold and one warm
   overlay render; warm must stay under 3 s with weather available.
6. Pull `/tmp/aerial-ov.png` from the Pi and inspect it, then confirm the
   seconds land inside the `#sec` slot on the TV.
7. `top` on the Pi: no Chromium process above 20% CPU between renders.

## Out of scope

- Phone controller layout.
- `backdrop-filter` blur on tiles.
- Any new data sources.
- Changing the repo default of `SCREEN_KIOSK_SERVICE`.

## Docs to update in the same change

- `ROADMAP.md`: a dated entry for the audit findings and this rework.
- `ARCHITECTURE.md`: the dashboard/overlay pipeline section (seconds slot
  contract, cache-first homelab, `_painted` ids).
- `README.md` / `HOWTO.md`: the aerial cache must be fetched after a
  rebuild, the Pi host timezone must match `TZ`, and the `screen-player`
  drop-in for `SCREEN_KIOSK_SERVICE`.
