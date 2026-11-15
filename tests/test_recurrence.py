"""Recurrence tests: DST, intervals, UNTIL/COUNT, legacy mapping, bad input.

The DST cases are the point: "every 2 weeks at 10:00 in Europe/London" must stay at 10:00 local, and the machine
running the recorder may be in a different zone entirely.
"""

from __future__ import annotations

import unittest
from datetime import date, datetime

import recurrence
from scheduler import ScheduleError


class LegacyMappingTests(unittest.TestCase):
    def test_weekly_and_weekdays_map_to_rules(self) -> None:
        self.assertEqual(recurrence.legacy_repeat_to_rrule("weekly"), "FREQ=WEEKLY")
        self.assertEqual(
            recurrence.legacy_repeat_to_rrule("weekdays"), "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"
        )
        self.assertIsNone(recurrence.legacy_repeat_to_rrule(None))
        self.assertIsNone(recurrence.legacy_repeat_to_rrule("monthly"))  # not silently guessed


class DstTests(unittest.TestCase):
    def test_wall_clock_survives_the_spring_change(self) -> None:
        # Europe/London clocks go forward on 2026-03-29; the series must straddle it.
        from datetime import timedelta

        rule = recurrence.build(
            "FREQ=WEEKLY;INTERVAL=2;BYDAY=WE", "2026-03-04 10:00", "Europe/London"
        )
        moments = list(rule.xafter(rule[0] - timedelta(seconds=1), count=4))
        self.assertEqual({m.hour for m in moments}, {10}, [str(m) for m in moments])
        self.assertEqual({m.minute for m in moments}, {0})
        offsets = {m.utcoffset() for m in moments}
        self.assertEqual(len(offsets), 2, f"DST boundary not crossed: {offsets}")

    def test_occurrences_are_naive_machine_local(self) -> None:
        occ = recurrence.occurrences("FREQ=WEEKLY", "2026-11-04 10:00", "UTC", count=2)
        self.assertEqual(len(occ), 2)
        self.assertIsNone(occ[0].tzinfo, "the store compares naive local datetimes")


    def test_interval_is_two_weeks_not_one(self) -> None:
        occ = recurrence.occurrences(
            "FREQ=WEEKLY;INTERVAL=2;BYDAY=WE", "2026-11-04 10:00", "Europe/London", count=3
        )
        gaps = [(b - a).days for a, b in zip(occ, occ[1:])]
        self.assertTrue(all(13 <= gap <= 15 for gap in gaps), gaps)

    def test_utc_until_is_normalised_from_local_time(self) -> None:
        # 2026-04-01 12:00 London is BST (+01:00) => 11:00Z.
        spec = recurrence.normalize_spec("FREQ=WEEKLY;UNTIL=20260401T120000", "Europe/London")
        self.assertIn("UNTIL=20260401T110000Z", spec)


class LimitTests(unittest.TestCase):
    def test_count_stops_the_series(self) -> None:
        occ = recurrence.occurrences(
            "FREQ=WEEKLY;COUNT=2", "2026-01-07 09:00", "UTC", count=5
        )
        self.assertEqual(len(occ), 2)

    def test_until_stops_the_series(self) -> None:
        occ = recurrence.occurrences(
            "FREQ=WEEKLY;UNTIL=20260121T090000Z", "2026-01-07 09:00", "UTC", count=10
        )
        self.assertEqual(len(occ), 3)  # Jan 7, 14, 21

    def test_next_after_now_skips_past_occurrences(self) -> None:
        nxt = recurrence.next_start_local(
            "FREQ=DAILY", "2026-01-01 08:00", "UTC", after=datetime(2026, 6, 1, 0, 0)
        )
        self.assertIsNotNone(nxt)
        assert nxt is not None
        self.assertGreater(nxt, datetime(2026, 6, 1, 0, 0))
        self.assertLess(nxt, datetime(2026, 6, 3, 0, 0))


class ValidationTests(unittest.TestCase):
    def test_unknown_timezone_is_rejected(self) -> None:
        with self.assertRaises(ScheduleError):
            recurrence.zone("Mars/Olympus_Mons")

    def test_missing_freq_is_rejected(self) -> None:
        with self.assertRaises(ScheduleError):
            recurrence.build("BYDAY=WE", "2026-01-01 10:00", "UTC")

    def test_garbage_until_is_rejected(self) -> None:
        with self.assertRaises(ScheduleError):
            recurrence.normalize_spec("FREQ=WEEKLY;UNTIL=not-a-date", "UTC")

    def test_describe_is_human_readable(self) -> None:
        text = recurrence.describe("FREQ=WEEKLY;INTERVAL=2;BYDAY=WE", "Europe/London")
        self.assertIn("Weekly", text)
        self.assertIn("every 2", text)
        self.assertIn("Europe/London", text)


class OccurrencesBetweenTests(unittest.TestCase):
    """The calendar asks for a month and must get every occurrence in it, including both edges."""

    def test_weekly_series_fills_the_window(self) -> None:
        days = recurrence.occurrences_between(
            "FREQ=WEEKLY;BYDAY=MO,WE",
            "2026-11-02 10:00",
            None,
            first_day=date(2026, 11, 1),
            last_day=date(2026, 11, 30),
        )
        self.assertEqual(
            [moment.strftime("%Y-%m-%d %H:%M") for moment in days[:4]],
            ["2026-11-02 10:00", "2026-11-04 10:00", "2026-11-09 10:00", "2026-11-11 10:00"],
        )
        self.assertTrue(all(moment.month == 11 for moment in days))
        self.assertEqual(len(days), 9)  # Nov 2..30, Mon+Wed

    def test_a_month_without_an_occurrence_is_empty(self) -> None:
        self.assertEqual(
            recurrence.occurrences_between(
                "FREQ=WEEKLY;UNTIL=20261110T000000Z",
                "2026-11-02 10:00",
                "UTC",
                first_day=date(2026, 12, 1),
                last_day=date(2026, 12, 31),
            ),
            [],
        )



if __name__ == "__main__":
    unittest.main()
