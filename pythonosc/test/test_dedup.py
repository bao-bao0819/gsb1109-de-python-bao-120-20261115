import threading
import unittest

from pythonosc.dedup import (
    ReadingRecord,
    ReadingStatus,
    SequenceDeduplicator,
    SequenceDedupError,
)


class TestSequenceDeduplicator(unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.quarantine = []
        self.dedup = SequenceDeduplicator(
            wrap_modulus=16,
            history_size=4,
            quarantine_cb=self.quarantine.append,
        )

    def test_first_reading_is_admitted(self):
        record = self.dedup.admit("sensor-a", 0, {"v": 1})
        self.assertEqual(ReadingStatus.NEW, record.status)
        self.assertEqual(0, record.epoch)
        self.assertTrue(self.dedup.is_warehouse_ready(record))
        self.assertEqual([], self.quarantine)

    def test_retry_with_same_sequence_and_payload_is_duplicate(self):
        first = self.dedup.admit("sensor-a", 1, {"v": 42})
        retry = self.dedup.admit("sensor-a", 1, {"v": 42})

        self.assertTrue(self.dedup.is_warehouse_ready(first))
        self.assertEqual(ReadingStatus.DUPLICATE, retry.status)
        self.assertFalse(self.dedup.is_warehouse_ready(retry))
        self.assertEqual((retry,), self.dedup.rejected)
        self.assertEqual(1, self.dedup.stats[ReadingStatus.DUPLICATE])
        self.assertEqual(1, len(self.quarantine))

    def test_consecutive_identical_values_with_new_sequences_are_kept(self):
        first = self.dedup.admit("sensor-a", 1, 100)
        second = self.dedup.admit("sensor-a", 2, 100)
        third = self.dedup.admit("sensor-a", 3, 100)

        self.assertEqual(ReadingStatus.NEW, first.status)
        self.assertEqual(ReadingStatus.NEW, second.status)
        self.assertEqual(ReadingStatus.NEW, third.status)
        self.assertEqual([], self.quarantine)

    def test_same_sequence_different_payload_is_quarantined_not_overwritten(self):
        first = self.dedup.admit("sensor-a", 5, {"v": 1})
        conflict = self.dedup.admit("sensor-a", 5, {"v": 2})

        self.assertTrue(self.dedup.is_warehouse_ready(first))
        self.assertEqual(ReadingStatus.CONFLICT, conflict.status)
        self.assertFalse(self.dedup.is_warehouse_ready(conflict))

        # Both payloads remain classified: the conflicting payload was only
        # quarantined once, no "last writer wins" ever happens.
        retry_first = self.dedup.admit("sensor-a", 5, {"v": 1})
        retry_second = self.dedup.admit("sensor-a", 5, {"v": 2})
        self.assertEqual(ReadingStatus.DUPLICATE, retry_first.status)
        self.assertEqual(ReadingStatus.DUPLICATE, retry_second.status)
        self.assertEqual(
            [ReadingStatus.CONFLICT, ReadingStatus.DUPLICATE, ReadingStatus.DUPLICATE],
            [record.status for record in self.quarantine],
        )
        self.assertEqual(
            [{"v": 2}, {"v": 1}, {"v": 2}],
            [record.payload for record in self.quarantine],
        )

    def test_wrapped_sequence_new_reading_is_not_suppressed(self):
        self.dedup.admit("sensor-a", 14, {"v": 14})
        self.dedup.admit("sensor-a", 15, {"v": 15})
        wrapped = self.dedup.admit("sensor-a", 0, {"v": 0})

        self.assertEqual(ReadingStatus.WRAPPED, wrapped.status)
        self.assertTrue(self.dedup.is_warehouse_ready(wrapped))
        self.assertEqual(0, wrapped.sequence)
        self.assertEqual(1, wrapped.wrap)

        # A retransmission of the pre-wrap reading must not come back in.
        stale_retry = self.dedup.admit("sensor-a", 15, {"v": 15})
        self.assertEqual(ReadingStatus.DUPLICATE, stale_retry.status)
        self.assertEqual(0, stale_retry.wrap)
        self.assertFalse(self.dedup.is_warehouse_ready(stale_retry))

    def test_reused_sequence_after_wrap_is_isolated_from_old_cycle(self):
        dedup = SequenceDeduplicator(wrap_modulus=4, history_size=16)
        for sequence, value in ((0, "a0"), (1, "a1"), (2, "a2"), (3, "a3")):
            record = dedup.admit("sensor-a", sequence, value)
            self.assertEqual(ReadingStatus.NEW, record.status)
            self.assertEqual(0, record.wrap)

        # Raw sequence 0 recurs after a wrap: new cycle, admitted.
        first_wrap = dedup.admit("sensor-a", 0, "b0")
        self.assertEqual(ReadingStatus.WRAPPED, first_wrap.status)
        self.assertEqual(1, first_wrap.wrap)

        # Retrying the new-cycle reading is a duplicate; replaying the old
        # cycle's payload against the new cycle is a conflict, never an
        # overwrite of either reading.
        self.assertEqual(
            ReadingStatus.DUPLICATE, dedup.admit("sensor-a", 0, "b0").status
        )
        self.assertEqual(
            ReadingStatus.CONFLICT, dedup.admit("sensor-a", 0, "a0").status
        )

        # A second full cycle wraps again and the same raw sequence is new.
        for sequence, value in ((1, "b1"), (2, "b2"), (3, "b3")):
            self.assertEqual(
                ReadingStatus.NEW, dedup.admit("sensor-a", sequence, value).status
            )
        second_wrap = dedup.admit("sensor-a", 0, "c0")
        self.assertEqual(ReadingStatus.WRAPPED, second_wrap.status)
        self.assertEqual(2, second_wrap.wrap)

    def test_restart_evidence_resets_sequence_baseline(self):
        self.dedup.admit("sensor-a", 5, {"v": 1}, restart_evidence="boot-1")
        restarted = self.dedup.admit("sensor-a", 1, {"v": 2}, restart_evidence="boot-2")

        self.assertEqual(ReadingStatus.RESTART, restarted.status)
        self.assertEqual(1, restarted.epoch)
        self.assertTrue(self.dedup.is_warehouse_ready(restarted))

        # The same sequence number in the new epoch is a fresh reading even
        # though sequence 1 existed before the restart.
        again = self.dedup.admit("sensor-a", 1, {"v": 2}, restart_evidence="boot-2")
        self.assertEqual(ReadingStatus.DUPLICATE, again.status)

    def test_restart_reuses_sequence_and_payload_but_still_admits(self):
        self.dedup.admit("sensor-a", 3, {"v": 7}, restart_evidence="boot-1")
        restarted = self.dedup.admit("sensor-a", 3, {"v": 7}, restart_evidence="boot-2")

        self.assertEqual(ReadingStatus.RESTART, restarted.status)
        self.assertTrue(self.dedup.is_warehouse_ready(restarted))
        self.assertEqual(1, restarted.epoch)
        self.assertEqual(0, restarted.wrap)

    def test_restart_resets_the_wrap_cycle(self):
        dedup = SequenceDeduplicator(wrap_modulus=4, history_size=16)
        for sequence in range(4):
            dedup.admit("sensor-a", sequence, sequence)
        wrapped = dedup.admit("sensor-a", 0, 99)
        self.assertEqual(1, wrapped.wrap)

        restarted = dedup.admit("sensor-a", 0, 100, restart_evidence="boot-2")
        self.assertEqual(ReadingStatus.RESTART, restarted.status)
        self.assertEqual(1, restarted.epoch)
        self.assertEqual(0, restarted.wrap)

    def test_none_restart_evidence_never_triggers_a_restart(self):
        self.dedup.admit("sensor-a", 3, {"v": 1}, restart_evidence="boot-1")
        record = self.dedup.admit("sensor-a", 4, {"v": 2})
        self.assertEqual(ReadingStatus.NEW, record.status)
        self.assertEqual(0, record.epoch)

    def test_lower_sequence_without_restart_evidence_is_conflict(self):
        self.dedup.admit("sensor-a", 5, {"v": 1})
        stale = self.dedup.admit("sensor-a", 3, {"v": 9})

        self.assertEqual(ReadingStatus.CONFLICT, stale.status)
        self.assertFalse(self.dedup.is_warehouse_ready(stale))

    def test_large_forward_jump_is_treated_as_stale_not_new(self):
        self.dedup.admit("sensor-a", 1, {"v": 1})
        # Half the 16-value range is 8; jumping by 9 cannot be a normal step.
        jumped = self.dedup.admit("sensor-a", 10, {"v": 2})
        self.assertEqual(ReadingStatus.CONFLICT, jumped.status)

    def test_streams_are_tracked_independently(self):
        left = self.dedup.admit("sensor-a", 1, {"v": 1})
        right = self.dedup.admit("sensor-b", 1, {"v": 1})

        self.assertEqual(ReadingStatus.NEW, left.status)
        self.assertEqual(ReadingStatus.NEW, right.status)

        left_retry = self.dedup.admit("sensor-a", 1, {"v": 1})
        right_retry = self.dedup.admit("sensor-b", 1, {"v": 1})
        self.assertEqual(ReadingStatus.DUPLICATE, left_retry.status)
        self.assertEqual(ReadingStatus.DUPLICATE, right_retry.status)

    def test_late_retransmission_after_newer_readings_is_caught(self):
        self.dedup.admit("sensor-a", 1, {"v": 1})
        self.dedup.admit("sensor-a", 2, {"v": 2})
        self.dedup.admit("sensor-a", 3, {"v": 3})

        late_retry = self.dedup.admit("sensor-a", 1, {"v": 1})
        self.assertEqual(ReadingStatus.DUPLICATE, late_retry.status)

        late_conflict = self.dedup.admit("sensor-a", 2, {"v": 99})
        self.assertEqual(ReadingStatus.CONFLICT, late_conflict.status)

    def test_history_eviction_forgets_old_sequences(self):
        dedup = SequenceDeduplicator(wrap_modulus=256, history_size=2)
        for sequence in range(5):
            dedup.admit("sensor-a", sequence, sequence)

        # Sequence 0 has been evicted from history, so its payload can no
        # longer be matched exactly; ordering guards still keep the stale
        # retransmission out of the warehouse instead of silently re-admitting.
        record = dedup.admit("sensor-a", 0, 0)
        self.assertEqual(ReadingStatus.CONFLICT, record.status)
        self.assertFalse(dedup.is_warehouse_ready(record))

        # After eviction the same raw sequence can legitimately recur once the
        # counter wraps: wrap evidence overrides the ordering guard.
        dedup.reset()
        for sequence in range(254, 256):
            dedup.admit("sensor-a", sequence, sequence)
        wrapped = dedup.admit("sensor-a", 0, 0)
        self.assertEqual(ReadingStatus.WRAPPED, wrapped.status)
        self.assertTrue(dedup.is_warehouse_ready(wrapped))

    def test_invalid_sequences_are_rejected(self):
        for bad in (-1, 16, "3", 1.5, True):
            with self.subTest(bad=bad):
                with self.assertRaises(SequenceDedupError):
                    self.dedup.admit("sensor-a", bad, {"v": 1})

    def test_invalid_constructor_arguments(self):
        with self.assertRaises(ValueError):
            SequenceDeduplicator(wrap_modulus=1)
        with self.assertRaises(ValueError):
            SequenceDeduplicator(history_size=0)

    def test_reset_clears_all_state(self):
        self.dedup.admit("sensor-a", 1, {"v": 1})
        self.dedup.admit("sensor-a", 1, {"v": 1})
        self.dedup.reset()

        record = self.dedup.admit("sensor-a", 1, {"v": 1})
        self.assertEqual(ReadingStatus.NEW, record.status)
        self.assertEqual((), self.dedup.rejected)
        self.assertEqual(0, self.dedup.stats[ReadingStatus.DUPLICATE])

    def test_quarantine_callback_receives_full_record(self):
        self.dedup.admit("sensor-a", 7, {"v": 1})
        self.dedup.admit("sensor-a", 7, {"v": 2})

        (record,) = self.quarantine
        self.assertIsInstance(record, ReadingRecord)
        self.assertEqual(ReadingStatus.CONFLICT, record.status)
        self.assertEqual("sensor-a", record.stream)
        self.assertEqual(7, record.sequence)
        self.assertEqual(0, record.epoch)
        self.assertEqual(0, record.wrap)
        self.assertEqual({"v": 2}, record.payload)

    def test_concurrent_admissions_are_serialized(self):
        dedup = SequenceDeduplicator(wrap_modulus=65536, history_size=512)
        errors = []

        def worker(worker_id):
            try:
                stream = f"sensor-{worker_id}"
                for sequence in range(100):
                    dedup.admit(stream, sequence, sequence)
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(worker_id,)) for worker_id in range(4)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual([], errors)
        self.assertEqual(400, dedup.stats[ReadingStatus.NEW])
        self.assertEqual(0, dedup.stats[ReadingStatus.DUPLICATE])
        self.assertEqual(0, dedup.stats[ReadingStatus.CONFLICT])


if __name__ == "__main__":
    unittest.main()
