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

The mpv screen player and ambient dashboard run on the **host** rather than in
Docker (they need to be DRM master on the console) — see below.

## Requirements

- **A Raspberry Pi 5** running Raspberry Pi OS (Trixie; Bookworm also works). A
  Pi 4 mostly works, but the 4K aerial dashboard assumes Pi 5 hardware decode.
- **Docker + Compose v2.** On Trixie, Debian's own repo ships no Compose v2
  plugin — install `docker-ce` + `docker-compose-plugin` from Docker's apt repo.
  Exact commands are in [HOWTO.md](HOWTO.md#set-up-a-fresh-pi).
- **A CEC-capable TV** with CEC turned on in its menus (Samsung Anynet+, Sony
  Bravia Sync, LG SimpLink, …) and a `/dev/cec0` node on the Pi. Without it the
  `cec` container will not start; see the CEC section below.
- **An Apple TV** on the same LAN. `ATV_ADDRESS` is required — the `appletv`
  service exits at startup without it.
- *Optional:* a Plex Media Server, and any HLS livestream URL.

For the host-side dashboard/player only:

```sh
sudo apt install -y mpv chromium-browser cage ffmpeg v4l-utils libdrm-tests python3 curl
```

On Trixie the `chromium-browser` package installs the binary as `chromium`;
`libdrm-tests` supplies `modetest` for finding your display's DRM mode index.

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
  self-heals a wedged livestream. The library browser shows Plex posters,
  presents flat TV episodes as one folder per season, and lets you dismiss a
  Continue-watching entry.
- **Ambient TV dashboard** — when nothing's playing the TV shows an info board over
  Apple's tvOS aerial clips (`aerial-screen`): clock, weather + 7-day forecast,
  server stats, media (Plex poster + livestream progress), next alarm, and
  now-playing tiles. Or a plain browser kiosk (`kiosk-screen`).
- **Sleep timer + auto-off** — 30/60/90-min timer, plus a nightly sweep that stands
  the TV down if it was left on the idle dashboard.
- **Health panel** — `/api/health` surfaces per-service status, disk, CPU temp,
  load, and uptime in the UI.
- **Installable (PWA)** — "Add to Home Screen" gives it an app-like launch.
- **Runs standalone** — everything lives on the Pi, so it keeps working when the
  rest of your network doesn't.

## Deploy to the Pi

```sh
# on the Pi (`pi` below is your own ~/.ssh/config host alias for it):
git clone ssh://git@git.thelunadog.com:2222/alex/smarthome.git ~/projects/smarthome

# or push a working copy from this machine instead:
rsync -a --exclude .env --exclude data/ ~/projects/smarthome/ pi:~/projects/smarthome/

# then, on the Pi:
cd ~/projects/smarthome
cp .env.example .env && $EDITOR .env      # at minimum set ATV_ADDRESS
docker compose up -d --build
```

Everything past `ATV_ADDRESS` is optional and env-driven (see `.env.example`):
`PLEX_URL`/`PLEX_TOKEN` enable the Plex browser, `JETSTREAM_*` the livestream
card, `HOMELAB_SSH` the server-stats line on the dashboard. Leave any of them
blank to turn that feature off. `TZ` sets the wall-clock zone alarms fire in;
keep the Pi's host timezone in sync with it (`sudo timedatectl set-timezone …`),
because the on-TV dashboard clock is rendered by Chromium on the host.

Check it came up with `curl -s localhost:8080/api/health`, or run the bundled
smoke-tester, which exercises the appletv + cec endpoints and prints each
result: `python3 test-client.py`. It exits nonzero if any request fails, so it's
usable as a CI check.

> On some older Docker builds on the Pi, BuildKit fails to build these images.
> If `docker compose build` errors out, prefix it: `DOCKER_BUILDKIT=0 docker
> compose build`.

## On-TV dashboard (host services, optional)

The ambient dashboard + local mpv player run on the Pi **host** (not Docker) —
they need to be DRM master on the console. Install the systemd units with the
helper (it fills the repo path into the units wherever you cloned):

```sh
sudo screen/install-services.sh
sudo systemctl enable --now screen-player
sudo systemctl enable --now aerial-screen   # OR kiosk-screen — pick one
```

These are systemd units running as root, and they do **not** read `.env` — that
file is for Docker Compose only. To point the file/shuffle sources at your media,
set `SCREEN_MEDIA_ROOT` in the unit itself (`systemctl edit screen-player`, then
`Environment=SCREEN_MEDIA_ROOT=/path/to/media`). The same applies to the other
`SCREEN_*` / `AERIAL_*` knobs documented at the top of each script.

The `file`/`shuffle` sources and the media browser read a read-only SMB/CIFS
share mounted on the host at `/mnt/share` (library root `/mnt/share/media`).
Setup — `cifs-utils`, the credentials file, and the fstab automount entry — is in
[HOWTO.md](HOWTO.md#mount-the-media-library-smbcifs).

The dashboard itself is `hub/app/static/dashboard.html`: a top band (clock + date)
and a bottom band of glass tiles (forecast, server, media, alarm, now-playing). The
live seconds are drawn by mpv (`screen/aerial-clock.lua`, `AERIAL_SEC_X/Y/FS`)
over the once-a-minute overlay render — tune those if the seconds sit off the clock
slot.

`aerial-screen` needs clips fetched once via `screen/fetch-aerials.sh` — it pulls
12 by default; `AERIAL_MAX=0` grabs Apple's whole catalogue, which is tens of GB
at 4K, onto your SD card. `RES=1080` downscales. The cache is gitignored
(`data/hub/aerials/`), so re-run it after a rebuild or a fresh clone.

`kiosk-screen` additionally wants `sudo screen/install-cursor.sh` once, or a
stuck mouse pointer sits in the middle of the TV.

The DRM mode indices (`SCREEN_DRM_MODE`, `AERIAL_DRM_MODE`) are **specific to the
display's mode list** — the committed defaults match the author's TV. If you get
a black screen or the wrong refresh rate, list your modes with
`modetest -c` (from `libdrm-tests`) and set the matching index.

## Apple TV pairing (one-time)

Companion needs a one-time PIN pairing (shown on the TV). Credentials persist to
`data/appletv/pyatv.conf`.

```sh
# .env is read by Compose, not your shell — pass the IP literally, or source it:
set -a; . ./.env; set +a

docker exec -it appletv \
  atvremote --scan-hosts "$ATV_ADDRESS" \
            --storage-filename /data/pyatv.conf \
            --protocol companion pair
```

The hub UI also exposes pairing under its Pairing panel, which is usually easier
than the CLI.

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

If the `cec` container refuses to start at all, the host has no `/dev/cec0` to
bind. Check with `ls /dev/cec*` — if yours enumerates as `cec1`, set
`CEC_ADAPTER=/dev/cec1` in `.env`. If there's no node at all, CEC isn't enabled:
you need `dtoverlay=vc4-kms-v3d` in `/boot/firmware/config.txt` and a reboot.

If `/api/status` reports no adapter: confirm the TV's CEC is enabled
(Anynet+/Bravia Sync/SimpLink/…), and that the Pi is on an HDMI input the TV can
see.

## Repo layout

`hub/`, `appletv/`, `cec/` are the three containers; `screen/` holds the
host-side player, dashboard, and their systemd units; `data/` is runtime state
(gitignored).

`ROADMAP.md` is the author's build log rather than user documentation, and
`CLAUDE.md` is instructions for AI coding agents working in this repo — neither
is needed to run the stack.

## License

MIT — see [LICENSE](LICENSE).
