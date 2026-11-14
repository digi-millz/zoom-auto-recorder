import unittest
from datetime import datetime, timezone

from ics import parse_events

SAMPLE = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
SUMMARY:Standup
DTSTART:20261109T150000Z
DESCRIPTION:Join https://us06web.zoom.us/j/85091120436?pwd=abc
RRULE:FREQ=WEEKLY
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
END:VCALENDAR
"""


class ParseEventsTests(unittest.TestCase):
    def test_extracts_only_zoom_events(self):
        events = parse_events(SAMPLE)
        self.assertEqual(len(events), 2)

    def test_utc_start_converted_to_local(self):
        event = parse_events(SAMPLE)[0]
        expected = (
            datetime(2026, 11, 9, 15, 0, tzinfo=timezone.utc)
            .astimezone()
            .replace(tzinfo=None)
        )
        self.assertEqual(event.when, expected)
        self.assertEqual(event.url, "https://us06web.zoom.us/j/85091120436?pwd=abc")
        self.assertEqual(event.summary, "Standup")
        self.assertEqual(event.repeat, "weekly")

    def test_folded_description_still_yields_link(self):
        events = parse_events(SAMPLE)
        self.assertEqual(events[1].url, "https://zoom.us/j/81234567890")
        self.assertIsNone(events[1].repeat)

    def test_empty_calendar(self):
        self.assertEqual(parse_events("BEGIN:VCALENDAR\nEND:VCALENDAR\n"), [])


if __name__ == "__main__":
    unittest.main()
