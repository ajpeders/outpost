# MTV on the TV via mpv — design

Date: 2026-09-19. Status: **approved design, no code written**.

Replaces the cage + chromium kiosk that Phase 8 shipped for the MTV button
with playback through mpv on DRM, the same path the jetstream livestream uses.
Spans two repos: `alex/mtv` (one new endpoint) and this one (screen-player,
hub, unit cleanup).

## Why

The kiosk path was verified broken on 2026-09-19 with `mtv-screen` active:

- chromium with `--disable-gpu` under cage software-decodes the MP4s at
  3840x2160 and pins one process at 100% CPU — stuttery video. This is the
  documented dead end from the aerials work (cage/chromium cannot paint a
  video surface on this Pi).
- no audio: the page starts muted until a tap that never comes on a mouseless
  kiosk, and the root cage session has no audio server anyway.
- cursor parked mid-screen: `install-cursor.sh` had never been run on this Pi
  (fixed on the day, but moot once the kiosk goes).

The mirrored videos are H.264 at ≤1080p (`sync.sh` caps at 1080 avc1). That is
the same software-decode load as the jetstream live stream, which plays clean.

## What MTV is

A deterministic broadcast, not a video site. `app/mtv.js` sorts the synced
library by id, shuffles it with a fixed-seed PRNG (mulberry32, seed
`1981 + channel`), and picks "what's on" as wall-clock seconds since
1981-08-01 modulo total duration. Every viewer computes the same answer with
no shared state. The admin page carries an exact copy of that math for its
NOW PLAYING panel.

## Design

### 1. MTV site: `GET /admin/api/now?ch=<num>` (repo `alex/mtv`)

Added to `admin/main.py` (FastAPI). Reads `/videos/manifest.json` and
`/config/channels.json`, ports the schedule math to Python, returns:

```json
{
  "channel": 1,
  "now":  {"id": "Cyn5kUj6Fj4", "title": "Drake - Choosin' Texas", "offset": 83.2,
           "remaining": 114.4, "duration": 197.65},
  "next": {"id": "8wTlhT-dQ0Y", "title": "I KNOW ITS TRUE DRAKE", "duration": 106.12},
  "url_base": "/videos/"
}
```

- `ch` defaults to 1. Unknown channel, missing manifest, or a channel with no
  playable items → 404 `{"error": "..."}` (the player's NO SIGNAL case).
- The port must reproduce the JavaScript exactly: `Math.imul` and `>>>`
  semantics via masking to 32 bits, `(x ^ (x >>> 14)) >>> 0` as an unsigned
  32-bit value divided by 2^32, `Math.floor(rnd() * (i + 1))` for the swap
  index, and string comparison of ids for the sort. Durations are the floats
  from the manifest; `offset` is `time.time()` based, not integer seconds.
- `title` is the raw manifest title. The credit split into artist / song
  stays where it is today (the player's `cleanTitle`), and the same split is
  ported alongside so `now.artist` / `now.song` are also returned.
- The admin page's NOW PLAYING panel switches to this endpoint and its
  JavaScript copy of the math is deleted. Count of implementations stays at
  two (browser player, Python) instead of growing to three.
- Access: unchanged Traefik `local-only@file` middleware on `/admin`. The Pi
  resolves `mtv.thelunadog.com` to the LAN address and gets 200 today.
- Test: `admin/test_schedule.py` runs a fixture manifest through the Python
  port and through the JavaScript functions under `node` (extracted verbatim
  from `mtv.js`) and asserts identical orderings for channels 1–4 and
  identical on-air picks at three fixed timestamps.

### 2. screen-player: `mtv` profile (`screen/screen_player.py`)

`POST /play` gains `source: "mtv"`:

```json
{"source": "mtv", "url": "https://mtv.thelunadog.com", "channel": 1}
```

`url` is the site base (from `MTV_URL`), not a media URL. On receipt:

1. `GET {url}/admin/api/now?ch={channel}` (5 s timeout). Failure → 502 with
   the upstream error; nothing is spawned.
2. Set `_profile = "mtv"`, `_source = "mtv"`, `_title = now.artist`,
   `_subtitle = now.song`, `_url = {url}{url_base}{now.id}.mp4`.
3. `_build_args` for `mtv`: the `live` flag set (`--vo=drm`, `--hwdec=no`,
   IPC socket, small cache) plus `--start={offset}`, `--prefetch-playlist=yes`,
   `--keep-open=no`, and OSD sizing (`--osd-font-size`, `--osd-margin-*`,
   `--osd-align-x=left --osd-align-y=bottom`) tuned once for 4K viewing from
   the couch. Drop `--hls-bitrate` (not HLS).
4. Spawn as today (`_spawn` handles kiosk stop + DRM handoff).
5. Start the **conductor** thread for this spawn.

**Conductor.** One thread per mtv spawn, exits when `_proc` changes or the
profile leaves `mtv`. It:

- Waits for the IPC socket, then `loadfile {next.url} append` so mpv prefetches
  the following song.
- Shows credits: `show-text "{artist}\n{song}" 8000` at start, and again when
  `remaining - 10 s` elapses (timer armed from the `now` response).
- Observes `playlist-pos` / `end-file` via IPC events (`observe_property`).
  On every file change: re-query `/admin/api/now`; set title/subtitle for
  `/status`; append the new `next`; arm the credits timers. If the file mpv
  just started is not `now.id`, or mpv's `time-pos` differs from `now.offset`
  by more than 2 s, correct with `loadfile {now.url} replace` plus
  `--start` via `loadfile … replace start={offset}`, then append `next`.
- If `/admin/api/now` fails mid-session, keep playing whatever is queued and
  retry on the next file change; log once per failure streak.

**Supervisor.** For `_profile == "mtv"` an mpv death is handled by calling the
same "ask now, spawn at offset" entry point rather than re-spawning `_url`,
so a crash rejoins the broadcast instead of restarting a song. The existing
settle delay applies.

**Deleted:** `MTV_KIOSK_SERVICE`, `/kiosk/mtv` and the `mtv` branch of
`_swap_kiosk`. `/kiosk/idle` stays (other callers).

### 3. Hub (`hub/app/main.py`, `hub/app/static/*`)

- `POST /api/screen/mtv` mirrors the jetstream livestream handler: wake TV to
  Pi input, `POST {SCREEN_URL}/play` with the mtv body above, return
  `{"ok": true, "playing": "mtv", "target": "pi", "title", "subtitle"}`.
  `MTV_URL` unset → 503 as today.
- `POST /api/screen/mtv/stop` is removed; the UI's stop calls the existing
  `POST /api/screen/stop`, which already returns the TV to the idle kiosk.
- `GET /api/screen/status` keeps `mtv: bool(MTV_URL)`.
- Now-playing tile: no change needed — it already renders screen-player's
  `title` / `subtitle` for other sources; the MTV button's stop handler is
  repointed.

### 4. Cleanup

- Delete `screen/mtv-screen.service`, `screen/mtv-kiosk.sh`.
- Remove `mtv-screen.service` from the `Conflicts=` lines in
  `aerial-screen.service` and `kiosk-screen.service`.
- `screen/install-services.sh`: drop `mtv-screen` from its unit list if
  enumerated there.
- Deploy on the Pi: `systemctl stop mtv-screen; systemctl disable mtv-screen`
  (if enabled), remove the installed unit file, `daemon-reload`, reinstall
  the two idle units, restart `screen-player` when idle.
- `docs/mtv-launch.md` rewritten for the mpv path (deploy = restart
  screen-player + rebuild hub; verify = `/status` shows `source: "mtv"`).
- README / ARCHITECTURE / ROADMAP / HOWTO updated: Phase 8 note replaced
  by the new behaviour; the "Known issues" block added on 2026-09-19 moves
  to History.

### 5. Testing

- Unit (offline, existing screen-player test style): fake `/admin/api/now`
  server; assert spawn args contain `--start=<offset>` and the right mp4 URL;
  assert the conductor issues `loadfile … append` for `next`; assert a
  simulated file change whose id differs from `now.id` triggers the corrective
  `loadfile … replace`; assert `/status` reports `source: "mtv"` with
  title/subtitle.
- Unit (mtv repo): the JS-vs-Python schedule parity test in §1.
- Manual acceptance on the Pi, nobody watching:
  1. Tap MTV on the phone → TV shows the video within ~5 s, **with sound**.
  2. Credits readable from the couch at 3840x2160; appear at song start and
     ~10 s before the end.
  3. A song boundary passes with no black gap and no DRM error in
     `/tmp/screen-mpv.log`.
  4. `top`: no process above ~60% CPU; `frame-drop-count` via IPC stays 0
     over two minutes.
  5. Phone shows the same artist/song as `/admin` NOW PLAYING, within 2 s.
  6. Stop → idle dashboard returns.

## Out of scope

- Channel switching from the phone (single channel today; `channel` is
  plumbed so it is a UI-only addition later).
- Styled HTML credits matching the site's look (OSD text chosen on purpose).
- The dashboard mini-stream (separate spec, 2026-09-19); it will reuse the
  `mtv` profile's "ask now, start at offset" function.
