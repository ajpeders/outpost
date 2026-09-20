# livingroom-pi roadmap

## Agent-sized TODO queue — 2026-09-18

These cards break selected existing priorities and observed gaps into small tasks.
They are the execution queue; the broader roadmap below remains product context.
Pick one card per change. Paths and commands are relative to this project root;
`(new)` marks a file to create. Read applicable `AGENTS.md` first. Check whether the
work has already landed before editing. If so, cite the implementation and checks
instead of rebuilding it. Install dependencies using this project's documented setup.

`ready` means no product decision is needed, not that every tool is installed.
Honor explicit dependencies and blocked/parked labels. Do not expand a card into an
architecture rewrite. If a contract or prerequisite is missing, record the blocker.
Mark a card complete only with its acceptance evidence; report changed files, checks
run, and remaining limitations. These TODOs do not authorize deployment, publishing,
live messages, or changes to production data.

- [x] **HOME-01 — Return a failing exit status from the smoke client on API failures** ✅ *(2026-09-18)*
  - **Why:** test-client.py displays errors, but its request helpers discard success/failure outcomes.
  - **Start here:** test-client.py, HOWTO.md, tests/test_client.py (new).
  - **Do:** Track failures across the selected command’s HTTP requests and return nonzero for connection failures or non-2xx responses. Preserve readable output and the default read-only sweep. Add stdlib unittest cases using mocked urllib responses.
  - **Done when:** Run python3 -m unittest discover -s tests -p test_client.py. All-success exits 0; HTTP/connection failures exit nonzero, including one failure in a multi-request sweep. Tests never contact the Pi or issue hardware commands.
  - **Evidence:** `test-client.py` counts failures and returns 1 (unknown command still 2); `tests/test_client.py` (7 stdlib unittests, mocked `urlopen`) passes. Live sweep exits 0; a dead endpoint exits 1.

- [ ] **HOME-02 — Finish the already-built Plex-on-Apple-TV acceptance check** (blocked: Apple TV + Plex player advertising)
  - **Why:** Current priorities identify the on-device Advertise as Player toggle as the missing prerequisite.
  - **Start here:** HOWTO.md, ROADMAP.md, hub/app/plex.py, hub/app/main.py.
  - **Do:** After the owner enables player advertising, verify the client is visible and run one chosen library item through the existing ATV button. Record resolved media/version, successful playback, and stop behavior. Distinguish route failure from client discovery failure.
  - **Done when:** A dated manual result confirms the intended file/version plays on Apple TV. If advertising is absent, retain blocked status. Do not revive the skipped Plex-alarm or dropped presence-automation work.

Self-contained living-room controller on a Raspberry Pi 5. **Off the homelab** —
survives homelab reboots. Apple TV stays the streaming brain; the Pi controls
the TV set (CEC), hosts the hub app, and runs scheduled automations.

## Status

- **Phase 0 — Pi online** ✅ Pi 5 up on the LAN (static LAN address, same MAC).
- **Phase 1 — Access + wifi** ✅ SSH access in; wifi up on `wlan0`,
  ethernet no longer required at the TV.
- **Phase 2 — Deploy + validate** ✅ Stack deployed and running on the Pi
  (`docker compose`, 3 containers). Companion **paired** (app list + now-playing
  live). CEC verified on real `/dev/cec0` (adapter detected, TV power reporting).
- **Phase 3 — Music-playing alarm** ✅ *(built)* Scheduler runs in the hub; alarms
  persist to `data/hub/alarms.json`. Full CRUD + toggle + **test** via `/api/alarms`,
  settable from the phone UI. Firing wakes the TV (CEC) and plays via one of five
  sources: **appletv_music** (launch app + play/pause over Companion), **airplay**
  (pyatv `atv.stream` to the Apple TV), or the Pi-side **livestream / file / shuffle**
  (mpv screen player). Ramp/volume per alarm. Day-of-week scheduling works end to
  end (empty `days` = every day, by design; UI shows it as "Every day").
- **Phase 4 — On-TV ambient dashboard** ✅ *(built)* `screen/` ships an mpv
  player + aerial overlay/kiosk services and `hub/app/static/dashboard.html`.
  Glanceable clock/weather/now-playing/alarm kiosk; phone-first, no TV input
  required. Built: 1080p-friendly ambient stills, Apple aerial overlay mode,
  weather caching through the hub, transparent cursor repair, persistent
  chromium overlay renderer, and optimized service handoff with screen-player.
- **Phase 5 — Scenes expansion** ✅ *(built)* Data-driven via `data/hub/scenes.json`
  (falls back to `scenes.DEFAULT_SCENES` in code if missing/invalid). The hub now
  exposes `GET /api/scenes` (list) + `POST /api/scenes/{name}/run` (execute).
  The web app renders one button per scene under the Alarms panel; each click
  fires the step sequence and toasts the result count. Shipped: **Movie Night**
  (CEC on → ATV on → launch Plex), **YouTube**, **Everything Off**. Add more in
  the JSON — `{label, steps: [{svc, method, path} | {delay}]}` — no rebuild.
- **Phase 6 — Live-stream transport controls** ✅ *(built)* Hub now exposes
  `POST /api/jetstream/skip` + `GET /api/jetstream/capabilities` (the latter tells
  the UI whether skip is wired). The web app's Now-Playing card shows a **⤵ Skip**
  button on the live row; the hub POSTs `JETSTREAM_SKIP_URL` with the `lt` viewer
  cookie so it advances the queue the same way a regular browser viewer does.
  mpv / the Apple TV pick up the new HLS playlist automatically. Set
  `JETSTREAM_SKIP_URL` + `JETSTREAM_COOKIE` in `.env`; leave blank to hide the
  button.
- **Phase 7 — Alarm: pick a specific album / playlist** ⏳ *(requested)* Today the
  appletv_music alarm just launches Apple Music and hits play (resumes the last
  thing). Companion is remote-control only — it **can't** address a specific Apple
  Music album/playlist. **Plex can**: the hub already browses/plays the Plex music
  library (`/api/plex/*`), so add a **plex** alarm source that plays a chosen
  album/playlist — AirPlay-streamed to the Apple TV, or via the Pi screen player.
  UI: a picker in the alarm editor. (Plex reachable; music section present, but 0
  playlists defined yet — albums work regardless.)

- **Aerials on the dashboard** ✅ *(built)* Apple's tvOS aerials play behind the
  clock/weather. In-browser `<video>` was a dead end — cage/chromium won't paint a
  video surface even with the GPU on (verified with correct ANGLE/GLES flags), a
  Wayland limitation, not GPU. So `screen/aerial-mode.py` does it the reliable way:
  **mpv plays the cached aerials on DRM (Pi-5 hardware decode, `hwdec=v4l2m2m`)** and
  the dashboard is composited on top as a **transparent overlay** — headless
  chromium renders `/dashboard?overlay=1` to a PNG, converted to BGRA and pushed via
  mpv `overlay-add`, refreshed each minute (seconds hidden in overlay mode). Runs as
  `aerial-screen.service` (alternative to the cage `kiosk-screen`; set
  `SCREEN_KIOSK_SERVICE=aerial-screen` so the screen player's mpv handoff stops the
  right one). Clips cached by `screen/fetch-aerials.sh` (`RES=720` to downscale).

- **Perf pass (2026-07-19)** ✅ Overlay renders are now cache-first: the dashboard
  caches IP geolocation for 24h and skips the weather fetch when the cache is
  <10 min old, so the per-minute chromium render no longer hits ip-api +
  open-meteo every cycle (~1,440 external calls/day → ~150). Phone UI: alarm
  list only re-renders on data change (no more 4s DOM churn eating button
  states), and polling pauses while the app is backgrounded. Shuffle caches the
  SMB file walk (5 min TTL) so alarm-time shuffles don't re-crawl the share.
  Container logs capped (5m × 3 files; they'd hit 85MB unrotated in 12 days).

## QoL round (2026-07-20) ✅ *(all built + deployed)*

- **Sleep timer** — `/api/sleep-timer` (set/status/cancel) + a 30/60/90-min
  panel in the UI; firing stops Pi playback + AirPlay, powers off the Apple TV
  and stands the TV down (talks to the screen service directly so it doesn't
  re-wake the TV).
- **Auto-off after midnight** — hub loop: 00:00-06:00 (env-tunable), TV on but
  only the idle dashboard showing (Pi input, nothing playing) for two 5-min
  checks → CEC standby, then a 1 h cooldown so turning it back on wins.
- **Resume watching** — media mpv now quits gracefully (IPC) so
  `--save-position-on-quit` lands in `data/screen/watch_later`; the library UI
  shows a "Continue watching" row (`/api/media/resume`). mpv clears entries
  when a file finishes.
- **Plex queue transport** — queue state machine with `/api/plex/queue`
  (+`/next`, `/prev`); the hero shows track x/y with Prev/Next/Stop when a
  playlist is AirPlaying. *(Verify next/prev feel during real listening.)*
- **Now-playing artwork** — appletv `/api/artwork` (cached by artwork_id);
  album art in the phone-UI hero for Apple Music / Apple TV playback.
  *(Verify with real playback.)*
- **Time-of-day aerials** — `classify-aerials.py` tags clips day/night by
  luminance (10-bit-aware) into `timeofday.json` (runs from fetch-aerials);
  aerial-mode plays day clips 07-19 h, night otherwise, swapping the playlist
  live over mpv IPC. Current cache: 6 day / 6 night.
- **Weather through the hub** — `/api/weather` (server-side IP-geolocation 24 h
  + open-meteo 10 min cache, stale-on-error); dashboard fetches same-origin
  with the old direct path as fallback. Overlay renders are now LAN-local.
- **Persistent overlay renderer** — aerial-mode drives ONE long-lived headless
  chromium over CDP (`--remote-debugging-pipe`, fd 3/4): reload → readyState →
  screenshot ≈ 3 s/cycle vs 8-15 s cold starts; auto re-attach on session loss,
  restart on wedge, cold one-shot as last resort.
- **Health panel** — `/api/health` (appletv/cec/screen/plex + disk, CPU temp,
  load, uptime) rendered as a System panel with status dots in the UI.
- **Shuffle no-repeat** — last-20 picks persisted (`data/screen/
  shuffle-history.json`) and excluded until the pool runs dry.
- **PWA** — manifest + generated icons + apple-touch/standalone meta; "Add to
  Home Screen" now installs it app-like. Favicon fixed too.
- **CEC speed/reliability** *(user ask)* — cec service rewritten from libCEC
  `cec-client` (~10 s cold start per command) to kernel-API `cec-ctl`:
  ~0.7 s per command, 15 ms cached status. Both host + container now assert
  the same playback-device config, ending the "adapter reset to unregistered"
  flakiness. Volume target env-tunable (`CEC_VOLUME_TARGET=5` for a soundbar).
- Also fixed en route: `/api/scenes` 500 (the `_comment` key in scenes.json
  broke the list), media titles prettified ("Hokum (2026)", "Show · S01E03")
  in hero/dashboard/resume rows, alarm rows no longer squish on narrow phones,
  idle hero copy, UI tested headless end-to-end (13 interactive checks).

## Phase 8 — MTV on the TV (2026-09-19) ✅ *(built + deployed)*

- **MTV button** — `POST /api/screen/mtv` wakes the TV and asks screen-player's
  `mtv` profile to join channel 1. `GET /api/screen/status` retains the `mtv`
  capability flag, and the phone shows only live-style controls for MTV.
- **Synchronized mpv playback** — screen-player asks
  `GET /admin/api/now?ch=1` for the current item, wall-clock offset, next item,
  and credits. mpv starts idle on DRM, loads each MP4 with its own `start`
  option, preloads the successor, and rechecks at every boundary. It corrects
  drift over two seconds, retries schedule outages without dropping a queued
  item, rejoins after a crash, and returns to the dashboard after 60 seconds of
  failed recovery.
- **History:** the original `mtv-screen` cage/Chromium kiosk was removed. It
  software-decoded video poorly, had no audio, and exposed a cursor. Chromium
  remains useful for static dashboard rendering but is not in the MTV path.
- **Deployed and accepted:** the MTV schedule endpoint and Pi consumer are live.
  Startup and a natural song boundary stayed within 1.7s of the authoritative
  schedule with AAC audio, no black gap, no DRM playback error, and zero frame
  or decoder drops. mpv used 82% of one CPU core for 1080p software decode
  (above the planned 60% check, but with clean playback and ample total Pi
  headroom). Normal stop restored `aerial-screen` and the dashboard.
- **Native 4K follow-up:** primarily blocked on the MTV mirror producing a 4K
  rendition alongside its browser-compatible 1080p files. Once available,
  teach the `mtv` profile to select 2160p, choose a 4K DRM mode and viable Pi 5
  hardware-decode path, scale OSD credits for the 4K framebuffer, and measure
  frame drops, CPU/GPU load, thermals, and song-boundary behavior. Keep 1080p
  as the fallback if native 4K cannot play cleanly.
- **MTV cache sizing:** observed a brief remote-MP4 buffering event on
  2026-09-19 where the 4-second cache underran, the cache refilled, and
  playback recovered without a restart. Investigate raising the `mtv`
  profile's cache and `demuxer-readahead-secs` to 20–30 seconds and verify
  this stays below the live HLS path's behaviour, then validate on a long
  song-boundary through one full track.
- **MTV persistent on-screen credits:** today's `show-text` call from the
  `MtvConductor` displays artist/song for 8 seconds at song start and once
  near the end. Users want the credits to stay on screen for the entire
  track. Replace the timed fade with a persistent, low-contrast OSD that
  re-asserts on every schedule refresh, hides gracefully on stop, and
  remains readable from the couch on the 4K framebuffer (font size, margin,
  outline sized for 1920x1080 today; review for 2160p later). Verify that
  the persistent OSD does not conflict with mpv's normal status line and
  that the conductor's IPC activity stays cheap enough for long tracks.
- **TV sleep while streaming:** the CEC `tv-keepalive` unit only acts when
  the Apple TV sleeps, not during Pi-side playback; the TV appears to drop
  to standby during a long MTV/jetstream session anyway. Verify on the Pi
  whether the TV's CEC wakeup is being driven (keepalive running, ATV
  asleep, recent `tv-reclaim.sh` calls), and decide whether playback should
  re-assert Pi active-source or call `image-view-on` while mpv is on. Likely
  fix: a small “active playback” extension to `tv-keepalive.py` that holds
  the TV on whenever `screen_player` reports `playing: true`, regardless of
  ATV state.

## UI overhaul (2026-07-20) ✅ *(built + deployed)*

- Controller visual system rebuilt around a stronger now-playing command hero,
  warmer mixed-accent palette, denser card rhythm, clearer active states, and a
  two-column operations layout on desktop that still collapses cleanly on phones.
- Existing JavaScript hooks and API behavior preserved; this was a UI shell
  overhaul, not a feature rewrite.
- Metadata cleanup added while testing: placeholder strings like `"None"`,
  `"null"`, and `"undefined"` are filtered before they can appear as a
  now-playing subtitle.
- Deployed with hub rebuild; `/healthz`, `/api/health`, `/api/screen/status`,
  and the root page all verified after deploy.

## Speed + homelab round (2026-07-31) ✅ *(built + deployed)*

- **Instant TV input switching** — switching to the Apple TV used to *release*
  the Pi's active-source claim and wait for tvOS to grab the input (5-10 s, and
  it stalled outright if the ATV was already awake). Now the hub sends CEC **Set
  Stream Path** straight to the ATV's HDMI port (`ATV_HDMI_INPUT`, default 3, via
  the screen player's `/tv/input`) and wakes the ATV in parallel. Measured
  **~0.8 s** both directions; `play_on_atv` and the Apple Music one-tap use the
  same path.
- **Dashboard comes up in 2.7 s instead of 4.9 s** (2026-08-01) — the aerial video
  is on screen in 0.2 s, but the overlay (clock/weather/homelab) lagged ~5 s
  behind it, and that render was mostly *sleeping*: a flat `sleep(SETTLE)`=2 s
  plus a 0.5 s post-reload sleep were 2.5 s of a 3.0 s warm render. Both are now
  a readiness poll — capture as soon as the clock has text and the weather temp
  has arrived (100 ms poll, 3 s cap, old fixed wait as fallback). Cold render
  3.7 s → 1.8 s, warm 3.0 s → 0.8 s. Each render now logs its duration and
  whether the renderer was warm.
- **Supervisor says what it did** (2026-08-03) — self-healing used to be
  invisible (only a new mpv PID in the journal), so diagnosing a night of
  livestream weirdness meant diffing process ids against timestamps. Each rejoin
  now logs its trigger and budget spend, plus budget-exhausted / refilled / mpv
  exited / playback finished: `journalctl -u screen-player | grep 'screen:'`.
  It paid off within a minute: the mode that actually fires is **not** decode
  corruption but mpv's video wedging while audio keeps running. That path now
  ignores stalls while `paused-for-cache` (a buffering hiccup stalls the frame
  counter too, and rejoining through one turns a stutter into a restart), which
  makes a 2-read (~6 s) trigger safe — frozen picture ~11 s → ~6 s.
  *(mpv's IPC socket is root-owned: querying properties as a normal user
  silently returns None for everything.)*
- **Livestream no longer corrupts permanently** (2026-08-01) — jetstream starts a
  new ffmpeg run (new fMP4 init segment) at every title change, ~every 20-30 min;
  mpv carries the stale init across the discontinuity, so the picture smears and
  only a rejoin clears it. The rejoin budget was 3 per playback and never
  refilled, so the third episode boundary of the evening left a permanently
  corrupted picture (8574 decode errors in one session; the source segments were
  verified decoding clean). Budget now refills after 300 s of healthy playback
  (`SCREEN_CORRUPT_WINDOW`).
- **Volume from the phone for Pi playback** (2026-08-01) — screen player
  `/control` gained `volume` ({level} absolute 0-130 / {step} relative) and
  `mute`, and reports `volume`/`muted` in `/status` for live as well as media.
  The now-playing hero gets a slider + mute button — absolute and instant, where
  the TV's CEC volume is coarse relative stepping with a ~0.5 s round trip and no
  readout.
- **Livestream starts in ~1 s instead of ~57 s** (2026-08-01) — the hub handed
  mpv jetstream's *master* playlist, so ffmpeg probed all three renditions before
  playing (6.71 s to open vs 0.68 s for a single variant) and that negotiation
  could outlast the 24 s segment window: the first segment it asked for was
  already deleted, `avformat_open_input` failed, and mpv only recovered by
  re-parsing the master as a plain m3u. The hub now resolves the
  highest-bandwidth rendition itself (cached 5 min, falls back to the configured
  URL) for both the Pi screen player and the Apple TV AirPlay path.
- **CEC status polling no longer hammers the bus** — the UI polls TV state every
  10 s but the cec cache expired after 5 s, so *every* poll ran a real bus query:
  0.8 s per call, and each one re-registers the shared `/dev/cec0` (the known
  cause of input-switch flakiness). TTL is now 45 s (`CEC_STATUS_TTL`), power
  commands publish the state they just caused, and the hub invalidates the cache
  after a host-side switch so nothing goes stale. `/api/health` went from
  **0.85 s → 0.03 s**.
- **Homelab stats on the TV dashboard** — `/api/homelab` gained an SSH probe
  (load→CPU%, RAM%, media-pool disk, hottest sensor) alongside the existing
  latency / Plex-sessions / jetstream-live info. `openssh-client` in the hub
  image, `~/.ssh` mounted read-only at `/ssh`, `HOMELAB_SSH` env (empty disables
  it). Overlay line reads e.g. `server · 15ms · cpu 16% · ram 51% · 1.8/4.5T · 37°
  · Plex idle · ● <live title>`.
- **Wifi drop fix** — the Pi kept becoming unreachable over SSH until a reboot:
  `wlan0` power-save was **on** (the brcmfmac radio naps through inbound
  traffic). Disabled live and persistently via
  `/etc/NetworkManager/conf.d/wifi-powersave-off.conf` (`wifi.powersave = 2`).
  The journal is now persistent (`/var/log/journal`) so a recurrence leaves
  evidence; PSU is clean (`throttled=0x0`). Pi is wifi-only — ethernet or a DHCP
  reservation for .220 would make it bulletproof.

## SMB media library mount (2026-09-18) ✅ *(set up on the Pi)*

The library browser and the `file`/`shuffle` sources were returning `400` on
`/api/media/list` because the SMB share wasn't mounted (no `cifs-utils`, no
fstab entry). The read-only CIFS mount is now set up on the host:

- `apt-get install cifs-utils`; credentials in `/etc/smarthome-smb.credentials`
  (root-only, `username=ween`).
- `/etc/fstab`: `//192.168.0.176/share /mnt/share cifs …,ro,_netdev,nofail,
  x-systemd.automount,x-systemd.mount-timeout=15s` (pre-change fstab backed up
  to `/etc/fstab.before-smarthome-library`).
- `systemctl start mnt-share.automount`; first access mounts it. Verified
  `findmnt`, `/api/media/list` (root + nested), `/api/media/resume`, and an SMB
  file read all return 200.
- Host-only: the hub proxies `/api/media/*` and never sees the mount. Library
  root is `SCREEN_MEDIA_ROOT` (default `/mnt/share/media`).
- Docs: HOWTO "Mount the media library (SMB/CIFS)", README dashboard section,
  ARCHITECTURE responsibilities/decisions.
- **Covers + seasons (2026-09-18):** the browser now shows Plex posters
  (`/api/media/poster`, token stays server-side) and presents flat TV episodes as
  one `Season N` folder per season (tap to open, episodes ordered by number,
  client-side regex). Continue-watching rows gained a ✕ that clears the saved
  resume point (`/api/media/resume/forget` → screen player deletes the
  watch-later file).

## TV dashboard rework (2026-09-18) ✅ *(built + deployed)*

An audit found the rebuilt Pi's dashboard degraded and the layout broken at TV
sizes. Implemented per
`docs/superpowers/specs/2026-09-18-tv-dashboard-rework-design.md`:

- **Baseline:** host timezone set to `America/Denver` to match the hub `TZ` (the
  host-rendered clock was 7 h ahead); aerials fetched; `screen-player` drop-in
  `SCREEN_KIOSK_SERVICE=aerial-screen`; `aerial-screen` enabled.
- **Layout:** `dashboard.html` rebuilt as an info board — top band (clock ~9vw +
  date) and a bottom band of glass tiles (forecast `flex: 2`, carrying the current
  conditions above the 7-day grid; server / media / alarm / now-playing
  `flex: 1`). The 7-day forecast is a `repeat(7,1fr)` grid inside its tile, so it
  no longer runs off the right edge. Verified no overflow at 1920x1080 and
  3840x2160; top and bottom bands share the same margins.
- **Seconds:** the page reserves an empty fixed-width `#sec` slot (zero height);
  `aerial-clock.lua` draws the live seconds (now DejaVu Sans bold, `\an5`) centred
  on the AM/PM line at `AERIAL_SEC_X/Y/FS` = 1133 / 351 / 115 at 4K.
- **Data:** weather is hub-only (the direct open-meteo / ip-api fallback is gone);
  homelab and weather are cache-first in `localStorage`; `HOMELAB_TTL` 20s → 120s.
  Tiles hide per explicit rules (never blank).
- **Media tile (2026-09-18):** shows the active Plex stream's poster
  (`/api/plex/artwork`), falling back to the livestream title looked up in Plex
  by name; plus the livestream title with position/duration + a progress bar. The
  hub caches the last-good livestream state and serves it stale on a title-API
  blip so it persists.
- **Server tile (2026-09-18):** labelled with the host name; a 3x2 grid of
  icon + value readouts (ping / CPU / RAM / disk% / temp / uptime). CPU is the
  real load (uncapped, can exceed 100%), disk is a percentage.
- **Simplification:** the Unsplash photo rotation, `<video>` aerial path and Ken
  Burns animation are removed; non-overlay is a static aurora gradient.

## Play on Apple TV via Plex (2026-07-31) 🟡 *(built + deployed, blocked on one toggle)*

Real 4K HDR/DV playback path: the Pi's V3D can't scan out 10-bit HDR above
1080p, so `screen_player` downshifts those files. This hands the *same library
file* to the Apple TV's Plex app instead, which decodes 4K DV natively.

- `POST /api/plex/play_on_atv {path}` + a per-file **ATV** button in the library
  UI. Resolves a share-relative path → Plex `ratingKey` (exact Part-file match,
  then basename, then movie-folder name; 10-min cache), forces the TV to the ATV
  input, launches Plex, waits for the client, sends `playMedia` directly to it.
- **Blocked:** the Plex tvOS app must have **Settings → Advertise as Player**
  enabled once, with the remote. Until then the endpoint 504s with that hint.
- Ruled out while trying to automate it: the app is running and signed in as the
  same account as `PLEX_TOKEN` (not an account mismatch); server `/clients` and
  `plex.tv/api/v2/resources` both show no player, so it genuinely isn't
  advertising; launching the app doesn't enable it; and a
  `plex://preplay?metadataKey=…` deep link through the new
  `POST :8010/api/open_url` (pyatv accepts a URL in place of a bundle id) is
  taken by tvOS but ignored by the Plex app — nothing reaches the server.
- Debugging channel that works: the Plex server log on the Plex host shows exactly what
  the ATV app requests —
  `docker exec plex sh -c "grep -a <apple-tv-ip> '/config/…/Logs/Plex Media Server.log'"`.
  (pyatv `/api/state` reports the *now-playing* app, not the foreground one, so
  it can't confirm a launch.)
- Library note: `_4k_archive` REMUXes weren't Plex-indexed, so the folder
  fallback played the 1080p copy. Marty Supreme's REMUX was moved into
  `movies/` **server-side** (the Pi's `/mnt/share` CIFS mount is read-only) and
  Plex rescanned — it's now a 4K + 1080p version pair and resolves as 4k.
  `_4k_archive` is empty; put future 4K rips straight into `movies/`.

## Dashboard mini stream (2026-09-19) 📋 *(planned, spec written, no code)*

A small live video in the top-right corner of the aerial dashboard, picked
from the phone: **Off | Livestream | MTV**, with a **Sound** toggle. Movie
(library file) deferred to v2. Design: `docs/superpowers/specs/2026-09-19-dashboard-mini-stream-design.md`.

Key constraint: Chromium can't paint video on the Pi, so mpv draws the corner
stream. mpv draws one video, so by default the **aerials pause** while the
mini stream is on and the aurora gradient fills in behind the board.

Steps, in order:

1. **Spike** — measure `lavfi-complex` compositing (1080p aerial + corner
   stream) on the Pi. Decides whether aerials can keep playing. Record the
   numbers here.
2. **MTV resolution** — ask the site's `/admin/api/now` endpoint; do not add a
   third copy of the schedule math in the hub. The screen-player MTV profile
   already uses this authority.
3. **Hub API** — `GET/POST /api/dashboard/mini`, persisted `data/hub/mini.json`,
   resolves URL/start/geometry.
4. **`aerial-mode.py`** — 5 s tick polling the hub; swap between aerial
   playlist and the corner stream; audio enabled-but-muted at launch.
5. **`dashboard.html`** — `mini=1` overlay mode: aurora background with a
   transparent hole, weather shifts left, source label, title in Media tile.
6. **`index.html`** — segmented control + sound toggle in the Watch panel.
7. **Acceptance on the Pi** — checklist in the spec; update ARCHITECTURE.md
   (dashboard/overlay pipeline) and HOWTO.md when built.

## Dashboard first-frame + Jetstream progress (2026-09-19) ✅ *(built + deployed)*

Alex saw empty server/media tiles right after switching the TV back to the Pi.
Cause: `aerial-mode.py` captured the first overlay as soon as the clock and
weather had painted, before `/api/homelab` (cold hub cache → SSH + Plex index,
seconds) returned; that empty frame then sat on the TV for the 60s refresh.

- `_painted()` now also waits (≤6s) for the homelab tile to settle.
- Media tile says **Jetstream** (+ live dot) over the title; "Plex idle" is
  gone — the Plex line only appears when a Plex session is actually streaming.
- `/api/homelab` `live.sampled_at` + page-side `tickLive()`: the position and
  progress bar advance locally every second instead of freezing for up to 2
  minutes (120s probe cache) — every overlay redraw now shows the true time.
- Livestream dropouts, both causes closed the same night: jetstream's idle
  watcher didn't count LAN viewers (fixed in the jetstream repo, 7dc6293), and
  the Pi's DNS fell back past AdGuard to public resolvers (fixed with an
  `/etc/hosts` pin on the Pi — HOWTO *Set up a fresh Pi*).
- Follow-up (not done): the TV overlay is a PNG redrawn once a minute, so on
  the TV the readout still steps per minute. Live ticking would need
  `aerial-clock.lua` to draw the progress line like it draws the seconds.

## Current Priorities

1. **Finish the ATV Plex path** — flip Advertise as Player on the Apple TV, then
   run the first end-to-end `play_on_atv` (see section above; everything else is
   built and verified).
2. **Phase 7: Plex alarm source** — add a Plex album/playlist picker to the
   alarm editor and fire it through the existing Plex queue/AirPlay path.
   *(User: skipped for now.)*
3. **Real-listening validation** — exercise Plex queue next/prev/stop and Apple
   TV artwork during a real playlist session; ROADMAP still calls these out as
   verify-needed.
4. **Dashboard mini stream** — run the step-1 spike, then build per the
   2026-09-19 spec (section above).
5. **Look into music now-playing on the TV dashboard** — surface what's playing
   (Apple Music via the Apple TV, and Plex music) on the dashboard overlay, not
   just Pi media. The appletv service already exposes now-playing + artwork
   (`/api/atv/artwork`), so the media/now-playing tile could show it.

## Dropped

- **Pi as media player (Kodi/Jellyfin)** — the Apple TV handles streaming via its
  own apps, so the Pi doesn't need to be a player.
- **Presence-driven automation (Hailo-8)** — camera + person detection driving
  auto TV on/off / pause-on-leave. Dropped 2026-08-28; don't re-propose it.

## Built so far (deployed + running on the Pi, off-homelab)

- `appletv/` — pyatv API: remote, now-playing, app list/launch, power, AirPlay
  **stream**, volume, and **Companion + AirPlay pairing endpoints**.
- `cec/` — HDMI-CEC TV control (power, volume, active-source handoff) on `/dev/cec0`.
- `hub/` — the app: unified UI (D-pad, apps, TV controls, pairing, scenes) + API
  proxy + **scene engine** + **alarm scheduler** + **Plex** browse/search/play +
  Pi-side **screen player** control.
- `screen/` — mpv media player + cage kiosk service + cursor-hiding support +
  `dashboard.html`.
- `test-client.py` — stdlib smoke-test client.

---

## Roadmap — parked / future

**Deferred:**
- **Atmos / DD+ / TrueHD passthrough** on the Pi (`mpv --audio-spdif`) — the one
  Dolby thing the Pi might do (HDMI bitstream to the TV/soundbar); currently we
  decode to PCM. Depends on the audio chain accepting it. (User: skip for now.)
- **YouTube videos / playlists + autoplay** — play a YouTube URL or playlist and
  auto-advance. Two possible paths: (a) on the Pi via `yt-dlp` → mpv (full
  transport control, but no YouTube app niceties), or (b) hand off to the Apple
  TV's own YouTube app over Companion (open app + drive it) — **preferred**, keeps
  playback on the streaming brain. Later. (User: maybe go through the Apple TV app.)

**Pi-native workload ideas** (better on the Pi than the homelab — they need the
Pi's physical presence at the TV / in the room, or homelab-independence).
Homelab keeps: heavy transcoding, storage, services.
1. **On-TV ambient dashboard (light head)** — cage/labwc kiosk showing
   clock/weather/now-playing/home status when idle. The Pi *is* the display.
2. **Universal AirPlay/Cast receiver** (uxplay/rpiplay) — throw phone/laptop
   content to the TV via the Pi.
3. **IR blaster** (GPIO) — control soundbar/receiver/older gear CEC can't.
4. **Local voice / wake-word** control for the room.
5. **Retro emulation** (RetroPie) at the TV.
6. **Doorbell/camera feed → TV overlay** (vision + display).

## Make this usable by others (added 2026-08-27)

- [x] Universalize the README / docs / code for outside users (2026-08-27):
  homelab-specific defaults are now env-driven and blank by default — compose
  `PLEX_URL`/`HOMELAB_SSH` empty (features self-disable), hub `TZ` defaults UTC,
  the `/ssh` mount is `${HUB_SSH_DIR:-./data/ssh}`. Local deployment keeps its
  real values in the gitignored `.env`. Personal `/home/alex/...` paths in
  `screen_player.py` / `tv-keepalive.py` are now script-relative; the systemd
  units carry a `%%REPO%%` token filled by `screen/install-services.sh` at
  install time, so the checkout is runnable wherever it's cloned. README gained
  an env-config rundown + a host-services install section; `.env.example`
  documents the homelab/ssh/media vars. (`livingroom-pi:` rsync target noted as
  a user-defined SSH alias.)

- [x] Second pass — legal, prerequisites, de-personalization (2026-08-28). An
  audit found the repo still unusable by a stranger in four ways:
  - **No LICENSE** — nobody had the legal right to use it. Now MIT.
  - **Owner's LAN in code defaults** — `plex.py` defaulted `PLEX_URL` to the real
    Plex server and `.env.example` shipped the real Apple TV IP as a pre-filled,
    working-looking value, so a cloner who skipped editing had their Pi probing
    someone else's network. Both blank now; `alarms.py` TZ default Denver → UTC;
    the `live.thelunadog` livestream regex reduced to `.m3u8`; `aerial-mode.py`
    uses `socket.gethostname()` instead of the hardcoded `pi5` room label.
  - **`.env.example` promised things it couldn't deliver** — `ATV_HDMI_INPUT`,
    `CEC_*`, `AUTO_OFF_*`, `HOMELAB_SSH_KEY`, `HOMELAB_DISK` were read by the
    code but never forwarded by compose, so setting them did nothing. Now wired,
    each with a default matching the code's — note several are parsed with
    `int()`/`float()`, so a bare `${VAR:-}` passthrough would crash the hub at
    import; compose's `:-` treats empty as unset, which keeps a blank `.env`
    line safe.
  - **README claimed `.env` configures the host services** — it does not; that
    file is Compose-only and no unit has an `EnvironmentFile=`. Corrected to
    `systemctl edit`, and the README gained a Requirements section (Pi 5, Docker,
    CEC-capable TV, `apt install` line), a `/dev/cec0` troubleshooting path, the
    fact that DRM mode indices are display-specific, and a note that `ROADMAP.md`
    and `CLAUDE.md` are internal.
  Also: `fetch-aerials.sh` defaulted to Apple's entire catalogue at 4K onto the
  SD card (`AERIAL_MAX=0`) — now 12. The watchdog is untracked and gitignored,
  its targets being purely the author's infrastructure. Verified by resolving
  `docker compose config` against an unedited `.env.example`: every numeric var
  non-empty, Plex correctly disabled, and the deployed values unchanged.
