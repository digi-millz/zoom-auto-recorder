"""Notification tests: message shape, a real POST to a local endpoint, retries, and no exceptions."""

from __future__ import annotations

import json
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import notify


class _Handler(BaseHTTPRequestHandler):
    status = 200
    bodies: list[dict] = []

    def do_POST(self) -> None:  # noqa: N802 - stdlib signature
        length = int(self.headers.get("Content-Length") or 0)
        payload = self.rfile.read(length)
        _Handler.bodies.append(json.loads(payload))
        self.send_response(_Handler.status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args) -> None:  # keep the test output clean
        return


def start_server(status: int = 200) -> tuple[ThreadingHTTPServer, str]:
    _Handler.status = status
    _Handler.bodies = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/hook"


def closed_port_url() -> str:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}/hook"


class MessageTests(unittest.TestCase):
    def test_done_message_names_the_meeting_and_file(self) -> None:
        text = notify.Event(notify.DONE, meeting="Weekly sync", path="C:/rec/a.mp4").text()
        self.assertIn("recording finished", text)
        self.assertIn("Weekly sync", text)
        self.assertIn("C:/rec/a.mp4", text)

    def test_failed_message_shows_the_exit_code(self) -> None:
        text = notify.Event(notify.FAILED, meeting="Board", exit_code=5).text()
        self.assertIn("failed", text)
        self.assertIn("exit 5", text)

    def test_needs_attention_carries_the_detail(self) -> None:
        text = notify.Event(
            notify.NEEDS_ATTENTION, meeting="Zoom Workplace", detail="Something unknown"
        ).text()
        self.assertIn("needs attention", text)
        self.assertIn("Something unknown", text)

    def test_payload_carries_both_text_keys(self) -> None:
        payload = notify.Event(notify.DONE, meeting="x", path="a.mp4").payload()
        self.assertEqual(payload["text"], payload["content"], "Slack uses text, Discord content")
        self.assertEqual(payload["event"], notify.DONE)
        self.assertEqual(payload["path"], "a.mp4")


class SendTests(unittest.TestCase):
    def test_posts_the_event(self) -> None:
        server, url = start_server()
        try:
            ok = notify.send(url, notify.Event(notify.DONE, meeting="Weekly", path="a.mp4"))
        finally:
            server.shutdown()
            server.server_close()
        self.assertTrue(ok)
        self.assertEqual(len(_Handler.bodies), 1)
        self.assertEqual(_Handler.bodies[0]["meeting"], "Weekly")
        self.assertEqual(_Handler.bodies[0]["event"], "done")

    def test_server_error_is_retried_once_then_reported(self) -> None:
        server, url = start_server(status=500)
        try:
            ok = notify.send(url, notify.Event(notify.FAILED, meeting="x"))
        finally:
            server.shutdown()
            server.server_close()
        self.assertFalse(ok)
        self.assertEqual(len(_Handler.bodies), 2, "one retry, not a loop")

    def test_empty_url_does_nothing(self) -> None:
        self.assertFalse(notify.send("", notify.Event(notify.DONE)))

    def test_unreachable_endpoint_never_raises(self) -> None:
        self.assertFalse(notify.send(closed_port_url(), notify.Event(notify.DONE, meeting="x")))


if __name__ == "__main__":
    unittest.main()
