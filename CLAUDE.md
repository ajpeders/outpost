# Outpost — working rules

This stack drives the living-room TV **live**. Restarts are user-visible.

## Deploy etiquette (IMPORTANT)

- **Before restarting `screen-player` or `aerial-screen`**: check
  `curl -s localhost:9595/status` — if `"playing": true`, someone is watching.
  Do NOT restart; defer until idle (or ask). A restart kills mpv mid-movie and
  dumps the TV back to the dashboard.
- Restarting the `hub` container is safe during playback (mpv lives on the
  host) but blips the phone UI ~5s; avoid during active browsing if possible.
- Check for other active Claude sessions (`pgrep -af claude`, tmux) before
  deploying — concurrent deploys have already killed a user's movie once.

## Deploy map

- `hub/app/static/*` is volume-mounted → edits are live instantly, no rebuild.
- Hub/cec/appletv Python → `DOCKER_BUILDKIT=0 docker compose build <svc> &&
  docker compose up -d <svc>` (plain build fails: buildx too old on this Pi).
- `screen/*.py` run on the host via systemd (`screen-player`, `aerial-screen`).

## Gotchas

- `pkill -f <pattern>` self-matches your own shell if the pattern appears in
  the command line — use `[b]racket` patterns interactively; in code anchor to
  the binary name.
- Verify overlay/dashboard changes visually: `/tmp/aerial-ov.png` is the live
  overlay render; mpv playback log is `/tmp/screen-mpv.log` (fresh per spawn).
