import json
import os
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

import store as store_module
from scheduler import ScheduleError
from store import (
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RUNNING,
    Entry,
    RunnerLock,
    ScheduleStore,
    StoreError,
    clear_stop,
    next_action,
    request_stop,
    split_entry_spec,
    stop_requested,
)


NOW = datetime(2026, 11, 9, 18, 0, 0)


def entry(entry_id, start, status=STATUS_PENDING):
    return Entry(id=entry_id, url=f"https://zoom.us/j/{entry_id}", start=start, status=status)


def chosen(action_result):
    action, item = action_result
    assert item is not None, action
    return item


class StorePersistenceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "schedule.json"

    def fetch(self, entry_id):
        store = ScheduleStore(self.path)
        loaded = store.get(entry_id)
        self.assertIsNotNone(loaded)
        assert loaded is not None
        return loaded

    def test_add_persists_across_instances(self):
        store = ScheduleStore(self.path)
        added = store.add(
            url="https://zoom.us/j/81234567890",
            start_at="2026-11-09 18:30",
            output_dir="./rec",
            note="weekly",
        )

        reloaded = self.fetch(added.id)
        self.assertEqual(reloaded.url, "https://zoom.us/j/81234567890")
        self.assertEqual(reloaded.start, "2026-11-09 18:30")
        self.assertEqual(reloaded.output_dir, "./rec")
        self.assertEqual(reloaded.note, "weekly")
        self.assertEqual(reloaded.status, STATUS_PENDING)

    def test_add_rejects_invalid_start(self):
        store = ScheduleStore(self.path)
        with self.assertRaises(ScheduleError):
            store.add(url="https://zoom.us/j/1", start_at="09.11.2026 18:30")

    def test_remove(self):
        store = ScheduleStore(self.path)
        added = store.add(url="https://zoom.us/j/1", start_at="2026-11-09 18:30")
        self.assertTrue(store.remove(added.id))
        self.assertIsNone(ScheduleStore(self.path).get(added.id))
        self.assertFalse(store.remove("missing"))

    def test_mark_persists_status_code_and_path(self):
        store = ScheduleStore(self.path)
        added = store.add(url="https://zoom.us/j/1", start_at="2026-11-09 18:30")
        store.mark(added.id, STATUS_DONE, exit_code=0, path="C:/rec/a.mkv")

        saved = self.fetch(added.id)
        self.assertEqual(saved.status, STATUS_DONE)
        self.assertEqual(saved.last_exit_code, 0)
        self.assertEqual(saved.last_path, "C:/rec/a.mkv")

    def test_save_is_atomic_and_leaves_no_temp_file(self):
        store = ScheduleStore(self.path)
        store.add(url="https://zoom.us/j/1", start_at="2026-11-09 18:30")
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])
        self.assertEqual(json.loads(self.path.read_text())["version"], 1)

    def test_corrupt_file_raises_store_error(self):
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(StoreError):
            ScheduleStore(self.path)

    def test_recover_abandoned_fails_running_entries_only(self):
        store = ScheduleStore(self.path)
        running = store.add(url="https://zoom.us/j/1", start_at="2026-11-09 18:30")
        pending = store.add(url="https://zoom.us/j/2", start_at="2026-11-09 19:30")
        store.mark(running.id, STATUS_RUNNING)

        self.assertEqual(store.recover_abandoned(), 1)
        self.assertEqual(self.fetch(running.id).status, STATUS_FAILED)
        self.assertEqual(self.fetch(pending.id).status, STATUS_PENDING)


class NextActionTests(unittest.TestCase):
    def test_idle_when_nothing_pending(self):
        self.assertEqual(next_action([], NOW, 600), ("idle", None))
        done = entry("a", "2026-11-09 17:00", STATUS_DONE)
        self.assertEqual(next_action([done], NOW, 600), ("idle", None))

    def test_wait_for_future_entry(self):
        item = entry("a", "2026-11-09 18:05")
        self.assertEqual(chosen(next_action([item], NOW, 600)).id, "a")
        self.assertEqual(next_action([item], NOW, 600)[0], "wait")

    def test_due_when_late_within_limit(self):
        item = entry("a", "2026-11-09 17:55")  # 5 min late
        self.assertEqual(next_action([item], NOW, 600)[0], "due")

    def test_missed_when_late_beyond_limit(self):
        item = entry("a", "2026-11-09 17:40")  # 20 min late
        self.assertEqual(next_action([item], NOW, 600)[0], "missed")

    def test_earliest_entry_wins(self):
        later = entry("b", "2026-11-09 19:00")
        sooner = entry("a", "2026-11-09 18:10")
        self.assertEqual(chosen(next_action([later, sooner], NOW, 600)).id, "a")

    def test_due_entry_is_not_starved_by_future_one(self):
        future = entry("b", "2026-11-09 20:00")
        due = entry("a", "2026-11-09 17:59")
        self.assertEqual(chosen(next_action([future, due], NOW, 600)).id, "a")
        self.assertEqual(next_action([future, due], NOW, 600)[0], "due")


class RunnerLockTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.schedule = Path(temp.name) / "schedule.json"
        self.locks = []

    def tearDown(self):
        for lock in self.locks:
            lock.release()

    def lock(self):
        lock = RunnerLock(self.schedule)
        self.locks.append(lock)
        return lock

    def test_second_acquire_fails_while_held(self):
        first, second = self.lock(), self.lock()
        self.assertTrue(first.acquire())
        self.assertFalse(second.acquire())

    def test_release_allows_reacquire(self):
        first = self.lock()
        self.assertTrue(first.acquire())
        first.release()
        self.assertTrue(self.lock().acquire())

    def test_stale_lock_is_replaced(self):
        lock_path = self.schedule.with_name(self.schedule.name + ".lock")
        lock_path.write_text("999999", encoding="utf-8")
        self.assertTrue(self.lock().acquire())
        self.assertNotEqual(lock_path.read_text(encoding="utf-8"), "999999")

    def test_empty_lock_being_written_is_not_stolen(self):
        # acquire() creates the file with O_EXCL, then writes its pid; a second runner must not read that as stale.
        lock_path = self.schedule.with_name(self.schedule.name + ".lock")
        lock_path.write_text("", encoding="utf-8")
        self.assertFalse(self.lock().acquire())

    def test_abandoned_empty_lock_is_reclaimed(self):
        lock_path = self.schedule.with_name(self.schedule.name + ".lock")
        lock_path.write_text("", encoding="utf-8")
        old = time.time() - (store_module.LOCK_SETTLE + 5)
        os.utime(lock_path, (old, old))
        self.assertTrue(self.lock().acquire())


class SplitEntrySpecTests(unittest.TestCase):
    def test_when_then_url(self):
        when, url = split_entry_spec("11/9/26 10am https://us06web.zoom.us/j/85091120436")
        self.assertEqual(when, "11/9/26 10am")
        self.assertEqual(url, "https://us06web.zoom.us/j/85091120436")

    def test_url_first(self):
        when, url = split_entry_spec("https://zoom.us/j/81234567890 2026-11-09 10:00")
        self.assertEqual(when, "2026-11-09 10:00")
        self.assertEqual(url, "https://zoom.us/j/81234567890")

    def test_missing_url(self):
        with self.assertRaises(ValueError):
            split_entry_spec("2026-11-09 10:00")

    def test_missing_when(self):
        with self.assertRaises(ValueError):
            split_entry_spec("https://zoom.us/j/81234567890")


class StopMarkerTests(unittest.TestCase):
    def test_request_and_clear(self):
        with tempfile.TemporaryDirectory() as temp:
            schedule = Path(temp) / "schedule.json"
            self.assertFalse(stop_requested(schedule))
            request_stop(schedule)
            self.assertTrue(stop_requested(schedule))
            clear_stop(schedule)
            self.assertFalse(stop_requested(schedule))


class RepeatTests(unittest.TestCase):
    def test_roll_repeat_rearms_for_next_week(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ScheduleStore(Path(temp) / "schedule.json")
            added = store.add(
                url="https://zoom.us/j/1", start_at="2026-11-09 10:00", repeat="weekly"
            )
            self.assertEqual(added.repeat, "weekly")
            store.mark(added.id, STATUS_DONE, exit_code=0, path="C:/rec/a.mkv")

            rolled = store.roll_repeat(added.id, now=datetime(2026, 11, 9, 11, 0))
            self.assertIsNotNone(rolled)
            assert rolled is not None
            self.assertEqual(rolled.start, "2026-11-16 10:00")
            self.assertEqual(rolled.status, STATUS_PENDING)
            self.assertEqual(rolled.last_path, "C:/rec/a.mkv")

    def test_roll_repeat_ignores_one_off_entries(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ScheduleStore(Path(temp) / "schedule.json")
            added = store.add(url="https://zoom.us/j/1", start_at="2026-11-09 10:00")
            self.assertIsNone(store.roll_repeat(added.id))


class UpdateTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "schedule.json"
        self.store = ScheduleStore(self.path)
        self.entry = self.store.add(
            url="https://zoom.us/j/1",
            start_at="2026-11-09 10:00",
            note="typo",
            email="a@b.com",
            repeat="weekly",
        )

    def test_edit_changes_only_the_given_fields(self):
        updated = self.store.update(self.entry.id, start_at="2026-11-10 11:30", note="fixed")
        assert updated is not None
        self.assertEqual(updated.start, "2026-11-10 11:30")
        self.assertEqual(updated.note, "fixed")
        self.assertEqual(updated.email, "a@b.com", "an omitted field must survive")
        self.assertEqual(updated.repeat, "weekly")

    def test_edit_rearms_a_finished_entry(self):
        self.store.mark(self.entry.id, STATUS_DONE, exit_code=0, path="C:/rec/a.mp4")
        updated = self.store.update(self.entry.id, start_at="2026-11-12 10:00")
        assert updated is not None
        self.assertEqual(updated.status, STATUS_PENDING)

    def test_editing_a_rule_moves_the_anchor(self):
        updated = self.store.update(
            self.entry.id, start_at="2026-11-16 10:00", rrule="FREQ=WEEKLY;INTERVAL=2;BYDAY=WE"
        )
        assert updated is not None
        self.assertEqual(updated.anchor, "2026-11-16 10:00")
        self.assertEqual(updated.rrule, "FREQ=WEEKLY;INTERVAL=2;BYDAY=WE")

    def test_a_bad_edit_leaves_the_file_alone(self):
        with self.assertRaises(ScheduleError):
            self.store.update(self.entry.id, email="nope")
        reloaded = ScheduleStore(self.path).entries[0]
        self.assertEqual(reloaded.email, "a@b.com")
        self.assertEqual(reloaded.start, "2026-11-09 10:00")

    def test_unknown_field_is_rejected(self):
        with self.assertRaises(ScheduleError):
            self.store.update(self.entry.id, nonsense="x")

    def test_unknown_entry_returns_none(self):
        self.assertIsNone(self.store.update("nosuchid", note="x"))


class BackupTests(unittest.TestCase):
    def test_previous_schedule_is_kept_as_bak(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "schedule.json"
            store = ScheduleStore(path)
            store.add(url="https://zoom.us/j/1", start_at="2026-12-01 10:00", note="first")
            store.add(url="https://zoom.us/j/2", start_at="2026-12-02 10:00", note="second")

            backup = Path(str(path) + ".bak")
            self.assertTrue(backup.exists(), "a mistaken delete should be recoverable")
            notes = [
                entry["note"] for entry in json.loads(backup.read_text(encoding="utf-8"))["entries"]
            ]
            self.assertEqual(notes, ["first"], "the .bak holds the generation before the last write")


if __name__ == "__main__":
    unittest.main()
