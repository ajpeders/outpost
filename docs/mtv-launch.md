# MTV on the TV — deploy & operate

Adds an **MTV** button to the hub's *Watch on the TV* panel. Tapping it makes the
Pi display `MTV_URL` (default `https://mtv.thelunadog.com`) fullscreen on the
TV's HDMI; tapping stop returns to the idle dashboard.

## How it works

MTV is a **website**, not a video file, so it does **not** go through mpv. The
hub instead swaps which systemd unit holds DRM master:

| Unit | What it puts on the HDMI |
|---|---|
| `aerial-screen` | idle dashboard (mpv aerials + overlay) — the usual resting state |
| `kiosk-screen` | idle dashboard (cage + chromium variant) |
| `mtv-screen` | cage + chromium kiosk pointed at `MTV_URL` |

Only one can be active at a time — all three `Conflicts=` each other, so systemd
refuses to start two even if something tries.

Flow:

```
hub UI  --POST /api/screen/mtv-->  hub
                                   |-- wakes TV, claims Pi input (CEC)
                                   '-- POST screen-player /kiosk/mtv
                                        |-- systemctl stop  $SCREEN_KIOSK_SERVICE
                                        '-- systemctl start mtv-screen
```

`/api/screen/mtv/stop` does the reverse (`/kiosk/idle`).

`$SCREEN_KIOSK_SERVICE` is whatever the idle kiosk is on that Pi — this box uses
`aerial-screen` via a drop-in (`screen-player.service.d/kiosk-service.conf`).

## Configuration

`.env`:

```sh
MTV_URL=https://mtv.thelunadog.com
```

Blank/absent → the hub reports `mtv: false` and the UI **hides the button**.
`docker-compose.yml` passes it into the `hub` container; the `mtv-screen` unit
reads the same `.env` via `EnvironmentFile=`.

## Deploy (fresh Pi, or after a clone)

```sh
cd ~/projects/smarthome

# 1. config
grep -q '^MTV_URL=' .env || echo 'MTV_URL=https://mtv.thelunadog.com' >> .env

# 2. install the unit (substitutes %%REPO%% for the real checkout path)
sudo screen/install-services.sh mtv-screen

# 3. rebuild the hub so it serves the new endpoints
DOCKER_BUILDKIT=0 docker compose build hub     # plain `build` fails: buildx too old on this Pi
docker compose up -d hub
```

### Do NOT `systemctl enable mtv-screen`

`enable` would start it at boot and the Pi would come up showing MTV instead of
the dashboard. It is `disabled` on purpose — the hub starts it on demand.
(`systemctl start` works regardless of enable state.)

If you changed `aerial-screen.service` / `kiosk-screen.service` (the `Conflicts=`
lines), reinstall those too and `daemon-reload` — safe while they run, since
reload does not restart anything:

```sh
sudo screen/install-services.sh aerial-screen kiosk-screen
```

## Verify

```sh
# capability flag (true → UI shows the button)
curl -s localhost:8080/api/screen/status | python3 -m json.tool | grep mtv

# launch — expect {"ok":true,...} then aerial-screen inactive / mtv-screen active
curl -sX POST localhost:8080/api/screen/mtv
systemctl is-active aerial-screen mtv-screen

# the page actually loaded:
sudo tr '\0' ' ' < /proc/$(pgrep -f 'chromium --kiosk' | head -1)/cmdline | tr ' ' '\n' | grep thelunadog

# stop — back to the dashboard
curl -sX POST localhost:8080/api/screen/mtv/stop
systemctl is-active aerial-screen mtv-screen
```

## Troubleshooting

- **`{"ok":false,"error":"{\"error\": \"not found\"}"}`** — the hub reached
  screen-player but it 404'd: the running `screen_player.py` predates the
  checkout. `sudo systemctl restart screen-player`. (The hub is a container and
  picks up changes on rebuild; screen-player is a host systemd unit and does
  not.)
- **`MTV_URL not configured` (503)** — `.env` has no `MTV_URL`, or the hub
  container wasn't recreated after adding it (`docker compose up -d hub`).
- **Button missing in the UI** — `mtv: false` from `/api/screen/status`, same
  cause as above.
- **TV stays on the dashboard after launch** — check `systemctl status
  mtv-screen`; a `Conflicts=` violation or a cage/chromium crash shows there.
  `mtv-kiosk.sh` waits up to 180 s for the hub's `/healthz` before starting
  chromium.
- **Both kiosks fighting over the display** — should be impossible via the
  `Conflicts=` lines. If it happens, the installed units are stale: reinstall
  them (`sudo screen/install-services.sh aerial-screen kiosk-screen mtv-screen`)
  and `daemon-reload`.

## Deploy etiquette

Per `CLAUDE.md`: check `curl -s localhost:9595/status` before restarting
`screen-player` — if `"playing": true`, someone is watching. Restarting the
`hub` container is safe during playback (mpv runs on the host) but blips the
phone UI for ~5 s.
