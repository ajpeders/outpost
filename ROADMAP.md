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
- **Phase 4 — On-TV ambient dashboard** 🔧 *in progress* `screen/` ships an mpv
  player + cage kiosk service and `hub/app/static/dashboard.html`. Glanceable
  clock/weather/now-playing/alarm kiosk; phone-first, no TV input required.
  Built: 1080p-friendly ambient stills, weather caching, transparent cursor
  repair, and optimized kiosk launcher.
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
