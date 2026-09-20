import json
import importlib.util
import os
import pathlib
import socket
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location("screen_player", _ROOT / "screen" / "screen_player.py")
player = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(player)


SCHEDULE = {
    "channel": 1,
    "now": {
        "id": "now-id",
        "artist": "Artist",
        "song": "Song",
        "offset": 12.5,
        "remaining": 87.5,
        "duration": 100,
    },
    "next": {
        "id": "next-id",
        "artist": "Next Artist",
        "song": "Next Song",
        "duration": 90,
    },
    "url_base": "/videos/",
}


class ScheduleHandler(BaseHTTPRequestHandler):
    response = SCHEDULE
    status = 200

    def do_GET(self):  # noqa: N802
        body = json.dumps(self.response).encode()
        self.send_response(self.status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class ScreenPlayerMtvTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), ScheduleHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.globals = {
            name: getattr(player, name)
            for name in ("_proc", "_profile", "_stopped", "_url", "_source",
                         "_title", "_subtitle", "_mtv")
        }

    def tearDown(self):
        for name, value in self.globals.items():
            setattr(player, name, value)

    def test_mtv_now_reads_schedule(self):
        data = player.mtv_now(self.base, 1)
        self.assertEqual(data["now"]["id"], "now-id")
        self.assertEqual(data["now"]["offset"], 12.5)

    def test_mtv_now_wraps_http_failure(self):
        ScheduleHandler.status = 404
        try:
            with self.assertRaises(player.MtvUnavailable):
                player.mtv_now(self.base, 1)
        finally:
            ScheduleHandler.status = 200

    def test_mtv_profile_starts_idle_without_media_argument(self):
        args = player._build_args("https://example.test/videos/now-id.mp4", None, False, "mtv")
        self.assertIn("--idle=yes", args)
        self.assertIn("--prefetch-playlist=yes", args)
        self.assertNotIn("--hls-bitrate=max", args)
        self.assertNotIn("https://example.test/videos/now-id.mp4", args)

    def test_play_mtv_uses_schedule_offset_profile(self):
        with patch.object(player, "_play") as play:
            result = player._play_mtv(self.base, 1)
        self.assertEqual(result["url"], self.base + "/videos/now-id.mp4")
        play.assert_called_once_with(
            self.base + "/videos/now-id.mp4", profile="mtv", supervise=True,
            mode=player.DRM_MODE, title="Artist", subtitle="Song", source="mtv")

    def test_conductor_refresh_replaces_queued_successor(self):
        proc = object()
        player._proc = proc
        player._profile = "mtv"
        player._stopped = False
        player._url = self.base + "/videos/now-id.mp4"
        player._source = "mtv"
        conductor = player.MtvConductor(proc, {
            "base": self.base,
            "channel": 1,
            "schedule": SCHEDULE,
        })
        commands = []

        def command(value):
            commands.append(value)
            if value == ["get_property", "path"]:
                return {"error": "success", "data": self.base + "/videos/now-id.mp4"}
            if value == ["get_property", "time-pos"]:
                return {"error": "success", "data": 13.0}
            return {"error": "success"}

        conductor.command = command
        with patch.object(player, "mtv_now", return_value=SCHEDULE):
            conductor._refresh()

        self.assertIn(["playlist-clear"], commands)
        self.assertIn(["loadfile", self.base + "/videos/next-id.mp4",
                       "append", -1, "start=0"], commands)
        self.assertNotIn(["loadfile", self.base + "/videos/now-id.mp4",
                          "replace", -1, "start=12.5"], commands)
        with patch.object(player, "_ipc_prop", return_value=None):
            status = player._status()
        self.assertEqual(status["source"], "mtv")
        self.assertEqual(status["title"], "Artist")

    def test_conductor_corrects_wrong_item(self):
        proc = object()
        player._proc = proc
        player._profile = "mtv"
        player._stopped = False
        conductor = player.MtvConductor(proc, {
            "base": self.base,
            "channel": 1,
            "schedule": SCHEDULE,
        })
        commands = []

        def command(value):
            commands.append(value)
            if value == ["get_property", "path"]:
                return {"data": self.base + "/videos/wrong.mp4"}
            if value == ["get_property", "time-pos"]:
                return {"data": 13.0}
            return {"error": "success"}

        conductor.command = command
        with patch.object(player, "mtv_now", return_value=SCHEDULE):
            conductor._refresh()

        self.assertIn(["loadfile", self.base + "/videos/now-id.mp4",
                       "replace", -1, "start=12.5"], commands)

    def test_conductor_uses_persistent_ipc_for_initial_playlist(self):
        class FakeProc:
            terminated = False

            def poll(self):
                return 1 if self.terminated else None

            def terminate(self):
                self.terminated = True

        proc = FakeProc()
        player._proc = proc
        player._profile = "mtv"
        player._stopped = False
        commands = []

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "mpv.sock")
            server = socket.socket(socket.AF_UNIX)
            server.bind(path)
            server.listen(1)

            def serve():
                conn, _ = server.accept()
                with conn:
                    buf = b""
                    while True:
                        chunk = conn.recv(4096)
                        if not chunk:
                            return
                        buf += chunk
                        while b"\n" in buf:
                            line, buf = buf.split(b"\n", 1)
                            request = json.loads(line)
                            commands.append(request["command"])
                            conn.sendall((json.dumps({
                                "error": "success",
                                "request_id": request["request_id"],
                            }) + "\n").encode())

            server_thread = threading.Thread(target=serve, daemon=True)
            server_thread.start()
            conductor = player.MtvConductor(proc, {
                "base": self.base,
                "channel": 1,
                "schedule": SCHEDULE,
            })
            with patch.object(player, "IPC_SOCKET", path):
                conductor.start()
                deadline = time.time() + 2
                while len(commands) < 4 and time.time() < deadline:
                    time.sleep(0.01)
                player._stopped = True
                conductor.closed.set()
                conductor.join(timeout=2)
            server.close()

        load_commands = [command for command in commands if command[0] == "loadfile"]
        self.assertEqual(load_commands, [
            ["loadfile", self.base + "/videos/now-id.mp4", "replace", -1, "start=12.5"],
            ["loadfile", self.base + "/videos/next-id.mp4", "append", -1, "start=0"],
        ])

    def test_schedule_failure_keeps_queue_and_logs_once(self):
        proc = object()
        player._proc = proc
        player._profile = "mtv"
        player._stopped = False
        conductor = player.MtvConductor(proc, {
            "base": self.base,
            "channel": 1,
            "schedule": SCHEDULE,
        })
        commands = []
        conductor.command = lambda value: commands.append(value) or {"data": "current"}
        with patch.object(player, "mtv_now", side_effect=player.MtvUnavailable("down")), \
                patch.object(player, "_log_event") as log:
            conductor._refresh()
            conductor._refresh()
        self.assertEqual(commands, [["get_property", "path"], ["get_property", "path"]])
        log.assert_called_once()

    def test_initial_idle_event_is_ignored_until_first_file_load(self):
        proc = object()
        player._proc = proc
        player._profile = "mtv"
        player._stopped = False
        conductor = player.MtvConductor(proc, {
            "base": self.base,
            "channel": 1,
            "schedule": SCHEDULE,
        })

        conductor._handle_event({
            "event": "property-change", "name": "idle-active", "data": True,
        })
        self.assertFalse(conductor.idle)

        with patch.object(conductor, "_refresh") as refresh:
            conductor._handle_event({"event": "file-loaded"})
        refresh.assert_called_once_with()
        self.assertTrue(conductor.loaded)

        conductor._handle_event({
            "event": "property-change", "name": "idle-active", "data": True,
        })
        self.assertTrue(conductor.idle)


if __name__ == "__main__":
    unittest.main()
