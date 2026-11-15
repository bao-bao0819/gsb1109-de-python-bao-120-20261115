from unittest import TestCase

import hl7
from hl7 import (
    BatchTrailerMismatchException,
    MalformedFileException,
    MissingBatchTrailerException,
)

from .samples import (
    sample_audit_file,
    sample_batch,
    sample_batch1,
    sample_batch2,
    sample_batch3,
    sample_batch_mismatch,
    sample_batch_noninteger_bts,
    sample_empty_batch,
    sample_file,
    sample_file1,
    sample_hl7,
)


class AuditBatchTest(TestCase):
    def test_accepted_batch(self):
        audit = hl7.audit_batch(hl7.parse_batch(sample_batch))
        self.assertEqual(audit.status, hl7.STATUS_ACCEPTED)
        self.assertTrue(audit.accepted)
        self.assertIsNone(audit.error)
        self.assertTrue(audit.has_header)
        self.assertTrue(audit.has_trailer)
        self.assertEqual(audit.declared_count, 1)
        self.assertEqual(audit.actual_count, 1)
        self.assertEqual(audit.batch_id, "abchs20070101123401-1")
        self.assertEqual(len(audit.messages), 1)
        self.assertFalse(audit.is_empty)

    def test_accepted_multi_message_batch(self):
        audit = hl7.audit_batch(hl7.parse_batch(sample_batch1))
        self.assertTrue(audit.accepted)
        self.assertEqual(audit.declared_count, 2)
        self.assertEqual(audit.actual_count, 2)
        self.assertEqual(len(audit.messages), 2)

    def test_batch_trailer_mismatch_isolated(self):
        audit = hl7.audit_batch(hl7.parse_batch(sample_batch_mismatch))
        self.assertEqual(audit.status, hl7.STATUS_REJECTED)
        self.assertFalse(audit.accepted)
        self.assertIsInstance(audit.error, BatchTrailerMismatchException)
        self.assertEqual(audit.declared_count, 2)
        self.assertEqual(audit.actual_count, 1)
        # The received message is retained on record even when rejected
        self.assertEqual(len(audit.messages), 1)
        self.assertIn("declares 2", str(audit.error))
        self.assertIn("1 received", str(audit.error))

    def test_missing_trailer_not_zero_success(self):
        # sample_batch3 has a BHS header but no BTS trailer
        audit = hl7.audit_batch(hl7.parse_batch(sample_batch3))
        self.assertFalse(audit.accepted)
        self.assertIsInstance(audit.error, MissingBatchTrailerException)
        self.assertIsNone(audit.declared_count)
        self.assertFalse(audit.has_trailer)
        # The message actually present is not silently dropped
        self.assertEqual(audit.actual_count, 1)
        self.assertEqual(len(audit.messages), 1)
        self.assertIn("no BTS trailer", str(audit.error))

    def test_non_integer_trailer_count_rejected(self):
        audit = hl7.audit_batch(hl7.parse_batch(sample_batch_noninteger_bts))
        self.assertFalse(audit.accepted)
        self.assertIsInstance(audit.error, BatchTrailerMismatchException)
        self.assertIsNone(audit.declared_count)
        self.assertEqual(audit.actual_count, 1)
        self.assertIn("non-integer", str(audit.error))

    def test_complete_empty_batch_retained(self):
        audit = hl7.audit_batch(hl7.parse_batch(sample_empty_batch))
        self.assertTrue(audit.accepted)
        self.assertTrue(audit.is_empty)
        self.assertEqual(audit.declared_count, 0)
        self.assertEqual(audit.actual_count, 0)
        self.assertEqual(audit.messages, [])

    def test_unwrapped_batch_has_no_declaration(self):
        # No BHS/BTS means nothing to reconcile; always accepted
        audit = hl7.audit_batch(hl7.parse_batch(sample_batch2))
        self.assertTrue(audit.accepted)
        self.assertFalse(audit.has_header)
        self.assertFalse(audit.has_trailer)
        self.assertIsNone(audit.declared_count)
        self.assertIsNone(audit.batch_id)
        self.assertEqual(audit.actual_count, 2)

    def test_missing_trailer_is_malformed_batch_subclass(self):
        audit = hl7.audit_batch(hl7.parse_batch(sample_batch3))
        self.assertIsInstance(audit.error, hl7.MalformedBatchException)


class AuditFileTest(TestCase):
    def test_well_formed_file(self):
        audit = hl7.audit_file(hl7.parse_file(sample_file1))
        self.assertTrue(audit.accepted)
        self.assertEqual(audit.errors, [])
        self.assertTrue(audit.has_header)
        self.assertTrue(audit.has_trailer)
        self.assertEqual(audit.declared_count, 2)
        self.assertEqual(audit.actual_count, 2)
        self.assertEqual([batch.index for batch in audit.batches], [1, 2])

    def test_single_batch_file(self):
        audit = hl7.audit_file(hl7.parse_file(sample_file))
        self.assertTrue(audit.accepted)
        self.assertEqual(audit.declared_count, 1)
        self.assertEqual(audit.actual_count, 1)
        self.assertEqual(audit.batches[0].declared_count, 1)

    def test_only_mismatching_batch_isolated(self):
        audit = hl7.audit_file(hl7.parse_file(sample_audit_file))
        # FTS declares 3 batches and 3 were received, so the file layer is fine
        self.assertEqual(audit.declared_count, 3)
        self.assertEqual(audit.actual_count, 3)
        self.assertEqual(audit.errors, [])

        rejected = audit.rejected_batches
        accepted = audit.accepted_batches
        self.assertEqual([batch.index for batch in rejected], [1])
        self.assertEqual([batch.index for batch in accepted], [2, 3])
        self.assertIsInstance(rejected[0].error, BatchTrailerMismatchException)
        # A trailer problem in batch 1 does not reject the whole file layer
        self.assertFalse(audit.accepted)

    def test_empty_batch_is_kept_in_file(self):
        audit = hl7.audit_file(hl7.parse_file(sample_audit_file))
        empty_batch = audit.batches[2]
        self.assertEqual(empty_batch.batch_id, "batch-3")
        self.assertTrue(empty_batch.is_empty)
        self.assertTrue(empty_batch.accepted)
        self.assertEqual(empty_batch.declared_count, 0)
        self.assertEqual(empty_batch.actual_count, 0)
        # All three batches are retained, including the empty and bad ones
        self.assertEqual(len(audit.batches), 3)

    def test_accepted_messages_exclude_isolated_batch(self):
        audit = hl7.audit_file(hl7.parse_file(sample_audit_file))
        # Only batch 2 carries a message and is accepted
        self.assertEqual(len(audit.accepted_messages), 1)
        self.assertEqual(audit.accepted_messages[0]["MSH.10"], "ctl-2")

    def test_fts_count_mismatch_recorded_as_file_error(self):
        bad_file = sample_audit_file.replace("FTS|3", "FTS|9")
        audit = hl7.audit_file(hl7.parse_file(bad_file))
        self.assertFalse(audit.accepted)
        self.assertEqual(len(audit.errors), 1)
        self.assertIsInstance(audit.errors[0], MalformedFileException)
        self.assertIn("declares 9", str(audit.errors[0]))
        # File-layer error does not erase the per-batch reconciliation
        self.assertEqual(
            [batch.status for batch in audit.batches],
            ["rejected", "accepted", "accepted"],
        )

    def test_missing_fts_when_fhs_present(self):
        # Remove the FTS trailer while keeping the FHS header
        audit = hl7.audit_file(hl7.parse_file(_drop_fts()))
        self.assertFalse(audit.accepted)
        self.assertTrue(
            any(isinstance(e, MalformedFileException) for e in audit.errors)
        )
        self.assertTrue(any("no FTS trailer" in str(e) for e in audit.errors))


def _drop_fts():
    lines = sample_audit_file.split("\r")
    return "\r".join(line for line in lines if not line.startswith("FTS"))


class MessageTableTest(TestCase):
    def test_file_message_table_maps_batches(self):
        records = hl7.message_table(hl7.parse_file(sample_audit_file))
        # Two messages total: one in the rejected batch, one in the good batch
        self.assertEqual(len(records), 2)
        first, second = records
        self.assertEqual((first.batch_index, first.message_index), (1, 1))
        self.assertEqual(first.control_id, "ctl-1")
        self.assertEqual(first.message_type, "ADT^A04^ADT_A01")
        self.assertEqual((second.batch_index, second.message_index), (2, 1))
        self.assertEqual(second.control_id, "ctl-2")
        self.assertIsInstance(first.message, hl7.Message)

    def test_batch_message_table(self):
        records = hl7.message_table(hl7.parse_batch(sample_batch1))
        self.assertEqual(len(records), 2)
        self.assertTrue(all(record.batch_index == 1 for record in records))
        self.assertEqual([record.message_index for record in records], [1, 2])
        self.assertEqual(
            [record.control_id for record in records],
            ["12334456778890", "12334456778891"],
        )

    def test_empty_batch_table(self):
        records = hl7.message_table(hl7.parse_batch(sample_empty_batch))
        self.assertEqual(records, [])


class AuditEntryPointTest(TestCase):
    def test_audit_accepts_raw_string(self):
        batch_audit = hl7.audit(sample_batch)
        self.assertIsInstance(batch_audit, hl7.BatchAudit)
        self.assertTrue(batch_audit.accepted)

        file_audit = hl7.audit(sample_file1)
        self.assertIsInstance(file_audit, hl7.FileAudit)
        self.assertTrue(file_audit.accepted)

    def test_audit_accepts_parsed_objects(self):
        self.assertIsInstance(hl7.audit(hl7.parse_batch(sample_batch)), hl7.BatchAudit)
        self.assertIsInstance(hl7.audit(hl7.parse_file(sample_file1)), hl7.FileAudit)

    def test_status_constants(self):
        self.assertEqual(hl7.STATUS_ACCEPTED, "accepted")
        self.assertEqual(hl7.STATUS_REJECTED, "rejected")

    def test_audit_rejects_plain_message(self):
        with self.assertRaises(TypeError):
            hl7.audit(hl7.parse(sample_hl7))
