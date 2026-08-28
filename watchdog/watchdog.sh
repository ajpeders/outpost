#!/usr/bin/env bash
# watchdog.sh — external homelab monitor, run from cron every 5 min on the Pi.
#
# The Pi is deliberately independent of the homelab, which makes it the only
# box that can notice the homelab is down. Alerts go to ntfy.sh (public
# upstream) — NOT the homelab's own ntfy, which dies with the server.
#
# Secret topic name lives in data/watchdog/topic (gitignored — the topic name
# is the credential). State: one data/watchdog/down.<key> file per active
# outage, "<down-since-epoch> <last-notify-epoch>".
set -u

DIR="$(cd "$(dirname "$0")" && pwd)"
STATE="$DIR/../data/watchdog"
mkdir -p "$STATE"
LOG="$STATE/watchdog.log"
TOPIC="$(cat "$STATE/topic" 2>/dev/null)" || { echo "watchdog: no topic file at $STATE/topic" >&2; exit 1; }

RENOTIFY=86400   # re-alert daily while a check stays down

log() { echo "$(date -Is) $*" >>"$LOG"; }

# Publish to ntfy.sh. Primary DNS is AdGuard on the homelab itself, so on the
# failure we most care about, name resolution may be degraded — fall back to
# resolving ntfy.sh via Cloudflare DoH at an IP literal (no DNS needed).
notify() { # title body priority tags
    local args=(-fsS -m 20 -o /dev/null -H "Title: $1" -H "Priority: $3" -H "Tags: $4" -d "$2")
    curl "${args[@]}" "https://ntfy.sh/$TOPIC" && return 0
    local ip
    ip=$(curl -fsS -m 10 -H 'accept: application/dns-json' \
        'https://1.1.1.1/dns-query?name=ntfy.sh&type=A' |
        python3 -c 'import sys,json;print(next(a["data"] for a in json.load(sys.stdin)["Answer"] if a["type"]==1))') || {
        log "NOTIFY-FAILED $1"
        return 1
    }
    curl "${args[@]}" --resolve "ntfy.sh:443:$ip" "https://ntfy.sh/$TOPIC" || { log "NOTIFY-FAILED $1"; return 1; }
}

check_ping() { ping -c1 -W3 "$1" >/dev/null 2>&1; }
check_url() { curl -fsS -m 10 -o /dev/null "$1" 2>/dev/null; }

run_check() { # key description type target
    local key=$1 desc=$2 type=$3 target=$4 ok=1 i now since lastnotify
    for i in 1 2 3; do
        "check_$type" "$target" && { ok=0; break; }
        [ "$i" -lt 3 ] && sleep 5
    done
    local f="$STATE/down.$key"
    now=$(date +%s)
    if [ "$ok" -ne 0 ]; then
        if [ ! -e "$f" ]; then
            log "DOWN $key ($target)"
            # state written only if the alert went out, so a failed publish retries next run
            notify "DOWN: $desc" "$target failed 3 checks from the Pi" high rotating_light &&
                echo "$now $now" >"$f"
        else
            read -r since lastnotify <"$f"
            if [ $((now - lastnotify)) -ge $RENOTIFY ]; then
                notify "STILL DOWN: $desc" "down since $(date -d "@$since" '+%F %T')" high rotating_light &&
                    echo "$since $now" >"$f"
            fi
        fi
    elif [ -e "$f" ]; then
        read -r since lastnotify <"$f"
        log "RESOLVED $key"
        notify "RESOLVED: $desc" "was down since $(date -d "@$since" '+%F %T')" default white_check_mark &&
            rm -f "$f"
    fi
}

run_check server "homelab server" ping 192.168.0.176
run_check ween-arch "ween-arch (backup target)" ping 192.168.0.40
run_check ingress-ntfy "ingress: ntfy.thelunadog.com" url "https://ntfy.thelunadog.com/v1/health"
run_check ingress-git "ingress: git.thelunadog.com" url "https://git.thelunadog.com/"

if [ -f "$LOG" ] && [ "$(wc -l <"$LOG")" -gt 2000 ]; then
    tail -n 1000 "$LOG" >"$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi
