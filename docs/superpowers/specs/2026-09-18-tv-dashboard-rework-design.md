# TV dashboard rework — design

Date: 2026-09-18. Scope: the on-TV ambient screen (`hub/app/static/dashboard.html`)
and the host services that display it. The phone controller (`index.html`) is
out of scope.

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

## Part 1 — baseline fixes (host, no code)

Done on the Pi before any UI work, while `curl localhost:9595/status` reports
`"playing": false`.

1. `sudo timedatectl set-timezone America/Denver`.
2. `screen/fetch-aerials.sh` to refill `data/hub/aerials/` (12 clips, runs
   `classify-aerials.py` for `timeofday.json`).
3. `sudo systemctl disable --now kiosk-screen && sudo systemctl enable --now aerial-screen`.
4. Confirm `screen-player`'s `SCREEN_KIOSK_SERVICE` is `aerial-screen` so mpv
   hands the display back to the right unit.

Success: `systemctl is-active aerial-screen` is `active`, `/tmp/aerial-ov.png`
exists and refreshes each minute, the TV clock matches `docker exec hub date`,
and no Chromium process stays above 20% CPU between renders.

## Part 2 — layout and visual system

`dashboard.html` in overlay mode becomes a fixed three-row page:

```
┌ top band ────────────────────────────────────────────────────────┐
│ 9:41 PM                                              66°  ☁      │
│ Friday, September 18                     Cloudy · Denver         │
├ open middle (aerial visible) ────────────────────────────────────┤
│                                                                  │
├ bottom band: one CSS grid row of tiles ──────────────────────────┤
│ [ Forecast (2fr) ][ Server (1fr) ][ Media (1fr) ][ Alarm (1fr) ] │
└──────────────────────────────────────────────────────────────────┘
```

- **Top left:** clock ~9vw (was 15vw), AM/PM inline, date beneath.
- **Top right:** icon, current temperature, condition and city.
- **Bottom band:** `display: grid; grid-template-columns: 2fr 1fr 1fr 1fr`,
  fixed gap, inside the same horizontal padding as the top band. Tiles are
  dark glass: `rgba(5,7,13,.55)` fill, 1px `rgba(255,255,255,.14)` border,
  radius 1.2vw. No `backdrop-filter` (headless render cost unverified; add
  later only if a timed render stays under budget).
  - **Forecast:** seven equal columns; day label, icon, hi and lo. Sized so
    seven columns always fit the 2fr cell.
  - **Server:** five labelled readouts in a row: latency, CPU, RAM, disk,
    temp. CPU is `min(100, cpu_pct)`.
  - **Media:** Plex line (idle / N streams, up to three titles) and
    livestream line (red dot + title). Both lines ellipsize inside the tile.
  - **Alarm:** next alarm time, relative day, source. Hidden when none;
    the grid then drops to `2fr 1fr 1fr`.
  - **Now playing (Pi):** when `/api/screen/status` reports playback, a
    now-playing tile takes the alarm slot. Belt and braces: mpv normally
    owns the display during playback.
- **Type:** system sans stack (`-apple-system, "Segoe UI", system-ui,
  sans-serif`) with `font-variant-numeric: tabular-nums` on clock and
  readouts. All text white; existing text-shadow kept for the open areas.
- **Icons:** keep the current inline monochrome SVG set.
- **Non-overlay mode** (plain browser or kiosk fallback): static aurora
  gradient only. The Unsplash photo rotation, `<video>` aerial path and the
  Ken Burns animation are deleted.

## Part 3 — data and refresh

The overlay renderer (`screen/aerial-mode.py`) reloads the page once a
minute and captures as soon as the clock and the weather temperature have
text. That contract is preserved.

- On load the page fires weather, screen status, alarms and homelab
  **in parallel** (one `Promise.allSettled`), instead of four staggered
  timers.
- **Homelab is cache-first.** The server and media tiles render immediately
  from the last good `/api/homelab` response in `localStorage`
  (`livingroom-dashboard-homelab-v1`, 10 min max age) and update in place
  when the fresh response arrives. This mirrors the existing weather cache
  and keeps a 5 s cache-miss SSH probe from stalling the capture.
- **Hub:** `HOMELAB_TTL` in `hub/app/main.py` goes from 20 s to 55 s so a
  once-a-minute overlay almost always hits cache. Hub restart is safe during
  playback.
- Seconds stay hidden in overlay mode; `aerial-clock.lua` draws them.
- Weather and geolocation caching are unchanged.

## Part 4 — error handling

- Every readout has an explicit empty state (`–`).
- A tile hides when its endpoint fails or returns nothing useful; the grid
  reflows so there is never a blank card.
- Any text that can grow (live title, Plex titles, city) is ellipsized
  inside its tile with `overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap`.
- Fetch timeouts stay as today (weather 7 s, screen 2.5 s, alarms 3 s,
  homelab 4 s).

## Part 5 — verification

1. Playwright renders `/dashboard?overlay=1` against the live hub at
   3840x2160 and 1920x1080. Assert no element's bounding box exceeds the
   viewport and the forecast tile has seven children.
2. Same page with a mocked empty `/api/homelab` and empty `/api/alarms`:
   assert the grid is three columns and no blank tile is present.
3. On the Pi: time one cold and one warm overlay render from the
   `aerial-screen` journal; warm must stay under 3 s.
4. Pull `/tmp/aerial-ov.png` from the Pi and inspect it visually.
5. `top` on the Pi: no Chromium process above 20% CPU between renders.

## Out of scope

- Phone controller layout.
- `backdrop-filter` blur on tiles (revisit after render timing).
- Any new data sources.

## Docs to update in the same change

- `ROADMAP.md`: a dated entry for the audit findings and this rework.
- `README.md` / `HOWTO.md`: note that the aerial cache must be fetched after
  a rebuild, and that the Pi host timezone must match `TZ`.
