import io
import unittest
from datetime import datetime, timedelta

from rich.console import Console

import scheduler


class ParseStartTests(unittest.TestCase):
    def test_parses_naive_local_datetime(self):
        self.assertEqual(scheduler.parse_start("2026-11-09 18:30"), datetime(2026, 11, 9, 18, 30))

    def test_strips_surrounding_whitespace(self):
        self.assertEqual(scheduler.parse_start("  2026-11-09 08:05 "), datetime(2026, 11, 9, 8, 5))

    def test_rejects_other_formats(self):
        with self.assertRaises(scheduler.ScheduleError):
            scheduler.parse_start("09.11.2026 18:30")


class ParseWhenTests(unittest.TestCase):
    def test_iso_minute(self):
        self.assertEqual(scheduler.parse_when("2026-11-09 10:00"), datetime(2026, 11, 9, 10, 0))

    def test_iso_with_t(self):
        self.assertEqual(scheduler.parse_when("2026-11-09T10:00"), datetime(2026, 11, 9, 10, 0))

    def test_us_short_date_with_meridiem(self):
        self.assertEqual(scheduler.parse_when("11/9/26 10am"), datetime(2026, 11, 9, 10, 0))

    def test_us_short_date_with_pm(self):
        self.assertEqual(scheduler.parse_when("11/9/26 10:30 pm"), datetime(2026, 11, 9, 22, 30))

    def test_missing_year_uses_current_year(self):
        now = datetime(2026, 11, 9, 9, 0)
        self.assertEqual(scheduler.parse_when("11/9 10:00", now=now), datetime(2026, 11, 9, 10, 0))

    def test_rejects_unparseable(self):
        with self.assertRaises(scheduler.ScheduleError):
            scheduler.parse_when("next tuesday")


class SecondsUntilTests(unittest.TestCase):
    def test_future_and_past(self):
        now = datetime(2026, 1, 1, 12, 0, 0)
        self.assertEqual(scheduler.seconds_until(now + timedelta(seconds=90), now), 90)
        self.assertEqual(scheduler.seconds_until(now - timedelta(seconds=5), now), -5)


class FormatDeltaTests(unittest.TestCase):
    def test_rounds_up_to_the_next_second(self):
        self.assertEqual(scheduler.format_delta(0), "00:00:00")
        self.assertEqual(scheduler.format_delta(0.2), "00:00:01")

    def test_hours_and_days(self):
        self.assertEqual(scheduler.format_delta(3661), "01:01:01")
        self.assertEqual(scheduler.format_delta(2 * 86400 + 3723), "2d 01:02:03")


class WaitWithCountdownTests(unittest.TestCase):
    def setUp(self):
        self.console = Console(file=io.StringIO(), width=100)

    def test_returns_immediately_when_deadline_passed(self):
        slept = []
        scheduler.wait_with_countdown(
            datetime(2026, 1, 1, 10, 0, 0),
            console=self.console,
            sleep=slept.append,
            clock=lambda: datetime(2026, 1, 1, 10, 0, 5),
        )
        self.assertEqual(slept, [])

    def test_stops_exactly_at_the_deadline_without_long_sleeps(self):
        now = [datetime(2026, 1, 1, 10, 0, 0)]
        target = datetime(2026, 1, 1, 10, 0, 2)
        slept = []

        def fake_sleep(seconds):
            slept.append(seconds)
            now[0] += timedelta(seconds=seconds)

        scheduler.wait_with_countdown(
            target,
            console=self.console,
            tick=0.5,
            sleep=fake_sleep,
            clock=lambda: now[0],
        )

        self.assertAlmostEqual(sum(slept), 2.0)
        self.assertEqual(now[0], target)
        self.assertTrue(all(0 < seconds <= 0.5 for seconds in slept))


class NextOccurrenceTests(unittest.TestCase):
    def test_weekly_advances_seven_days(self):
        start = datetime(2026, 11, 9, 10, 0)
        self.assertEqual(
            scheduler.next_occurrence(start, "weekly", datetime(2026, 11, 9, 11, 0)),
            datetime(2026, 11, 16, 10, 0),
        )

    def test_weekdays_friday_rolls_to_monday(self):
        friday = datetime(2026, 11, 13, 10, 0)
        self.assertEqual(
            scheduler.next_occurrence(friday, "weekdays", datetime(2026, 11, 13, 11, 0)),
            datetime(2026, 11, 16, 10, 0),
        )

    def test_unknown_repeat_rejected(self):
        with self.assertRaises(scheduler.ScheduleError):
            scheduler.next_occurrence(
                datetime(2026, 11, 9), "monthly", datetime(2026, 11, 9)
            )


if __name__ == "__main__":
    unittest.main()
