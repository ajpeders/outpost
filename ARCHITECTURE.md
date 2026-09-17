# Architecture

How the living-room controller is put together, and why it's shaped this way.
`README.md` covers what it does and how to run it; this file covers the design.

## The constraint that shapes everything

mpv and Chromium must be **DRM master** on the console to paint the TV. A
container can't reliably hold DRM master, so the repo is split in two:

| Runs in Docker | Runs on the host (systemd, as root) |
|---|---|
| `hub` :8080, `appletv` :8010, `cec` :8020 | `screen/screen_player.py` :9595, `aerial-mode.py`, `kiosk.sh`, `tv-keepalive.py` |
| Restartable, rebuildable, no display access | Owns the HDMI output, needs root for DRM |

Consequences worth remembering:

- `.env` is read by **Docker Compose only**. Host units don't see it; their
  knobs (`SCREEN_*`, `AERIAL_*`) are set with `systemctl edit <unit>`.
- Only one host unit can hold the display at a time. `aerial-screen` and
  `kiosk-screen` are mutually exclusive, and the screen player stops the kiosk
  before mpv takes over (`SCREEN_KIOSK_SERVICE` names which one to stop).
- mpv's IPC socket is root-owned. Querying it as a normal user silently
  returns `None` for every property rather than erroring.

## Topology

```
  phone / browser
        │  :8080 (same-origin UI + API)
        ▼
  ┌──────────┐   /api/atv/*   ┌────────────┐  pyatv/Companion   Apple TV
  │   hub    │ ─────────────► │  appletv   │ ─────────────────►  (LAN)
  │  :8080   │   /api/cec/*   │   :8010    │
  │          │ ─────────────► ┌────────────┐  cec-ctl → /dev/cec0
  │          │                │    cec     │ ─────────────────►  TV set
  │          │                │   :8020    │       (HDMI-CEC)
  │          │  HTTP :9595    └────────────┘
  │          │ ─────────────► screen_player (host, root)
  └──────────┘                     │ spawns
        │                          ▼
        │                    mpv on DRM/KMS ──► Pi HDMI output
        │  reads/writes
        ▼
   data/  (alarms, scenes, pyatv creds, resume points)
```

All three containers use `network_mode: host`. That is deliberate:

- pyatv needs LAN-local mDNS discovery and AirPlay, which bridge networking
  breaks.
- CEC is local hardware anyway.
- The hub reaches the other two at `localhost:8010`/`:8020`, so there's no
  service discovery and no inter-container DNS to go wrong.

The hub is the only front door. The UI talks to one same-origin API surface and
the hub proxies onward, so the browser never needs CORS or multiple ports.

## Responsibilities

**`hub`** — the app. Serves the SPA (`hub/app/static/`) and the dashboard, proxies
`/api/atv/*` and `/api/cec/*`, and owns everything stateful:

- `alarms.py` — schedule persisted to `data/hub/alarms.json`, fired by a 20-second
  tick against the wall clock in `TZ`. No cron, no external scheduler. Five
  sources: `appletv_music`, `airplay`, `livestream`, `file`, `shuffle`.
- `scenes.py` — data-driven step sequences from `data/hub/scenes.json`, falling
  back to `DEFAULT_SCENES` in code when the file is missing or invalid. A step is
  `{svc, method, path}` or `{delay}`, so new scenes need no rebuild.
- `plex.py` — library browse/search/play and the queue state machine.
- Sleep timer, nightly auto-off, `/api/health`, `/api/weather`, `/api/homelab`.

**`appletv`** — a thin async wrapper over pyatv, connecting to a *known* IP by
unicast scan. Credentials live in `data/appletv/pyatv.conf`, written once by
pairing. It also carries a runtime patch for tvOS 26, which answers pyatv's
`/playback-info` poll with HTTP 500 and would otherwise tear down the AirPlay
session mid-stream.

**`cec`** — a thin async wrapper over `cec-ctl` (v4l-utils), talking to the
kernel CEC API. One global lock serializes bus transactions, because concurrent
senders on the same adapter can wedge the bus.

**`screen_player`** (host) — spawns and supervises mpv, picks a DRM mode to match
the content, exposes `/status`, `/play`, `/control`, `/stop`, and handles TV
input reclaim. The supervisor relaunches mpv when playback wedges.

**`aerial-mode`** (host) — the ambient dashboard. mpv hardware-decodes cached
Apple aerials on DRM while a headless Chromium renders `/dashboard?overlay=1` to
a PNG that's pushed over mpv's `overlay-add` once a minute.

## Key decisions

- **`cec-ctl` instead of libCEC.** `cec-client` cold-started in 5–10s *per
  command*. The kernel API is ~0.7s. It also ended the "adapter reset to
  unregistered" flakiness: host and container now assert the same
  playback-device config, so adapter state survives whoever talked last.
- **CEC status cached 45s** (`CEC_STATUS_TTL`). The UI polls every 10s, and every
  real query costs ~0.8s *and* re-registers the shared `/dev/cec0`, which is what
  used to break input switching. Commands publish the state they just caused, so
  the UI still updates instantly; only changes made on the TV's own remote wait
  out the TTL. `/api/health` went 0.85s → 0.03s.
- **Set Stream Path for input switching.** Releasing the Pi's active-source claim
  and waiting for tvOS to assert took 5–10s and stalled if the Apple TV was
  already awake. Addressing the Apple TV's HDMI port directly
  (`ATV_HDMI_INPUT`) and waking it in parallel measures ~0.8s.
- **mpv on DRM rather than `<video>` in the browser.** cage/Chromium won't paint
  a video surface even with the GPU on — a Wayland limitation, not a GPU one.
  Hence video underneath and the dashboard composited on top as an overlay.
- **One long-lived Chromium over CDP** for overlay renders (~0.8s warm) instead
  of cold-starting per minute (8–15s), with re-attach on session loss and a
  cold one-shot as the last resort.
- **Rejoin budget that refills.** The livestream's ffmpeg restarts at each title
  change, and mpv carries a stale init segment across the discontinuity. A fixed
  budget of 3 rejoins ran out mid-evening and left a permanently corrupt picture,
  so the budget now refills after 300s of healthy playback
  (`SCREEN_CORRUPT_WINDOW`).
- **The hub resolves the HLS rendition** instead of handing mpv the master
  playlist, which made ffmpeg probe every rendition (~57s to start, sometimes
  outliving the segment window). Now ~1s.
- **Everything optional is env-gated.** Blank `PLEX_URL`, `JETSTREAM_*` or
  `HOMELAB_SSH` disables that feature and hides its UI, so the stack runs with
  only `ATV_ADDRESS` set.
- **Container logs capped** at 5MB × 3. The UI polls every few seconds, and
  unrotated json-file logs hit 85MB in 12 days on the SD card.

## State

Everything mutable lives in `data/` (gitignored), so a rebuild only needs this
directory plus `.env`:

| Path | Written by | Contents |
|---|---|---|
| `data/hub/alarms.json` | hub | alarm schedule |
| `data/hub/scenes.json` | you | scene definitions |
| `data/hub/aerials/` | `fetch-aerials.sh` | cached clips + `timeofday.json` |
| `data/appletv/pyatv.conf` | pairing | Apple TV credentials |
| `data/screen/watch_later/` | mpv | resume points |
| `data/screen/shuffle-history.json` | screen player | last-20 no-repeat list |
| `${HUB_SSH_DIR}` → `/ssh` (read-only) | you | key + known_hosts for the homelab probe |

## Failure behaviour

- Containers are `restart: unless-stopped`; host units are `Restart=always`.
- The screen player's supervisor detects a wedged stream (video PTS frozen while
  audio runs) and rejoins, ignoring stalls while `paused-for-cache` so a
  buffering hiccup isn't mistaken for corruption. Each action is logged:
  `journalctl -u screen-player | grep 'screen:'`.
- Scenes fall back to in-code defaults if `scenes.json` is unreadable.
- The homelab probe, Plex and jetstream all fail soft — their panels degrade
  rather than breaking the UI.
- The stack has no homelab dependency by design: if the rest of the network is
  down, the living room still works.

## Hardware notes

- **Pi 5**, Raspberry Pi OS. `/dev/cec0` requires `dtoverlay=vc4-kms-v3d`.
- **DRM mode indices are display-specific.** The committed defaults match the
  author's TV; list yours with `modetest -c`.
- **Wi-Fi power save must stay off** (`/etc/NetworkManager/conf.d/wifi-powersave-off.conf`),
  or the brcmfmac radio naps through inbound SSH.
- **Hailo-8L AI accelerator** sits on the only PCIe slot, so NVMe storage would
  need a dual adapter. Nothing in this stack uses the Hailo yet.
- **Camera Module 3 (imx708)** is attached and detected; also unused so far.
