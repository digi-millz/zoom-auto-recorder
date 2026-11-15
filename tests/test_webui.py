import json
import tempfile
import threading
import unittest
from datetime import date
from unittest.mock import patch
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

    def test_the_microphone_flag_round_trips_through_the_api(self) -> None:
        status, body = self.request(
            "/api/entries",
            method="POST",
            payload={"when": "2026-11-09 10:00", "url": "https://zoom.us/j/1", "mic": True},
        )
        entry = json.loads(body)["entry"]
        self.assertEqual(status, 201)
        self.assertTrue(entry["mic"])
        status, body = self.request(
            f"/api/entries/{entry['id']}", method="POST", payload={"mic": False}
        )
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["entry"]["mic"])

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

    def test_status_exposes_the_default_zone(self):
        _status, body = self.request("/api/status")
        payload = json.loads(body)
        self.assertEqual(payload["default_tz"], "America/New_York")
        self.assertIn("output_dir", payload)

    def test_no_email_is_configured_out_of_the_box(self) -> None:
        # There is deliberately no address baked into the source.
        _status, body = self.request("/api/status")
        self.assertEqual(json.loads(body)["default_email"], "")

    def test_settings_store_the_zoom_email(self) -> None:
        status, body = self.request(
            "/api/settings", method="POST", payload={"email": "me@example.com"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["email"], "me@example.com")
        _status, state = self.request("/api/status")
        self.assertEqual(json.loads(state)["default_email"], "me@example.com")

    def test_an_entry_keeps_its_own_email(self) -> None:
        self.request(
            "/api/entries",
            method="POST",
            payload={
                "when": "2026-11-18 10:00",
                "url": "https://zoom.us/j/1",
                "email": "guest@example.com",
            },
        )
        _status, body = self.request("/api/status")
        self.assertEqual(json.loads(body)["entries"][0]["email"], "guest@example.com")

    def test_garbage_email_is_rejected(self) -> None:
        code, _body = self.request_error(
            "/api/settings", method="POST", payload={"email": "not-an-email"}
        )
        self.assertEqual(code, 400)
        code, _body = self.request_error(
            "/api/entries",
            method="POST",
            payload={"when": "2026-11-18 10:00", "url": "https://zoom.us/j/1", "email": "bad"},
        )
        self.assertEqual(code, 400)

    def test_settings_store_the_notification_url(self) -> None:
        status, body = self.request(
            "/api/settings",
            method="POST",
            payload={"notify_url": "https://hooks.example.com/abc"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["notify_url"], "https://hooks.example.com/abc")
        _status, state = self.request("/api/status")
        self.assertEqual(json.loads(state)["notify_url"], "https://hooks.example.com/abc")

    def test_a_bad_notification_url_is_rejected(self) -> None:
        code, _body = self.request_error(
            "/api/settings", method="POST", payload={"notify_url": "hooks.example.com/abc"}
        )
        self.assertEqual(code, 400)

    def test_settings_store_the_calendar_feed(self) -> None:
        status, body = self.request(
            "/api/settings",
            method="POST",
            payload={"calendar_url": "https://calendar.example.com/basic.ics"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(body)["calendar_url"], "https://calendar.example.com/basic.ics"
        )
        _status, state = self.request("/api/status")
        self.assertEqual(
            json.loads(state)["calendar_url"], "https://calendar.example.com/basic.ics"
        )

    def test_a_bad_calendar_url_is_rejected(self) -> None:
        code, _body = self.request_error(
            "/api/settings", method="POST", payload={"calendar_url": "calendar.example.com"}
        )
        self.assertEqual(code, 400)

    def test_preview_returns_occurrences_for_a_rule(self):
        status, body = self.request(
            "/api/preview",
            method="POST",
            payload={
                "start": "2026-11-18 10:00",
                "tz": "America/New_York",
                "rrule": "FREQ=WEEKLY;INTERVAL=2;BYDAY=WE",
            },
        )
        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["occurrences"]), 5)
        self.assertIn("America/New_York", payload["summary"])
        days = [date.fromisoformat(moment[:10]) for moment in payload["occurrences"]]
        gaps = [(later - earlier).days for earlier, later in zip(days, days[1:])]
        self.assertTrue(all(13 <= gap <= 15 for gap in gaps), gaps)

    def test_preview_without_a_rule_is_empty(self):
        status, body = self.request(
            "/api/preview", method="POST", payload={"start": "2026-11-18 10:00", "rrule": ""}
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["occurrences"], [])

    def test_bad_zone_is_rejected(self):
        code, body = self.request_error(
            "/api/preview",
            method="POST",
            payload={"start": "2026-11-18 10:00", "tz": "Mars/Olympus", "rrule": "FREQ=DAILY"},
        )
        self.assertEqual(code, 400)
        self.assertIn("timezone", body.lower())

    def test_entry_keeps_its_zone_and_rule(self):
        self.request(
            "/api/entries",
            method="POST",
            payload={
                "when": "2026-11-18 10:00",
                "url": "https://zoom.us/j/123",
                "rrule": "FREQ=WEEKLY;INTERVAL=2;BYDAY=WE",
                "tz": "America/New_York",
            },
        )
        _status, body = self.request("/api/status")
        entry = json.loads(body)["entries"][0]
        self.assertEqual(entry["rrule"], "FREQ=WEEKLY;INTERVAL=2;BYDAY=WE")
        self.assertEqual(entry["tz"], "America/New_York")

    def test_edit_an_entry(self) -> None:
        _status, body = self.request(
            "/api/entries",
            method="POST",
            payload={"when": "2026-11-18 10:00", "url": "https://zoom.us/j/1", "note": "typo"},
        )
        entry_id = json.loads(body)["entry"]["id"]
        status, body = self.request(
            f"/api/entries/{entry_id}",
            method="POST",
            payload={"when": "2026-11-19 12:00", "note": "fixed"},
        )
        self.assertEqual(status, 200)
        entry = json.loads(body)["entry"]
        self.assertEqual(entry["start"], "2026-11-19 12:00")
        self.assertEqual(entry["note"], "fixed")
        self.assertEqual(entry["url"], "https://zoom.us/j/1", "untouched fields survive")

    def test_editing_an_unknown_entry_is_a_404(self) -> None:
        code, _body = self.request_error(
            "/api/entries/nosuchid", method="POST", payload={"note": "x"}
        )
        self.assertEqual(code, 404)

    def test_a_bad_edit_is_a_400(self) -> None:
        _status, body = self.request(
            "/api/entries",
            method="POST",
            payload={"when": "2026-11-18 10:00", "url": "https://zoom.us/j/1"},
        )
        entry_id = json.loads(body)["entry"]["id"]
        code, _body = self.request_error(
            f"/api/entries/{entry_id}", method="POST", payload={"email": "nope"}
        )
        self.assertEqual(code, 400)

    def write_shot(self, session: str = "20261114-194039", cls: str = "zRichMsgBoxClass") -> bytes:
        directory = self.dir / "rec" / "_shots" / session
        directory.mkdir(parents=True, exist_ok=True)
        image = directory / f"194041_00360d4e_before_{cls}.png"
        payload = b"\x89PNG\r\n\x1a\nfake"
        image.write_bytes(payload)
        return payload

    def test_shots_are_listed_and_served_with_the_query_token(self) -> None:
        payload = self.write_shot()
        status, body = self.request("/api/shots")
        listed = json.loads(body)["shots"]
        self.assertEqual(status, 200)
        self.assertEqual(listed[0]["cls"], "zRichMsgBoxClass")

        with urlopen(self.base + listed[0]["url"] + "?t=" + self.server.state.token, timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), payload)
        code, _body = self.request_error(listed[0]["url"])
        self.assertEqual(code, 401)

    def test_a_taught_rule_is_appended_and_a_denylisted_label_is_refused(self) -> None:
        rules = self.dir / "rules.json"
        rules.write_text(
            json.dumps({"version": 1, "deny": ["Cancel"], "rules": []}), encoding="utf-8"
        )
        with patch.object(webui, "RULES_FILE", rules):
            status, body = self.request(
                "/api/rules",
                method="POST",
                payload={"cls": "zRichMsgBoxClass", "label": "Acknowledge", "scope": "dialog"},
            )
            refused, _body = self.request_error(
                "/api/rules",
                method="POST",
                payload={"cls": "zRichMsgBoxClass", "label": "Cancel", "scope": "dialog"},
            )
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(body)["rule"]["match"]["text"], ["Acknowledge"])
        self.assertEqual(refused, 400)
        self.assertEqual(len(json.loads(rules.read_text(encoding="utf-8"))["rules"]), 1)

    def test_a_shot_path_cannot_escape_the_folder(self) -> None:
        self.write_shot()
        code, _body = self.request_error(
            "/api/shots/20261114-194039/../../schedule.json?t=" + self.server.state.token
        )
        self.assertEqual(code, 404)



    def test_occurrences_fill_every_repeat_day_of_the_month(self) -> None:
        _status, body = self.request(
            "/api/entries",
            method="POST",
            payload={
                "when": "2026-11-02 10:00",
                "url": "https://us06web.zoom.us/j/85091120436",
                "note": "standup",
                "rrule": "FREQ=WEEKLY;BYDAY=MO,WE",
            },
        )
        entry = json.loads(body)["entry"]

        status, body = self.request("/api/occurrences?month=2026-11")
        days = [item["start"][:10] for item in json.loads(body)["occurrences"]]
        self.assertEqual(status, 200)
        self.assertEqual(days[:3], ["2026-11-02", "2026-11-04", "2026-11-09"])
        self.assertEqual(len(days), 9)
        self.assertEqual(set(item["id"] for item in json.loads(body)["occurrences"]), {entry["id"]})

        code, _body = self.request_error("/api/occurrences?month=November")
        self.assertEqual(code, 400)

if __name__ == "__main__":
    unittest.main()
