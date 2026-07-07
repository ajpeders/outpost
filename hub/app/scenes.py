"""Shortcuts: one tap -> a sequence of appletv + cec actions."""
from __future__ import annotations

import asyncio
import json
import os


DEFAULT_SCENES: dict = {
    "movie": {
        "label": "Movie Night",
        "steps": [
            {"svc": "cec", "method": "POST", "path": "/api/tv/on"},
            {"svc": "atv", "method": "POST", "path": "/api/power/on"},
            {"delay": 2.5},
            {"svc": "atv", "method": "POST", "path": "/api/launch/com.plexapp.plex"},
        ],
    },
    "youtube": {
        "label": "YouTube",
        "steps": [
            {"svc": "cec", "method": "POST", "path": "/api/tv/on"},
            {"svc": "atv", "method": "POST", "path": "/api/power/on"},
            {"delay": 2.5},
            {"svc": "atv", "method": "POST", "path": "/api/launch/com.google.ios.youtube"},
        ],
    },
    "off": {
        "label": "Everything Off",
        "steps": [
            {"svc": "atv", "method": "POST", "path": "/api/power/off"},
            {"svc": "cec", "method": "POST", "path": "/api/tv/off"},
        ],
    },
}


def load_scenes() -> dict:
    path = os.environ.get("SCENES_FILE", "/data/scenes.json")
    try:
        with open(path) as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError):
        return DEFAULT_SCENES


async def run_scene(name: str, client, atv_url: str, cec_url: str) -> dict:
    scenes = load_scenes()
    scene = scenes.get(name)
    if scene is None:
        raise KeyError(name)

    base = {"atv": atv_url, "cec": cec_url}
    results: list[dict] = []
    for step in scene["steps"]:
        if "delay" in step:
            await asyncio.sleep(float(step["delay"]))
            continue
        url = base[step["svc"]] + step["path"]
        try:
            r = await client.request(step.get("method", "POST"), url)
            results.append({
                "step": f'{step["svc"]}{step["path"]}',
                "status": r.status_code,
                "ok": r.is_success,
            })
        except Exception as exc:
            results.append({
                "step": f'{step["svc"]}{step["path"]}',
                "status": None,
                "ok": False,
                "error": str(exc),
            })
    return {"scene": name, "label": scene.get("label", name), "results": results}
