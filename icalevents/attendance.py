"""
Attendance, delegation and minimum-quorum analysis for calendar events.

The analyzer is deliberately conservative:

* only ``PARTSTAT=ACCEPTED`` is counted as confirmed;
* ``PARTSTAT=NEEDS-ACTION`` and a missing ``PARTSTAT`` are awaiting a reply;
* ``PARTSTAT=TENTATIVE`` is reported separately and is never confirmed;
* ``PARTSTAT=DECLINED`` is absent and is not part of the possible headcount;
* a delegate (``DELEGATED-TO``/``DELEGATED-FROM``) represents the delegator
  and the two people are never counted as two attendees;
* delegation cycles are reported separately and are not counted as ordinary
  attendance, so a malformed chain cannot inflate a quorum.

The public entry points are :func:`analyze_event` and :func:`analyze_events`.
Both accept an :class:`AliasTable` (or a mapping of canonical person ->
aliases) so that duplicate invitations are collapsed only when the caller has
explicitly declared the aliases.  Nothing is inferred from similar names or
e-mail local parts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from email.utils import parseaddr
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

from .icalparser import Event

__all__ = [
    "AliasTable",
    "AttendancePolicy",
    "AttendanceReport",
    "AttendanceStatus",
    "AttendeeRecord",
    "CountRange",
    "DelegationChain",
    "RequirementCheck",
    "RequirementStatus",
    "RoleRequirement",
    "analyze_calendar",
    "analyze_event",
    "analyze_events",
]


class AttendanceStatus(str, Enum):
    """How an invited person relates to the meeting."""

    CONFIRMED = "confirmed"
    TENTATIVE = "tentative"
    PENDING = "pending"
    DECLINED = "declined"
    CONFLICTED = "conflicted"
    DELEGATED = "delegated"
    OTHER = "other"


class RequirementStatus(str, Enum):
    """Result of checking one role's minimum headcount."""

    SATISFIED = "satisfied"
    AT_RISK = "at_risk"
    VIOLATED = "violated"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True)
class CountRange:
    """
    Inclusive lower and upper bound on the number of people who may attend.

    ``minimum`` contains only confirmed people.  ``maximum`` also contains
    people who replied tentatively, have not replied yet, or have conflicting
    replies.  Declined people are excluded.  When an invitation was delegated,
    the delegator is replaced by the delegate rather than counted twice.
    """

    minimum: int
    maximum: int

    def __post_init__(self) -> None:
        if self.minimum < 0 or self.maximum < 0:
            raise ValueError("headcount bounds cannot be negative")
        if self.minimum > self.maximum:
            raise ValueError("minimum headcount cannot exceed maximum")

    def __contains__(self, value: object) -> bool:
        return (
            isinstance(value, int)
            and not isinstance(value, bool)
            and self.minimum <= value <= self.maximum
        )


@dataclass(frozen=True)
class RoleRequirement:
    """Minimum number of confirmed people required for one iCalendar role."""

    role: str
    minimum: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "role", _normalize_role(self.role))
        if not isinstance(self.minimum, int) or isinstance(self.minimum, bool):
            raise TypeError("minimum must be an integer")
        if self.minimum < 0:
            raise ValueError("minimum must not be negative")


@dataclass(frozen=True)
class AttendancePolicy:
    """
    Rules used to check whether a meeting can reach its minimum attendance.

    ``requirements`` is a sequence of :class:`RoleRequirement` objects or
    ``(role, minimum)`` pairs.  A mapping of role -> minimum is also accepted
    by :meth:`from_mapping` and by :func:`analyze_event`.

    ``count_non_participants`` controls whether people with
    ``ROLE=NON-PARTICIPANT`` contribute to the total headcount.  They are
    always listed in the response buckets, but informational attendees should
    not make a quorum.
    """

    requirements: tuple[RoleRequirement, ...] = ()
    count_non_participants: bool = False

    def __post_init__(self) -> None:
        requirements = self.requirements
        if isinstance(requirements, Mapping):
            requirements = tuple(requirements.items())

        merged: dict[str, RoleRequirement] = {}
        for item in requirements:
            requirement = _coerce_requirement(item)
            current = merged.get(requirement.role)
            if current is None or requirement.minimum > current.minimum:
                merged[requirement.role] = requirement
        object.__setattr__(self, "requirements", tuple(merged.values()))

    @classmethod
    def from_mapping(cls, requirements: Mapping[str, int]) -> AttendancePolicy:
        """Build a policy from ``{"CHAIR": 1, "REQ-PARTICIPANT": 3}``."""

        return cls(
            requirements=tuple(
                RoleRequirement(role, minimum)
                for role, minimum in requirements.items()
            )
        )


@dataclass(frozen=True)
class RequirementCheck:
    """Outcome of one role requirement and the headcount range it saw."""

    role: str
    required: int
    headcount: CountRange
    status: RequirementStatus

    @property
    def met(self) -> bool:
        """True only when confirmation already reaches the requirement."""

        return self.status is RequirementStatus.SATISFIED


@dataclass(frozen=True)
class DelegationChain:
    """
    A delegation path.

    For ``a -> b -> c`` ``persons`` is ``("a", "b", "c")`` and ``cycle`` is
    false.  For a cycle the first person is repeated at the end and ``cycle``
    is true, for example ``("a", "b", "a")``.
    """

    persons: tuple[str, ...]
    cycle: bool = False


@dataclass(frozen=True)
class AttendeeRecord:
    """One deduplicated person in one event instance."""

    person: str
    display_name: str | None
    status: AttendanceStatus
    partstat: str | None
    role: str
    is_proxy: bool
    delegated_from: tuple[str, ...]
    delegated_to: tuple[str, ...]
    in_delegation_cycle: bool
    sources: int
    cutype: str
    addresses: tuple[str, ...]
    names: tuple[str, ...]
    counts_toward_headcount: bool
    is_synthetic: bool = False

    @property
    def is_delegator(self) -> bool:
        """True when this person handed the invitation to somebody else."""

        return bool(self.delegated_to)

    @property
    def is_delegate(self) -> bool:
        """True when this person received the invitation from somebody else."""

        return bool(self.delegated_from)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly representation."""

        return {
            "person": self.person,
            "display_name": self.display_name,
            "status": self.status.value,
            "partstat": self.partstat,
            "role": self.role,
            "is_proxy": self.is_proxy,
            "is_delegator": self.is_delegator,
            "is_delegate": self.is_delegate,
            "delegated_from": list(self.delegated_from),
            "delegated_to": list(self.delegated_to),
            "in_delegation_cycle": self.in_delegation_cycle,
            "sources": self.sources,
            "cutype": self.cutype,
            "addresses": list(self.addresses),
            "names": list(self.names),
            "counts_toward_headcount": self.counts_toward_headcount,
            "is_synthetic": self.is_synthetic,
        }


@dataclass(frozen=True)
class AttendanceReport:
    """Attendance analysis for one calendar event instance."""

    uid: str | None
    summary: str | None
    start: date | datetime | None
    end: date | datetime | None
    event_status: str | None
    confirmed: tuple[AttendeeRecord, ...]
    tentative: tuple[AttendeeRecord, ...]
    pending: tuple[AttendeeRecord, ...]
    declined: tuple[AttendeeRecord, ...]
    conflicted: tuple[AttendeeRecord, ...]
    delegated: tuple[AttendeeRecord, ...]
    other: tuple[AttendeeRecord, ...]
    proxies: tuple[AttendeeRecord, ...]
    excluded: tuple[AttendeeRecord, ...]
    duplicates_merged: int
    delegation_chains: tuple[DelegationChain, ...]
    delegation_cycles: tuple[DelegationChain, ...]
    headcount: CountRange
    role_counts: Mapping[str, CountRange]
    requirements: tuple[RequirementCheck, ...]

    @property
    def is_cancelled(self) -> bool:
        """True when the calendar instance itself was cancelled."""

        return str(self.event_status or "").strip().upper() == "CANCELLED"

    @property
    def all_requirements_met(self) -> bool:
        """True only when every applicable requirement is already confirmed."""

        return all(
            check.status is RequirementStatus.NOT_APPLICABLE or check.met
            for check in self.requirements
        )

    @property
    def has_pending_replies(self) -> bool:
        """True when at least one invited person has not given one clear answer."""

        return bool(self.pending or self.conflicted)

    @property
    def records(self) -> tuple[AttendeeRecord, ...]:
        """All deduplicated people across every response bucket."""

        buckets = (
            self.confirmed
            + self.tentative
            + self.pending
            + self.declined
            + self.conflicted
            + self.delegated
            + self.other
        )
        return tuple(sorted(buckets, key=lambda record: record.person))

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly representation of the complete report."""

        return {
            "uid": self.uid,
            "summary": self.summary,
            "start": _json_value(self.start),
            "end": _json_value(self.end),
            "event_status": self.event_status,
            "headcount": _range_dict(self.headcount),
            "role_counts": {
                role: _range_dict(value) for role, value in self.role_counts.items()
            },
            "requirements": [
                {
                    "role": check.role,
                    "required": check.required,
                    "minimum": check.headcount.minimum,
                    "maximum": check.headcount.maximum,
                    "status": check.status.value,
                    "met": check.met,
                }
                for check in self.requirements
            ],
            "all_requirements_met": self.all_requirements_met,
            "duplicates_merged": self.duplicates_merged,
            "delegation_chains": [
                {"persons": list(chain.persons), "cycle": chain.cycle}
                for chain in self.delegation_chains
            ],
            "delegation_cycles": [
                {"persons": list(chain.persons), "cycle": True}
                for chain in self.delegation_cycles
            ],
            "proxies": [record.to_dict() for record in self.proxies],
            "excluded": [record.to_dict() for record in self.excluded],
            "confirmed": [record.to_dict() for record in self.confirmed],
            "tentative": [record.to_dict() for record in self.tentative],
            "pending": [record.to_dict() for record in self.pending],
            "declined": [record.to_dict() for record in self.declined],
            "conflicted": [record.to_dict() for record in self.conflicted],
            "delegated": [record.to_dict() for record in self.delegated],
            "other": [record.to_dict() for record in self.other],
        }


class AliasTable:
    """
    Explicit mapping from aliases to canonical people.

    The canonical direction is used when constructing from a mapping::

        AliasTable({
            "alice@example.com": ["a.smith@example.com", "Alice Smith"],
        })

    An iterable of groups is also accepted, where the first item of a group
    is canonical::

        AliasTable([("alice@example.com", "a.smith@example.com", "Alice Smith")])

    Use :meth:`from_alias_mapping` for the opposite direction
    (``alias -> canonical``).  Aliases are matched case-insensitively and
    ``mailto:`` prefixes are ignored.  A name and an e-mail address are kept
    distinct unless the caller explicitly aliases them.
    """

    def __init__(
        self,
        groups: Mapping[str, Iterable[str] | str]
        | Iterable[Sequence[str]]
        | None = None,
    ) -> None:
        self._canonical_by_alias: dict[str, str] = {}
        self._canonical_display: dict[str, str] = {}
        self._aliases_by_canonical: dict[str, set[str]] = {}

        if groups is None:
            return

        if isinstance(groups, Mapping):
            for canonical, aliases in groups.items():
                self.add(canonical, aliases)
        elif isinstance(groups, Sequence) and not isinstance(groups, (str, bytes)):
            if all(isinstance(item, str) for item in groups):
                if groups:
                    self.add(groups[0], groups[1:])
            else:
                for group in groups:
                    values = list(group)
                    if values:
                        self.add(values[0], values[1:])
        else:
            for group in groups:
                values = list(group)
                if not values:
                    continue
                self.add(values[0], values[1:])

    @classmethod
    def from_alias_mapping(cls, aliases: Mapping[str, str]) -> AliasTable:
        """Build a table from ``alias -> canonical`` pairs."""

        groups: dict[str, list[str]] = {}
        for alias, canonical in aliases.items():
            groups.setdefault(str(canonical), []).append(str(alias))
        return cls(groups)

    def add(self, canonical: str, aliases: Iterable[str] | str = ()) -> None:
        """Add one canonical person and its aliases."""

        canonical_key = _canonical_key(canonical)
        if not canonical_key:
            raise ValueError("canonical person must not be empty")

        existing = self._canonical_by_alias.get(canonical_key)
        if existing is not None and existing != canonical_key:
            raise ValueError(
                "%r is already an alias of %r" % (canonical, existing)
            )

        self._canonical_display.setdefault(canonical_key, str(canonical).strip())
        self._canonical_by_alias[canonical_key] = canonical_key
        self._aliases_by_canonical.setdefault(canonical_key, set()).add(canonical_key)

        if aliases is None:
            aliases = ()
        elif isinstance(aliases, str):
            aliases = (aliases,)

        for alias in aliases:
            alias_key = _canonical_key(alias)
            if not alias_key:
                continue
            owner = self._canonical_by_alias.get(alias_key)
            if owner is not None and owner != canonical_key:
                raise ValueError(
                    "%r is already an alias of %r" % (alias, owner)
                )
            self._canonical_by_alias[alias_key] = canonical_key
            self._aliases_by_canonical[canonical_key].add(alias_key)

    def canonical(self, value: str | None) -> str | None:
        """Resolve one address or name to its canonical key, if aliased."""

        key = _canonical_key(value)
        if key is None:
            return None
        return self._canonical_by_alias.get(key)

    def display(self, canonical: str) -> str | None:
        """Return the display label supplied for a canonical person."""

        return self._canonical_display.get(canonical)

    def __contains__(self, value: object) -> bool:
        if not isinstance(value, str):
            return False
        return _canonical_key(value) in self._canonical_by_alias

    def __len__(self) -> int:
        return len(self._aliases_by_canonical)


@dataclass
class _MergedAttendee:
    person: str
    display_name: str | None = None
    addresses: set[str] = field(default_factory=set)
    names: set[str] = field(default_factory=set)
    responses: list[tuple[str | None, AttendanceStatus]] = field(
        default_factory=list
    )
    roles: list[str] = field(default_factory=list)
    role_explicit: bool = False
    delegated_to: set[str] = field(default_factory=set)
    delegated_from: set[str] = field(default_factory=set)
    cutype: str = "INDIVIDUAL"
    sources: int = 0
    synthetic: bool = False
    is_proxy: bool = False
    in_cycle: bool = False

    @property
    def role(self) -> str:
        if self.roles:
            return _higher_role(self.roles)
        return "REQ-PARTICIPANT"

    @property
    def status(self) -> AttendanceStatus:
        if self.in_cycle or self.delegated_to:
            return AttendanceStatus.DELEGATED
        if not self.responses:
            return AttendanceStatus.PENDING
        statuses = {status for _, status in self.responses}
        if AttendanceStatus.DECLINED in statuses and len(statuses) > 1:
            return AttendanceStatus.CONFLICTED
        return max(statuses, key=lambda status: _STATUS_PRIORITY[status])

    @property
    def partstat(self) -> str | None:
        if not self.responses:
            return None
        status = self.status
        values = sorted(
            {raw for raw, candidate in self.responses if candidate is status and raw},
        )
        if len(values) == 1:
            return values[0]
        if status is AttendanceStatus.CONFLICTED:
            return ",".join(
                sorted(
                    {
                        raw or "NEEDS-ACTION"
                        for raw, _ in self.responses
                    }
                )
            )
        return None


def analyze_event(
    event: Event,
    aliases: AliasTable | Mapping[str, Iterable[str] | str] | None = None,
    policy: AttendancePolicy | Mapping[str, int] | None = None,
) -> AttendanceReport:
    """
    Analyze attendance for one parsed :class:`~icalevents.icalparser.Event`.

    :param event: a calendar event instance
    :param aliases: explicit alias table, or a mapping canonical -> aliases
    :param policy: role minimums, either an :class:`AttendancePolicy` or a
        mapping such as ``{"CHAIR": 1, "REQ-PARTICIPANT": 3}``
    :return: deduplicated attendance, headcount range and requirement checks
    """

    alias_table = _coerce_aliases(aliases)
    attendance_policy = _coerce_policy(policy)

    merged: dict[str, _MergedAttendee] = {}
    for index, attendee in enumerate(_event_attendees(event)):
        raw = _read_attendee(attendee, alias_table, index)
        _merge_attendee(merged, raw)

    _add_synthetic_delegates(merged)
    _normalize_delegation_links(merged)
    graph = _delegation_graph(merged)
    cycles = _find_cycles(graph)
    cycle_people = {
        person for chain in cycles for person in chain.persons[:-1]
    }
    chains = _find_chains(graph, cycle_people)

    for person in cycle_people:
        if person in merged:
            merged[person].in_cycle = True

    _inherit_delegated_roles(merged)
    seats = _seat_ranges(merged, graph, attendance_policy)

    records: list[AttendeeRecord] = []
    for person in sorted(merged):
        item = merged[person]
        status = item.status
        role = item.role
        counts = _counts_toward_headcount(item, role, attendance_policy)
        records.append(
            AttendeeRecord(
                person=person,
                display_name=(
                    item.display_name
                    or alias_table.display(person)
                    or (sorted(item.addresses)[0] if item.addresses else None)
                    or person
                ),
                status=status,
                partstat=item.partstat,
                role=role,
                is_proxy=item.is_proxy or bool(item.delegated_from),
                delegated_from=tuple(sorted(item.delegated_from)),
                delegated_to=tuple(sorted(item.delegated_to)),
                in_delegation_cycle=item.in_cycle,
                sources=item.sources,
                cutype=item.cutype,
                addresses=tuple(sorted(item.addresses)),
                names=tuple(sorted(item.names)),
                counts_toward_headcount=counts,
                is_synthetic=item.synthetic,
            )
        )

    by_status = {status: [] for status in AttendanceStatus}
    for record in records:
        by_status[record.status].append(record)

    headcount = _combine_ranges(seats)
    role_counts = _role_ranges(seats, attendance_policy)
    cancelled = str(event.status or "").strip().upper() == "CANCELLED"
    checks = tuple(
        _check_requirement(requirement, role_counts, cancelled=cancelled)
        for requirement in attendance_policy.requirements
    )

    return AttendanceReport(
        uid=event.uid,
        summary=event.summary,
        start=event.start,
        end=event.end,
        event_status=event.status,
        confirmed=tuple(by_status[AttendanceStatus.CONFIRMED]),
        tentative=tuple(by_status[AttendanceStatus.TENTATIVE]),
        pending=tuple(by_status[AttendanceStatus.PENDING]),
        declined=tuple(by_status[AttendanceStatus.DECLINED]),
        conflicted=tuple(by_status[AttendanceStatus.CONFLICTED]),
        delegated=tuple(by_status[AttendanceStatus.DELEGATED]),
        other=tuple(by_status[AttendanceStatus.OTHER]),
        proxies=tuple(
            record
            for record in records
            if record.is_proxy
            and not record.delegated_to
            and record.status is not AttendanceStatus.DECLINED
        ),
        excluded=tuple(
            record for record in records if not record.counts_toward_headcount
        ),
        duplicates_merged=sum(max(0, record.sources - 1) for record in records),
        delegation_chains=tuple(chains),
        delegation_cycles=tuple(cycles),
        headcount=headcount,
        role_counts=MappingProxyType(role_counts),
        requirements=checks,
    )


def analyze_events(
    events: Iterable[Event],
    aliases: AliasTable | Mapping[str, Iterable[str] | str] | None = None,
    policy: AttendancePolicy | Mapping[str, int] | None = None,
) -> list[AttendanceReport]:
    """Analyze each event instance independently and preserve input order."""

    return [analyze_event(event, aliases=aliases, policy=policy) for event in events]


def analyze_calendar(
    url: str | None = None,
    file: str | Path | None = None,
    string_content: bytes | str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    aliases: AliasTable | Mapping[str, Iterable[str] | str] | None = None,
    policy: AttendancePolicy | Mapping[str, int] | None = None,
    fix_apple: bool = False,
    strict: bool = False,
) -> list[AttendanceReport]:
    """
    Read a calendar source and analyze every occurrence in the time range.

    The arguments mirror :func:`icalevents.icalevents.events`; each occurrence
    is analyzed independently, so a recurring meeting keeps its own replies.
    """

    from .icalevents import events as load_events

    return analyze_events(
        load_events(
            url=url,
            file=file,
            string_content=string_content,
            start=start,
            end=end,
            fix_apple=fix_apple,
            strict=strict,
        ),
        aliases=aliases,
        policy=policy,
    )


def _event_attendees(event: Event) -> list[Any]:
    attendee = event.attendee
    if attendee is None:
        return []
    if isinstance(attendee, (list, tuple, set)):
        return list(attendee)
    return [attendee]


def _read_attendee(
    attendee: Any, aliases: AliasTable, index: int
) -> dict[str, Any]:
    params = getattr(attendee, "params", None)
    address, parsed_name = _parse_address(attendee)
    name = _param_string(params, "CN") or parsed_name

    address_key = _address_key(address)
    name_key = _name_key(name)
    aliased_address = aliases.canonical(address) if address_key else None
    aliased_name = aliases.canonical(name) if name_key else None

    if aliased_address and aliased_name and aliased_address != aliased_name:
        raise ValueError(
            "attendee %r is aliased to both %r and %r"
            % (str(attendee), aliased_address, aliased_name)
        )
    if aliased_address:
        person = aliased_address
    elif aliased_name:
        person = aliased_name
    elif address_key:
        person = address_key
    else:
        person = name_key or "unknown:%d" % index

    partstat = _param_string(params, "PARTSTAT")
    if partstat is not None:
        partstat = partstat.strip().upper().replace("_", "-")

    return {
        "person": person,
        "display_name": name or None,
        "address": address_key,
        "name": name_key,
        "partstat": partstat,
        "status": _status_for_partstat(partstat),
        "role": _normalize_role(_param_string(params, "ROLE"))
        if _param_string(params, "ROLE")
        else None,
        "role_explicit": _param_string(params, "ROLE") is not None,
        "delegated_to": tuple(
            sorted(
                key
                for key in (
                    _reference_key(value, aliases)
                    for value in _param_values(_param(params, "DELEGATED-TO"))
                )
                if key
            )
        ),
        "delegated_from": tuple(
            sorted(
                key
                for key in (
                    _reference_key(value, aliases)
                    for value in _param_values(_param(params, "DELEGATED-FROM"))
                )
                if key
            )
        ),
        "cutype": (
            _param_string(params, "CUTYPE") or "INDIVIDUAL"
        ).strip().upper(),
    }


def _merge_attendee(
    merged: dict[str, _MergedAttendee], raw: Mapping[str, Any]
) -> None:
    person = raw["person"]
    item = merged.get(person)
    if item is None:
        item = _MergedAttendee(
            person=person,
            display_name=raw["display_name"],
        )
        merged[person] = item

    if item.synthetic:
        item.synthetic = False
    if item.display_name is None:
        item.display_name = raw["display_name"]

    if raw["address"]:
        item.addresses.add(raw["address"])
    if raw["name"]:
        item.names.add(raw["name"])
    item.responses.append((raw["partstat"], raw["status"]))
    item.delegated_to.update(raw["delegated_to"])
    item.delegated_from.update(raw["delegated_from"])
    item.sources += 1

    if raw["role"] is not None:
        item.role_explicit = True
        if raw["role"] not in item.roles:
            item.roles.append(raw["role"])

    if item.cutype == "INDIVIDUAL":
        item.cutype = raw["cutype"]
    elif raw["cutype"] != "INDIVIDUAL":
        item.cutype = _higher_cutype(item.cutype, raw["cutype"])


def _normalize_delegation_links(merged: dict[str, _MergedAttendee]) -> None:
    """Make ``DELEGATED-FROM`` and ``DELEGATED-TO`` describe the same edge."""

    for person, item in merged.items():
        for source in item.delegated_from:
            origin = merged.get(source)
            if origin is not None:
                origin.delegated_to.add(person)

    for source in merged.values():
        for target_key in source.delegated_to:
            target = merged.get(target_key)
            if target is None:
                continue
            target.delegated_from.add(source.person)


def _add_synthetic_delegates(merged: dict[str, _MergedAttendee]) -> None:
    """List a named delegate even when no ATTENDEE line exists for them."""

    for source in list(merged.values()):
        for target in list(source.delegated_to):
            if target in merged:
                continue
            delegate = _MergedAttendee(
                person=target,
                display_name=target,
                delegated_from={source.person},
                synthetic=True,
                is_proxy=True,
            )
            merged[target] = delegate


def _delegation_graph(
    merged: Mapping[str, _MergedAttendee],
) -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {
        person: set(item.delegated_to) for person, item in merged.items()
    }
    for person, item in merged.items():
        for source in item.delegated_from:
            graph.setdefault(source, set()).add(person)
    return graph


def _inherit_delegated_roles(
    merged: Mapping[str, _MergedAttendee],
) -> None:
    """
    Give a delegate the delegator's role when the delegate has no explicit role.

    The loop runs to a fixed point so ``chair -> delegate -> delegate`` keeps
    the chair role all the way down the chain.
    """

    for _ in range(len(merged) + 1):
        changed = False
        for source in merged.values():
            for target_key in source.delegated_to:
                target = merged.get(target_key)
                if target is None or target.role_explicit:
                    continue
                if source.role not in target.roles:
                    target.roles.append(source.role)
                    changed = True
        if not changed:
            break


def _counts_toward_headcount(
    item: _MergedAttendee, role: str, policy: AttendancePolicy
) -> bool:
    if item.status in {AttendanceStatus.DECLINED, AttendanceStatus.DELEGATED}:
        return False
    if item.delegated_to:
        return False
    return _person_is_countable(item, role, policy)


def _seat_ranges(
    merged: Mapping[str, _MergedAttendee],
    graph: Mapping[str, set[str]],
    policy: AttendancePolicy,
) -> list[tuple[str, CountRange]]:
    """
    Reduce each delegation tree to its terminal attendees.

    Delegation replaces the delegator's seat rather than adding a second one.
    A delegator and an intermediate delegate therefore do not create extra
    seats; every terminal person (a person who did not delegate further)
    decides whether the seat is confirmed, still possible, or lost.  A
    component containing a delegation cycle is discarded entirely so a
    malformed chain cannot inflate a quorum.
    """

    seats: list[tuple[str, CountRange]] = []
    for component in _components(graph):
        members = [
            merged[person]
            for person in component
            if person in merged and not merged[person].in_cycle
        ]
        for terminal in members:
            if terminal.delegated_to:
                continue
            if not _person_is_countable(terminal, terminal.role, policy):
                continue
            seats.append((terminal.role, _terminal_range(terminal.status)))
    return seats


def _terminal_range(status: AttendanceStatus) -> CountRange:
    if status is AttendanceStatus.CONFIRMED:
        return CountRange(1, 1)
    if status in {
        AttendanceStatus.TENTATIVE,
        AttendanceStatus.PENDING,
        AttendanceStatus.CONFLICTED,
        AttendanceStatus.OTHER,
    }:
        return CountRange(0, 1)
    return CountRange(0, 0)


def _person_is_countable(
    item: _MergedAttendee, role: str, policy: AttendancePolicy
) -> bool:
    if item.in_cycle:
        return False
    if item.cutype in {"ROOM", "RESOURCE", "GROUP"}:
        return False
    if not policy.count_non_participants and role == "NON-PARTICIPANT":
        return False
    return True


def _components(graph: Mapping[str, set[str]]) -> list[list[str]]:
    seen: set[str] = set()
    components: list[list[str]] = []
    for person in sorted(graph):
        if person in seen:
            continue
        stack = [person]
        group: list[str] = []
        seen.add(person)
        while stack:
            current = stack.pop()
            group.append(current)
            neighbors = set(graph.get(current, ()))
            neighbors.update(
                candidate
                for candidate, targets in graph.items()
                if current in targets
            )
            for neighbor in sorted(neighbors):
                if neighbor not in seen:
                    seen.add(neighbor)
                    stack.append(neighbor)
        components.append(sorted(group))
    return components


def _combine_ranges(ranges: Iterable[tuple[str, CountRange]]) -> CountRange:
    minimum = 0
    maximum = 0
    for _, value in ranges:
        minimum += value.minimum
        maximum += value.maximum
    return CountRange(minimum, maximum)


def _role_ranges(
    seats: Iterable[tuple[str, CountRange]], policy: AttendancePolicy
) -> dict[str, CountRange]:
    grouped: dict[str, list[CountRange]] = {}
    for role, value in seats:
        grouped.setdefault(role, []).append(value)
    for requirement in policy.requirements:
        grouped.setdefault(requirement.role, [])
    return {
        role: _combine_ranges((role, value) for value in values)
        for role, values in sorted(grouped.items())
    }


def _check_requirement(
    requirement: RoleRequirement,
    ranges: Mapping[str, CountRange],
    cancelled: bool = False,
) -> RequirementCheck:
    headcount = ranges.get(requirement.role, CountRange(0, 0))
    if cancelled:
        status = RequirementStatus.NOT_APPLICABLE
    elif headcount.minimum >= requirement.minimum:
        status = RequirementStatus.SATISFIED
    elif headcount.maximum >= requirement.minimum:
        status = RequirementStatus.AT_RISK
    else:
        status = RequirementStatus.VIOLATED
    return RequirementCheck(
        role=requirement.role,
        required=requirement.minimum,
        headcount=headcount,
        status=status,
    )


def _find_cycles(graph: Mapping[str, set[str]]) -> list[DelegationChain]:
    """Enumerate every elementary delegation cycle, once per rotation."""

    cycles: set[tuple[str, ...]] = set()
    for start in sorted(graph):
        stack: list[tuple[str, list[str]]] = [(start, [start])]
        while stack:
            node, path = stack.pop()
            for target in sorted(graph.get(node, ())):
                if target == start:
                    cycles.add(_rotate_cycle(path))
                elif target > start and target not in path:
                    stack.append((target, path + [target]))

    return [
        DelegationChain(persons=cycle, cycle=True)
        for cycle in sorted(cycles)
    ]


def _find_chains(
    graph: Mapping[str, set[str]], cycle_people: set[str]
) -> list[DelegationChain]:
    """Return maximal acyclic delegation paths that do not touch a cycle."""

    incoming: dict[str, int] = {person: 0 for person in graph}
    for targets in graph.values():
        for target in targets:
            incoming[target] = incoming.get(target, 0) + 1

    roots = sorted(
        person
        for person, count in incoming.items()
        if count == 0 and person not in cycle_people
    )
    chains: set[tuple[str, ...]] = set()

    def walk(person: str, path: tuple[str, ...]) -> None:
        if person in cycle_people:
            return
        targets = sorted(graph.get(person, ()))
        next_people = [
            target
            for target in targets
            if target not in path and target not in cycle_people
        ]
        if not next_people:
            if len(path) > 1 and not any(
                member in cycle_people for member in path
            ):
                chains.add(path)
            return
        for target in next_people:
            walk(target, path + (target,))

    for root in roots:
        walk(root, (root,))

    return [DelegationChain(persons=chain) for chain in sorted(chains)]


def _rotate_cycle(core: list[str]) -> tuple[str, ...]:
    if not core:
        return ()
    rotations = [tuple(core[index:] + core[:index]) for index in range(len(core))]
    best = min(rotations)
    return best + (best[0],)


def _status_for_partstat(partstat: str | None) -> AttendanceStatus:
    if partstat is None:
        return AttendanceStatus.PENDING
    return _PARTSTAT_TO_STATUS.get(partstat, AttendanceStatus.OTHER)


def _coerce_aliases(
    aliases: AliasTable | Mapping[str, Iterable[str] | str] | None,
) -> AliasTable:
    if aliases is None:
        return AliasTable()
    if isinstance(aliases, AliasTable):
        return aliases
    return AliasTable(aliases)


def _coerce_policy(
    policy: AttendancePolicy | Mapping[str, int] | None,
) -> AttendancePolicy:
    if policy is None:
        return AttendancePolicy()
    if isinstance(policy, AttendancePolicy):
        return policy
    if isinstance(policy, Mapping):
        return AttendancePolicy.from_mapping(policy)
    return AttendancePolicy(requirements=tuple(policy))


def _coerce_requirement(
    value: RoleRequirement | Sequence[Any] | Mapping[str, Any],
) -> RoleRequirement:
    if isinstance(value, RoleRequirement):
        return value
    if isinstance(value, Mapping):
        return RoleRequirement(value["role"], value["minimum"])
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if len(value) != 2:
            raise ValueError("requirement sequence must contain role and minimum")
        return RoleRequirement(value[0], value[1])
    raise TypeError("unsupported requirement: %r" % (value,))


def _parse_address(value: Any) -> tuple[str | None, str | None]:
    text = str(value).strip()
    if not text:
        return None, None

    lowered = text.casefold()
    if lowered.startswith("mailto:"):
        address = text[len("mailto:") :].strip().strip("<>").strip()
        return (address or None), None

    name, address = parseaddr(text)
    address = address.strip()
    if address.casefold().startswith("mailto:"):
        address = address[len("mailto:") :].strip().strip("<>").strip()
    if address and "@" in address:
        return address, name.strip() or None
    if address:
        return None, name.strip() or address
    return None, name.strip() or None


def _reference_key(value: Any, aliases: AliasTable) -> str | None:
    address, name = _parse_address(value)
    if address:
        key = aliases.canonical(address) or _address_key(address)
        return key
    if name:
        return aliases.canonical(name) or _name_key(name)
    return None


def _address_key(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if text.casefold().startswith("mailto:"):
        text = text[len("mailto:") :]
    text = text.strip().strip("<>").strip()
    return text.casefold() or None


def _name_key(value: str | None) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split()).casefold()
    return "name:" + text if text else None


def _canonical_key(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.casefold().startswith("mailto:"):
        text = text[len("mailto:") :]
    text = text.strip().strip("<>").strip()
    if not text:
        return None
    if "@" in text:
        return text.casefold()
    return "name:" + " ".join(text.split()).casefold()


def _normalize_role(role: str | None) -> str:
    if role is None:
        return "REQ-PARTICIPANT"
    key = str(role).strip().upper().replace("_", "-")
    return _ROLE_ALIASES.get(key, key or "REQ-PARTICIPANT")


def _higher_role(roles: Iterable[str]) -> str:
    return max(
        roles,
        key=lambda role: (_ROLE_PRIORITY.get(role, 0), role),
    )


def _higher_cutype(left: str, right: str) -> str:
    priority = {
        "INDIVIDUAL": 0,
        "UNKNOWN": 1,
        "GROUP": 2,
        "RESOURCE": 3,
        "ROOM": 4,
    }
    return left if priority.get(left, 0) >= priority.get(right, 0) else right


def _param(params: Any, name: str) -> Any:
    if not params:
        return None
    target = name.upper()
    for key, value in params.items():
        if str(key).upper() == target:
            return value
    return None


def _param_string(params: Any, name: str) -> str | None:
    value = _param(params, name)
    if value is None:
        return None
    if isinstance(value, (list, tuple, set)):
        if not value:
            return None
        value = next(iter(value))
    return str(value)


def _param_values(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _range_dict(value: CountRange) -> dict[str, int]:
    return {"minimum": value.minimum, "maximum": value.maximum}


def _json_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


_PARTSTAT_TO_STATUS = {
    "ACCEPTED": AttendanceStatus.CONFIRMED,
    "DECLINED": AttendanceStatus.DECLINED,
    "TENTATIVE": AttendanceStatus.TENTATIVE,
    "NEEDS-ACTION": AttendanceStatus.PENDING,
    "DELEGATED": AttendanceStatus.DELEGATED,
}

_STATUS_PRIORITY = {
    AttendanceStatus.CONFIRMED: 5,
    AttendanceStatus.TENTATIVE: 4,
    AttendanceStatus.PENDING: 3,
    AttendanceStatus.CONFLICTED: 2,
    AttendanceStatus.DECLINED: 1,
    AttendanceStatus.DELEGATED: 0,
    AttendanceStatus.OTHER: 0,
}

_ROLE_ALIASES = {
    "CHAIR": "CHAIR",
    "REQ-PARTICIPANT": "REQ-PARTICIPANT",
    "REQUIRED": "REQ-PARTICIPANT",
    "REQ": "REQ-PARTICIPANT",
    "OPT-PARTICIPANT": "OPT-PARTICIPANT",
    "OPTIONAL": "OPT-PARTICIPANT",
    "OPT": "OPT-PARTICIPANT",
    "NON-PARTICIPANT": "NON-PARTICIPANT",
    "NON": "NON-PARTICIPANT",
}

_ROLE_PRIORITY = {
    "CHAIR": 4,
    "REQ-PARTICIPANT": 3,
    "OPT-PARTICIPANT": 2,
    "NON-PARTICIPANT": 1,
}
