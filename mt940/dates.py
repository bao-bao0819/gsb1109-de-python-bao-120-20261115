"""Resolve year-less entry dates against a statement coverage window.

MT940 transaction lines carry a complete value date (``YYMMDD``) but the
optional entry date is only ``MMDD``. This module turns that literal into one
or more calendar dates without consulting the system clock. Resolution uses,
in order:

* the statement coverage window, normally derived from opening and closing
  balances around the transaction;
* optional bank rules, for example a maximum posting lag after the value date;
* the value date as the only plausibility anchor when neither is available.

When more than one year remains possible, :func:`resolve_entry_date` returns
every candidate instead of selecting one. An impossible month/day such as
``0230`` is reported as invalid; it is never normalised to the next month.
"""

from __future__ import annotations

import dataclasses
import datetime
from typing import TYPE_CHECKING, Any

from . import models

if TYPE_CHECKING:
    from collections.abc import Mapping


@dataclasses.dataclass(frozen=True, slots=True)
class EntryDateRules:
    """Bank-specific limits applied while resolving an entry date.

    All limits are optional. A ``None`` offset leaves that direction
    unrestricted. ``window_start`` and ``window_end`` supply a fallback
    coverage window when the statement has no usable balance dates; when both
    the statement and the rule provide a bound, the narrower bound wins.

    Attributes:
        name: Optional bank or rule-set label retained in evidence.
        max_days_from_value: Maximum absolute number of days between the
            entry date and the value date, in either direction.
        max_days_before_value: Maximum days an entry date may precede the
            value date. ``0`` requires the entry date to be on or after it.
        max_days_after_value: Maximum days an entry date may follow the value
            date. ``0`` requires the entry date to be on or before it.
        years_before: Calendar years before the value date whose occurrence
            of the literal is tested. A coverage window then filters them.
        years_after: Calendar years after the value date whose occurrence of
            the literal is tested.
        window_start: Optional first day of a fallback coverage window.
        window_end: Optional last day of a fallback coverage window.
        require_window: Reject the entry date unless a coverage window exists.
    """

    name: str = ''
    max_days_from_value: int | None = None
    max_days_before_value: int | None = None
    max_days_after_value: int | None = None
    years_before: int = 1
    years_after: int = 1
    window_start: datetime.date | None = None
    window_end: datetime.date | None = None
    require_window: bool = False

    def __post_init__(self) -> None:
        """Validate non-negative offsets and an ordered fallback window.

        Raises:
            ValueError: An offset or year span is negative, or the fallback
                window starts after it ends.
        """
        for name in (
            'max_days_from_value',
            'max_days_before_value',
            'max_days_after_value',
            'years_before',
            'years_after',
        ):
            value = getattr(self, name)
            if value is not None and value < 0:
                msg = f'{name} must not be negative'
                raise ValueError(msg)
        if (
            self.window_start is not None
            and self.window_end is not None
            and self.window_start > self.window_end
        ):
            msg = 'window_start must not be after window_end'
            raise ValueError(msg)


#: Rules used when the caller supplies none. They add no posting-lag
#: restriction; the coverage window and value-date proximity still apply.
DEFAULT_RULES = EntryDateRules()

#: Largest accepted gap, in days, between an entry date and its value date
#: when no complete window bounds the candidate. A wider gap is another year,
#: not a posting delay, and cannot be resolved without guessing.
_MAX_ENTRY_GAP_DAYS = 330

#: Longest span accepted as one coverage window. Longer spans mean merged
#: statements or inverted balances, not a usable resolution bound.
_MAX_WINDOW_DAYS = 366

#: Width of an ``MMDD`` entry date literal.
_LITERAL_LENGTH = 4


@dataclasses.dataclass(frozen=True, slots=True)
class EntryDateResolution:
    """Result of resolving one entry-date literal.

    Attributes:
        raw: Original literal, preserved even when it is invalid or ambiguous.
        status: ``resolved``, ``ambiguous``, ``invalid``, ``outside_window``,
            ``outside_bank_rule``, ``window_required`` or ``absent``.
        basis: Short reason code for the result.
        date: Resolved entry date when exactly one candidate remains.
        candidates: Candidates accepted by the active window and rules.
        calendar_candidates: Calendar-valid candidates before window/rule
            filtering, oldest first.
        window: Effective coverage window used, or ``None``.
        value_date: Value date against which offsets were measured.
        rules: Rule set applied while resolving.
        explanation: Human-readable description of the decision.
    """

    raw: str | None
    status: str
    basis: str
    date: models.Date | None
    candidates: tuple[models.Date, ...]
    calendar_candidates: tuple[models.Date, ...]
    window: tuple[datetime.date | None, datetime.date | None] | None
    value_date: datetime.date | None
    rules: EntryDateRules
    explanation: str


def _as_date(value: object) -> datetime.date | None:
    """Return a date-like balance value unchanged, or ``None``."""
    return value if isinstance(value, datetime.date) else None


def balance_window(
    balances: list[tuple[str, datetime.date]],
    value_date: datetime.date,
) -> tuple[datetime.date | None, datetime.date | None]:
    """Find the balance window surrounding one transaction value date.

    Using dates rather than file order keeps separate statement blocks in one
    merged parse from borrowing each other's endpoints. The latest opening on
    or before the value date and the earliest closing on or after it bound the
    transaction. Missing sides remain ``None``.

    Args:
        balances: ``(field name, date)`` pairs recorded while parsing.
        value_date: The transaction's complete value date.

    Returns:
        Inclusive ``(start, end)`` bounds, either of which may be ``None``.
    """
    openings = [
        day
        for name, day in balances
        if 'opening_balance' in name and day <= value_date
    ]
    closings = [
        day
        for name, day in balances
        if 'closing_balance' in name and day >= value_date
    ]
    start = max(openings) if openings else None
    end = min(closings) if closings else None
    if (
        start is not None
        and end is not None
        and (end < start or (end - start).days > _MAX_WINDOW_DAYS)
    ):
        # A closing before its opening, or a span over a year, cannot be one
        # coverage window. The values disagree, so neither becomes a bound.
        return None, None
    return start, end


def statement_window(
    data: Mapping[str, object],
) -> tuple[datetime.date | None, datetime.date | None]:
    """Derive a coverage window from statement metadata balances.

    Opening-balance keys provide the earliest start and closing-balance keys
    the latest end. When a statement has balances under other names but no
    opening or closing endpoint, all balance dates are used as a fallback.

    Args:
        data: Statement metadata mapping, normally ``Transactions.data``.

    Returns:
        ``(start, end)`` with either side ``None`` when no such balance date
        exists. The bounds are inclusive.
    """
    openings: list[datetime.date] = []
    closings: list[datetime.date] = []
    all_dates: list[datetime.date] = []
    for key, value in data.items():
        if not key.endswith('_balance'):
            continue
        balance_date = _as_date(getattr(value, 'date', None))
        if balance_date is None:
            continue
        all_dates.append(balance_date)
        if 'opening_balance' in key:
            openings.append(balance_date)
        elif 'closing_balance' in key:
            closings.append(balance_date)
    if openings or closings:
        return (
            min(openings) if openings else None,
            max(closings) if closings else None,
        )
    if all_dates:
        return min(all_dates), max(all_dates)
    return None, None


def _effective_window(
    window: tuple[datetime.date | None, datetime.date | None] | None,
    rules: EntryDateRules,
) -> tuple[datetime.date | None, datetime.date | None] | None:
    """Combine statement and rule windows, keeping the narrower bound."""
    starts = [
        bound
        for bound in (
            window[0] if window else None,
            rules.window_start,
        )
        if bound is not None
    ]
    ends = [
        bound
        for bound in (
            window[1] if window else None,
            rules.window_end,
        )
        if bound is not None
    ]
    start = max(starts) if starts else None
    end = min(ends) if ends else None
    if start is None and end is None:
        return None
    return start, end


def _candidate_years(
    value_date: datetime.date,
    rules: EntryDateRules,
) -> list[int]:
    """List years worth testing for an entry month/day.

    The value date and the configured span define the search. A coverage
    window then filters these candidates, but never adds a distant year from
    a stale balance: posting delays are measured in days, not years.
    """
    return list(
        range(
            value_date.year - rules.years_before,
            value_date.year + rules.years_after + 1,
        )
    )


def _calendar_candidates(
    raw: str, years: list[int]
) -> tuple[list[models.Date], bool]:
    """Build calendar-valid dates for every tested year.

    Returns:
        The valid dates and whether the raw month/day looked like four
        digits. An impossible date is skipped rather than normalised.
    """
    if len(raw) != _LITERAL_LENGTH or not raw.isdigit():
        return [], False
    month = int(raw[:2])
    day = int(raw[2:])
    candidates: list[models.Date] = []
    for year in years:
        try:
            candidate = datetime.date(year, month, day)
        except ValueError:
            continue
        candidates.append(
            models.Date(candidate.year, candidate.month, candidate.day)
        )
    return candidates, True


def _within_window(
    candidate: datetime.date,
    window: tuple[datetime.date | None, datetime.date | None] | None,
) -> bool:
    """Return whether a candidate lies inside the inclusive window."""
    if window is None:
        return True
    start, end = window
    if start is not None and candidate < start:
        return False
    return not (end is not None and candidate > end)


def _near_value_date(
    candidate: datetime.date, value_date: datetime.date
) -> bool:
    """Limit a candidate to a plausible posting delay around the value date."""
    return abs((candidate - value_date).days) <= _MAX_ENTRY_GAP_DAYS


def _within_rule(
    candidate: datetime.date, value_date: datetime.date, rules: EntryDateRules
) -> bool:
    """Return whether a candidate respects the bank's posting-lag limits."""
    delta = (candidate - value_date).days
    if (
        rules.max_days_from_value is not None
        and abs(delta) > rules.max_days_from_value
    ):
        return False
    if (
        rules.max_days_before_value is not None
        and delta < -rules.max_days_before_value
    ):
        return False
    return not (
        rules.max_days_after_value is not None
        and delta > rules.max_days_after_value
    )


def _basis(
    window: tuple[datetime.date | None, datetime.date | None] | None,
    rules: EntryDateRules,
) -> str:
    """Describe which filters contributed to a resolution."""
    parts: list[str] = []
    if window is not None:
        parts.append('statement_window')
    if (
        rules.max_days_from_value is not None
        or rules.max_days_before_value is not None
        or rules.max_days_after_value is not None
    ):
        parts.append('bank_rule')
    return '+'.join(parts) if parts else 'value_date_year'


def _explanation(
    raw: str | None,
    value_date: datetime.date | None,
    window: tuple[datetime.date | None, datetime.date | None] | None,
    candidates: tuple[models.Date, ...],
    status: str,
    basis: str,
) -> str:
    """Build a compact human-readable account of one resolution."""
    pieces = [f'entry literal {raw!r}']
    if value_date is not None:
        pieces.append(f'value date {value_date.isoformat()}')
    if window is not None:
        start, end = window
        pieces.append(
            'coverage window {}..{}'.format(
                start.isoformat() if start else 'open',
                end.isoformat() if end else 'open',
            )
        )
    if candidates:
        pieces.append(
            'candidates ' + ', '.join(item.isoformat() for item in candidates)
        )
    pieces.append(f'status={status}')
    pieces.append(f'basis={basis}')
    return '; '.join(pieces)


def _resolution(
    raw: str | None,
    value_date: datetime.date | None,
    window: tuple[datetime.date | None, datetime.date | None] | None,
    rules: EntryDateRules,
    *,
    status: str,
    basis: str,
    date: models.Date | None = None,
    candidates: tuple[models.Date, ...] = (),
    calendar_candidates: tuple[models.Date, ...] = (),
) -> EntryDateResolution:
    """Assemble a resolution and its evidence."""
    return EntryDateResolution(
        raw=raw,
        status=status,
        basis=basis,
        date=date,
        candidates=candidates,
        calendar_candidates=calendar_candidates,
        window=window,
        value_date=value_date,
        rules=rules,
        explanation=_explanation(
            raw, value_date, window, candidates, status, basis
        ),
    )


def resolve_entry_date(
    raw: str | None,
    value_date: datetime.date | None,
    *,
    window: tuple[datetime.date | None, datetime.date | None] | None = None,
    rules: EntryDateRules | None = None,
) -> EntryDateResolution:
    """Resolve a year-less ``MMDD`` entry date into legal calendar dates.

    The system clock is never consulted. When the coverage window and bank
    rules leave more than one candidate, the result is ``ambiguous`` and the
    candidates are returned in ascending date order. Invalid month/day pairs
    produce ``invalid`` rather than a normalised date in the following month.

    Args:
        raw: Entry-date literal from ``:61:``, normally four digits. ``None``,
            empty or blank text means the field was omitted.
        value_date: Complete value date from the same transaction.
        window: Inclusive statement coverage window. Either bound may be
            ``None`` when only one balance is available.
        rules: Bank-specific posting limits. ``None`` uses
            :data:`DEFAULT_RULES`.

    Returns:
        An :class:`EntryDateResolution`. ``date`` is set only when exactly one
        candidate remains; ``raw`` and the candidate lists survive every
        status.
    """
    active_rules = rules or DEFAULT_RULES
    if raw is None or not raw.strip():
        return _resolution(
            raw,
            value_date,
            _effective_window(window, active_rules),
            active_rules,
            status='absent',
            basis='absent',
        )

    effective_window = _effective_window(window, active_rules)
    if active_rules.require_window and effective_window is None:
        return _resolution(
            raw,
            value_date,
            None,
            active_rules,
            status='window_required',
            basis='window_required',
        )

    if value_date is None:
        return _resolution(
            raw,
            None,
            effective_window,
            active_rules,
            status='invalid',
            basis='missing_value_date',
        )

    years = _candidate_years(value_date, active_rules)
    calendar_candidates, numeric = _calendar_candidates(raw, years)
    calendar = tuple(calendar_candidates)
    if not numeric:
        return _resolution(
            raw,
            value_date,
            effective_window,
            active_rules,
            status='invalid',
            basis='invalid_literal',
            calendar_candidates=calendar,
        )
    if not calendar:
        return _resolution(
            raw,
            value_date,
            effective_window,
            active_rules,
            status='invalid',
            basis='invalid_month_day',
            calendar_candidates=calendar,
        )

    in_window = tuple(
        candidate
        for candidate in calendar
        if _within_window(candidate, effective_window)
    )
    if effective_window is not None and not in_window:
        return _resolution(
            raw,
            value_date,
            effective_window,
            active_rules,
            status='outside_window',
            basis='outside_window',
            calendar_candidates=calendar,
        )

    flexible_window = effective_window is None or (
        effective_window[0] is None or effective_window[1] is None
    )
    accepted = tuple(
        candidate
        for candidate in in_window
        if (not flexible_window or _near_value_date(candidate, value_date))
        and _within_rule(candidate, value_date, active_rules)
    )
    if not accepted:
        return _resolution(
            raw,
            value_date,
            effective_window,
            active_rules,
            status='outside_bank_rule',
            basis='outside_bank_rule',
            calendar_candidates=calendar,
        )

    if len(accepted) == 1:
        return _resolution(
            raw,
            value_date,
            effective_window,
            active_rules,
            status='resolved',
            basis=_basis(effective_window, active_rules),
            date=accepted[0],
            candidates=accepted,
            calendar_candidates=calendar,
        )

    return _resolution(
        raw,
        value_date,
        effective_window,
        active_rules,
        status='ambiguous',
        basis='ambiguous',
        candidates=accepted,
        calendar_candidates=calendar,
    )


def apply_resolution(
    data: dict[str, Any], resolution: EntryDateResolution
) -> None:
    """Write a resolution and its evidence into transaction fields.

    ``entry_date`` and the compatibility alias ``guessed_entry_date`` hold the
    resolved date only when exactly one candidate exists; otherwise both are
    ``None`` so no year is invented. The original literal, reason code and
    status are always kept. Unresolved results also record an explanation and
    list their candidates.

    Args:
        data: Mutable transaction data mapping.
        resolution: Result to apply.

    The operation returns ``None``. Absent input removes stale entry-date
    fields, including evidence from an earlier parse of the same collection.
    """
    evidence_keys = (
        'entry_date_raw',
        'entry_date_basis',
        'entry_date_status',
        'entry_date_explanation',
        'entry_date_candidates',
    )
    if resolution.status == 'absent':
        data.pop('entry_date', None)
        data.pop('guessed_entry_date', None)
        for key in evidence_keys:
            data.pop(key, None)
        return

    data['entry_date'] = resolution.date
    data['guessed_entry_date'] = resolution.date
    data['entry_date_raw'] = resolution.raw
    data['entry_date_basis'] = resolution.basis
    data['entry_date_status'] = resolution.status
    if resolution.date is not None:
        data.pop('entry_date_explanation', None)
        data.pop('entry_date_candidates', None)
        return
    data['entry_date_explanation'] = resolution.explanation
    if resolution.candidates:
        data['entry_date_candidates'] = list(resolution.candidates)
    else:
        data.pop('entry_date_candidates', None)
