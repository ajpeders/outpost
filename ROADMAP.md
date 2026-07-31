# livingroom-pi roadmap

Self-contained living-room controller on a Raspberry Pi 5. **Off the homelab** —
survives homelab reboots. Apple TV stays the streaming brain; the Pi controls
the TV set (CEC), hosts the hub app, and runs scheduled automations.

## Status

- **Phase 0 — Pi online** ✅ Pi 5 up on the LAN (`pi5`, 192.168.0.219 / .220, same MAC).
- **Phase 1 — Access + wifi** ✅ SSH access in; wifi up on `wlan0` (192.168.0.220),
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
- **Homelab stats on the TV dashboard** — `/api/homelab` gained an SSH probe
  (load→CPU%, RAM%, media-pool disk, hottest sensor) alongside the existing
  latency / Plex-sessions / jetstream-live info. `openssh-client` in the hub
  image, `~/.ssh` mounted read-only at `/ssh`, `HOMELAB_SSH` env (empty disables
  it). Overlay line reads e.g. `isis · 15ms · cpu 16% · ram 51% · 1.8/4.5T · 37°
  · Plex idle · ● <live title>`.
- **Wifi drop fix** — the Pi kept becoming unreachable over SSH until a reboot:
  `wlan0` power-save was **on** (the brcmfmac radio naps through inbound
  traffic). Disabled live and persistently via
  `/etc/NetworkManager/conf.d/wifi-powersave-off.conf` (`wifi.powersave = 2`).
  The journal is now persistent (`/var/log/journal`) so a recurrence leaves
  evidence; PSU is clean (`throttled=0x0`). Pi is wifi-only — ethernet or a DHCP
  reservation for .220 would make it bulletproof.

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
- Debugging channel that works: the Plex server log on isis shows exactly what
  the ATV app requests —
  `docker exec plex sh -c "grep -a 192.168.0.39 '/config/…/Logs/Plex Media Server.log'"`.
  (pyatv `/api/state` reports the *now-playing* app, not the foreground one, so
  it can't confirm a launch.)
- Library note: `_4k_archive` REMUXes weren't Plex-indexed, so the folder
  fallback played the 1080p copy. Marty Supreme's REMUX was moved into
  `movies/` **server-side** (the Pi's `/mnt/share` CIFS mount is read-only) and
  Plex rescanned — it's now a 4K + 1080p version pair and resolves as 4k.
  `_4k_archive` is empty; put future 4K rips straight into `movies/`.

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
4. **Presence automation spike** — Hailo/person-detection remains the most
   valuable Pi-native future item once the room-control surface settles.

## Dropped

- **Pi as media player (Kodi/Jellyfin)** — the Apple TV handles streaming via its
  own apps, so the Pi doesn't need to be a player.

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
Pi's physical presence at the TV / in the room, the idle Hailo-8, or
homelab-independence). Homelab keeps: heavy transcoding, storage, services.
1. **Presence-driven automation (Hailo-8)** — camera + on-device person
   detection → auto TV on/off (CEC), pause-on-leave / resume-on-return, scenes.
   The killer app for the currently-idle 26-TOPS accelerator; also fixes the
   idle black screen.
2. **On-TV ambient dashboard (light head)** — cage/labwc kiosk showing
   clock/weather/now-playing/home status when idle. The Pi *is* the display.
3. **Universal AirPlay/Cast receiver** (uxplay/rpiplay) — throw phone/laptop
   content to the TV via the Pi.
4. **IR blaster** (GPIO) — control soundbar/receiver/older gear CEC can't.
5. **Local voice / wake-word** control for the room.
6. **Retro emulation** (RetroPie) at the TV.
7. **Doorbell/camera feed → TV overlay** (vision + display).
