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
- Accepted divergences from the browser player: (1) it skips ids that failed
  to play in that session (its `bad` set), so one viewer with a broken file
  can be a song off from the server; (2) when the remembered channel is
  empty it falls to the nearest channel with content, whereas the endpoint
  404s and the caller stays on the channel it asked for. Nothing else in the
  math is session-local.
- The admin page's NOW PLAYING panel switches to this endpoint and its
  JavaScript copy of the math is deleted. The panel lists every lineup
  channel, so it calls `/admin/api/now?ch=` once per channel (four calls,
  every 10 s, LAN only — fine). Count of implementations stays at two
  (browser player, Python) instead of growing to three.
- Access: unchanged Traefik `local-only@file` middleware on `/admin`. The Pi
  resolves `mtv.thelunadog.com` to the LAN address and gets 200 today.
- Test: `admin/test_schedule.py` runs a fixture manifest through the Python
  port and through the JavaScript functions under `node` (extracted verbatim
  from `mtv.js`) and asserts identical orderings for channels 1–4, identical
  on-air picks at three fixed timestamps, and identical `creditText` output
  for titles with and without `artist`/`track` fields. It runs on the dev
  box and in the repo's Forgejo CI job, both of which have `node`; the admin
  container does not and never runs it.

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
2. Stash `_mtv = {"base": base, "channel": channel, "offset": now.offset,
   "next": now.next}` for the conductor (`_stop()` clears it), then call the
   existing `_play(url={base}{url_base}{now.id}.mp4, headers=None,
   audio_only=False, profile="mtv", supervise=True, mode=DRM_MODE,
   title=now.artist, subtitle=now.song, source="mtv")`. Reusing `_play`
   is what resets `_stopped`, `_supervise`, `_headers`, `_audio_only`,
   `_mode` and the corruption counters the way every other source does;
   `_play_mtv` adds nothing to that state.
3. `_build_args(profile="mtv")`: the `live` flag set (`--vo=drm`, `--hwdec=no`,
   `--input-ipc-server`, small cache, `--sid=no`) minus `--hls-bitrate`, plus
   `--idle=yes`, `--prefetch-playlist=yes`, `--keep-open=no`, and OSD
   sizing constants (`--osd-font-size`, `--osd-margin-x/y`,
   `--osd-align-x=left`, `--osd-align-y=bottom`, `--osd-border-size`).
   **DRM mode is `DRM_MODE` (1920x1080), same as `live`**: the files are
   ≤1080p and the TV upscales; the OSD constants are sized for a 1080p
   framebuffer read from the couch on a 4K set. **No `--start` and no file
   on the command line**: `--start` is a per-file option and would apply to
   every appended song. Instead mpv starts idle and the conductor loads
   every entry with its own per-file `start` (`loadfile {url} replace -1
   start={offset}`, then `loadfile {next_url} append -1 start=0`). One
   mechanism for first song, next song and drift correction. (mpv ≥ 0.38
   `loadfile` syntax: `<url> <flags> <index> <options>`; the Pi runs 0.40.0
   — the plan pins this and verifies the first loadfile on the box.)
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
  `observe_property 1 time-pos` and `observe_property 2 idle-active`, read
  newline-delimited JSON forever. Replies to its own commands are matched by
  `request_id`; the `file-loaded` event and the two property changes drive
  the state machine below. Socket loss → thread exits; the supervisor's
  respawn creates a new conductor.
- `command(list) -> reply`: send on the same socket with a fresh
  `request_id`, block for the reply; a lock serialises writes, the reader
  thread routes replies. Commands used: `loadfile`, `show-text`,
  `get_property path`.

State machine (a "song" is identified by the id parsed from mpv's `path`,
which is the full mp4 URL; the conductor builds the expected URL from
`now.id` and compares URLs):

1. On connect: `loadfile now replace -1 start={offset}`; `loadfile next
   append -1 start=0`. Expect a `file-loaded` for `now`.
2. On `file-loaded` (fires once per song start, including after our own
   replace, and only once the file is actually open — so `time-pos` is
   valid): read `path`; call `mtv_now`; then read the **current** `time-pos`
   (after the HTTP round-trip, so network latency does not look like
   drift). If `path` is `now`'s URL and `|time-pos − now.offset| ≤ 2 s`: it
   is the right song at the right place — set `_title/_subtitle`, append
   the new `next` with `start=0`, show start credits. Otherwise (drift, or
   the library changed under us): `loadfile now replace -1 start={offset}`
   then append `next`; the ensuing `file-loaded` re-runs this step and
   passes. To rule out a replace loop on a persistently disagreeing
   server, at most one corrective replace per `file-loaded`; a second
   mismatch in a row is logged and left alone until the next natural
   boundary.
3. `mtv_now` failure in step 2: keep playing what is queued (mpv already has
   `next`), log once per failure streak, retry at the next `file-loaded`.
   `idle-active` becoming true (playlist ran dry because `next` was never
   appended) → retry `mtv_now` every 5 s until it answers, then step 1.
4. Credits: `show-text "{artist}\n{song}" 8000` in step 2; end credits fire
   once per song when `duration − time-pos ≤ 10` seen on the `time-pos`
   property stream (mpv throttles it to a few per second). Pause via
   `/control` therefore delays the credits correctly.

**Supervisor.** For `_profile == "mtv"`, on mpv death after the existing
settle delay call `_play_mtv(_mtv.base, _mtv.channel)` instead of
re-spawning `_url`, so a crash rejoins the broadcast. If that raises
`MtvUnavailable`, retry on each supervisor tick (3 s) for up to 60 s, then
give up: `_stop()` and `_kiosk("start")` (dashboard back, same as a movie
ending) and log the reason. The `live`-only decode-corruption and
video-freeze watchdogs do **not** apply to `mtv` (no HLS discontinuities);
stated here so the omission is deliberate.

**Deleted:** `MTV_KIOSK_SERVICE`, `_swap_kiosk`, and both `/kiosk/mtv` and
`/kiosk/idle` (the latter's only caller is the hub endpoint §3 deletes).
`_kiosk`, `_kiosk_status` and `_kiosk_restart` stay — they serve the idle
dashboard handoff and `/kiosk/restart`. The module docstring lines describing
the kiosk swap go too.

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
  2. Credits readable from the couch on the 4K TV (mpv outputs 1080p, the TV
     upscales); appear at song start and ~10 s before the end.
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
