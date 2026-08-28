# livingroom-pi

Self-contained living-room controller that runs **entirely on a Raspberry Pi 5**
wired to the TV. No homelab / Traefik dependency — if the homelab is down, the
living room still works.

Three services, all host-networked:

| Service | Port | What it does |
|---|---|---|
| `hub` | 8080 | The app you use — unified web UI + API proxy + scene engine + alarm scheduler + Plex/screen-player control. Front door for everything below. |
| `appletv` | 8010 | Control the Apple TV over pyatv (Companion): remote buttons, now-playing, **list/launch apps**, power, AirPlay stream, volume. Web remote at `/`. |
| `cec` | 8020 | Control the **TV set** over HDMI-CEC: power, volume, input/source switching. |

Two pieces run on the **host** rather than in Docker, because they need to be DRM
master on the console or outlive the stack: the mpv screen player + ambient
dashboard (systemd, see below) and the homelab watchdog (cron).

## Features

- **Unified remote** — D-pad, buttons, app launcher, now-playing, and TV controls
  from one phone-friendly web UI (`hub` on :8080).
- **Apple TV control** (Companion) — remote commands, list/launch apps, power,
  volume, and now-playing state.
- **TV-set control** (HDMI-CEC) — power on/off, volume, and active-source handoff
  so the Pi grabs the input.
- **Scenes** — one tap runs a CEC + Apple TV sequence: **Movie Night** (TV on →
  Apple TV on → launch Plex), **YouTube**, **Everything Off**. Data-driven via
  `data/hub/scenes.json`.
- **Music alarms** — scheduled wake that turns the TV on and plays music, from any
  of five sources: Apple Music/app over Companion, AirPlay stream to the Apple TV,
  or the Pi's own livestream / file / shuffle player. Set/edit/toggle/test from the
  UI; persists to `data/hub/alarms.json`.
- **Plex** — browse libraries, playlists, and search; play tracks/playlists.
- **Pi-side screen player** — mpv-based local playback (livestream / file / shuffle)
  with volume/mute from the phone, resume-where-you-left-off, and a supervisor that
  self-heals a wedged livestream.
- **Ambient TV dashboard** — when nothing's playing the TV shows a clock/weather/
  now-playing/system kiosk over Apple's tvOS aerial clips (`aerial-screen`), or a
  plain browser kiosk (`kiosk-screen`).
- **Sleep timer + auto-off** — 30/60/90-min timer, plus a nightly sweep that stands
  the TV down if it was left on the idle dashboard.
- **Health panel** — `/api/health` surfaces per-service status, disk, CPU temp,
  load, and uptime in the UI.
- **Installable (PWA)** — "Add to Home Screen" gives it an app-like launch.
- **Off-homelab** — runs entirely on the Pi; survives homelab reboots. A cron
  watchdog even alerts when the *homelab* goes down (see below).

## Deploy to the Pi

```sh
# from this machine, once the Pi is on the LAN
# (`pi` here is your own ~/.ssh/config host alias for the Pi):
rsync -a --exclude .env --exclude data/ ~/livingroom-pi/ pi:~/livingroom-pi/

# on the Pi:
cd ~/livingroom-pi
cp .env.example .env && $EDITOR .env      # at minimum set ATV_ADDRESS
docker compose up -d --build
```

Everything past `ATV_ADDRESS` is optional and env-driven (see `.env.example`):
`PLEX_URL`/`PLEX_TOKEN` enable the Plex browser, `JETSTREAM_*` the livestream
card, `HOMELAB_SSH` the server-stats line on the dashboard. Leave any of them
blank to turn that feature off. `TZ` sets the wall-clock zone alarms fire in.

## On-TV dashboard (host services, optional)

The ambient dashboard + local mpv player run on the Pi **host** (not Docker) —
they need to be DRM master on the console. Install the systemd units with the
helper (it fills the repo path into the units wherever you cloned):

```sh
sudo screen/install-services.sh
sudo systemctl enable --now screen-player
sudo systemctl enable --now aerial-screen   # OR kiosk-screen — pick one
```

`SCREEN_MEDIA_ROOT` (in the unit's environment or `.env`) points the file/shuffle
sources at your media; `aerial-screen` needs clips fetched once via
`screen/fetch-aerials.sh`.

## Apple TV pairing (one-time)

Companion needs a one-time PIN pairing (shown on the TV). Credentials persist to
`data/appletv/pyatv.conf`.

```sh
docker exec -it appletv \
  atvremote --scan-hosts "$ATV_ADDRESS" \
            --storage-filename /data/pyatv.conf \
            --protocol companion pair
```

## Apps on the Apple TV

```sh
curl -s  http://<pi>:8010/api/apps                     # list installed apps + bundle ids
curl -sX POST http://<pi>:8010/api/launch/com.netflix.Netflix
```

## Scenes

One-tap combos the hub fires against itself (TV + Apple TV control sequences).
Edit `data/hub/scenes.json` (no rebuild needed) or rely on the bundled defaults:

```sh
curl -s  http://<pi>:8080/api/scenes                              # {"movie":"Movie Night", ...}
curl -sX POST http://<pi>:8080/api/scenes/movie/run              # fires Movie Night
```

Step shape: `{"svc":"cec"|"atv", "method":"POST", "path":"/api/..."}` or
`{"delay":<seconds>}`. Falls back to the in-code defaults if the JSON is
missing or invalid.

## CEC quick test

```sh
curl -s  http://<pi>:8020/api/status                   # adapter present? TV power? bus devices
curl -sX POST http://<pi>:8020/api/tv/on
curl -sX POST http://<pi>:8020/api/tv/volume/up
```

If `/api/status` reports no adapter: confirm `/dev/cec0` exists on the Pi
(`ls /dev/cec*`), that the TV's CEC is enabled (Anynet+/Bravia Sync/SimpLink/…),
and that the Pi is on an HDMI input the TV can see.

## Homelab watchdog

`watchdog/watchdog.sh` (cron, every 5 min) is the outside observer for the
homelab: pings the server + ween-arch and checks two ingress URLs, alerting
via **ntfy.sh upstream** — not the homelab ntfy, which dies with the server.
The secret topic name is the credential and lives in `data/watchdog/topic`
(gitignored). Alerts fire once on down (re-notify daily), one-shot on
recovery; state + log in `data/watchdog/`. Publish falls back to resolving
ntfy.sh via Cloudflare DoH at 1.1.1.1, since the Pi's primary DNS is AdGuard
on the homelab itself.
