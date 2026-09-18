# How-to

Step-by-step guides for the things you actually do. `README.md` is the overview,
`ARCHITECTURE.md` explains the design.

Throughout, `<pi>` is the Pi's address and the repo root is wherever you cloned
it (currently `~/projects/smarthome`).

## Set up a fresh Pi

On Raspberry Pi OS **Trixie**, Debian's own repo has no Compose v2 plugin, so use
Docker's repo:

```sh
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
  https://download.docker.com/linux/debian $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker "$USER"   # log out and back in, or use sudo docker meanwhile
```

Then:

```sh
git clone ssh://git@git.thelunadog.com:2222/alex/smarthome.git ~/projects/smarthome
cd ~/projects/smarthome
cp .env.example .env && $EDITOR .env      # at minimum set ATV_ADDRESS
docker compose up -d --build
```

Verify:

```sh
curl -s localhost:8080/api/health          # per-service status, disk, temp, load
curl -s -o /dev/null -w '%{http_code}\n' localhost:8010/    # appletv → 200
curl -s -o /dev/null -w '%{http_code}\n' localhost:8020/docs # cec → 200 (no route at /)
python3 test-client.py                     # smoke-tests appletv + cec endpoints
```

`GET /` on the `cec` service returns 404 by design — it has no root route.

## Restore onto a rebuilt Pi

The code comes from git; only runtime state needs restoring. From the desktop
backup at `~/usb-backup/pi5/`:

```sh
# on the desktop — extract what you need from the split archives
cd ~/usb-backup/pi5
mkdir -p extract
zstd -dc pi5-part4-livingroom-env-*.tar.zst | tar -x -C extract   # .env, compose, screen/
zstd -dc pi5-part3-config-etc-*.tar.zst | tar -x -C extract \
  --strip-components=3 'home/alex/livingroom-pi/data'             # runtime state

# copy onto the Pi
scp extract/.env <pi>:~/projects/smarthome/.env
rsync -a extract/data/ <pi>:~/projects/smarthome/data/
```

Parts 1 and 2 are truncated streams (the SD card died mid-backup); `zstd -dc |
tar -x` still extracts everything up to the cut, so ignore the EOF warning.

If the hub's homelab stats panel is enabled, it needs the SSH key that
`HUB_SSH_DIR` points at (`/home/alex/.ssh` by default) and a `known_hosts`
covering `HOMELAB_SSH`. Restore `id_ed25519` there and `chmod 600` it.

## Pair the Apple TV

Pairing is one-time; credentials persist to `data/appletv/pyatv.conf`. The hub
UI's Pairing panel is usually easiest. By CLI:

```sh
set -a; . ./.env; set +a      # .env is Compose's, not your shell's
docker exec -it appletv \
  atvremote --scan-hosts "$ATV_ADDRESS" \
            --storage-filename /data/pyatv.conf \
            --protocol companion pair
```

## Add or change a scene

Edit `data/hub/scenes.json` — no rebuild needed. Each scene is
`{label, steps:[...]}`, and each step is either `{svc, method, path}` where `svc`
is `cec` or `atv`, or `{delay: seconds}`.

```sh
curl -s localhost:8080/api/scenes                 # list
curl -sX POST localhost:8080/api/scenes/movie/run # fire one
```

An invalid JSON file silently falls back to the in-code defaults, so check
`/api/scenes` after editing. A stray key like `_comment` at the top level has
broken the listing before.

## Set up the on-TV dashboard

These are host systemd units, not containers:

```sh
sudo apt install -y mpv chromium-browser cage ffmpeg v4l-utils python3 curl
sudo screen/install-services.sh          # substitutes the repo path into the units
sudo systemctl enable --now screen-player
sudo systemctl enable --now aerial-screen   # OR kiosk-screen — never both
```

- Aerial clips: `screen/fetch-aerials.sh` (12 clips by default, ~4.3GB at 4K;
  `RES=720` or `RES=1080` downscales, `AERIAL_MAX=0` grabs Apple's whole
  catalogue — tens of GB). The cache lives in `data/hub/aerials/` (gitignored),
  so re-run it after a rebuild or a fresh clone or `aerial-screen` can't start.
- For `kiosk-screen`, also run `sudo screen/install-cursor.sh` once or a mouse
  pointer sits stuck in the middle of the TV.
- These units do **not** read `.env`. Set their variables on the unit:
  `sudo systemctl edit screen-player` → `Environment=SCREEN_MEDIA_ROOT=/path`.
- If you use `aerial-screen`, set `SCREEN_KIOSK_SERVICE=aerial-screen` on
  `screen-player` so the mpv handoff stops the right unit.

## Mount the media library (SMB/CIFS)

The Pi-side screen player plays files straight off an SMB share (no transcode).
The share is mounted read-only on the **host** at `/mnt/share`; the library root
is `SCREEN_MEDIA_ROOT`, default `/mnt/share/media`. The hub never sees the mount
— it proxies `/api/media/*` to the screen player.

On the Pi:

```sh
sudo apt-get install -y cifs-utils
sudo install -m 600 /dev/stdin /etc/smarthome-smb.credentials <<'EOF'
username=<smb-user>
password=<smb-password>
EOF
sudo mkdir -p /mnt/share
```

Append this line to `/etc/fstab`:

```
//<server>/<share> /mnt/share cifs credentials=/etc/smarthome-smb.credentials,ro,vers=3.1.1,iocharset=utf8,nosuid,nodev,noexec,_netdev,nofail,x-systemd.automount,x-systemd.mount-timeout=15s 0 0
```

Then activate it:

```sh
sudo systemctl daemon-reload
sudo systemctl start mnt-share.automount
ls /mnt/share                                    # first access triggers the mount
findmnt /mnt/share                               # should show the cifs mount
curl -s 'localhost:8080/api/media/list?path='    # lists the library root
```

- `x-systemd.automount` mounts on first access; `nofail` keeps a missing share
  from blocking boot, and `_netdev` waits for the network.
- `ro` keeps the Pi from writing to the share.
- The credentials file is root-only (`chmod 600`); keep it out of the repo.
- A different subtree is set per-unit, not via `.env`: `sudo systemctl edit
  screen-player` → `Environment=SCREEN_MEDIA_ROOT=/mnt/share/other`.
- If `/api/media/list` returns **400**, the share isn't mounted or reachable:
  check `findmnt /mnt/share` and `journalctl -u mnt-share.mount`.

## Update and redeploy

```sh
cd ~/projects/smarthome
git pull
docker compose up -d --build
```

Editing files under `hub/app/static/` needs no rebuild — that directory is
bind-mounted into the container.

## Check logs and health

```sh
docker compose ps
docker compose logs -f hub                      # or cec / appletv
journalctl -u screen-player | grep 'screen:'    # supervisor decisions
journalctl -u aerial-screen -f                  # overlay render timings
curl -s localhost:8080/api/health
```

## Enable the Hailo AI accelerator

The HAT needs its driver installed; a fresh OS won't have it:

```sh
sudo apt install -y hailo-all
ls /dev/hailo0                       # appears without a reboot
hailortcli fw-control identify       # should report HAILO8L, fw 4.23.0
```

Over SSH, apt prints debconf "requires a controlling tty" warnings. They're
noise; the install still succeeds.

## Check the camera

```sh
rpicam-hello --list-cameras          # should list imx708
```

Nothing detected almost always means the ribbon cable, not software: power off,
reseat both ends with the contacts the right way round, confirm you're using the
Pi 5's narrow 22-pin cable (older cameras need a 22-to-15-pin adapter), and try
the other camera port.

## Troubleshooting

**`cec` container won't start.** The host has no `/dev/cec0` to bind. Check `ls
/dev/cec*`; if yours is `cec1`, set `CEC_ADAPTER=/dev/cec1` in `.env`. If there's
no node at all, you need `dtoverlay=vc4-kms-v3d` in `/boot/firmware/config.txt`
and a reboot.

**`/api/status` reports no adapter.** Turn CEC on in the TV's menus (Anynet+ /
Bravia Sync / SimpLink / …) and make sure the Pi is on an input the TV can see.

**Black screen or wrong refresh rate on the TV.** The DRM mode indices are
specific to your display's mode list. List them with `modetest -c` (from
`libdrm-tests`) and set `SCREEN_DRM_MODE` / `AERIAL_DRM_MODE` to match.

**mpv IPC queries all return `None`.** The socket is root-owned — query it as
root.

**The TV clock is hours off.** The dashboard is rendered by Chromium on the
host, so its clock uses the host timezone, not the hub's `TZ`. Set the host to
match the hub (`sudo timedatectl set-timezone America/Denver`).

**The clock's seconds sit off the slot.** The seconds are drawn by mpv
(`screen/aerial-clock.lua`) over the once-a-minute overlay, not by the page.
Retune `AERIAL_SEC_X` (slot center-x), `AERIAL_SEC_Y` (AM/PM vertical center) and
`AERIAL_SEC_FS` (font size) on `aerial-screen` (`sudo systemctl edit
aerial-screen`), then restart it — measure the values from a 3840x2160 render of
`/dashboard?overlay=1`.

**The Pi disappears from SSH until a reboot.** Wi-Fi power save. Confirm
`/etc/NetworkManager/conf.d/wifi-powersave-off.conf` sets `wifi.powersave = 2`.

**`docker compose build` fails on BuildKit.** Prefix it: `DOCKER_BUILDKIT=0
docker compose build`.

**SD card errors (`I/O error, dev mmcblk0`) or a read-only root.** The card is
failing, and no filesystem check fixes bad flash. Back up `.env` and `data/`
immediately, then replace the card — a USB SSD lasts far longer. NVMe would need
a dual adapter because the Hailo HAT holds the only PCIe slot.
