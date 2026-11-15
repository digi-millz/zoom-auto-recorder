"""iCalendar import: Zoom links, timezones, and recurrence that is no longer flattened."""

import threading
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import ics
from ics import parse_events

SAMPLE = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
SUMMARY:Standup
DTSTART:20261109T150000Z
DESCRIPTION:Join https://us06web.zoom.us/j/85091120436?pwd=abc
RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,WE
END:VEVENT
BEGIN:VEVENT
SUMMARY:No link here
DTSTART:20261110T100000
END:VEVENT
BEGIN:VEVENT
SUMMARY:Folded
DTSTART;TZID=Europe/Berlin:20261111T090000
DESCRIPTION:Long text then a link
 https://zoom.us/j/81234567890
END:VEVENT
BEGIN:VEVENT
SUMMARY:Monthly
DTSTART:20261201T150000Z
RRULE:FREQ=MONTHLY;BYMONTHDAY=1
URL:https://zoom.us/j/89712840393
END:VEVENT
END:VCALENDAR
"""


class ParseEventsTests(unittest.TestCase):
    def test_extracts_only_zoom_events(self):
        self.assertEqual(len(parse_events(SAMPLE)), 3, "the event without a link is skipped")

    def test_utc_start_converted_to_local(self):
        event = parse_events(SAMPLE)[0]
        expected = (
            datetime(2026, 11, 9, 15, 0, tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
        )
        self.assertEqual(event.when, expected)
        self.assertEqual(event.url, "https://us06web.zoom.us/j/85091120436?pwd=abc")
        self.assertEqual(event.summary, "Standup")

    def test_recurrence_is_preserved_not_flattened(self):
        event = parse_events(SAMPLE)[0]
        self.assertEqual(event.rrule, "FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,WE")

    def test_tzid_is_kept_and_applied(self):
        event = parse_events(SAMPLE)[1]
        self.assertEqual(event.tz, "Europe/Berlin")
        expected = (
            datetime(2026, 11, 11, 9, 0, tzinfo=ics.ZoneInfo("Europe/Berlin"))
            .astimezone()
            .replace(tzinfo=None)
        )
        self.assertEqual(event.when, expected)
        self.assertEqual(event.url, "https://zoom.us/j/81234567890")

    def test_unsupported_rule_becomes_one_occurrence(self):
        event = parse_events(SAMPLE)[2]
        self.assertIsNone(event.rrule, "BYMONTHDAY cannot be scheduled here")
        self.assertEqual(event.url, "https://zoom.us/j/89712840393")

    def test_empty_calendar(self):
        self.assertEqual(parse_events("BEGIN:VCALENDAR\nEND:VCALENDAR\n"), [])


class FetchTests(unittest.TestCase):
    body = SAMPLE.encode("utf-8")
    seen_headers: dict = {}

    def test_fetches_a_calendar_url(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - stdlib signature
                FetchTests.seen_headers = dict(self.headers)
                self.send_response(200)
                self.send_header("Content-Type", "text/calendar")
                self.send_header("Content-Length", str(len(FetchTests.body)))
                self.end_headers()
                self.wfile.write(FetchTests.body)

            def log_message(self, *args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        try:
            text = ics.fetch(f"http://127.0.0.1:{server.server_address[1]}/cal.ics")
        finally:
            server.shutdown()
            server.server_close()

        self.assertIn("BEGIN:VEVENT", text)
        self.assertEqual(len(parse_events(text)), 3)
        self.assertIn("zoom-auto-recorder", FetchTests.seen_headers.get("User-Agent", ""))
