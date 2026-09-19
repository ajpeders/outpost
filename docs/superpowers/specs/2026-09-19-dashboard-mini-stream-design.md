# Dashboard mini stream — design

Date: 2026-09-19. Status: **planned, no code written**. Gated on the spike in
step 0 below.

Scope: a small live video in the top-right corner of the on-TV ambient
dashboard (`aerial-screen`), chosen and toggled from the phone controller.
v1 sources are the **jetstream livestream** and **MTV**. A **movie** source
(library file) is explicitly deferred. Sound can be on or off.

## Why

The ambient dashboard is an info board over aerial clips. Sometimes you want
to keep the board (clock, weather, alarms) and still keep half an eye on the
livestream or MTV, without giving the whole TV to it. Today the only choice
is fullscreen or nothing.

## The constraint that shapes the design

The dashboard is one mpv process scanning out straight to DRM with zero-copy
HEVC decode; the HTML board is rendered to a PNG once a minute and composited
by mpv (`overlay-add`). Chromium on the Pi cannot paint a video surface
(documented dead end), so the corner stream **must be drawn by mpv**.

mpv draws one video per process. Compositing a second one means the
`lavfi-complex` software path, which takes the 4K aerial off zero-copy: decode,
copy, scale and blend two videos on four cores every frame. Existing tuning
notes say 4K only runs clean on the zero-copy path. So the baseline design is:

> While the mini stream is on, the aerial clip does not play. The corner stream
> is mpv's only video; the dashboard overlay fills the rest of the screen with
> its own background. Aerials resume when the mini stream is turned off.

Step 0 measures whether that assumption can be relaxed.

## Step 0 — spike (before any feature code)

On the Pi, with `aerial-screen` stopped, run mpv by hand with `--lavfi-complex`
compositing a **1080p** aerial clip (software decode) plus the jetstream
stream scaled into a corner, output at the 4K mode. Read `frame-drop-count`
and CPU after two minutes. Repeat with a 720p corner stream.

- Clean (0 drops, CPU headroom): aerials keep playing behind the mini stream;
  the design below changes only in the "mpv side" section (add the second
  input instead of replacing the playlist). Note: this also needs 1080p
  aerial variants cached by `fetch-aerials.sh`.
- Drops: baseline design stands. Record the numbers in ROADMAP.

Takes over the TV for a few minutes; do it when nobody is watching.

## Design

### State (hub owns it)

One JSON blob, persisted at `data/hub/mini.json`, default off:

```json
{ "source": "off" | "livestream" | "mtv", "audio": false }
```

Hub API, same-origin like everything else:

| Route | Does |
|---|---|
| `GET /api/dashboard/mini` | Current state **plus** resolved playback: `url`, `start` (seconds, MTV only), `title`, and the corner geometry (`x, y, w, h` at 3840x2160). |
| `POST /api/dashboard/mini` `{source?, audio?}` | Update, persist, return the same shape. 400 on unknown source; 503 if the chosen source is not configured (`JETSTREAM_URL` / `MTV_URL` unset). |

The hub resolves URLs because it already knows how: livestream uses the existing
`_jetstream_play_url()` (single rendition, cached); MTV uses the schedule
module below. Capability flags `livestream` / `mtv` on `/api/screen/status`
already exist and gate the UI.

### MTV schedule module

`hub/app/mtv_schedule.py` (new): a port of `startLocal` from `mtv.js` (seeded
shuffle over `manifest.json`, epoch 1981-08-01, seed 1981). Input: manifest +
wall-clock time. Output: `{id, title, url, start, ends_at}` for the on-air
item. Manifest fetched from `MTV_URL/videos/manifest.json`, cached 10 min.

This is the same piece the planned fullscreen MTV rework needs (ROADMAP,
Phase 8 "Rework"), so it is written once and used by both. Unit tests compare
its output to the JS for a handful of fixed timestamps.

### mpv side (`screen/aerial-mode.py`)

The main loop currently sleeps `REFRESH` (60 s) between overlay renders. It
changes to a **5 s tick** that polls `GET /api/dashboard/mini`, and still
renders the overlay every 60 s.

On a state change:

- **off → source:** `loadfile <url> replace` (with `start=<start>` for MTV),
  then set `video-zoom`, `video-align-x=1`, `video-align-y=-1`,
  `video-margin-ratio-top/right` so the picture lands in the corner geometry,
  `mute` per `audio`. Aerial playlist is dropped.
- **source → off:** `loadlist <playlist> replace`, `playlist-shuffle`, reset
  zoom/align/margins, `mute=yes`.
- **audio change only:** set `mute`.
- **MTV and mpv idle (EOF):** ask the hub again and load the next on-air item.
  Re-fetching from the hub (not computing locally) keeps the schedule logic in
  one place.

mpv is launched with audio **enabled but muted** (today it's `--no-audio`),
using the same ALSA/HDMI output flags the screen player uses, so sound can be
toggled without a restart.

Every state change also triggers an overlay re-render so the board reflows
immediately, not up to a minute later.

### Overlay page (`dashboard.html`)

New query params, passed by `aerial-mode.py` from the API response:
`mini=1&mx=&my=&mw=&mh=`.

When `mini=1` in overlay mode:

- The page paints the existing **aurora gradient** as an opaque background
  instead of transparent, with one transparent rectangle at the mini geometry
  (a hole for mpv's video to show through).
- The weather block in the top band moves left so it does not sit under the
  hole. The clock and the bottom tiles are untouched.
- A thin 1px border and a small source label ("LIVE" red dot / "MTV") sit just
  outside the hole, in the same glass style as the tiles.
- The Media tile shows the mini source title (jetstream now-playing, or the MTV
  track) so credits are visible without an OSD.

Geometry lives in one place: hub constants `MINI_X/Y/W/H` (defaults roughly
3840x2160 top-right, 30 % width, 16:9, below the top band's clock height),
sent to both mpv and the page, so they can never disagree.

If the spike passes (aerials keep playing), the page stays transparent
everywhere and only the border/label is drawn.

### Phone controller (`index.html`)

In the "Watch on the TV" panel, a new row **Dashboard mini stream**:
segmented control `Off | Livestream | MTV` plus a `Sound` toggle. Hidden when
neither source is configured. Reads state on load and after each tap; toast
on error with the hub's `detail`.

### Interaction with fullscreen playback

The screen player already stops `aerial-screen` before mpv takes the display
for fullscreen playback and restarts it afterwards. Mini state persists in
`mini.json`, so the corner stream comes back when the dashboard does. Nothing
in the screen player changes.

### Failure rules

- Stream fails to open or dies (mpv goes idle while source is on): retry at
  the next tick, up to 6 tries (30 s). Then fall back to aerials, keep
  `source` as set, and report `error: "<reason>"` on `GET /api/dashboard/mini`
  so the controller can show it. Retry again every 5 min.
- Hub unreachable from `aerial-mode.py`: keep whatever is playing; the clock
  keeps ticking because it's mpv's OSD.
- Manifest fetch fails for MTV: hub returns 503 with detail; the controller
  shows it; mpv keeps the aerials.

## Testing

- `tests/test_mtv_schedule.py`: on-air item and offset for fixed timestamps,
  matches `mtv.js` (fixtures generated once from the JS in node).
- `tests/test_mini_api.py`: state round-trip, persistence, 400/503 paths, URL
  resolution mocked.
- Manual acceptance on the Pi (recorded in ROADMAP with date):
  1. Toggle each source and back; corner shows video, board reflows, aerials
     resume on off.
  2. Sound toggle audible on the TV.
  3. `frame-drop-count` stays 0 over five minutes on each source.
  4. Play something fullscreen, stop it: mini stream returns.
  5. Kill the jetstream transcode mid-stream: fallback to aerials within 30 s,
     error visible in the controller.

## Out of scope (v1)

- Movie / library file as a source (needs the SMB path piped through the same
  mechanism; straightforward once v1 exists).
- Corner position choice or resizing from the phone.
- Showing the mini stream on the cage `kiosk-screen` dashboard (cannot; mpv only).
