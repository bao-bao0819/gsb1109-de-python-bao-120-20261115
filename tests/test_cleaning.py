"""Tests for the cleaned export of coded message values (pydifact.cleaning)."""

import pytest

from pydifact.cleaning import (
    clean_message,
    directory_for_message,
    iter_code_resolutions,
)
from pydifact.segmentcollection import Interchange, Message
from pydifact.segments import Segment


def make_message(message_version: str, *segments: Segment) -> Message:
    """Build a message from an interchange string with the given UNH version."""
    interchange_str = (
        "UNB+UNOA:4+SENDER+RECIPIENT+20250322:1300+1'"
        f"UNH+1+APERAK:{message_version}:UN'"
    )
    interchange_str += "".join(
        f"{segment.tag}+"
        + "+".join(
            ":".join(element) if isinstance(element, list) else element
            for element in segment.elements
        )
        + "'"
        for segment in segments
    )
    interchange_str += "UNT+99+1'UNZ+1+1'"
    interchange = Interchange.from_str(interchange_str)
    return next(interchange.get_messages())


@pytest.fixture
def bgm_message_d23a() -> Message:
    return make_message("D:23A", Segment("BGM", "27", "APERAK004", "69"))


def test_directory_for_message(bgm_message_d23a):
    assert directory_for_message(bgm_message_d23a) == "d23a"


def test_directory_for_message_without_release():
    message = Message("1", ["APERAK"])
    assert directory_for_message(message) is None


def test_resolution_is_bound_to_message_version_and_directory(bgm_message_d23a):
    cleaned = bgm_message_d23a.clean_export()
    assert cleaned.message_version == "D.23A"
    assert cleaned.directory == "d23a"
    entry = next(e for e in cleaned.entries if e.data_element_id == "1225")
    assert entry.message_version == "D.23A"
    assert entry.directory == "d23a"
    assert entry.path == "BGM:3"
    assert entry.value == "69"
    assert entry.resolved
    assert entry.title == "New declaration data"


def test_same_code_resolves_differently_per_message_version():
    """Code 1225/69 changed its meaning between directories d21a and d23a."""
    message_d21a = make_message("D:21A", Segment("BGM", "27", "APERAK004", "69"))
    message_d23a = make_message("D:23A", Segment("BGM", "27", "APERAK004", "69"))

    def title_of(message):
        return next(
            e.title
            for e in message.clean_export().entries
            if e.data_element_id == "1225"
        )

    assert title_of(message_d21a) == "Advanced cargo information"
    assert title_of(message_d23a) == "New declaration data"


def test_unknown_code_keeps_original_declaration():
    message = make_message("D:23A", Segment("BGM", "27", "APERAK004", "ZZZ"))
    cleaned = message.clean_export()
    entry = next(e for e in cleaned.entries if e.data_element_id == "1225")
    assert not entry.resolved
    assert entry.value == "ZZZ"
    assert entry.title is None
    assert entry.description is None
    assert entry in cleaned.unresolved


def test_unmatched_directory_does_not_fall_back_to_latest():
    """Directory d96a is not bundled: no resolution against the latest directory."""
    message = make_message("D:96A", Segment("BGM", "27", "APERAK004", "9"))
    cleaned = message.clean_export()
    assert cleaned.directory is None
    assert cleaned.message_version == "D.96A"
    entry = next(
        e for e in cleaned.entries if e.segment_tag == "BGM" and e.value == "9"
    )
    # code 9 would resolve to "Original" in the latest directory d24a
    assert not entry.resolved
    assert entry.directory is None
    assert entry.title is None
    assert entry.value == "9"


def test_qualifiers_are_not_taken_from_sibling_composites():
    """CPI carries two composites with 1131/3055 qualifiers; each code must
    be qualified by its own composite occurrence only."""
    message = make_message(
        "D:23A",
        Segment("CPI", ["1", "LISTA", "6"], ["A", "LISTB", "ZZZ"]),
    )
    entries = {e.path: e for e in message.clean_export().entries}

    first = entries["CPI:1.1"]
    assert first.composite_id == "C229"
    assert first.data_element_id == "5237"
    assert first.title == "All charges"
    assert first.code_list_id == "LISTA"
    assert first.responsible_agency == "6"

    second = entries["CPI:2.1"]
    assert second.composite_id == "C231"
    assert second.data_element_id == "4215"
    assert second.title == "Account"
    assert second.code_list_id == "LISTB"
    assert second.responsible_agency == "ZZZ"


def test_qualifier_does_not_leak_into_simple_elements():
    message = make_message(
        "D:23A",
        Segment("CPI", ["1", "LISTA", "6"], ["A", "LISTB", "ZZZ"], "X"),
    )
    entry = next(e for e in message.clean_export().entries if e.path == "CPI:3")
    assert entry.data_element_id == "4237"
    assert entry.code_list_id is None
    assert entry.responsible_agency is None


def test_explicit_directory_overrides_message_version():
    message = make_message("D:21A", Segment("BGM", "27", "APERAK004", "69"))
    cleaned = clean_message(message, directory="d23a")
    assert cleaned.directory == "d23a"
    entry = next(e for e in cleaned.entries if e.data_element_id == "1225")
    assert entry.directory == "d23a"
    assert entry.title == "New declaration data"


def test_iter_code_resolutions_matches_clean_export(bgm_message_d23a):
    assert tuple(iter_code_resolutions(bgm_message_d23a)) == (
        bgm_message_d23a.clean_export().entries
    )
