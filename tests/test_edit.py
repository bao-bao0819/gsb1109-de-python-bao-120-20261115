import pytest

from pydifact import Parser, replace_component
from pydifact.exceptions import EditConflictError

INTERCHANGE = (
    "UNA:+,? '"
    "UNB+UNOA:4+SENDER+RECIPIENT+260115:0930+1'"
    "UNH+1+RECADV:D:96A:UN'"
    "LIN+3++5020436373085:EN'"
    "FTX+AAI+1++Damaged on arrival'"
    "LIN+4++5020436373108:EN'"
    "FTX+AAI+1++OK'"
    "UNT+7+1'"
    "UNZ+1+1'"
)


def test_replace_note_component_escapes_service_characters():
    """Only the addressed component changes; service characters in the
    new value are released with the escape character."""
    result = replace_component(
        INTERCHANGE,
        "FTX",
        {0: "AAI", 3: "Damaged on arrival"},
        3,
        0,
        new_value="Received: 2 boxes damaged + 1 missing?",
    )
    assert result == INTERCHANGE.replace(
        "FTX+AAI+1++Damaged on arrival'",
        "FTX+AAI+1++Received?: 2 boxes damaged ?+ 1 missing??'",
    )


def test_all_other_segments_stay_byte_identical():
    result = replace_component(
        INTERCHANGE,
        "FTX",
        {0: "AAI", 3: "OK"},
        3,
        0,
        new_value="Checked",
    )
    before = INTERCHANGE.split("'")
    after = result.split("'")
    assert len(before) == len(after)
    changed = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
    assert changed == [6]
    assert after[6] == "FTX+AAI+1++Checked"


def test_result_still_parses_to_same_segments_except_edit():
    result = replace_component(
        INTERCHANGE,
        "FTX",
        {0: "AAI", 3: "OK"},
        3,
        0,
        new_value="Checked",
    )
    old_segments = list(Parser().parse(INTERCHANGE))
    new_segments = list(Parser().parse(result))
    assert len(old_segments) == len(new_segments)
    for old, new in zip(old_segments, new_segments):
        if old.tag == "FTX" and old.elements[3] == "OK":
            assert new.elements[3] == "Checked"
        else:
            assert old == new


def test_non_unique_identity_is_refused():
    """Two line items carry an FTX+AAI note: the qualifier alone does
    not identify the authorized one."""
    with pytest.raises(EditConflictError, match="matches 2 'FTX' segments"):
        replace_component(INTERCHANGE, "FTX", {0: "AAI"}, 3, 0, new_value="x")


def test_changed_source_is_refused():
    """The authorized note text no longer appears: 0 matches."""
    with pytest.raises(EditConflictError, match="matches 0 'FTX' segments"):
        replace_component(
            INTERCHANGE,
            "FTX",
            {0: "AAI", 3: "Damaged on arriva"},
            3,
            0,
            new_value="x",
        )


def test_explicit_empty_component_is_not_trailing_omission():
    """The identity must not match a segment where the component is
    omitted instead of explicitly empty, and the edit must preserve the
    explicit empty element."""
    message = "FTX+AAI+1++OLD'\nFTX+AAI+2'"
    result = replace_component(message, "FTX", {1: "1", 2: ""}, 3, new_value="NEW")
    assert result == "FTX+AAI+1++NEW'\nFTX+AAI+2'"


def test_omitted_target_component_is_refused():
    """Writing into an omitted position would turn trailing omission
    into explicit components - refused."""
    message = "FTX+AAI+2'"
    with pytest.raises(EditConflictError, match="no element 3"):
        replace_component(message, "FTX", {1: "2"}, 3, new_value="NEW")


def test_omitted_target_subcomponent_is_refused():
    message = "FTX+AAI+1+OLD'"
    with pytest.raises(EditConflictError, match="no component 1"):
        replace_component(message, "FTX", {1: "1"}, 2, 1, new_value="NEW")


def test_trailing_empty_components_are_preserved():
    """The explicit empty component behind the edited one must survive."""
    message = "FTX+AAI+1+OLD:'"
    result = replace_component(message, "FTX", {1: "1"}, 2, 0, new_value="NEW")
    assert result == "FTX+AAI+1+NEW:'"


def test_escaped_separators_in_existing_content_do_not_split():
    """Released service characters inside a value are content, not
    structure - for both matching and editing."""
    message = "FTX+AAI+1+Fragile?: handle with ?'care?''"
    result = replace_component(
        message,
        "FTX",
        {2: "Fragile?: handle with ?'care?'"},
        2,
        new_value="top: ok",
    )
    assert result == "FTX+AAI+1+top?: ok'"


def test_identity_matches_raw_appearance():
    """Identity values are matched as they appear in the segment, i.e.
    with release characters in place."""
    message = "FTX+AAI+1+a?:b'"
    with pytest.raises(EditConflictError, match="matches 0"):
        replace_component(message, "FTX", {2: "a:b"}, 2, new_value="x")
    result = replace_component(message, "FTX", {2: "a?:b"}, 2, new_value="x")
    assert result == "FTX+AAI+1+x'"


def test_component_level_identity():
    message = "LIN+3++5020436373085:EN'\nLIN+4++5020436373108:EN'"
    result = replace_component(
        message, "LIN", {(2, 0): "5020436373108"}, 0, new_value="5"
    )
    assert result == "LIN+3++5020436373085:EN'\nLIN+5++5020436373108:EN'"


def test_whole_element_replacement():
    """component=None replaces the whole element, escaping the value."""
    message = "FTX+AAI+1+OLD:X'"
    result = replace_component(message, "FTX", {0: "AAI"}, 2, new_value="A:B")
    assert result == "FTX+AAI+1+A?:B'"


def test_custom_control_characters_from_una():
    message = "UNA|+,? 'UNB+UNOA:4+SENDER+RECIPIENT+260115:0930+1'FTX+AAI+1+a|b'"
    result = replace_component(message, "FTX", {0: "AAI"}, 2, 1, new_value="x|y+z'w?")
    assert result.endswith("FTX+AAI+1+a|x?|y?+z?'w??'")


def test_line_breaks_between_segments_are_preserved():
    message = "LIN+3++5020436373085:EN'\r\nFTX+AAI+1++OLD'\nUNT+4+1'"
    result = replace_component(message, "FTX", {3: "OLD"}, 3, new_value="NEW")
    assert result == "LIN+3++5020436373085:EN'\r\nFTX+AAI+1++NEW'\nUNT+4+1'"


def test_new_value_must_be_str():
    with pytest.raises(TypeError):
        replace_component(INTERCHANGE, "FTX", {0: "AAI"}, 3, new_value=None)


def test_negative_indexes_are_rejected():
    with pytest.raises(ValueError):
        replace_component(INTERCHANGE, "FTX", {0: "AAI"}, -1, new_value="x")
    with pytest.raises(ValueError):
        replace_component(INTERCHANGE, "FTX", {-1: "AAI"}, 0, new_value="x")
