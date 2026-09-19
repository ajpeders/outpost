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
- `title` is the raw manifest title. `now.artist` / `now.song` (and the same
  on `next`) come from a port of the player's `creditText` (`mtv.js`): prefer
  the manifest item's `artist` / `track` fields when present, otherwise strip
  the "(Official Video)"-style suffix and split on the first " - ".
- Accepted divergence: the browser player skips ids that failed to play in
  that session (its `bad` set), so one viewer with a broken file can be a song
  off from the server. Nothing else in the math is session-local.
- The admin page's NOW PLAYING panel switches to this endpoint and its
  JavaScript copy of the math is deleted. Count of implementations stays at
  two (browser player, Python) instead of growing to three.
- Access: unchanged Traefik `local-only@file` middleware on `/admin`. The Pi
  resolves `mtv.thelunadog.com` to the LAN address and gets 200 today.
- Test: `admin/test_schedule.py` runs a fixture manifest through the Python
  port and through the JavaScript functions under `node` (extracted verbatim
  from `mtv.js`) and asserts identical orderings for channels 1–4, identical
  on-air picks at three fixed timestamps, and identical `creditText` output
  for titles with and without `artist`/`track` fields.

### 2. screen-player: `mtv` profile (`screen/screen_player.py`)

`POST /play` gains `source: "mtv"`:

```json
{"source": "mtv", "url": "https://mtv.thelunadog.com", "channel": 1}
```

`url` is the site base (from `MTV_URL`), not a media URL. Three new units,
each with one job:

**(a) `mtv_now(base, channel) -> dict`.** `GET {base}/admin/api/now?ch=…`,
5 s timeout, raises `MtvUnavailable(str)` on any HTTP/network/JSON failure.
Pure client, no state. Used by (b) and (c).

**(b) `_play_mtv(base, channel)` — the "ask now, spawn at offset" entry
point.** Called by the `/play` handler and by the supervisor on respawn; the
dashboard mini-stream spec will call it too.

1. `now = mtv_now(base, channel)`. On `MtvUnavailable` → the caller decides
   (see below); nothing is spawned.
2. Set globals: `_profile="mtv"`, `_source="mtv"`, `_url={base}{url_base}{now.id}.mp4`,
   `_title=now.artist`, `_subtitle=now.song`, and a new `_start: float | None`
   (= `now.offset`) which `_build_args` reads for the `mtv` profile only and
   `_stop()` clears. Also stash `_mtv = {"base": base, "channel": channel,
   "next": now.next}` for the conductor.
3. `_build_args(profile="mtv")`: the `live` flag set (`--vo=drm`, `--hwdec=no`,
   `--input-ipc-server`, small cache, `--sid=no`) minus `--hls-bitrate`, plus
   `--prefetch-playlist=yes`, `--keep-open=no`, OSD sizing constants
   (`--osd-font-size`, `--osd-margin-x/y`, `--osd-align-x=left`,
   `--osd-align-y=bottom`, `--osd-border-size`) tuned once for 4K from the
   couch. **No `--start` and no file on the command line**: `--start` is a
   per-file option and would apply to every appended song. Instead mpv is
   spawned with `--idle=yes` and the conductor loads every entry with its
   own per-file `start` (`loadfile {url} replace -1 start={offset}`, then
   `loadfile {next_url} append -1 start=0`). One mechanism for first song,
   next song and drift correction. (mpv ≥ 0.38 `loadfile` syntax:
   `<url> <flags> <index> <options>`; the Pi runs 0.40.0 — the plan pins
   this and verifies the first loadfile on the box.) `_start` is therefore
   not read by `_build_args`; it only feeds the conductor's first loadfile.
4. `_spawn()` as today (kiosk stop, DRM handoff), then start (c).

`/play` with `source: "mtv"` calls (b); on `MtvUnavailable` returns 502
`{"error": "mtv: <reason>"}`. Success body:
`{"ok": true, "url": <mp4 url>, "profile": "mtv", "source": "mtv",
"title": <artist>, "subtitle": <song>}` — the hub relays title/subtitle.

**(c) Conductor thread — `MtvConductor(proc, mtv_state)`.** One per mtv
spawn; exits when `_proc` is no longer `proc` or `_profile != "mtv"`.
Owns a **persistent** IPC connection (the existing `_ipc()` is one-shot
request/reply and stays that way for `/control` and `/status`). Interface:

- `events`: connect to the socket (retry for 5 s after spawn), send
  `observe_property 1 playlist-pos`, read newline-delimited JSON forever.
  Replies to its own commands are matched by `request_id`; property-change
  events drive the state machine below. Socket loss → thread exits; the
  supervisor's respawn creates a new conductor.
- `command(list)`: send on the same socket with a fresh `request_id`; a lock
  serialises writes, the reader thread routes replies. Two commands are
  used: `loadfile` and `show-text`.

State machine:

1. On connect: `loadfile now replace -1 start={offset}`; `loadfile next append
   -1 start=0`; credits at start; arm end-credits.
2. On `playlist-pos` change (a song boundary, or our own replace): query
   `mtv_now`. If `now.id` equals the file mpv is playing (`path` property)
   and `|time-pos − now.offset| ≤ 2 s`: update `_title/_subtitle`, append the
   new `next` with `start=0`, credits at start, arm end-credits. Otherwise
   (drift or library change): `loadfile now replace -1 start={offset}`, then
   append `next`, credits, arm. The replace itself triggers another
   `playlist-pos` event; the check passes on that pass.
3. `mtv_now` failure mid-session: keep playing what is queued (mpv already
   has `next`), log once per failure streak, retry on the next boundary. If
   the playlist runs dry (mpv goes idle with no next), retry `mtv_now` every
   5 s until it answers, then step 1.
4. Credits: `show-text "{artist}\n{song}" 8000` at each song start; the end
   credits are armed from mpv's `time-pos`, not wall clock — the conductor
   also observes `time-pos` (property id 2, mpv throttles it to ~4/s) and
   fires once when `duration − time-pos ≤ 10`. Pause via `/control` therefore
   delays the credits correctly.

**Supervisor.** For `_profile == "mtv"`, on mpv death after the existing
settle delay call `_play_mtv(_mtv.base, _mtv.channel)` instead of
re-spawning `_url`, so a crash rejoins the broadcast. If that raises
`MtvUnavailable`, retry on each supervisor tick (3 s) for up to 60 s, then
give up: `_stop()` and `_kiosk("start")` (dashboard back, same as a movie
ending) and log the reason. The `live`-only decode-corruption and
video-freeze watchdogs do **not** apply to `mtv` (no HLS discontinuities);
stated here so the omission is deliberate.

**Deleted:** `MTV_KIOSK_SERVICE`, `/kiosk/mtv` and the `mtv` branch of
`_swap_kiosk`. `/kiosk/idle` stays (other callers). The module docstring
lines describing `/kiosk/mtv` go too.

### 3. Hub (`hub/app/main.py`, `hub/app/static/*`)

- `POST /api/screen/mtv` mirrors the jetstream livestream handler: wake TV to
  Pi input, `POST {SCREEN_URL}/play` with the mtv body above, return
  `{"ok": true, "playing": "mtv", "target": "pi", "title", "subtitle"}` with
  title/subtitle taken from screen-player's `/play` response. Screen-player
  502 → relayed as 502 `{"ok": false, "error": …}`. `MTV_URL` unset → 503
  as today. The hub's 30 s client timeout comfortably covers the 5 s upstream
  GET plus spawn.
- `POST /api/screen/mtv/stop` is deleted as a dead endpoint: nothing in the
  UI calls it (stop already goes through `POST /api/screen/stop`, which quits
  mpv, restarts the idle kiosk and re-claims the Pi input — verified).
- `GET /api/screen/status` keeps `mtv: bool(MTV_URL)`.
- Phone UI (`static/index.html`): the now-playing render branches on
  `profile === 'live'` vs everything-else-is-library, so `profile: "mtv"`
  would light up the Library tab, pop the file browser and show the media
  transport (seek, CC, next episode). Add an `mtv` branch: highlight the MTV
  button in the Watch panel, `showTransport('live')` (stop only), no library
  panel. Title/subtitle already render from the status fields.

### 4. Cleanup

- Delete `screen/mtv-screen.service`, `screen/mtv-kiosk.sh`.
- Remove `mtv-screen.service` from the `Conflicts=` lines in
  `aerial-screen.service` and `kiosk-screen.service`.
- `screen/install-services.sh`: remove `mtv-screen` from its unit list and
  the header comment describing it.
- Hub `main.py` and `screen_player.py` docstrings that mention `/kiosk/mtv`
  or `mtv-screen` are updated.
- Deploy on the Pi: `systemctl stop mtv-screen; systemctl disable mtv-screen`
  (if enabled), remove the installed unit file, `daemon-reload`, reinstall
  the two idle units, restart `screen-player` when idle.
- `docs/mtv-launch.md` rewritten for the mpv path (deploy = restart
  screen-player + rebuild hub; verify = `/status` shows `source: "mtv"`).
- README / ARCHITECTURE / ROADMAP / HOWTO updated: Phase 8 note replaced
  by the new behaviour; the "Known issues" block added on 2026-09-19 moves
  to History. The mini-stream spec's plan to port the schedule math into
  `hub/app/mtv_schedule.py` is marked superseded in ROADMAP: clients ask
  the site over HTTP instead.

### 5. Testing

- Unit (offline, existing screen-player test style): fake `/admin/api/now`
  server and a fake mpv IPC socket that records commands and can emit
  property-change events. Assert: spawn args carry `--idle=yes` and no file;
  the conductor's first two commands are `loadfile now replace -1
  start=<offset>` and `loadfile next append -1 start=0`; a `playlist-pos`
  event with `path` equal to `now.id` appends the new next with `start=0`
  and no replace; an event with a different `path` (or `time-pos` off by
  more than 2 s) issues the corrective replace; `mtv_now` failure leaves the
  queue untouched and logs once; `/status` reports `source: "mtv"` with
  title/subtitle; `/play` 502s with `mtv:` prefix when the endpoint is down.
- Unit (mtv repo): the JS-vs-Python schedule parity test in §1.
- Plan ordering: the mtv-repo endpoint ships and is deployed first; the
  screen-player tests stub it and never depend on the live site.
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
