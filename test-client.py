#!/usr/bin/env python3
"""Tiny smoke-test client for the living-room APIs (appletv + cec).

Stdlib only — runs on the Pi or any box with python3, no dependencies.

Examples
  ./test-client.py                         # read-only sweep of both services
  ./test-client.py --atv http://<pi>:8010 status
  ./test-client.py apps                     # list Apple TV apps
  ./test-client.py launch com.plexapp.plex
  ./test-client.py key play_pause           # a remote button
  ./test-client.py power on
  ./test-client.py tv on                    # CEC: turn the TV set on
  ./test-client.py vol up
  ./test-client.py source active            # CEC: switch TV to the Pi input
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

RESET, DIM, GREEN, RED, YEL = "\033[0m", "\033[2m", "\033[32m", "\033[31m", "\033[33m"


def call(method: str, url: str, timeout: float = 20.0):
    req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except urllib.error.URLError as e:
        return None, str(e.reason)


def show(label: str, status, body: str) -> None:
    if status is None:
        print(f"{RED}✗{RESET} {label:<22} {DIM}unreachable: {body}{RESET}")
        return
    ok = 200 <= status < 300
    mark = f"{GREEN}✓{RESET}" if ok else f"{RED}✗{RESET}"
    try:
        body = json.dumps(json.loads(body), indent=2)
    except ValueError:
        pass
    color = "" if ok else YEL
    body = "\n".join("    " + ln for ln in body.splitlines())
    print(f"{mark} {label:<22} {color}HTTP {status}{RESET}\n{DIM}{body}{RESET}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--atv", default=os.environ.get("ATV_URL", "http://localhost:8010"))
    p.add_argument("--cec", default=os.environ.get("CEC_URL", "http://localhost:8020"))
    p.add_argument("cmd", nargs="?", default="sweep",
                   help="sweep|status|state|apps|launch|key|power|tv|vol|source")
    p.add_argument("arg", nargs="?", help="argument for launch/key/power/tv/vol/source")
    a = p.parse_args(argv)

    atv, cec = a.atv.rstrip("/"), a.cec.rstrip("/")
    failures = 0

    def report(status, body, label):
        nonlocal failures
        if status is None or not (200 <= status < 300):
            failures += 1
        show(label, status, body)

    def G(base, path, label):
        s, b = call("GET", base + path); report(s, b, label)

    def P(base, path, label):
        s, b = call("POST", base + path); report(s, b, label)

    match (a.cmd, a.arg):
        case ("sweep", _):
            print(f"{DIM}appletv → {atv}   cec → {cec}{RESET}\n")
            G(atv, "/api/status", "appletv status")
            G(atv, "/api/state",  "appletv now-playing")
            G(atv, "/api/apps",   "appletv apps")
            G(cec, "/api/status", "cec status")
        case ("status", _):
            G(atv, "/api/status", "appletv status")
            G(cec, "/api/status", "cec status")
        case ("state", _):   G(atv, "/api/state", "now-playing")
        case ("apps", _):    G(atv, "/api/apps", "apps")
        case ("launch", b) if b: P(atv, f"/api/launch/{b}", f"launch {b}")
        case ("key", k) if k:    P(atv, f"/api/command/{k}", f"key {k}")
        case ("power", d) if d:  P(atv, f"/api/power/{d}", f"power {d}")
        case ("tv", d) if d:     P(cec, f"/api/tv/{d}", f"tv {d}")
        case ("vol", d) if d:    P(cec, f"/api/tv/volume/{d}", f"vol {d}")
        case ("source", d) if d: P(cec, f"/api/source/{d}", f"source {d}")
        case (c, _):
            print(f"{RED}unknown or incomplete command: {c} {a.arg or ''}{RESET}")
            print("try: sweep | status | state | apps | launch <b> | key <k> | "
                  "power on|off | tv on|off | vol up|down|mute | source active|release")
            return 2
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
