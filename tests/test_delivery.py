import datetime
from unittest import TestCase

import hl7
from hl7.delivery import build_delivery_table, format_delivery_table

MESSAGE = (
    "MSH|^~\\&|SYS-A|FACA|RCV-A|FACB|20261115090000||ADT^A01|CTRL-1|P|2.4\r"
    "PID|||12345\r"
)


def message(
    sender="SYS-A", receiver="RCV-A", control_id="CTRL-1", sent_at="20261115090000"
):
    return (
        "MSH|^~\\&|{sender}|FACA|{receiver}|FACB|{sent_at}||ADT^A01|"
        "{control_id}|P|2.4\rPID|||12345\r"
    ).format(
        sender=sender,
        receiver=receiver,
        control_id=control_id,
        sent_at=sent_at,
    )


def ack(
    control_id="CTRL-1",
    code="AA",
    received_at="20261115090005",
    text="",
    layer=None,
    **kwargs,
):
    sender = kwargs.get("sender", "RCV-A")
    receiver = kwargs.get("receiver", "SYS-A")
    if layer:
        return {
            "control_id": control_id,
            "code": code,
            "layer": layer,
            "received_at": received_at,
            "sending_application": sender,
            "sending_facility": "FACB",
            "receiving_application": receiver,
            "receiving_facility": "FACA",
            "text": text,
        }
    return (
        "MSH|^~\\&|{sender}|FACB|{receiver}|FACA|{received_at}||ACK^A01^ACK|"
        "ACK-{control_id}|P|2.4\rMSA|{code}|{control_id}|{text}\r"
    ).format(
        sender=sender,
        receiver=receiver,
        received_at=received_at,
        code=code,
        control_id=control_id,
        text=text,
    )


class DeliveryTableTest(TestCase):
    def test_receive_and_application_layers_are_independent(self):
        table = build_delivery_table(
            [message()],
            [
                ack(
                    code="AA",
                    layer="receive",
                    received_at="20261115090001",
                    text="received",
                ),
                ack(
                    code="AR",
                    layer="application",
                    received_at="20261115090002",
                    text="application reject",
                ),
            ],
        )
        row = table.rows[0]
        self.assertEqual(row.status("receive"), "accepted")
        self.assertEqual(row.status("application"), "rejected")
        self.assertEqual(row.final_status, "application_rejected")
        self.assertEqual(row.code("receive"), "AA")
        self.assertEqual(row.code("application"), "AR")

    def test_application_reject_is_not_overridden_by_earlier_receive_accept(self):
        table = build_delivery_table(
            [message()],
            [
                ack(
                    code="AA",
                    layer="receive",
                    received_at="20261115090001",
                    text="receive accept",
                ),
                ack(
                    code="AE",
                    layer="application",
                    received_at="20261115090005",
                    text="application error",
                ),
                ack(
                    code="AA",
                    layer="receive",
                    received_at="20261115090009",
                    text="receive accept",
                ),
            ],
        )
        row = table.rows[0]
        self.assertEqual(row.status("application"), "error")
        self.assertEqual(row.final_status, "application_error")
        self.assertEqual(row.code("application"), "AE")

    def test_latest_accept_does_not_override_reject_in_same_layer(self):
        table = build_delivery_table(
            [message()],
            [
                ack(code="AR", received_at="20261115090001", text="application reject"),
                ack(code="AA", received_at="20261115090009", text="application accept"),
            ],
        )
        row = table.rows[0]
        self.assertEqual(row.status("application"), "rejected")
        self.assertEqual(row.final_status, "application_rejected")
        self.assertEqual(row.code("application"), "AR")

    def test_retransmissions_are_retained_as_attempts(self):
        table = build_delivery_table(
            [
                message(sent_at="20261115090000"),
                message(sent_at="20261115091000"),
                message(sent_at="20261115092000"),
            ],
            [ack(code="AA", received_at="20261115092005")],
        )
        self.assertEqual(len(table.rows), 1)
        row = table.rows[0]
        self.assertEqual(row.attempt_count, 3)
        self.assertEqual(len(row.retransmissions), 2)
        self.assertEqual(len(table.rows[0].receipts), 1)

    def test_receipts_on_each_retransmission_are_all_retained(self):
        table = build_delivery_table(
            [
                message(sent_at="20261115090000"),
                message(sent_at="20261115091000"),
            ],
            [
                ack(code="AR", received_at="20261115090005", text="application reject"),
                ack(code="AA", received_at="20261115091005", text="application accept"),
            ],
        )
        row = table.rows[0]
        self.assertEqual(len(row.receipts), 2)
        self.assertEqual(row.status("application"), "rejected")
        self.assertEqual(row.final_status, "application_rejected")

    def test_late_receipt_is_kept_and_flagged(self):
        table = build_delivery_table(
            [message(sent_at="20261115090000")],
            [ack(code="AA", received_at="20261115120000")],
            late_after=3600,
        )
        row = table.rows[0]
        self.assertEqual(len(row.receipts), 1)
        self.assertEqual(len(row.late_receipts), 1)
        self.assertTrue(row.receipts[0].late)
        self.assertEqual(row.final_status, "delivered")

    def test_receipt_without_layer_stays_a_candidate(self):
        receipt = {"control_id": "CTRL-1", "code": "ZZ"}
        table = build_delivery_table([message()], [receipt])
        self.assertEqual(len(table.candidates), 1)
        self.assertIsNone(table.candidates[0].receipt.layer)
        self.assertEqual(table.rows[0].status("receive"), "pending")
        self.assertEqual(table.rows[0].status("application"), "pending")
        self.assertEqual(len(table.rows[0].receipts), 0)

    def test_candidate_is_not_attached_to_any_row(self):
        table = build_delivery_table(
            [message(), message(sender="SYS-B", receiver="RCV-B", control_id="CTRL-1")],
            [{"control_id": "CTRL-1", "code": "ZZ"}],
        )
        self.assertEqual(len(table.candidates), 1)
        self.assertEqual(
            table.candidates[0].possible_matches, [(row.key) for row in table.rows]
        )
        for row in table.rows:
            self.assertEqual(row.receipts, [])

    def test_ambiguous_control_id_is_candidate_not_claimed_twice(self):
        table = build_delivery_table(
            [
                message(sender="SYS-A", receiver="RCV-A", control_id="SAME"),
                message(sender="SYS-B", receiver="RCV-B", control_id="SAME"),
            ],
            [
                ack(
                    control_id="SAME",
                    code="AA",
                    received_at="20261115090005",
                    sender="",
                    receiver="",
                    text="",
                )
            ],
        )
        self.assertEqual(len(table.candidates), 1)
        candidate = table.candidates[0]
        self.assertEqual(candidate.reason, "insufficient_source_evidence")
        self.assertEqual(len(table.rows[0].receipts), 0)
        self.assertEqual(len(table.rows[1].receipts), 0)
        self.assertEqual(table.rows[0].status("receive"), "pending")
        self.assertEqual(table.rows[1].status("receive"), "pending")

    def test_direction_identifies_one_of_two_same_control_ids(self):
        table = build_delivery_table(
            [
                message(sender="SYS-A", receiver="RCV-A", control_id="SAME"),
                message(sender="SYS-B", receiver="RCV-B", control_id="SAME"),
            ],
            [ack(control_id="SAME", code="AA", received_at="20261115090005")],
        )
        first, second = table.rows
        self.assertEqual(len(first.receipts), 1)
        self.assertEqual(len(second.receipts), 0)
        self.assertEqual(first.final_status, "delivered")
        self.assertEqual(second.final_status, "pending")
        self.assertEqual(table.candidates, [])

    def test_unmatched_receipt_is_listed(self):
        table = build_delivery_table([message()], [ack(control_id="NOPE")])
        self.assertEqual(len(table.unmatched), 1)
        self.assertEqual(table.unmatched[0].control_id, "NOPE")

    def test_format_lists_layers_attempts_and_candidates(self):
        table = build_delivery_table(
            [
                message(control_id="SAME"),
                message(sender="SYS-B", receiver="RCV-B", control_id="SAME"),
            ],
            [
                ack(
                    control_id="SAME",
                    code="ZZ",
                    received_at="20261115090005",
                    sender="",
                    receiver="",
                )
            ],
        )
        output = format_delivery_table(table)
        self.assertIn("attempts", output)
        self.assertIn("application", output)
        self.assertIn("insufficient_source_evidence", output)

    def test_attempt_times_are_exposed(self):
        table = build_delivery_table(
            [
                message(sent_at="20261115090000"),
                message(sent_at="20261115091000"),
            ],
            [],
        )
        row = table.rows[0]
        self.assertEqual(row.attempt_times, ["20261115090000", "20261115091000"])
        self.assertEqual(
            row.as_row()["attempt_times"], ["20261115090000", "20261115091000"]
        )

    def test_nested_message_lists_are_flattened(self):
        table = build_delivery_table([[message()], [message(control_id="CTRL-2")]], [])
        self.assertEqual([row.control_id for row in table.rows], ["CTRL-1", "CTRL-2"])

    def test_source_pointing_elsewhere_is_a_candidate(self):
        table = build_delivery_table(
            [message()],
            [
                {
                    "control_id": "CTRL-1",
                    "code": "AA",
                    "layer": "receive",
                    "source": "OTHER-SYSTEM",
                }
            ],
        )
        self.assertEqual(len(table.rows[0].receipts), 0)
        self.assertEqual(table.candidates[0].reason, "direction_conflict")

    def test_parsed_batch_is_split_into_rows(self):
        batch = (
            "MSH|^~\\&|SYS-A|FACA|RCV-A|FACB|20261115090000||ADT^A01|CTRL-1|P|2.4\r"
            "PID|||12345\r"
            "MSH|^~\\&|SYS-A|FACA|RCV-A|FACB|20261115090100||ADT^A01|CTRL-2|P|2.4\r"
            "PID|||12346\r"
        )
        table = build_delivery_table([hl7.parse_batch(batch)], [])
        self.assertEqual(len(table.rows), 2)
        self.assertEqual({row.control_id for row in table.rows}, {"CTRL-1", "CTRL-2"})

    def test_late_after_timedelta(self):
        table = build_delivery_table(
            [message(sent_at="20261115090000")],
            [ack(code="AA", received_at="20261115090500")],
            late_after=datetime.timedelta(minutes=1),
        )
        self.assertTrue(table.rows[0].receipts[0].late)

    def test_attempts_without_timestamps_still_group(self):
        table = build_delivery_table(
            [
                {"sender": "SYS-A", "receiver": "RCV-A", "control_id": "C1"},
                {"sender": "SYS-A", "receiver": "RCV-A", "control_id": "C1"},
            ],
            [{"control_id": "C1", "code": "AA", "layer": "receive"}],
        )
        self.assertEqual(len(table.rows), 1)
        self.assertEqual(table.rows[0].attempt_count, 2)
        self.assertEqual(len(table.rows[0].receipts), 1)
