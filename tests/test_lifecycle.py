#    pydifact - a python edifact library
#
# Tests for business-message lifecycle reconciliation: retransmissions under a
# new interchange reference, same-number corrections, cancellation chains and
# unorderable correction conflicts.
import pytest

from pydifact.exceptions import (
    CorrectionConflictWarning,
    MessageLifecycleError,
)
from pydifact.lifecycle import (
    BGM_TAG,
    CANCELLATION,
    CORRECTION,
    DUPLICATE,
    ORIGINAL,
    RETRANSMISSION,
    BusinessKey,
    LifecycleRegistry,
    default_key_extractor,
)
from pydifact.segmentcollection import Interchange
from pydifact.segments import Segment


class FakeMessage:
    """Minimal message exposing the surface the lifecycle module relies on."""

    def __init__(self, bgm, message_type="ORDERS", sender=None):
        self._segments = [bgm]
        self.type = message_type
        self.sender = sender

    def get_segment(self, tag):
        for segment in self._segments:
            if segment.tag == tag:
                return segment
        return None


def bgm_scalar(document_id, function_code, document_code="220"):
    # BGM with a scalar C106, e.g. "BGM+220+PO123456+9"
    return Segment(BGM_TAG, document_code, document_id, function_code)


def bgm_composite(document_id, function_code, version=None, revision=None):
    # BGM with a composite C106 carrying 1004/1056/1060, e.g.
    # "BGM+220::9+PO1:V1:R2+5"
    identification = [document_id, version or "", revision or ""]
    return Segment(BGM_TAG, ["220", "", "9"], identification, function_code)


# --------------------------------------------------------------------------- #
# business key and function code conventions
# --------------------------------------------------------------------------- #


def test_scalar_bgm_extraction():
    function = default_key_extractor(FakeMessage(bgm_scalar("PO1", "9")))
    assert function.document_id == "PO1"
    assert function.code == "9"
    assert function.phase == ORIGINAL
    assert function.version is None
    assert function.revision is None


def test_composite_bgm_extraction_keeps_version_and_revision():
    function = default_key_extractor(
        FakeMessage(bgm_composite("PO1", "5", version="V1", revision="R2"))
    )
    assert function.document_id == "PO1"
    assert function.code == "5"
    assert function.phase == CORRECTION
    assert function.version == "V1"
    assert function.revision == "R2"


def test_function_codes_map_to_phases():
    assert (
        default_key_extractor(FakeMessage(bgm_scalar("D", "1"))).phase == CANCELLATION
    )
    assert default_key_extractor(FakeMessage(bgm_scalar("D", "5"))).phase == CORRECTION
    assert default_key_extractor(FakeMessage(bgm_scalar("D", "9"))).phase == ORIGINAL
    assert (
        default_key_extractor(FakeMessage(bgm_scalar("D", "7"))).phase == RETRANSMISSION
    )
    # an unknown code is treated as an original transmission
    assert default_key_extractor(FakeMessage(bgm_scalar("D", "ZZ"))).phase == ORIGINAL


def test_missing_bgm_segment_raises():
    message = FakeMessage(Segment("DTM", ["137", "20250101", "102"]))
    with pytest.raises(MessageLifecycleError):
        default_key_extractor(message)


def test_missing_document_identifier_raises():
    message = FakeMessage(Segment(BGM_TAG, "220", "", "9"))
    with pytest.raises(MessageLifecycleError):
        default_key_extractor(message)


def test_business_key_includes_type_and_sender():
    registry = LifecycleRegistry()
    record = registry.register(
        FakeMessage(bgm_scalar("PO1", "9"), sender="ACME"),
        transmission_ref="IC1",
    )
    assert record.key == BusinessKey("PO1", "ORDERS", "ACME")
    assert str(record.key) == "ORDERS/ACME/PO1"


def test_same_document_but_different_sender_is_a_different_chain():
    registry = LifecycleRegistry()
    registry.register(
        FakeMessage(bgm_scalar("PO1", "9"), sender="A"), transmission_ref="IC1"
    )
    registry.register(
        FakeMessage(bgm_scalar("PO1", "9"), sender="B"), transmission_ref="IC2"
    )
    assert len(list(registry.records())) == 2


# --------------------------------------------------------------------------- #
# retransmission under a new interchange reference
# --------------------------------------------------------------------------- #


def test_retransmission_under_new_interchange_reference_is_linked():
    registry = LifecycleRegistry()
    record = registry.register(
        FakeMessage(bgm_scalar("PO1", "9")), transmission_ref="IC1"
    )
    record = registry.register(
        FakeMessage(bgm_scalar("PO1", "9")), transmission_ref="IC2"
    )

    assert len(list(registry.records())) == 1
    assert len(record.events) == 2
    assert record.events[1].phase == RETRANSMISSION
    # the original remains the current state
    assert record.current is record.events[0]
    assert registry.duplicates == []


def test_exact_redelivery_of_same_transmission_is_parked():
    registry = LifecycleRegistry()
    registry.register(FakeMessage(bgm_scalar("PO1", "9")), transmission_ref="IC1")
    record = registry.register(
        FakeMessage(bgm_scalar("PO1", "9")), transmission_ref="IC1"
    )

    assert len(registry.duplicates) == 1
    assert registry.duplicates[0].phase == DUPLICATE
    # the duplicate is still traceable in the chain, but not applied twice
    assert len(record.events) == 2
    assert record.current is record.events[0]


# --------------------------------------------------------------------------- #
# same-number corrections
# --------------------------------------------------------------------------- #


def test_higher_revision_correction_supersedes_original():
    registry = LifecycleRegistry()
    registry.register(FakeMessage(bgm_scalar("PO1", "9")), transmission_ref="IC1")
    record = registry.register(
        FakeMessage(bgm_composite("PO1", "5", revision="2")),
        transmission_ref="IC2",
    )

    assert record.cancelled is False
    assert record.current.function.revision == "2"
    assert [event.phase for event in record.corrections] == [CORRECTION]


def test_lower_revision_correction_is_treated_as_stale():
    registry = LifecycleRegistry()
    registry.register(
        FakeMessage(bgm_composite("PO1", "9", revision="3")),
        transmission_ref="IC1",
    )
    registry.register(
        FakeMessage(bgm_composite("PO1", "5", revision="4")),
        transmission_ref="IC2",
    )
    record = registry.register(
        FakeMessage(bgm_composite("PO1", "5", revision="2")),
        transmission_ref="IC3",
    )

    # revision 4 stays current; the late revision 2 is recorded but ignored
    assert record.current.function.revision == "4"
    assert len(record.corrections) == 2
    assert record.is_conflicted is False


def test_numeric_revisions_order_numerically():
    registry = LifecycleRegistry()
    registry.register(FakeMessage(bgm_scalar("PO1", "9")), transmission_ref="IC1")
    registry.register(
        FakeMessage(bgm_composite("PO1", "5", revision="2")),
        transmission_ref="IC2",
    )
    record = registry.register(
        FakeMessage(bgm_composite("PO1", "5", revision="10")),
        transmission_ref="IC3",
    )
    # "10" must sort above "2" numerically, not lexicographically
    assert record.current.function.revision == "10"


# --------------------------------------------------------------------------- #
# unorderable correction conflicts
# --------------------------------------------------------------------------- #


def test_two_corrections_without_revision_conflict_and_are_both_kept():
    registry = LifecycleRegistry()
    registry.register(FakeMessage(bgm_scalar("PO1", "9")), transmission_ref="IC1")
    registry.register(FakeMessage(bgm_scalar("PO1", "5")), transmission_ref="IC2")
    with pytest.warns(CorrectionConflictWarning):
        record = registry.register(
            FakeMessage(bgm_scalar("PO1", "5")), transmission_ref="IC3"
        )

    assert record.is_conflicted is True
    assert len(record.conflicts) == 2
    # the first applied correction is still reported as current
    assert record.current is record.conflicts[0]


def test_two_corrections_with_same_revision_conflict():
    registry = LifecycleRegistry()
    registry.register(FakeMessage(bgm_scalar("PO1", "9")), transmission_ref="IC1")
    registry.register(
        FakeMessage(bgm_composite("PO1", "5", revision="7")),
        transmission_ref="IC2",
    )
    with pytest.warns(CorrectionConflictWarning):
        record = registry.register(
            FakeMessage(bgm_composite("PO1", "5", revision="7")),
            transmission_ref="IC3",
        )
    assert len(record.conflicts) == 2


def test_strict_registry_raises_on_conflict():
    registry = LifecycleRegistry(strict=True)
    registry.register(FakeMessage(bgm_scalar("PO1", "9")), transmission_ref="IC1")
    registry.register(FakeMessage(bgm_scalar("PO1", "5")), transmission_ref="IC2")
    with pytest.raises(MessageLifecycleError):
        registry.register(FakeMessage(bgm_scalar("PO1", "5")), transmission_ref="IC3")


def test_higher_revision_resolves_an_existing_conflict():
    registry = LifecycleRegistry()
    registry.register(FakeMessage(bgm_scalar("PO1", "9")), transmission_ref="IC1")
    registry.register(FakeMessage(bgm_scalar("PO1", "5")), transmission_ref="IC2")
    with pytest.warns(CorrectionConflictWarning):
        registry.register(FakeMessage(bgm_scalar("PO1", "5")), transmission_ref="IC3")
    record = registry.register(
        FakeMessage(bgm_composite("PO1", "5", revision="9")),
        transmission_ref="IC4",
    )
    assert record.is_conflicted is False
    assert record.current.function.revision == "9"


# --------------------------------------------------------------------------- #
# cancellation chain and tombstone behaviour
# --------------------------------------------------------------------------- #


def test_cancellation_marks_chain_cancelled():
    registry = LifecycleRegistry()
    registry.register(FakeMessage(bgm_scalar("PO1", "9")), transmission_ref="IC1")
    record = registry.register(
        FakeMessage(bgm_scalar("PO1", "1")), transmission_ref="IC2"
    )

    assert record.cancelled is True
    assert record.current is None
    assert registry.active_documents() == []


def test_cancellation_first_blocks_late_original():
    registry = LifecycleRegistry()
    record = registry.register(
        FakeMessage(bgm_scalar("PO1", "1")), transmission_ref="IC1"
    )
    # the late original arrives, even under a brand-new interchange reference
    record = registry.register(
        FakeMessage(bgm_scalar("PO1", "9")), transmission_ref="IC2"
    )

    assert record.cancelled is True
    assert record.current is None
    assert record.original is not None  # the late original is recorded ...
    assert registry.active_documents() == []  # ... but never resurrects the order


def test_correction_after_cancellation_does_not_resurrect():
    registry = LifecycleRegistry()
    registry.register(FakeMessage(bgm_scalar("PO1", "1")), transmission_ref="IC1")
    record = registry.register(
        FakeMessage(bgm_composite("PO1", "5", revision="2")),
        transmission_ref="IC2",
    )
    # the late correction is kept on the chain for audit, but never applied
    assert record.cancelled is True
    assert record.current is None
    assert len(record.events) == 2
    assert record.events[1].phase == CORRECTION


# --------------------------------------------------------------------------- #
# end-to-end against parsed interchanges
# --------------------------------------------------------------------------- #

EDI_TEMPLATE = (
    "UNB+UNOA:4+{sender}:8+RECV:ZZ+20260101:0000+{control}'\n"
    "UNH+1+ORDERS:D:96A:UN'\n"
    "BGM+220::{doc_code}+{doc_id}{revision_part}+{function}'\n"
    "UNT+3+1'\n"
)


def _interchange(control, function, doc_id, revision=None, sender="12345678"):
    revision_part = f":1:{revision}" if revision is not None else ""
    edi = EDI_TEMPLATE.format(
        sender=sender,
        control=control,
        doc_code="9",
        doc_id=doc_id,
        revision_part=revision_part,
        function=function,
    )
    return Interchange.from_str(edi)


def test_from_interchange_uses_control_reference_and_bgm_key():
    interchange = _interchange("807", "9", "801715918011")
    registry = LifecycleRegistry.from_interchange(interchange)

    record = registry.get(BusinessKey("801715918011", "ORDERS", "12345678"))
    assert record is not None
    assert record.events[0].transmission_ref == "807"


def test_full_lifecycle_across_separate_interchanges():
    registry = LifecycleRegistry()

    # original order
    original = _interchange("IC1", "9", "PO1")
    # re-sent under a new interchange reference
    retransmission = _interchange("IC2", "9", "PO1")
    # same-number correction with a revision
    correction = _interchange("IC3", "5", "PO1", revision="2")
    # cancellation
    cancellation = _interchange("IC4", "1", "PO1")

    for interchange in (original, retransmission, correction, cancellation):
        message = next(interchange.get_messages())
        registry.register(
            message,
            transmission_ref=interchange.control_reference,
            sender="12345678",
        )

    record = registry.get(BusinessKey("PO1", "ORDERS", "12345678"))
    assert [event.transmission_ref for event in record.events] == [
        "IC1",
        "IC2",
        "IC3",
        "IC4",
    ]
    assert [event.phase for event in record.events] == [
        ORIGINAL,
        RETRANSMISSION,
        CORRECTION,
        CANCELLATION,
    ]
    assert record.cancelled is True
    assert record.current is None
    assert record.is_conflicted is False
