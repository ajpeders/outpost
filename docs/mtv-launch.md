# MTV on the TV - deploy and operate

The hub's **MTV** button plays the synchronized MTV broadcast through mpv on
the Pi's HDMI. The TV gets direct video and audio; Chromium is not involved.

## How it works

MTV is a deterministic wall-clock broadcast. The site remains the schedule
authority:

```text
hub UI -> POST /api/screen/mtv -> hub wakes TV and claims Pi input
                                -> screen-player POST /play source=mtv
                                -> GET MTV_URL/admin/api/now?ch=1
                                -> mpv loads current MP4 at returned offset
                                   and queues the next MP4
```

At each song boundary, screen-player rechecks the endpoint, corrects drift over
two seconds, refreshes the queued successor, and displays artist/song credits.
If the endpoint briefly fails, the queued song continues. If mpv dies, the
supervisor rejoins the current wall-clock position; after 60 seconds without a
schedule response it restores the idle dashboard.

## Prerequisite

The MTV service must deploy `GET /admin/api/now?ch=<number>` before this change.
From the Pi, this must return JSON with `now`, `next`, and `url_base`:

```sh
curl -fsS 'https://mtv.thelunadog.com/admin/api/now?ch=1' | python3 -m json.tool
```

A 404 means the MTV service is not ready. Do not deploy/restart screen-player
until that endpoint is available.

## Configuration

Set the site base URL in `.env`:

```sh
MTV_URL=https://mtv.thelunadog.com
```

Blank or absent hides the button. `MTV_URL` is passed to the hub container; no
host-unit environment variable is required.

## Deploy

Check for another deployment session and active playback first. A
screen-player restart kills the current mpv session.

```sh
pgrep -af claude
curl -s localhost:9595/status | python3 -m json.tool
```

Proceed only when `playing` is false:

```sh
sudo systemctl stop mtv-screen 2>/dev/null || true
sudo systemctl disable mtv-screen 2>/dev/null || true
sudo rm -f /etc/systemd/system/mtv-screen.service
sudo screen/install-services.sh screen-player aerial-screen kiosk-screen
sudo systemctl daemon-reload
sudo systemctl restart screen-player

DOCKER_BUILDKIT=0 docker compose build hub
docker compose up -d hub
```

Changes under `hub/app/static/` are bind-mounted and become live without a
rebuild, but the Python hub change requires the rebuild above.

## Verify

```sh
# Button capability and idle status
curl -s localhost:8080/api/screen/status | python3 -m json.tool

# Launch; expect title and subtitle from the schedule endpoint
curl -sX POST localhost:8080/api/screen/mtv | python3 -m json.tool

# Expect profile/source mtv and the current MP4 URL
curl -s localhost:9595/status | python3 -m json.tool

# Inspect conductor/supervisor decisions and mpv output
journalctl -u screen-player | grep 'screen:'
sudo tail -f /tmp/screen-mpv.log

# Normal stop returns to the dashboard
curl -sX POST localhost:8080/api/screen/stop
```

Manual acceptance: confirm sound, readable credits at song start and near its
end, a clean song boundary, matching phone/site metadata, no dropped frames,
and dashboard restoration after stop.

## Troubleshooting

- **`mtv: HTTP Error 404`**: the MTV schedule endpoint is not deployed, or the
  configured base URL is wrong.
- **`MTV_URL not configured` (503)**: add it to `.env` and recreate the hub.
- **Button missing**: `/api/screen/status` reports `mtv: false`; check the same
  environment setting.
- **Blank TV with `profile: mtv`**: inspect `/tmp/screen-mpv.log` and
  `journalctl -u screen-player`. The conductor terminates an mpv whose IPC
  socket disappears so the supervisor can rejoin it.
- **Old `mtv-screen` unit still exists**: run the cleanup commands under
  Deploy. It is obsolete and must remain stopped/disabled.

## Deploy etiquette

Never restart `screen-player` or `aerial-screen` while `/status` reports
`"playing": true`. Restarting the hub is playback-safe but briefly interrupts
the phone UI.
