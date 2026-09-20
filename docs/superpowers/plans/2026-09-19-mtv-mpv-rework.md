# MTV via mpv — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the chromium kiosk behind the hub's MTV button with mpv playback of the MTV broadcast, driven by a new `/admin/api/now` endpoint on the MTV site.

**Architecture:** The MTV site (repo `alex/mtv`, FastAPI admin container) gains a Python port of the player's schedule math and serves "what's on now" as JSON. On the Pi, screen-player gets an `mtv` profile: an idle mpv fed by a conductor thread over a persistent IPC socket, one song ahead, drift-corrected at every file boundary. The hub launches it exactly like the jetstream livestream. The cage/chromium kiosk path is deleted.

**Tech Stack:** Python 3.12 (FastAPI) in the mtv admin container; Python 3 stdlib (`http.server`, `socket`, `threading`) in screen-player; mpv 0.40 JSON IPC; node (CI + dev box) for the JS parity test.

**Spec:** `docs/superpowers/specs/2026-09-19-mtv-mpv-rework-design.md` (this repo). Read it first.

**Repos and where to work:**

| Repo | Checkout to use | Notes |
|---|---|---|
| `alex/mtv` | `/home/agent/projects/mtv` (as user `agent`: `sudo -u agent -i` or `sudo -u agent bash -c '…'`) | Push to `main` → CI → auto-deploy on isis by `bin/deploy` (~3 min). The isis checkout `/home/ween/homelab/apps/mtv` has unrelated uncommitted edits to `app/index.html` and `app/mtv.js`; do not touch them. |
| smarthome | `/home/alex/projects/smarthome` | **The working tree already contains an uncommitted, near-complete implementation of spec §2–§4 written by another session.** Chunk 2 reviews and finishes it rather than rewriting. Check `git status` before every edit; if files are changing under you, stop and tell the user. |

**Deploy etiquette (from `CLAUDE.md`):** before restarting `screen-player` on the Pi, `curl -s localhost:9595/status` must show `"playing": false`. Check `pgrep -af claude` for other sessions before deploying.

---

## Chunk 1: MTV site — `/admin/api/now` (repo `alex/mtv`)

### Task 1: Schedule math port with JS parity test

**Files:**
- Create: `admin/schedule.py`
- Create: `admin/test_schedule.py`
- Create: `admin/fixtures/manifest.json`

- [ ] **Step 1: Create the fixture manifest**

`admin/fixtures/manifest.json` — nine videos, mixed titles, channel map for 1 and 2 (channel 3 absent from the map so it airs the full library; one zero-duration item that must be filtered):

```json
{
  "videos": [
    {"id": "aaa111", "title": "Drake - Choosin' Texas (Feat. Don Toliver) (Official Video)", "duration": 197.65},
    {"id": "bbb222", "title": "I KNOW ITS TRUE DRAKE", "duration": 106.12},
    {"id": "ccc333", "title": "AZ Chike – Look Like My Mama [Official Music Video]", "duration": 232.25},
    {"id": "ddd444", "title": "ZILLA — \"Westside Gunn\" (Lyrics)", "duration": 153.09},
    {"id": "eee555", "title": "matt proxy - misery (official music video)", "duration": 280.01},
    {"id": "fff666", "title": "WALLFLOWER - DOMINIC FIKE", "duration": 177.28, "artist": "Dominic Fike", "track": "Wallflower"},
    {"id": "ggg777", "title": "Small Pond (Visualizer)", "duration": 0},
    {"id": "hhh888", "title": "Some Band - Some Song - With Dash", "duration": 200.5},
    {"id": "iii999", "title": "No Separator Here", "duration": 90.0}
  ],
  "channels": {
    "1": ["aaa111", "bbb222", "ccc333", "ddd444", "eee555", "fff666", "ggg777"],
    "2": ["hhh888", "iii999", "aaa111"]
  }
}
```

- [ ] **Step 2: Write the failing parity test**

`admin/test_schedule.py`. It extracts `mulberry32` and `creditText` verbatim from `../app/mtv.js`, reimplements only the glue (`scheduleFor`, `onAir`) in JS exactly as `mtv.js` does, runs it under `node`, and compares with the Python port.

```python
"""Parity test: admin/schedule.py must match app/mtv.js exactly.
Runs on the dev box or CI (needs `node`); never inside the admin container."""
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
MTV_JS = HERE.parent / "app" / "mtv.js"
FIXTURE = HERE / "fixtures" / "manifest.json"
sys.path.insert(0, str(HERE))
import schedule  # noqa: E402

TIMESTAMPS = [1_000_000_000.0, 1_758_300_000.25, 2_000_000_000.5]
CHANNELS = [1, 2, 3, 4]


def js_function(name):
    """Slice `function name(...) {...}` out of mtv.js by brace matching."""
    src = MTV_JS.read_text()
    start = src.index(f"function {name}(")
    depth, i = 0, src.index("{", start)
    while True:
        c = src[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
        i += 1


HARNESS = r"""
%(mulberry32)s
%(creditText)s
var EPOCH = 365472000, SEED = 1981;
function scheduleFor(manifest, num) {
  var vids = manifest.videos || manifest;
  var chans = manifest.channels || null;
  var ids = chans && chans[String(num)];
  var items = vids.filter(function (it) {
    return it.duration > 0 && (!ids || ids.indexOf(it.id) !== -1);
  });
  items.sort(function (a, b) { return a.id < b.id ? -1 : 1; });
  var rnd = mulberry32(SEED + num);
  for (var i = items.length - 1; i > 0; i--) {
    var j = Math.floor(rnd() * (i + 1));
    var t = items[i]; items[i] = items[j]; items[j] = t;
  }
  return items;
}
function onAir(lib, nowSec) {
  var total = lib.reduce(function (s, it) { return s + it.duration; }, 0);
  if (!total) return null;
  var off = (nowSec - EPOCH) %% total;
  for (var i = 0; i < lib.length; i++) {
    if (off < lib[i].duration) return { id: lib[i].id, off: off };
    off -= lib[i].duration;
  }
  return { id: lib[0].id, off: 0 };
}
var input = JSON.parse(require("fs").readFileSync(0, "utf8"));
var out = { order: {}, onair: {}, credits: {} };
input.channels.forEach(function (num) {
  var lib = scheduleFor(input.manifest, num);
  out.order[num] = lib.map(function (it) { return it.id; });
  out.onair[num] = input.timestamps.map(function (t) { return onAir(lib, t); });
});
(input.manifest.videos).forEach(function (it) { out.credits[it.id] = creditText(it); });
process.stdout.write(JSON.stringify(out));
"""


class Parity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads(FIXTURE.read_text())
        harness = HARNESS % {"mulberry32": js_function("mulberry32"),
                             "creditText": js_function("creditText")}
        payload = json.dumps({"manifest": cls.manifest, "channels": CHANNELS,
                              "timestamps": TIMESTAMPS})
        cls.js = json.loads(subprocess.run(
            ["node", "-e", harness], input=payload, text=True,
            capture_output=True, check=True).stdout)

    def test_order_matches_js(self):
        for num in CHANNELS:
            lib = schedule.schedule_for(self.manifest, num)
            self.assertEqual([it["id"] for it in lib], self.js["order"][str(num)], f"ch {num}")

    def test_on_air_matches_js(self):
        for num in CHANNELS:
            lib = schedule.schedule_for(self.manifest, num)
            for t, expect in zip(TIMESTAMPS, self.js["onair"][str(num)]):
                slot = schedule.on_air(lib, t)
                if expect is None:
                    self.assertIsNone(slot)
                    continue
                self.assertEqual(slot["item"]["id"], expect["id"], f"ch {num} @ {t}")
                self.assertAlmostEqual(slot["offset"], expect["off"], places=6)

    def test_credits_match_js(self):
        for it in self.manifest["videos"]:
            self.assertEqual(schedule.credit(it), self.js["credits"][it["id"]], it["id"])

    def test_zero_duration_and_unknown_channel(self):
        ids = [it["id"] for it in schedule.schedule_for(self.manifest, 1)]
        self.assertNotIn("ggg777", ids)
        self.assertEqual(schedule.schedule_for(self.manifest, 4), [])   # ch 4 not in map → full library? no: see below
        self.assertIsNone(schedule.on_air([], 0))


if __name__ == "__main__":
    unittest.main()
```

Note on the last assertion: `mtv.js` airs the **full library** on any channel absent from `manifest.channels` (the `!ids` branch). Channel 4 is not in the fixture map, so its schedule is the full library minus the zero-duration item, not `[]`. Fix the assertion before running:

```python
        self.assertEqual(len(schedule.schedule_for(self.manifest, 4)), 8)
```

- [ ] **Step 3: Run it to verify it fails**

Run (as `agent`): `cd /home/agent/projects/mtv/admin && python3 test_schedule.py -v`
Expected: `ModuleNotFoundError: No module named 'schedule'`

- [ ] **Step 4: Write the port**

`admin/schedule.py`:

```python
"""Python port of the player's broadcast schedule (app/mtv.js `startLocal`).

Must stay bit-for-bit identical to the JavaScript: admin/test_schedule.py runs
both and compares. Change this file and mtv.js together, never one alone.
"""
import re

EPOCH = 365472000   # 1981-08-01, MTV sign-on
SEED = 1981
_MASK = 0xFFFFFFFF


def _imul(a, b):
    return ((a & _MASK) * (b & _MASK)) & _MASK


def mulberry32(seed):
    """Same sequence as the JS mulberry32 — all arithmetic on uint32 bits."""
    a = seed & _MASK

    def rnd():
        nonlocal a
        a = (a + 0x6D2B79F5) & _MASK
        t = a
        t = _imul(t ^ (t >> 15), t | 1)
        t = ((t + _imul(t ^ (t >> 7), t | 61)) ^ t) & _MASK
        return ((t ^ (t >> 14)) & _MASK) / 4294967296
    return rnd


def schedule_for(manifest, num):
    """Deterministic play order for channel `num`: sort by id, seeded shuffle."""
    vids = manifest.get("videos") if isinstance(manifest, dict) else manifest
    vids = vids or []
    chans = manifest.get("channels") if isinstance(manifest, dict) else None
    ids = chans.get(str(num)) if chans else None
    items = [it for it in vids
             if isinstance(it, dict) and (it.get("duration") or 0) > 0
             and (ids is None or it.get("id") in ids)]
    items.sort(key=lambda it: it["id"])
    rnd = mulberry32(SEED + num)
    for i in range(len(items) - 1, 0, -1):
        j = int(rnd() * (i + 1))
        items[i], items[j] = items[j], items[i]
    return items


def on_air(lib, now_sec):
    """{"index", "item", "offset"} for wall-clock `now_sec`, or None if empty."""
    total = sum(it["duration"] for it in lib)
    if not total:
        return None
    off = (now_sec - EPOCH) % total
    for i, it in enumerate(lib):
        if off < it["duration"]:
            return {"index": i, "item": it, "offset": off}
        off -= it["duration"]
    return {"index": 0, "item": lib[0], "offset": 0.0}


_SUFFIX = re.compile(
    r"\s*[\[(](?:(?:official\s+)?(?:music\s+|lyric\s+)?video|official\s+audio|visuali[sz]er|lyrics?)[\])]\s*$",
    re.I)
_SPLIT = re.compile(r"^(.+?)\s+[-–—]\s+(.+)$", re.S)
_QUOTES = re.compile(r'^["“](.*)["”]$', re.S)


def credit(item):
    """Port of creditText: {"artist", "song"}; manifest artist/track win."""
    raw = item if isinstance(item, str) else (item or {}).get("title") or ""
    clean = _SUFFIX.sub("", raw).strip()
    m = _SPLIT.match(clean)
    obj = item if isinstance(item, dict) else {}
    artist = obj.get("artist") or (m.group(1) if m else "")
    song = obj.get("track") or (m.group(2) if m else clean)
    return {"artist": artist, "song": _QUOTES.sub(r"\1", song)}
```

Two JS subtleties the port must keep: JS `.+?` / `.+` do **not** match newlines by default, but titles never contain newlines, so `re.S` is harmless; and JS `%` on a negative left operand is negative — `now_sec` is always after 1981 so it never occurs.

- [ ] **Step 5: Run the test to verify it passes**

Run: `cd /home/agent/projects/mtv/admin && python3 test_schedule.py -v`
Expected: `Ran 4 tests … OK`

If `test_order_matches_js` fails, the PRNG port is off: print the first five values of `mulberry32(1982)()` in both and compare — the usual culprit is a missing `& _MASK` after the addition.

- [ ] **Step 6: Commit**

```bash
cd /home/agent/projects/mtv
git add admin/schedule.py admin/test_schedule.py admin/fixtures/manifest.json
git commit -m "admin: Python port of the broadcast schedule, with JS parity test

The TV will ask the site what is on air instead of running a browser, so the
schedule math needs a server-side copy. The test runs the real mtv.js
functions under node and compares, so the two cannot drift silently."
```

### Task 2: `GET /admin/api/now`

**Files:**
- Modify: `admin/main.py` (after `get_lineup`, ~line 88)
- Modify: `admin/Dockerfile:11` (COPY line)
- Create: `admin/test_now.py`

- [ ] **Step 1: Write the failing endpoint test**

`admin/test_now.py` — uses FastAPI's TestClient against temp config/video dirs. `main.py` hardcodes `/config` and `/videos`; the test monkeypatches the module constants.

```python
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "manifest.json").read_text())
LINEUP = [{"num": 1, "name": "01", "playlist": ""}, {"num": 2, "name": "02", "playlist": ""}]


class NowEndpoint(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "videos").mkdir()
        main.CONFIG_DIR = root
        main.CHANNELS = root / "channels.json"
        main.VIDEOS = root / "videos"
        main.MANIFEST = root / "videos" / "manifest.json"
        main.CHANNELS.write_text(json.dumps(LINEUP))
        main.MANIFEST.write_text(json.dumps(FIXTURE))
        self.c = TestClient(main.app)

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_channel_shape(self):
        r = self.c.get("/admin/api/now")
        self.assertEqual(r.status_code, 200)
        b = r.json()
        self.assertEqual(b["channel"], 1)
        self.assertEqual(b["url_base"], "/videos/")
        for key in ("id", "title", "artist", "song", "offset", "remaining", "duration"):
            self.assertIn(key, b["now"])
        for key in ("id", "title", "artist", "song", "duration"):
            self.assertIn(key, b["next"])
        self.assertAlmostEqual(b["now"]["offset"] + b["now"]["remaining"], b["now"]["duration"], places=3)
        self.assertNotEqual(b["now"]["id"], "ggg777")

    def test_next_follows_now_in_schedule(self):
        b = self.c.get("/admin/api/now?ch=2").json()
        order = [it["id"] for it in main.schedule.schedule_for(FIXTURE, 2)]
        i = order.index(b["now"]["id"])
        self.assertEqual(b["next"]["id"], order[(i + 1) % len(order)])

    def test_unknown_channel_404(self):
        self.assertEqual(self.c.get("/admin/api/now?ch=9").status_code, 404)

    def test_missing_manifest_404(self):
        main.MANIFEST.unlink()
        r = self.c.get("/admin/api/now")
        self.assertEqual(r.status_code, 404)
        self.assertIn("error", r.json())

    def test_missing_lineup_404(self):
        main.CHANNELS.unlink()
        self.assertEqual(self.c.get("/admin/api/now").status_code, 404)

    def test_empty_channel_404(self):
        m = dict(FIXTURE, channels={"1": []})
        main.MANIFEST.write_text(json.dumps(m))
        self.assertEqual(self.c.get("/admin/api/now?ch=1").status_code, 404)


if __name__ == "__main__":
    unittest.main()
```

Dev-box prerequisite: `python3 -c "import fastapi, httpx"`; if missing, `pip install --user fastapi==0.115.12 httpx` (TestClient needs httpx). CI installs the same (Task 4).

- [ ] **Step 2: Run to verify it fails**

Run: `cd /home/agent/projects/mtv/admin && python3 test_now.py -v`
Expected: every test fails with status 404 from FastAPI's default "Not Found" (route missing) — `test_unknown_channel_404` may pass by accident; that's fine.

- [ ] **Step 3: Implement the endpoint**

In `admin/main.py`, add `import schedule` next to the other imports, and after `get_lineup`:

```python
@app.get("/admin/api/now")
def now_playing(ch: int = 1):
    """What channel `ch` is airing at this instant, per the player's schedule
    math (admin/schedule.py == app/mtv.js). Consumed by the living-room Pi's
    screen player and by this page's NOW PLAYING panel."""
    def nope(msg):
        return JSONResponse({"error": msg}, status_code=404)
    if not CHANNELS.exists():
        return nope("no channels.json yet")
    lineup = read_json(CHANNELS)
    if not isinstance(lineup, list):
        return nope("channels.json is unreadable")
    if not any(isinstance(c, dict) and c.get("num") == ch for c in lineup):
        return nope(f"no channel {ch} in the lineup")
    manifest = read_json(MANIFEST)
    if manifest is None:
        return nope("no manifest yet — nothing synced")
    lib = schedule.schedule_for(manifest, ch)
    slot = schedule.on_air(lib, time.time())
    if slot is None:
        return nope(f"channel {ch} has no playable videos")
    now, nxt = slot["item"], lib[(slot["index"] + 1) % len(lib)]

    def describe(it):
        c = schedule.credit(it)
        return {"id": it["id"], "title": it.get("title", ""),
                "artist": c["artist"], "song": c["song"],
                "duration": float(it["duration"])}
    body = describe(now)
    body["offset"] = float(slot["offset"])
    body["remaining"] = float(now["duration"]) - body["offset"]
    return {"channel": ch, "now": body, "next": describe(nxt), "url_base": "/videos/"}
```

`read_json` already exists in `main.py` (used by `get_lineup`/`status`).

- [ ] **Step 4: Run both test files**

Run: `cd /home/agent/projects/mtv/admin && python3 test_now.py -v && python3 test_schedule.py -v`
Expected: `OK` twice.

- [ ] **Step 5: Ship `schedule.py` in the image**

`admin/Dockerfile` line `COPY main.py index.html ./` → `COPY main.py schedule.py index.html ./`

- [ ] **Step 6: Commit**

```bash
cd /home/agent/projects/mtv
git add admin/main.py admin/Dockerfile admin/test_now.py
git commit -m "admin: GET /admin/api/now — what a channel is airing right now

Single source of truth the Pi's mpv player asks instead of rendering the
page in a browser. 404s mirror the player's NO SIGNAL cases."
```

### Task 3: Admin page NOW PLAYING uses the endpoint

**Files:**
- Modify: `admin/index.html:280-322` (delete the JS schedule copy) and `:328-380` (`refreshOnAir`)

- [ ] **Step 1: Delete the duplicated math**

Remove the block from the comment `// ---- now playing: EXACT copy of the player's schedule math (mtv.js) ----` through the end of `function onAir(lib) {…}` (lines ~280–322). Keep `fmtOff`.

- [ ] **Step 2: Rewrite `refreshOnAir`**

Replace the whole `function refreshOnAir() {…}` with:

```javascript
        // ---- now playing: asks /admin/api/now per channel (schedule.py is
        // the server-side twin of mtv.js; no JS copy of the math here) ----
        function refreshOnAir() {
          fetch("/admin/api/lineup")
            .then(function (r) { return r.ok ? r.json() : []; })
            .catch(function () { return []; })
            .then(function (lineup) {
              var el = $("onair");
              if (!lineup.length) { el.textContent = "NO CHANNELS CONFIGURED"; return; }
              return Promise.all(lineup.map(function (ch) {
                return fetch("/admin/api/now?ch=" + encodeURIComponent(ch.num), { cache: "no-store" })
                  .then(function (r) { return r.json().then(function (b) { return { ok: r.ok, b: b }; }); })
                  .catch(function () { return { ok: false, b: { error: "unreachable" } }; });
              })).then(function (results) {
                el.textContent = "";
                lineup.forEach(function (ch, i) {
                  var d = document.createElement("div");
                  var channel = document.createElement("span");
                  channel.className = "monitor-channel";
                  channel.textContent = "CH " + String(ch.num).padStart(2, "0");
                  d.appendChild(channel);
                  var res = results[i];
                  if (res.ok) {
                    var title = document.createElement("span");
                    title.className = "monitor-track";
                    title.textContent = res.b.now.title;
                    d.appendChild(title);
                    var o = document.createElement("span");
                    o.className = "off";
                    o.textContent = fmtOff(res.b.now.offset);
                    d.appendChild(o);
                  } else {
                    var off = document.createElement("span");
                    off.textContent = "Off air · " + (res.b.error || "waiting for videos");
                    d.appendChild(off);
                    d.className = "off";
                  }
                  el.appendChild(d);
                });
              });
            });
        }
```

- [ ] **Step 3: Syntax check and grep for leftovers**

Run: `cd /home/agent/projects/mtv && node --check <(sed -n '/<script>/,/<\/script>/p' admin/index.html | sed '1d;$d') && ! grep -n "mulberry32\|scheduleFor\|EPOCH" admin/index.html && echo clean`
Expected: `clean`

- [ ] **Step 4: Commit**

```bash
git add admin/index.html
git commit -m "admin: NOW PLAYING panel reads /admin/api/now instead of its own schedule copy"
```

### Task 4: CI runs the tests; docs

**Files:**
- Modify: `.forgejo/workflows/ci.yml` (end of the `run:` block)
- Modify: `README.md`, `ARCHITECTURE.md`, `HOWTO.md`, `ROADMAP.md`

- [ ] **Step 1: Add the tests to CI**

Append to the `run:` block in `.forgejo/workflows/ci.yml`:

```yaml
          # schedule port must match mtv.js; endpoint contract
          pip install --quiet fastapi==0.115.12 httpx
          (cd admin && python3 test_schedule.py && python3 test_now.py)
```

- [ ] **Step 2: Docs**

- `README.md`: one line under the admin description: "`GET /admin/api/now?ch=N` — what channel N is airing (used by the living-room Pi)."
- `ARCHITECTURE.md`: in the schedule section, note the Python twin in `admin/schedule.py`, the parity test, and that the admin page now consumes the endpoint. Update the "schedule math is implemented twice" wording.
- `HOWTO.md`: "Check what's on air from a shell: `curl -s 'https://mtv.thelunadog.com/admin/api/now?ch=1' | python3 -m json.tool`" (LAN/VPN only).
- `ROADMAP.md`: History entry for 2026-09-19: endpoint added for the Pi; "Known limits" bullet about the double implementation → now "browser + Python, guarded by `admin/test_schedule.py`".

- [ ] **Step 3: Commit and push**

```bash
git add .forgejo/workflows/ci.yml README.md ARCHITECTURE.md HOWTO.md ROADMAP.md
git commit -m "ci+docs: run schedule parity and endpoint tests; document /admin/api/now"
git push origin main
```

- [ ] **Step 4: Watch the deploy**

CI must pass; `bin/deploy` on isis picks it up within ~3 min. Verify from the Pi (the consumer):

```bash
ssh pi5-alex "curl -s 'https://mtv.thelunadog.com/admin/api/now?ch=1' | python3 -m json.tool | head -20"
```

Expected: JSON with `now.id`, `now.offset`, `next.id`. If 404 `{"detail":"Not Found"}`, the container hasn't been rebuilt yet: `ssh isis-alex 'tail -5 /home/ween/homelab/state/deploy/deploy.log'`.

---

## Chunk 2: smarthome — finish, verify, deploy

The working tree already implements spec §2–§4 (uncommitted, by another session). This chunk reviews it against the spec, fixes the one known defect, gets the tests green, commits, and deploys. **Do not rewrite what exists.**

### Task 5: Baseline the in-progress work

**Files:** none modified

- [ ] **Step 1: Confirm nobody is editing**

Run: `cd /home/alex/projects/smarthome && git status --short && find screen hub tests -newer docs/superpowers/plans/2026-09-19-mtv-mpv-rework.md -type f`
Expected: the 13 modified/deleted files plus `tests/test_screen_player_mtv.py`; the `find` prints nothing (no file newer than this plan). If it prints files, another session is still active — stop and tell the user.

- [ ] **Step 2: Run the existing tests as they stand**

Run: `cd /home/alex/projects/smarthome && python3 -m unittest discover -s tests -p 'test_*.py' -v 2>&1 | tail -25`
Expected: all pass. Record any failures; they are Task 6 inputs.

- [ ] **Step 3: Spec conformance checklist** — read the diff (`git diff`) and tick each:

- [ ] `mtv_now()` GETs `/admin/api/now?ch=`, 5 s timeout, raises `MtvUnavailable`
- [ ] `_play_mtv()` stashes `_mtv = {base, channel, schedule}` and calls `_play(profile="mtv", mode=DRM_MODE, source="mtv", …)`
- [ ] `_build_args("mtv")`: `--idle=yes`, `--prefetch-playlist=yes`, `--keep-open=no`, OSD flags, no `--start`, no file, no `--hls-bitrate`
- [ ] Conductor: persistent socket, `observe_property` time-pos + idle-active, `file-loaded` → refresh, one corrective replace per boundary, terminates mpv on IPC loss, takes `_lock` for globals
- [ ] Conductor ignores `idle-active` before the first `file-loaded` ← **known missing, Task 6**
- [ ] Supervisor: mtv death → `_play_mtv`; `MtvUnavailable` retries ≤60 s then `_stop()`
- [ ] `/play` 502 `{"error": "mtv: …"}` on `MtvUnavailable`; success body has `url/profile/source/title/subtitle`
- [ ] `_swap_kiosk`, `/kiosk/mtv`, `/kiosk/idle`, `MTV_KIOSK_SERVICE` gone; `_kiosk`, `_kiosk_status`, `_kiosk_restart` remain
- [ ] Hub `/api/screen/mtv` posts `{"source":"mtv","url":MTV_URL,"channel":1}` to `/play`, relays title/subtitle and errors; `/api/screen/mtv/stop` deleted
- [ ] `index.html`: `profile === 'mtv'` branch → MTV button highlighted, `showTransport('live')`, no library panel
- [ ] `mtv-screen.service`, `mtv-kiosk.sh` deleted; `Conflicts=` lines and `install-services.sh` cleaned; docstrings updated
- [ ] `docs/mtv-launch.md`, README, ARCHITECTURE, ROADMAP, HOWTO describe the mpv path; ROADMAP marks the `hub/app/mtv_schedule.py` idea superseded

Anything unticked (other than the idle gate) becomes an extra step in Task 6 with the same test-first shape.

### Task 6: Gate `idle-active` behind the first `file-loaded`

**Files:**
- Modify: `screen/screen_player.py` (`MtvConductor.__init__`, `run`)
- Test: `tests/test_screen_player_mtv.py`

- [ ] **Step 1: Write the failing test**

Look at how `tests/test_screen_player_mtv.py` fakes the IPC socket (it has a fake mpv server that records commands and can emit events; reuse its helpers — do not add a second fake). Add:

```python
    def test_initial_idle_active_does_not_reload(self):
        """observe_property idle-active delivers an initial `true` under
        --idle=yes before the first loadfile lands; that must not trigger the
        run-dry recovery (which would double-load the first song)."""
        with self.fake_mpv() as mpv, self.fake_site() as site:
            self.start_conductor(mpv, site)
            mpv.emit({"event": "property-change", "id": 2, "name": "idle-active", "data": True})
            time.sleep(0.3)
            loads = [c for c in mpv.commands if c[0] == "loadfile"]
            self.assertEqual(len(loads), 2, loads)          # initial replace + append only
            self.assertEqual(site.calls, 1)                 # no extra /admin/api/now
            mpv.emit({"event": "file-loaded"})
            time.sleep(0.3)
            mpv.emit({"event": "property-change", "id": 2, "name": "idle-active", "data": True})
            time.sleep(0.3)
            self.assertGreaterEqual(site.calls, 3)          # refresh + idle recovery now allowed
```

Adapt the helper names (`fake_mpv`, `fake_site`, `start_conductor`, `commands`, `calls`, `emit`) to what the file actually provides.

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m unittest tests.test_screen_player_mtv -k initial_idle -v`
Expected: FAIL — `loads` has 4 entries or `site.calls == 2`.

- [ ] **Step 3: Implement the gate**

In `MtvConductor.__init__` add `self.seen_file_loaded = False`. In `run()`:

```python
            if event and event.get("event") == "file-loaded":
                self.seen_file_loaded = True
                self.idle = False
                self._refresh()
            elif event and event.get("event") == "property-change":
                if event.get("name") == "idle-active":
                    # --idle=yes reports idle once at observe time, before our
                    # first loadfile lands; only a *later* idle means run-dry
                    self.idle = bool(event.get("data")) and self.seen_file_loaded
```

- [ ] **Step 4: Run the whole suite**

Run: `python3 -m unittest discover -s tests -p 'test_*.py' -v 2>&1 | tail -15`
Expected: all pass.

- [ ] **Step 5: Commit the implementation (everything in the working tree)**

This commits the other session's work plus the fix; review the diff once more, then:

```bash
cd /home/alex/projects/smarthome
git add -A
git commit -m "mtv: play the broadcast through mpv, drop the chromium kiosk

The kiosk software-decoded at 4K with no audio path — the dead end the
aerials work had already documented. screen-player gets an mtv profile: an
idle mpv fed one song ahead by a conductor over IPC, drift-corrected at each
file boundary against the site's /admin/api/now. The hub launches it like
the jetstream livestream. mtv-screen unit and mtv-kiosk.sh are gone.

Spec: docs/superpowers/specs/2026-09-19-mtv-mpv-rework-design.md

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
git push origin main
```

### Task 7: Deploy to the Pi

**Files:** none (operations)

- [ ] **Step 1: Preconditions**

```bash
ssh pi5-alex 'curl -s localhost:9595/status; echo; systemctl is-active mtv-screen aerial-screen screen-player'
pgrep -af claude | grep -v "$$"
```
Expected: `"playing": false`. If `true`, wait — do not restart anything. Note whether `mtv-screen` is active (it must be stopped before the unit is removed).

- [ ] **Step 2: Pull the code onto the Pi**

```bash
ssh pi5-alex 'cd ~/projects/smarthome && git pull --ff-only && git log --oneline -1'
```

- [ ] **Step 3: Remove the kiosk unit, reinstall the idle units**

```bash
ssh pi5-alex 'sudo systemctl stop mtv-screen 2>/dev/null; sudo systemctl disable mtv-screen 2>/dev/null; sudo rm -f /etc/systemd/system/mtv-screen.service; sudo systemctl daemon-reload; cd ~/projects/smarthome && sudo screen/install-services.sh aerial-screen kiosk-screen && sudo systemctl daemon-reload && systemctl list-unit-files | grep -c mtv-screen'
```
Expected: last line `0`.

- [ ] **Step 4: Restart screen-player (idle only) and rebuild the hub**

```bash
ssh pi5-alex 'cd ~/projects/smarthome && sudo systemctl restart screen-player && sleep 2 && curl -s localhost:9595/status && DOCKER_BUILDKIT=0 docker compose build hub >/dev/null && docker compose up -d hub && sleep 5 && curl -s localhost:8080/api/screen/status'
```
Expected: both status bodies return; `"mtv": true`.

- [ ] **Step 5: Acceptance (spec §5), nobody watching**

```bash
ssh pi5-alex 'curl -sX POST localhost:8080/api/screen/mtv; echo; sleep 8; curl -s localhost:9595/status; echo; grep -c "file-loaded\|MTV" /tmp/screen-player.log 2>/dev/null; tail -5 /tmp/screen-mpv.log'
```
Then in the room:
1. Video with **sound** within ~5 s.
2. Credits legible from the couch; again ~10 s before the song ends.
3. Wait through one song boundary: no black gap, no DRM error in `/tmp/screen-mpv.log`.
4. `ssh pi5-alex top -bn1 | head -12` — no process above ~60%.
5. Phone now-playing tile = `/admin` NOW PLAYING within 2 s; MTV button highlighted, no library panel.
6. Stop from the phone → dashboard returns.

Record the numbers (CPU, boundary behaviour) in `ROADMAP.md` under the 2026-09-19 entry and commit:

```bash
git commit -am "docs(roadmap): MTV via mpv deployed — acceptance numbers"
git push origin main
```

- [ ] **Step 6: Sync the agent user's clone**

```bash
sudo -u agent bash -c 'cd /home/agent/projects/smarthome && git pull --ff-only'
```
