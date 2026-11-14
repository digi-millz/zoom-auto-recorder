import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import webui


class WebUiTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        self.server = webui.make_server(
            schedule_path=self.dir / "schedule.json",
            output_dir=self.dir / "rec",
            log_file=self.dir / "run.log",
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def request(self, path, method="GET", payload=None, *, token=True, content_type="application/json", headers=None):
        body = json.dumps(payload).encode() if payload is not None else None
        request = Request(self.base + path, data=body, method=method)
        if token:
            request.add_header("X-Auth-Token", self.server.state.token)
        if body is not None:
            request.add_header("Content-Type", content_type)
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        with urlopen(request, timeout=5) as response:
            return response.status, response.read().decode()

    def request_error(self, path, method="GET", payload=None, **kwargs):
        with self.assertRaises(HTTPError) as caught:
            self.request(path, method, payload, **kwargs)
        return caught.exception.code, caught.exception.read().decode()

    def test_serves_the_calendar_page(self):
        status, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn("Add calendar event", body)
        self.assertIn("/api/status", body)

    def test_add_list_and_delete_entry(self):
        status, body = self.request(
            "/api/entries",
            method="POST",
            payload={
                "when": "11/9/26 10am",
                "url": "https://us06web.zoom.us/j/85091120436",
                "note": "standup",
            },
        )
        self.assertEqual(status, 201)
        entry = json.loads(body)["entry"]
        self.assertEqual(entry["start"], "2026-11-09 10:00")
        self.assertEqual(entry["status"], "pending")

        status, body = self.request("/api/status")
        payload = json.loads(body)
        self.assertEqual([item["id"] for item in payload["entries"]], [entry["id"]])
        self.assertEqual(payload["next"]["id"], entry["id"])

        status, _ = self.request(f"/api/entries/{entry['id']}", method="DELETE")
        self.assertEqual(status, 200)
        status, body = self.request("/api/status")
        self.assertEqual(json.loads(body)["entries"], [])

    def test_once_repeat_is_accepted_as_no_repeat(self):
        status, body = self.request(
            "/api/entries",
            method="POST",
            payload={
                "when": "2026-11-09 10:00",
                "url": "https://zoom.us/j/1",
                "repeat": "none",
            },
        )
        self.assertEqual(status, 201)
        self.assertIsNone(json.loads(body)["entry"]["repeat"])

    def test_invalid_date_is_rejected(self):
        code, body = self.request_error(
            "/api/entries",
            method="POST",
            payload={"when": "next tuesday", "url": "https://zoom.us/j/1"},
        )
        self.assertEqual(code, 400)
        self.assertIn("cannot parse", body)

    def test_missing_url_is_rejected(self):
        code, _ = self.request_error(
            "/api/entries", method="POST", payload={"when": "2026-11-09 10:00"}
        )
        self.assertEqual(code, 400)

    def test_requires_a_token(self):
        code, _ = self.request_error("/api/status", token=False)
        self.assertEqual(code, 401)

    def test_rejects_foreign_origin(self):
        code, _ = self.request_error(
            "/api/entries",
            method="POST",
            payload={"when": "2026-11-09 10:00", "url": "https://zoom.us/j/1"},
            headers={"Origin": "http://evil.example"},
        )
        self.assertEqual(code, 403)

    def test_rejects_non_json_content_type(self):
        code, _ = self.request_error(
            "/api/entries",
            method="POST",
            payload={"when": "2026-11-09 10:00", "url": "https://zoom.us/j/1"},
            content_type="text/plain",
        )
        self.assertEqual(code, 415)

    def test_stop_without_runner_is_a_no_op(self):
        status, body = self.request("/api/runner/stop", method="POST", payload={})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"stopped": False})


if __name__ == "__main__":
    unittest.main()
