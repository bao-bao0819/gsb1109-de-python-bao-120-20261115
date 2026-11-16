import datetime
from decimal import Decimal

import pytest

from pydifact.quantity import (
    ConversionRule,
    NormalizationStatus,
    ProductCatalog,
    QtyValue,
    QuantityKind,
    classify_quantity,
    load_qualifier_titles,
    normalize_quantities,
)
from pydifact.segments import Segment

PRODUCT = "4012345000004"


def lin(product_id=PRODUCT, line="1"):
    return Segment("LIN", line, "", [product_id, "EN"])


def qty(qualifier, quantity, unit=""):
    composite = [qualifier, quantity] + ([unit] if unit else [])
    return Segment("QTY", composite)


def dtm(date="20261115", fmt="102"):
    return Segment("DTM", ["137", date, fmt])


@pytest.fixture
def catalog():
    return ProductCatalog(
        [
            ConversionRule(
                product_id=PRODUCT,
                from_unit="CT",
                to_unit="EA",
                factor=Decimal("12"),
                effective_from=datetime.date(2026, 1, 1),
                effective_to=datetime.date(2026, 7, 1),
            ),
            # packaging spec change: same product, new version
            ConversionRule(
                product_id=PRODUCT,
                from_unit="CT",
                to_unit="EA",
                factor=Decimal("24"),
                effective_from=datetime.date(2026, 7, 1),
            ),
            ConversionRule(
                product_id=PRODUCT,
                from_unit="KGM",
                to_unit="EA",
                factor=Decimal("2.5"),
                effective_from=datetime.date(2026, 1, 1),
            ),
        ]
    )


def test_load_qualifier_titles():
    titles = load_qualifier_titles()
    assert titles["12"] == "Despatch quantity"
    assert titles["46"] == "Pieces delivered"
    assert titles["48"] == "Received quantity"


def test_classify_quantity():
    assert classify_quantity("46", "EA") == QuantityKind.PIECES
    assert classify_quantity("12", "EA") == QuantityKind.DESPATCHED
    assert classify_quantity("48", "EA") == QuantityKind.RECEIVED
    # a weight unit means net weight, whatever the qualifier
    assert classify_quantity("12", "KGM") == QuantityKind.NET_WEIGHT
    # known but unmapped qualifier
    assert classify_quantity("21", "EA") is None


def test_qty_value_from_segment():
    value = QtyValue.from_segment(qty("12", "47.5", "KGM"))
    assert value.qualifier == "12"
    assert value.quantity_raw == "47.5"
    assert value.unit == "KGM"
    with pytest.raises(ValueError):
        QtyValue.from_segment(Segment("LIN", "1"))


def test_identity_conversion_keeps_exact_decimal(catalog):
    results = normalize_quantities([dtm(), lin(), qty("46", "2.500", "EA")], catalog)
    assert len(results) == 1
    result = results[0]
    assert result.status == NormalizationStatus.NORMALIZED
    assert result.kind == QuantityKind.PIECES
    assert result.original_quantity == Decimal("2.500")
    assert result.original_quantity_raw == "2.500"
    # exact decimal, no float rounding, trailing zero preserved
    assert result.normalized_quantity == Decimal("2.500")
    assert result.normalized_unit == "EA"


def test_despatched_cartons_converted_with_effective_version(catalog):
    results = normalize_quantities(
        [dtm("20260201"), lin(), qty("12", "3", "CT")], catalog
    )
    (result,) = results
    assert result.status == NormalizationStatus.NORMALIZED
    assert result.kind == QuantityKind.DESPATCHED
    assert result.normalized_quantity == Decimal("36")
    assert result.rule is not None
    assert result.rule.factor == Decimal("12")


def test_packaging_spec_change_uses_effective_version(catalog):
    old = normalize_quantities([dtm("20260630"), lin(), qty("12", "3", "CT")], catalog)
    new = normalize_quantities([dtm("20260701"), lin(), qty("12", "3", "CT")], catalog)
    assert old[0].normalized_quantity == Decimal("36")
    assert new[0].normalized_quantity == Decimal("72")


def test_net_weight_converted_via_product_rule(catalog):
    results = normalize_quantities([dtm(), lin(), qty("12", "47.5", "KGM")], catalog)
    (result,) = results
    assert result.status == NormalizationStatus.NORMALIZED
    assert result.kind == QuantityKind.NET_WEIGHT
    assert result.original_quantity == Decimal("47.5")
    assert result.normalized_quantity == Decimal("118.75")


def test_decimal_precision_is_exact(catalog):
    precise_catalog = ProductCatalog(
        [
            ConversionRule(
                product_id=PRODUCT,
                from_unit="CT",
                to_unit="EA",
                factor=Decimal("0.3"),
                effective_from=datetime.date(2026, 1, 1),
            )
        ]
    )
    (result,) = normalize_quantities(
        [dtm(), lin(), qty("12", "0.1", "CT")], precise_catalog
    )
    assert result.normalized_quantity == Decimal("0.03")


def test_missing_rule_is_not_guessed(catalog):
    results = normalize_quantities([dtm(), lin(), qty("12", "5", "PAL")], catalog)
    (result,) = results
    assert result.status == NormalizationStatus.UNCONVERTED
    assert result.normalized_quantity is None
    assert result.original_quantity == Decimal("5")
    assert "PAL" in result.reason


def test_missing_document_date_is_not_guessed(catalog):
    results = normalize_quantities([lin(), qty("12", "3", "CT")], catalog)
    (result,) = results
    assert result.status == NormalizationStatus.UNCONVERTED
    assert result.normalized_quantity is None
    assert "document date" in result.reason


def test_explicit_document_date_overrides_message(catalog):
    results = normalize_quantities(
        [dtm("20260201"), lin(), qty("12", "3", "CT")],
        catalog,
        document_date=datetime.date(2026, 8, 1),
    )
    assert results[0].normalized_quantity == Decimal("72")


def test_unknown_qualifier_is_not_guessed(catalog):
    results = normalize_quantities([dtm(), lin(), qty("9999", "3", "CT")], catalog)
    (result,) = results
    assert result.status == NormalizationStatus.UNKNOWN_QUALIFIER
    assert result.qualifier_title is None
    assert result.normalized_quantity is None


def test_unmapped_known_qualifier_still_converts(catalog):
    # 21 = Ordered quantity: known in the code table, but not one of the
    # mapped inbound kinds. The unit conversion itself is still evidence-based.
    results = normalize_quantities(
        [dtm("20260201"), lin(), qty("21", "3", "CT")], catalog
    )
    (result,) = results
    assert result.qualifier_title == "Ordered quantity"
    assert result.kind is None
    assert result.status == NormalizationStatus.NORMALIZED
    assert result.normalized_quantity == Decimal("36")


def test_invalid_quantity_is_reported(catalog):
    results = normalize_quantities([dtm(), lin(), qty("12", "abc", "EA")], catalog)
    (result,) = results
    assert result.status == NormalizationStatus.INVALID_QUANTITY
    assert result.original_quantity is None
    assert result.original_quantity_raw == "abc"


def test_qty_without_lin_context_is_not_guessed(catalog):
    results = normalize_quantities([dtm(), qty("12", "3", "CT")], catalog)
    (result,) = results
    assert result.status == NormalizationStatus.UNCONVERTED
    assert result.product_id is None


def test_multiple_line_items(catalog):
    other = "9876543210001"
    segments = [
        dtm("20260201"),
        lin(PRODUCT, "1"),
        qty("12", "2", "CT"),
        lin(other, "2"),
        qty("12", "4", "CT"),
    ]
    results = normalize_quantities(segments, catalog)
    assert [r.product_id for r in results] == [PRODUCT, other]
    assert results[0].normalized_quantity == Decimal("24")
    # no rule for the second product: not guessed
    assert results[1].status == NormalizationStatus.UNCONVERTED
    assert results[1].normalized_quantity is None


def test_document_date_from_dtm_formats(catalog):
    segments = [
        Segment("DTM", ["137", "202611151030", "203"]),
        lin(),
        qty("12", "1", "CT"),
    ]
    (result,) = normalize_quantities(segments, catalog)
    assert result.status == NormalizationStatus.NORMALIZED
    assert result.normalized_quantity == Decimal("24")


def test_full_message_roundtrip(catalog):
    from pydifact.segmentcollection import Interchange

    message = (
        "UNA:+.? '"
        "UNB+UNOC:4+SENDER+RECEIVER+20261115:1200+1'"
        "UNH+1+DESADV:D:24A:UN'"
        "BGM+351+DESP001'"
        "DTM+137:20261115:102'"
        f"LIN+1++{PRODUCT}:EN'"
        "QTY+12:2:CT'"
        "QTY+12:47.5:KGM'"
        "UNT+7+1'"
        "UNZ+1+1'"
    )
    interchange = Interchange.from_str(message)
    results = []
    for msg in interchange.get_messages():
        results.extend(normalize_quantities(msg.segments, catalog))
    assert [r.normalized_quantity for r in results] == [
        Decimal("48"),
        Decimal("118.75"),
    ]
