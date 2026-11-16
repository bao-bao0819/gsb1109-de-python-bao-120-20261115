"""Tests for matching message segments against a message directory structure."""

import pytest

from pydifact.exceptions import MessageStructureError
from pydifact.segmentcollection import Interchange
from pydifact.segments import Segment
from pydifact.structure import MatchedGroup, MatchedSegment, MessageStructure


def order_segments(*items: list[Segment]) -> list[Segment]:
    """Wrap item segments into a minimal ORDERS message skeleton."""
    return [
        Segment("BGM", "220", "PO123456", "9"),
        Segment("DTM", ["137", "20250322", "102"]),
        *(segment for item in items for segment in item),
        Segment("UNS", "S"),
        Segment("CNT", ["2", str(len(items))]),
    ]


def item(
    number: str, *, quantities: int = 1, price: str | None = "25.00"
) -> list[Segment]:
    segments = [Segment("LIN", number, "", [f"ARTICLE{number}", "EN"])]
    segments += [Segment("QTY", ["21", "8"])] * quantities
    if price is not None:
        segments.append(Segment("PRI", ["AAA", price]))
    return segments


@pytest.fixture
def structure() -> MessageStructure:
    return MessageStructure.from_directory("ORDERS")


def test_item_groups_paths_and_occurrence_order(structure):
    """Several LIN/QTY/PRI item groups, incl. repeats within one group."""
    result = structure.match(
        order_segments(
            item("1", quantities=2),
            item("2"),
            item("3", quantities=2),
        )
    )

    groups = list(result.iter_groups())
    assert [group.path for group in groups] == [
        "SG29[1]",
        "SG29[1]/SG33[1]",
        "SG29[2]",
        "SG29[2]/SG33[1]",
        "SG29[3]",
        "SG29[3]/SG33[1]",
    ]
    assert [group.occurrence for group in groups] == [1, 1, 2, 1, 3, 1]

    # the repeated QTY segments stay within their own item group occurrence
    first_item = groups[0]
    quantities = [
        child
        for child in first_item.children
        if isinstance(child, MatchedSegment) and child.segment.tag == "QTY"
    ]
    assert [qty.occurrence for qty in quantities] == [1, 2]
    assert [qty.position for qty in quantities] == [4, 5]


def test_missing_optional_price_is_not_filled_from_next_item(structure):
    """A missing optional PRI must not borrow the next item's PRI."""
    result = structure.match(
        order_segments(
            item("1", price=None),  # no price in the first item
            item("2", price="30.00"),
        )
    )

    first_item, second_item = result.get_groups("SG29")
    # the first item group has no price group at all
    assert [
        child.group_id
        for child in first_item.children
        if isinstance(child, MatchedGroup)
    ] == []
    # the price belongs to the second item group
    price_groups = [
        child
        for child in second_item.children
        if isinstance(child, MatchedGroup) and child.group_id == "SG33"
    ]
    assert len(price_groups) == 1
    assert price_groups[0].path == "SG29[2]/SG33[1]"
    price = price_groups[0].children[0]
    assert isinstance(price, MatchedSegment)
    assert price.segment.tag == "PRI"
    assert price.segment.elements == [["AAA", "30.00"]]


def test_illegal_order_names_the_original_segment(structure):
    """A QTY after the price group cannot be placed: error, no reordering."""
    segments = order_segments(
        [
            Segment("LIN", "1", "", ["ARTICLE1", "EN"]),
            Segment("PRI", ["AAA", "25.00"]),
            Segment("QTY", ["21", "8"]),
        ]
    )
    with pytest.raises(MessageStructureError) as excinfo:
        structure.match(segments)
    message = str(excinfo.value)
    # the error points to the offending segment of the original message
    assert "#5" in message
    assert "QTY" in message


def test_price_before_first_item_is_rejected(structure):
    """A PRI outside any item group is an illegal order, not a floating price."""
    segments = order_segments([Segment("PRI", ["AAA", "25.00"])])
    with pytest.raises(MessageStructureError) as excinfo:
        structure.match(segments)
    message = str(excinfo.value)
    assert "#3" in message
    assert "PRI" in message


def test_unknown_segment_is_rejected(structure):
    segments = order_segments([Segment("LIN", "1", "", ["ARTICLE1", "EN"])])
    segments.insert(3, Segment("XYZ", "foo"))
    with pytest.raises(MessageStructureError) as excinfo:
        structure.match(segments)
    assert "XYZ" in str(excinfo.value)


def test_missing_required_segment_is_rejected(structure):
    segments = order_segments(item("1"))
    del segments[1]  # remove the mandatory DTM segment
    with pytest.raises(MessageStructureError) as excinfo:
        structure.match(segments)
    assert "DTM" in str(excinfo.value)


def test_order_file_group_structure(structure, path):
    """End-to-end: the sample order restores its group hierarchy."""
    interchange = Interchange.from_file(f"{path}/order.edi")
    message = next(interchange.get_messages())
    result = message.match_structure()

    assert [group.path for group in result.iter_groups()] == [
        "SG1[1]",
        "SG2[1]",
        "SG2[2]",
        "SG2[3]",
        "SG29[1]",
        "SG29[1]/SG34[1]",
        "SG29[2]",
        "SG29[2]/SG34[1]",
    ]

    items = list(result.get_groups("SG29"))
    assert [item.occurrence for item in items] == [1, 2]
    for item_group, line_number in zip(items, ["1", "2"]):
        line = next(item_group.iter_segments())
        assert line.segment.tag == "LIN"
        assert line.segment.elements[0] == line_number


def test_unknown_message_type_raises(structure):
    with pytest.raises(FileNotFoundError):
        MessageStructure.from_directory("NOSUCHMSG")


def test_invalid_directory_raises():
    with pytest.raises(ValueError):
        MessageStructure.from_directory("ORDERS", directory="../etc")
