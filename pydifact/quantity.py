# Pydifact - a python edifact library
#
# Inbound quantity normalization for warehouse receiving.
#
# This module unifies product quantities from QTY segments of inbound
# messages (e.g. DESADV, RECADV). A QTY qualifier (data element 6063) may
# express pieces, a net weight or a despatched quantity, and the quantity
# itself may be given in any unit (pieces, cartons, kilograms, ...).
# Quantities are converted into one unified base unit per product using
# explicit, effectively-dated conversion rules.
#
# Design principles:
#   * The original value is always kept, as raw string and as Decimal.
#   * All arithmetic uses Decimal, so fractions stay exact.
#   * Missing conversion evidence is never guessed: if no product rule,
#     no document date or no product context is available, the quantity
#     is reported as unconverted with an explicit reason.

import datetime
import xml.etree.ElementTree as ET  # nosec B405 - parses only bundled XML
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from functools import lru_cache
from pathlib import Path

from pydifact.constants import EDI_DEFAULT_DIRECTORY, Element
from pydifact.segments import Segment

#: Data element whose code table qualifies a QTY quantity.
QUANTITY_QUALIFIER_ELEMENT = "6063"

#: DTM qualifier for "Document/message date".
DOCUMENT_DATE_QUALIFIER = "137"

#: Measure units (data element 6411) that express a weight.
WEIGHT_UNITS = frozenset(
    {"KGM", "GRM", "MGM", "HGM", "DGM", "CGM", "TNE", "LBR", "ONZ"}
)


class QuantityKind(str, Enum):
    """What a QTY quantity expresses, for inbound unification."""

    #: Number of pieces (件数).
    PIECES = "pieces"
    #: A net weight (净重), signalled by a weight measure unit.
    NET_WEIGHT = "net_weight"
    #: Quantity despatched by the seller (已发数量).
    DESPATCHED = "despatched"
    #: Quantity received by the buyer.
    RECEIVED = "received"


#: Mapping from 6063 qualifier codes to quantity kinds. Only qualifiers
#: with a clearly documented meaning are mapped; anything else stays
#: unmapped instead of being guessed.
QUALIFIER_KIND_MAP = {
    "12": QuantityKind.DESPATCHED,  # Despatch quantity
    "46": QuantityKind.PIECES,  # Pieces delivered
    "48": QuantityKind.RECEIVED,  # Received quantity
}


class NormalizationStatus(str, Enum):
    """Outcome of normalizing one QTY quantity."""

    #: Converted into the base unit (including identity conversions).
    NORMALIZED = "normalized"
    #: Not converted: conversion evidence is missing. The original
    #: value is kept untouched.
    UNCONVERTED = "unconverted"
    #: The qualifier is not present in the 6063 code table.
    UNKNOWN_QUALIFIER = "unknown_qualifier"
    #: The quantity value is not a valid decimal number.
    INVALID_QUANTITY = "invalid_quantity"


@lru_cache(maxsize=8)
def _load_codes_xml(directory: str) -> ET.Element:
    """Load and cache codes.xml from the given syntax directory."""
    syntax_root = Path(__file__).parent / "syntax"
    data_path = (syntax_root / directory / "data").resolve()
    if not data_path.is_relative_to(syntax_root.resolve()):
        raise ValueError(
            f"Invalid directory '{directory}': must be a name below pydifact.syntax."
        )
    codes_path = data_path / "codes.xml"
    if not codes_path.exists():
        raise FileNotFoundError(f"codes.xml not found in directory: {directory}")
    tree = ET.parse(codes_path)  # nosec B314 - bundled package XML
    return tree.getroot()


def load_qualifier_titles(directory: str = EDI_DEFAULT_DIRECTORY) -> dict[str, str]:
    """Return the 6063 code table (qualifier code -> title) of a directory."""
    root = _load_codes_xml(directory)
    element = root.find(f".//data_element[@id='{QUANTITY_QUALIFIER_ELEMENT}']")
    if element is None:
        return {}
    return {
        code.get("id", ""): code.get("title", "") for code in element.findall("code")
    }


def classify_quantity(qualifier: str, unit: str) -> QuantityKind | None:
    """Classify a QTY quantity.

    A weight measure unit means the quantity is a net weight, regardless
    of the qualifier. Otherwise the qualifier code decides. Returns None
    when the qualifier has no mapped kind.
    """
    if unit.upper() in WEIGHT_UNITS:
        return QuantityKind.NET_WEIGHT
    return QUALIFIER_KIND_MAP.get(qualifier)


@dataclass(frozen=True)
class ConversionRule:
    """One product conversion rule, versioned by its effective period.

    ``factor`` converts ``from_unit`` into ``to_unit`` by multiplication:
    ``quantity_to = quantity_from * factor``. The rule applies to dates
    ``on`` with ``effective_from <= on < effective_to``; an
    ``effective_to`` of None means the rule is open-ended.
    """

    product_id: str
    from_unit: str
    to_unit: str
    factor: Decimal
    effective_from: datetime.date
    effective_to: datetime.date | None = None

    def is_effective_on(self, on: datetime.date) -> bool:
        if on < self.effective_from:
            return False
        return self.effective_to is None or on < self.effective_to


class ProductCatalog:
    """Conversion rules per product, selected by effective version."""

    def __init__(self, rules: Iterable[ConversionRule] = ()) -> None:
        self._rules: list[ConversionRule] = list(rules)

    def add_rule(self, rule: ConversionRule) -> None:
        self._rules.append(rule)

    def find_rule(
        self,
        product_id: str,
        from_unit: str,
        to_unit: str,
        on_date: datetime.date | None,
    ) -> ConversionRule | None:
        """Return the rule version effective on ``on_date``, if any.

        When several versions overlap, the one with the latest
        ``effective_from`` wins. Without a date there is no evidence to
        pick a version, so None is returned instead of guessing.
        """
        if on_date is None:
            return None
        candidates = [
            rule
            for rule in self._rules
            if rule.product_id == product_id
            and rule.from_unit == from_unit.upper()
            and rule.to_unit == to_unit.upper()
            and rule.is_effective_on(on_date)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda rule: rule.effective_from)


@dataclass(frozen=True)
class QtyValue:
    """The C186 composite of a QTY segment."""

    qualifier: str
    quantity_raw: str
    unit: str

    @classmethod
    def from_segment(cls, segment: Segment) -> "QtyValue":
        if segment.tag != "QTY":
            raise ValueError(f"Expected a QTY segment, got {segment.tag}.")
        composite = segment.elements[0] if segment.elements else []
        if not isinstance(composite, list):
            composite = [composite]
        qualifier = composite[0] if len(composite) > 0 else ""
        quantity_raw = composite[1] if len(composite) > 1 else ""
        unit = composite[2] if len(composite) > 2 else ""
        return cls(qualifier=qualifier, quantity_raw=quantity_raw, unit=unit)


@dataclass(frozen=True)
class NormalizedQuantity:
    """One QTY quantity, its classification and its unified value.

    The original value is always preserved in ``original_quantity_raw``
    and ``original_quantity``. ``normalized_quantity`` is None whenever
    the quantity could not be converted without guessing.
    """

    product_id: str | None
    qualifier: str
    qualifier_title: str | None
    kind: QuantityKind | None
    original_quantity: Decimal | None
    original_quantity_raw: str
    original_unit: str
    normalized_quantity: Decimal | None
    normalized_unit: str | None
    rule: ConversionRule | None
    status: NormalizationStatus
    reason: str | None = None


def _parse_quantity(raw: str) -> Decimal | None:
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def _parse_dtm_date(segment: Segment) -> datetime.date | None:
    """Extract the date of a DTM+137 segment (formats 101/102/203)."""
    composite = segment.elements[0] if segment.elements else []
    if not isinstance(composite, list) or len(composite) < 3:
        return None
    qualifier, value, fmt = composite[0], composite[1], composite[2]
    if qualifier != DOCUMENT_DATE_QUALIFIER:
        return None
    try:
        if fmt == "102" or fmt == "203":
            return datetime.date(int(value[0:4]), int(value[4:6]), int(value[6:8]))
        if fmt == "101":
            year = int(value[0:2])
            year += 2000 if year < 70 else 1900
            return datetime.date(year, int(value[2:4]), int(value[4:6]))
    except (ValueError, IndexError):
        return None
    return None


def _product_id_from_lin(segment: Segment) -> str | None:
    """Extract the item number (7140) from a LIN segment, if present."""
    composite: Element = segment.elements[2] if len(segment.elements) > 2 else []
    if isinstance(composite, list) and composite and composite[0]:
        return composite[0]
    return None


def iter_line_quantities(
    segments: Iterable[Segment],
) -> Iterator[tuple[str | None, datetime.date | None, Segment]]:
    """Yield (product_id, document_date, QTY segment) triples.

    Tracks the current LIN item number and the current DTM+137 document
    date while walking the segments of a message.
    """
    product_id: str | None = None
    document_date: datetime.date | None = None
    for segment in segments:
        if segment.tag == "LIN":
            product_id = _product_id_from_lin(segment)
        elif segment.tag == "DTM":
            date = _parse_dtm_date(segment)
            if date is not None:
                document_date = date
        elif segment.tag == "QTY":
            yield product_id, document_date, segment


def normalize_quantities(
    segments: Iterable[Segment],
    catalog: ProductCatalog,
    base_unit: str = "EA",
    document_date: datetime.date | None = None,
    directory: str = EDI_DEFAULT_DIRECTORY,
) -> list[NormalizedQuantity]:
    """Unify all QTY quantities of an inbound message into ``base_unit``.

    Args:
        segments: The message segments (e.g. ``message.segments``).
        catalog: Product conversion rules, versioned by effective date.
        base_unit: The unified target unit, defaults to "EA" (each).
        document_date: Date used to select the effective rule version.
            If None, the DTM+137 document date of the message is used.
        directory: EDIFACT directory for the 6063 qualifier code table.

    Returns:
        One NormalizedQuantity per QTY segment, in message order.
        Quantities without sufficient conversion evidence are returned
        with status UNCONVERTED and their original value untouched.
    """
    titles = load_qualifier_titles(directory)
    base_unit = base_unit.upper()
    results: list[NormalizedQuantity] = []
    for product_id, line_date, segment in iter_line_quantities(segments):
        qty = QtyValue.from_segment(segment)
        title = titles.get(qty.qualifier)
        kind = classify_quantity(qty.qualifier, qty.unit)
        quantity = _parse_quantity(qty.quantity_raw)

        def result(
            status: NormalizationStatus,
            normalized_quantity: Decimal | None = None,
            normalized_unit: str | None = None,
            rule: ConversionRule | None = None,
            reason: str | None = None,
        ) -> NormalizedQuantity:
            return NormalizedQuantity(
                product_id=product_id,
                qualifier=qty.qualifier,
                qualifier_title=title,
                kind=kind,
                original_quantity=quantity,
                original_quantity_raw=qty.quantity_raw,
                original_unit=qty.unit,
                normalized_quantity=normalized_quantity,
                normalized_unit=normalized_unit,
                rule=rule,
                status=status,
                reason=reason,
            )

        if title is None:
            results.append(
                result(
                    NormalizationStatus.UNKNOWN_QUALIFIER,
                    reason=(
                        f"Qualifier '{qty.qualifier}' is not in the "
                        f"{QUANTITY_QUALIFIER_ELEMENT} code table of {directory}."
                    ),
                )
            )
            continue

        if quantity is None:
            results.append(
                result(
                    NormalizationStatus.INVALID_QUANTITY,
                    reason=f"Quantity '{qty.quantity_raw}' is not a decimal number.",
                )
            )
            continue

        unit = qty.unit.upper()
        if unit == base_unit:
            results.append(
                result(
                    NormalizationStatus.NORMALIZED,
                    normalized_quantity=quantity,
                    normalized_unit=base_unit,
                )
            )
            continue

        if product_id is None:
            results.append(
                result(
                    NormalizationStatus.UNCONVERTED,
                    reason="No LIN product context for this QTY; "
                    "cannot select a conversion rule.",
                )
            )
            continue

        on_date = document_date or line_date
        rule = catalog.find_rule(product_id, unit, base_unit, on_date)
        if rule is None:
            if on_date is None:
                reason = (
                    "No document date (DTM+137) available to select the "
                    "effective conversion rule version."
                )
            else:
                reason = (
                    f"No conversion rule for product '{product_id}' from "
                    f"{unit} to {base_unit} effective on {on_date}."
                )
            results.append(result(NormalizationStatus.UNCONVERTED, reason=reason))
            continue

        results.append(
            result(
                NormalizationStatus.NORMALIZED,
                normalized_quantity=quantity * rule.factor,
                normalized_unit=base_unit,
                rule=rule,
            )
        )
    return results
