import unittest
from datetime import date

from icalevents.attendance import (
    AliasTable,
    AttendancePolicy,
    AttendanceStatus,
    CountRange,
    RequirementStatus,
    analyze_event,
    analyze_events,
)
from icalevents.icalevents import events


def _calendar(*attendees, uid="meeting-1", status=None):
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "BEGIN:VEVENT",
        "UID:%s" % uid,
        "SUMMARY:Planning",
        "DTSTART:20260101T100000Z",
        "DTEND:20260101T110000Z",
    ]
    if status:
        lines.append("STATUS:%s" % status)
    lines.extend(attendees)
    lines.extend(["END:VEVENT", "END:VCALENDAR"])
    return "\r\n".join(lines)


def _attendee(address, **params):
    parts = ["ATTENDEE"]
    for key, value in params.items():
        parts.append('%s="%s"' % (key.replace("_", "-"), value))
    return ";".join(parts) + ":mailto:%s" % address


def _parse(content):
    [event] = events(
        string_content=content, start=date(2025, 1, 1), end=date(2027, 1, 1)
    )
    return event


class AttendanceTests(unittest.TestCase):
    def test_statuses_are_kept_apart(self):
        event = _parse(
            _calendar(
                _attendee("alice@example.com", PARTSTAT="ACCEPTED", CN="Alice"),
                _attendee("bob@example.com", PARTSTAT="NEEDS-ACTION", CN="Bob"),
                _attendee("carol@example.com", PARTSTAT="TENTATIVE", CN="Carol"),
                _attendee("dave@example.com", PARTSTAT="DECLINED", CN="Dave"),
                _attendee("erin@example.com", CN="Erin"),
            )
        )

        report = analyze_event(event)

        self.assertEqual([r.person for r in report.confirmed], ["alice@example.com"])
        self.assertEqual(
            [r.person for r in report.pending],
            ["bob@example.com", "erin@example.com"],
        )
        self.assertEqual([r.person for r in report.tentative], ["carol@example.com"])
        self.assertEqual([r.person for r in report.declined], ["dave@example.com"])
        self.assertEqual(report.headcount, CountRange(1, 4))
        self.assertNotIn("dave@example.com", report.headcount)

    def test_missing_partstat_is_not_a_decline(self):
        event = _parse(_calendar(_attendee("bob@example.com", CN="Bob")))

        report = analyze_event(event)

        self.assertEqual(report.pending[0].status, AttendanceStatus.PENDING)
        self.assertIsNone(report.pending[0].partstat)
        self.assertEqual(report.declined, ())
        self.assertEqual(report.headcount, CountRange(0, 1))

    def test_duplicate_invitation_counts_once(self):
        event = _parse(
            _calendar(
                _attendee("alice@example.com", PARTSTAT="ACCEPTED", CN="Alice"),
                _attendee("Alice@Example.com", PARTSTAT="ACCEPTED", CN="Alice"),
                _attendee("alice@example.com", PARTSTAT="DECLINED", CN="Alice"),
            )
        )

        report = analyze_event(event)

        self.assertEqual(len(report.records), 1)
        self.assertEqual(report.records[0].sources, 3)
        self.assertEqual(report.duplicates_merged, 2)
        self.assertEqual(report.records[0].status, AttendanceStatus.CONFLICTED)
        self.assertEqual(report.records[0].partstat, "ACCEPTED,DECLINED")
        self.assertEqual(report.headcount, CountRange(0, 1))

    def test_alias_table_collapses_declared_aliases_only(self):
        event = _parse(
            _calendar(
                _attendee("alice@example.com", PARTSTAT="ACCEPTED", CN="A. Smith"),
                _attendee("asmith@example.com", PARTSTAT="ACCEPTED", CN="Alice"),
                _attendee("alice.smith@example.com", CN="Alice Smith"),
            )
        )

        aliases = AliasTable(
            {"alice@example.com": ["asmith@example.com", "A. Smith"]}
        )
        report = analyze_event(event, aliases=aliases)

        self.assertEqual(len(report.records), 2)
        self.assertEqual(
            {r.person for r in report.records},
            {"alice@example.com", "alice.smith@example.com"},
        )
        self.assertEqual(report.headcount, CountRange(1, 2))

    def test_alias_table_from_alias_mapping(self):
        aliases = AliasTable.from_alias_mapping(
            {"a.smith@example.com": "alice@example.com"}
        )

        self.assertEqual(
            aliases.canonical("mailto:A.Smith@example.com"), "alice@example.com"
        )
        self.assertIsNone(aliases.canonical("someone@example.com"))

    def test_ambiguous_alias_conflict_is_rejected(self):
        aliases = AliasTable({"alice@example.com": ["shared@example.com"]})

        with self.assertRaises(ValueError):
            aliases.add("bob@example.com", ["shared@example.com"])

    def test_delegate_replaces_delegator_and_is_reported_once(self):
        event = _parse(
            _calendar(
                _attendee(
                    "alice@example.com",
                    PARTSTAT="DELEGATED",
                    ROLE="CHAIR",
                    CN="Alice",
                    DELEGATED_TO="mailto:bob@example.com",
                ),
                _attendee(
                    "bob@example.com",
                    PARTSTAT="ACCEPTED",
                    CN="Bob",
                    DELEGATED_FROM="mailto:alice@example.com",
                ),
            )
        )

        report = analyze_event(event, policy={"CHAIR": 1})

        self.assertEqual([r.person for r in report.delegated], ["alice@example.com"])
        self.assertEqual([r.person for r in report.confirmed], ["bob@example.com"])
        self.assertEqual([r.person for r in report.proxies], ["bob@example.com"])
        self.assertEqual(report.headcount, CountRange(1, 1))
        self.assertEqual(report.requirements[0].status, RequirementStatus.SATISFIED)
        self.assertTrue(report.requirements[0].met)

    def test_named_delegate_without_attendee_line_is_listed(self):
        event = _parse(
            _calendar(
                _attendee(
                    "alice@example.com",
                    PARTSTAT="DELEGATED",
                    CN="Alice",
                    DELEGATED_TO="mailto:bob@example.com",
                )
            )
        )

        report = analyze_event(event)

        bob = next(r for r in report.records if r.person == "bob@example.com")
        self.assertEqual(bob.status, AttendanceStatus.PENDING)
        self.assertEqual(bob.delegated_from, ("alice@example.com",))
        self.assertTrue(bob.is_proxy)
        self.assertTrue(bob.is_delegate)
        self.assertIsNone(bob.partstat)
        self.assertEqual(report.headcount, CountRange(0, 1))
        self.assertEqual(
            report.delegation_chains[0].persons,
            ("alice@example.com", "bob@example.com"),
        )

    def test_delegate_without_delegated_from_is_still_a_proxy(self):
        event = _parse(
            _calendar(
                _attendee(
                    "alice@example.com",
                    PARTSTAT="DELEGATED",
                    ROLE="CHAIR",
                    DELEGATED_TO="mailto:bob@example.com",
                ),
                _attendee("bob@example.com", PARTSTAT="ACCEPTED"),
            )
        )

        report = analyze_event(event, policy={"CHAIR": 1})

        bob = next(r for r in report.records if r.person == "bob@example.com")
        self.assertTrue(bob.is_proxy)
        self.assertEqual(bob.delegated_from, ("alice@example.com",))
        self.assertEqual(bob.role, "CHAIR")
        self.assertEqual(report.headcount, CountRange(1, 1))

    def test_delegation_cycle_is_separate_and_not_counted(self):
        event = _parse(
            _calendar(
                _attendee(
                    "alice@example.com",
                    PARTSTAT="ACCEPTED",
                    CN="Alice",
                    DELEGATED_TO="mailto:bob@example.com",
                ),
                _attendee(
                    "bob@example.com",
                    PARTSTAT="ACCEPTED",
                    CN="Bob",
                    DELEGATED_TO="mailto:alice@example.com",
                ),
            )
        )

        report = analyze_event(event)

        self.assertEqual(report.headcount, CountRange(0, 0))
        self.assertEqual(len(report.delegation_cycles), 1)
        self.assertEqual(
            report.delegation_cycles[0].persons,
            ("alice@example.com", "bob@example.com", "alice@example.com"),
        )
        self.assertEqual(report.delegation_chains, ())
        self.assertTrue(all(r.in_delegation_cycle for r in report.delegated))

    def test_long_delegation_cycle_is_reported(self):
        event = _parse(
            _calendar(
                _attendee(
                    "a@example.com",
                    PARTSTAT="ACCEPTED",
                    DELEGATED_TO="mailto:b@example.com",
                ),
                _attendee(
                    "b@example.com",
                    PARTSTAT="ACCEPTED",
                    DELEGATED_TO="mailto:c@example.com",
                ),
                _attendee(
                    "c@example.com",
                    PARTSTAT="ACCEPTED",
                    DELEGATED_TO="mailto:a@example.com",
                ),
            )
        )

        report = analyze_event(event)

        self.assertEqual(report.headcount, CountRange(0, 0))
        self.assertEqual(len(report.delegation_cycles), 1)
        self.assertEqual(
            report.delegation_cycles[0].persons,
            ("a@example.com", "b@example.com", "c@example.com", "a@example.com"),
        )

    def test_chained_delegation_keeps_chair_role(self):
        event = _parse(
            _calendar(
                _attendee(
                    "alice@example.com",
                    PARTSTAT="DELEGATED",
                    ROLE="CHAIR",
                    DELEGATED_TO="mailto:bob@example.com",
                ),
                _attendee(
                    "bob@example.com",
                    PARTSTAT="DELEGATED",
                    DELEGATED_TO="mailto:carol@example.com",
                ),
                _attendee("carol@example.com", PARTSTAT="ACCEPTED"),
            )
        )

        report = analyze_event(event, policy={"CHAIR": 1})

        chair = report.role_counts["CHAIR"]
        self.assertEqual(chair, CountRange(1, 1))
        self.assertEqual(report.requirements[0].status, RequirementStatus.SATISFIED)
        self.assertEqual([r.person for r in report.confirmed], ["carol@example.com"])

    def test_requirement_at_risk_versus_violated(self):
        event = _parse(
            _calendar(
                _attendee("alice@example.com", PARTSTAT="ACCEPTED", ROLE="CHAIR"),
                _attendee(
                    "bob@example.com", PARTSTAT="TENTATIVE", ROLE="REQ-PARTICIPANT"
                ),
                _attendee(
                    "carol@example.com", PARTSTAT="DECLINED", ROLE="REQ-PARTICIPANT"
                ),
            )
        )

        report = analyze_event(
            event, policy={"CHAIR": 1, "REQ-PARTICIPANT": 2}
        )

        chair, required = report.requirements
        self.assertEqual(chair.status, RequirementStatus.SATISFIED)
        self.assertEqual(required.status, RequirementStatus.VIOLATED)
        self.assertEqual(required.headcount, CountRange(0, 1))
        self.assertFalse(report.all_requirements_met)

    def test_requirement_is_at_risk_with_pending_replies(self):
        event = _parse(
            _calendar(
                _attendee(
                    "alice@example.com", PARTSTAT="ACCEPTED", ROLE="REQ-PARTICIPANT"
                ),
                _attendee("bob@example.com", ROLE="REQ-PARTICIPANT"),
            )
        )

        report = analyze_event(event, policy={"REQ-PARTICIPANT": 2})

        self.assertEqual(report.requirements[0].headcount, CountRange(1, 2))
        self.assertEqual(report.requirements[0].status, RequirementStatus.AT_RISK)
        self.assertFalse(report.requirements[0].met)

    def test_optional_and_non_participants_are_excluded_from_quorum(self):
        event = _parse(
            _calendar(
                _attendee("chair@example.com", PARTSTAT="ACCEPTED", ROLE="CHAIR"),
                _attendee(
                    "opt@example.com", PARTSTAT="ACCEPTED", ROLE="OPT-PARTICIPANT"
                ),
                _attendee(
                    "info@example.com", PARTSTAT="ACCEPTED", ROLE="NON-PARTICIPANT"
                ),
            )
        )

        default_report = analyze_event(event, policy={"CHAIR": 1})
        full_report = analyze_event(
            event,
            policy=AttendancePolicy(
                requirements=(("CHAIR", 1),),
                count_non_participants=True,
            ),
        )

        self.assertEqual(default_report.headcount, CountRange(2, 2))
        self.assertEqual(full_report.headcount, CountRange(3, 3))
        self.assertEqual(
            {r.person for r in default_report.excluded}, {"info@example.com"}
        )

    def test_rooms_and_resources_do_not_count(self):
        event = _parse(
            _calendar(
                _attendee(
                    "alice@example.com", PARTSTAT="ACCEPTED", CUTYPE="INDIVIDUAL"
                ),
                _attendee("room@example.com", PARTSTAT="ACCEPTED", CUTYPE="ROOM"),
                _attendee(
                    "projector@example.com", PARTSTAT="ACCEPTED", CUTYPE="RESOURCE"
                ),
            )
        )

        report = analyze_event(event)

        self.assertEqual(report.headcount, CountRange(1, 1))
        self.assertEqual(len(report.excluded), 2)

    def test_cancelled_event_does_not_fail_attendance(self):
        event = _parse(
            _calendar(
                _attendee("alice@example.com", PARTSTAT="ACCEPTED", ROLE="CHAIR"),
                status="CANCELLED",
            )
        )

        report = analyze_event(event, policy={"CHAIR": 1})

        self.assertTrue(report.is_cancelled)
        self.assertEqual(
            report.requirements[0].status, RequirementStatus.NOT_APPLICABLE
        )
        self.assertTrue(report.all_requirements_met)
        self.assertFalse(report.has_pending_replies)

    def test_policy_rejects_negative_minimum(self):
        with self.assertRaises(ValueError):
            AttendancePolicy.from_mapping({"CHAIR": -1})

    def test_policy_is_normalized_and_merged(self):
        policy = AttendancePolicy(
            requirements=(("required", 1), ("REQ-PARTICIPANT", 3))
        )

        self.assertEqual(len(policy.requirements), 1)
        self.assertEqual(policy.requirements[0].role, "REQ-PARTICIPANT")
        self.assertEqual(policy.requirements[0].minimum, 3)

    def test_analyze_calendar_reads_source(self):
        from icalevents.attendance import analyze_calendar

        content = _calendar(_attendee("alice@example.com", PARTSTAT="ACCEPTED"))

        [report] = analyze_calendar(
            string_content=content,
            start=date(2025, 1, 1),
            end=date(2027, 1, 1),
        )

        self.assertEqual(report.headcount, CountRange(1, 1))

    def test_conflicted_reply_is_not_confirmed_and_needs_resolution(self):
        event = _parse(
            _calendar(
                _attendee("alice@example.com", PARTSTAT="ACCEPTED", ROLE="CHAIR"),
                _attendee(
                    "bob@example.com", PARTSTAT="ACCEPTED", ROLE="REQ-PARTICIPANT"
                ),
                _attendee(
                    "bob@example.com", PARTSTAT="DECLINED", ROLE="REQ-PARTICIPANT"
                ),
            )
        )

        report = analyze_event(event, policy={"REQ-PARTICIPANT": 1})

        self.assertTrue(report.has_pending_replies)
        self.assertEqual([r.person for r in report.conflicted], ["bob@example.com"])
        self.assertEqual(report.requirements[0].headcount, CountRange(0, 1))
        self.assertEqual(report.requirements[0].status, RequirementStatus.AT_RISK)

    def test_name_only_attendee_uses_name_identity(self):
        event = _parse(_calendar("ATTENDEE;PARTSTAT=ACCEPTED;CN=Alice Smith:mailto:"))

        report = analyze_event(event)

        self.assertEqual([r.person for r in report.confirmed], ["name:alice smith"])
        self.assertEqual(report.confirmed[0].display_name, "Alice Smith")

    def test_declined_person_is_not_in_possible_range(self):
        event = _parse(
            _calendar(
                _attendee("alice@example.com", PARTSTAT="ACCEPTED"),
                _attendee("dave@example.com", PARTSTAT="DECLINED"),
            )
        )

        report = analyze_event(event)

        self.assertEqual(report.headcount, CountRange(1, 1))
        dave = next(r for r in report.records if r.person == "dave@example.com")
        self.assertFalse(dave.counts_toward_headcount)
        self.assertEqual(
            {r.person for r in report.excluded}, {"dave@example.com"}
        )

    def test_analysis_of_multiple_instances_is_independent(self):
        first = _parse(
            _calendar(
                _attendee("alice@example.com", PARTSTAT="ACCEPTED"),
                uid="first",
            )
        )
        second = _parse(
            _calendar(
                _attendee("alice@example.com", PARTSTAT="DECLINED"),
                uid="second",
            )
        )

        reports = analyze_events([first, second])

        self.assertEqual(reports[0].headcount, CountRange(1, 1))
        self.assertEqual(reports[1].headcount, CountRange(0, 0))
        self.assertEqual(reports[1].declined[0].person, "alice@example.com")

    def test_role_and_partstat_underscores_are_accepted(self):
        event = _parse(
            _calendar(
                "ATTENDEE;PARTSTAT=NEEDS_ACTION;ROLE=REQ_PARTICIPANT:"
                "mailto:alice@example.com",
                "ATTENDEE;PARTSTAT=ACCEPTED;ROLE=CHAIR:mailto:bob@example.com",
            )
        )

        report = analyze_event(event, policy={"REQ-PARTICIPANT": 1})

        alice = next(r for r in report.pending if r.person == "alice@example.com")
        self.assertEqual(alice.partstat, "NEEDS-ACTION")
        self.assertEqual(alice.role, "REQ-PARTICIPANT")
        self.assertEqual(report.requirements[0].status, RequirementStatus.AT_RISK)

    def test_alias_group_sequence_is_accepted(self):
        aliases = AliasTable(
            [("alice@example.com", "a.smith@example.com"), ("bob@example.com",)]
        )
        self.assertEqual(len(aliases), 2)

        event = _parse(
            _calendar(
                _attendee(
                    "alice@example.com",
                    PARTSTAT="DELEGATED",
                    DELEGATED_TO="mailto:carol@example.com",
                ),
                _attendee("carol@example.com", PARTSTAT="ACCEPTED"),
                _attendee("a.smith@example.com", PARTSTAT="ACCEPTED"),
                _attendee("Alice", PARTSTAT="ACCEPTED"),
            )
        )
        report = analyze_event(event, aliases=aliases)

        alice = next(
            r for r in report.delegated if r.person == "alice@example.com"
        )
        self.assertEqual(alice.sources, 2)
        self.assertFalse(alice.is_synthetic)
        self.assertEqual(report.headcount, CountRange(2, 2))

    def test_synthetic_delegate_is_replaced_by_real_attendee(self):
        event = _parse(
            _calendar(
                _attendee(
                    "alice@example.com",
                    PARTSTAT="DELEGATED",
                    DELEGATED_TO="mailto:bob@example.com",
                ),
                _attendee("bob@example.com", PARTSTAT="ACCEPTED", CN="Bob"),
            )
        )

        report = analyze_event(event)

        bob = next(r for r in report.records if r.person == "bob@example.com")
        self.assertFalse(bob.is_synthetic)
        self.assertEqual(bob.display_name, "Bob")
        self.assertEqual(bob.status, AttendanceStatus.CONFIRMED)

    def test_report_to_dict_is_json_friendly(self):
        event = _parse(
            _calendar(_attendee("alice@example.com", PARTSTAT="ACCEPTED"))
        )

        payload = analyze_event(event, policy={"REQ-PARTICIPANT": 1}).to_dict()

        self.assertEqual(payload["headcount"], {"minimum": 1, "maximum": 1})
        self.assertEqual(payload["requirements"][0]["status"], "satisfied")
        self.assertTrue(payload["all_requirements_met"])
        self.assertEqual(payload["confirmed"][0]["person"], "alice@example.com")
        self.assertEqual(payload["start"], "2026-01-01T10:00:00+00:00")

    def test_empty_event_has_zero_range(self):
        event = _parse(_calendar())

        report = analyze_event(event, policy={"CHAIR": 1})

        self.assertEqual(report.headcount, CountRange(0, 0))
        self.assertEqual(report.requirements[0].status, RequirementStatus.VIOLATED)
        self.assertEqual(report.records, ())


if __name__ == "__main__":
    unittest.main()
