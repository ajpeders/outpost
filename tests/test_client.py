"""Unit tests for the smoke-test client (`test-client.py`).

Everything here mocks `urllib.request.urlopen`, so the tests never contact the
Pi and never issue hardware commands. Run with:

    python3 -m unittest discover -s tests -p test_client.py
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import pathlib
import unittest
import urllib.error
import urllib.request
from unittest import mock

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("test_client", _ROOT / "test-client.py")
tc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tc)

ATV, CEC = "http://atv.test", "http://cec.test"


class _Resp:
    """Minimal stand-in for the object urlopen() returns (context manager)."""

    def __init__(self, status: int, body: bytes = b"{}") -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *exc) -> bool:
        return False


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://x", code, "err", {},
                                  io.BytesIO(b'{"error":"boom"}'))


def _url(req) -> str:
    return getattr(req, "full_url", req)


class CallTests(unittest.TestCase):
    def test_success_returns_status_and_body(self):
        with mock.patch("urllib.request.urlopen", return_value=_Resp(200, b'{"ok":true}')):
            self.assertEqual(tc.call("GET", "http://x/api/status"), (200, '{"ok":true}'))

    def test_http_error_returns_code_and_body(self):
        with mock.patch("urllib.request.urlopen", side_effect=_http_error(500)):
            status, body = tc.call("GET", "http://x/api/status")
        self.assertEqual(status, 500)
        self.assertIn("boom", body)

    def test_connection_error_returns_none(self):
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
            status, body = tc.call("GET", "http://x/api/status")
        self.assertIsNone(status)
        self.assertIn("refused", body)


class MainExitStatusTests(unittest.TestCase):
    def _run(self, responder, argv):
        def fake(req, timeout=None):
            return responder(_url(req))
        with mock.patch("urllib.request.urlopen", side_effect=fake), \
                contextlib.redirect_stdout(io.StringIO()):
            return tc.main(["--atv", ATV, "--cec", CEC, *argv])

    def test_all_ok_exits_zero(self):
        self.assertEqual(self._run(lambda url: _Resp(200), ["sweep"]), 0)

    def test_one_failure_in_multi_request_sweep_exits_nonzero(self):
        def responder(url):
            return _Resp(500) if url.endswith("/api/apps") else _Resp(200)
        self.assertEqual(self._run(responder, ["sweep"]), 1)

    def test_connection_failure_exits_nonzero(self):
        def responder(url):
            raise urllib.error.URLError("refused")
        self.assertEqual(self._run(responder, ["sweep"]), 1)

    def test_unknown_command_exits_two(self):
        self.assertEqual(self._run(lambda url: _Resp(200), ["bogus"]), 2)


if __name__ == "__main__":
    unittest.main()
