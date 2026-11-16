"""
Travel feasibility analysis for collected itineraries.

Given the expanded event instances produced by icalparser.parse_events
(recurring events already have their EXDATE exceptions and RECURRENCE-ID
overrides applied), this module checks whether consecutive meetings leave
enough time to travel between their locations.

Travel times are directed (A -> B may differ from B -> A) and can be
adjusted by time-of-day rules (e.g. rush hour). Unknown locations or
unknown location pairs are never treated as zero distance: they are
reported as unverifiable conflicts. When an event is cancelled, the newly
adjacent pair of events is re-evaluated instead of just dropping the old
conflicts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta

from icalevents.icalparser import Event

REASON_UNKNOWN_LOCATION = "unknown-location"
REASON_UNKNOWN_LEG = "unknown-leg"
REASON_INSUFFICIENT_TIME = "insufficient-time"


@dataclass(frozen=True)
class TimeRule:
    """
    Adjusts travel times for departures within a time-of-day window.

    The base travel time is multiplied by factor, then extra is added.
    Windows may cross midnight (start > end).

    :param start: window start (time of day)
    :param end: window end (time of day, exclusive)
    :param factor: multiplier applied to the base travel time
    :param extra: additional travel time added after the multiplier
    """

    start: time
    end: time
    factor: float = 1.0
    extra: timedelta = timedelta(0)

    def applies(self, moment: datetime) -> bool:
        """
        Check if a departure moment falls into this window.

        :param moment: departure datetime
        :return: True if the window applies
        """
        current = moment.time()
        if self.start <= self.end:
            return self.start <= current < self.end
        return current >= self.start or current < self.end


class TravelTable:
    """
    Directed travel times between locations plus time-of-day rules.
    """

    def __init__(self) -> None:
        self._legs: dict[tuple[str, str], timedelta] = {}
        self._rules: list[TimeRule] = []

    def set_travel_time(
        self, origin: str, destination: str, duration: timedelta
    ) -> None:
        """
        Set the directed base travel time from origin to destination.

        :param origin: origin location name
        :param destination: destination location name
        :param duration: base travel time
        """
        self._legs[(origin, destination)] = duration

    def add_time_rule(self, rule: TimeRule) -> None:
        """
        Add a time-of-day rule applied to matching departures.

        :param rule: time rule
        """
        self._rules.append(rule)

    def travel_time(
        self, origin: str, destination: str, depart_at: datetime
    ) -> timedelta | None:
        """
        Effective travel time for a directed leg at a departure moment.

        :param origin: origin location name
        :param destination: destination location name
        :param depart_at: departure moment used for time-of-day rules
        :return: travel time, or None if the leg is unknown
        """
        if origin == destination:
            return timedelta(0)

        base = self._legs.get((origin, destination))
        if base is None:
            return None

        factor = 1.0
        extra = timedelta(0)
        for rule in self._rules:
            if rule.applies(depart_at):
                factor *= rule.factor
                extra += rule.extra

        return base * factor + extra


@dataclass
class LegConflict:
    """
    One infeasible or unverifiable leg between two adjacent events.
    """

    previous: Event
    following: Event
    gap: timedelta
    required: timedelta | None
    reason: str

    @property
    def min_buffer(self) -> timedelta | None:
        """
        Minimum extra buffer that would make this leg feasible.

        :return: required minus gap, or None if the leg is unverifiable
        """
        if self.required is None:
            return None
        return self.required - self.gap


@dataclass
class ConflictChain:
    """
    Maximal run of consecutive leg conflicts sharing events.
    """

    conflicts: list[LegConflict]

    @property
    def events(self) -> list[Event]:
        """
        Events linked by this chain, in chronological order.

        :return: events of the chain
        """
        result = [self.conflicts[0].previous]
        result.extend(conflict.following for conflict in self.conflicts)
        return result

    @property
    def min_buffer(self) -> timedelta | None:
        """
        Minimum total buffer that would resolve the whole chain.

        :return: sum of leg buffers, or None if any leg is unverifiable
        """
        buffers = [conflict.min_buffer for conflict in self.conflicts]
        if any(buffer is None for buffer in buffers):
            return None
        return sum(buffers, timedelta(0))


def analyze_itinerary(
    events: list[Event],
    table: TravelTable,
    cancelled: list[Event | str] | None = None,
) -> list[ConflictChain]:
    """
    Check adjacent meetings for travel feasibility.

    Events with status CANCELLED and events listed in cancelled are removed
    first; the remaining events are then re-paired, so the newly adjacent
    neighbours of a cancelled event are checked against each other.

    :param events: expanded event instances (exceptions already applied)
    :param table: directed travel times and time-of-day rules
    :param cancelled: events or uids to treat as cancelled
    :return: conflict chains in chronological order
    """
    remaining = _active_events(events, cancelled or [])
    remaining.sort()

    conflicts = []
    for previous, following in zip(remaining, remaining[1:]):
        conflict = _check_leg(previous, following, table)
        if conflict:
            conflicts.append(conflict)

    return _chains(conflicts)


def _active_events(events: list[Event], cancelled: list[Event | str]) -> list[Event]:
    cancelled_uids = {item for item in cancelled if isinstance(item, str)}
    cancelled_instances = {
        (item.uid, item.start) for item in cancelled if isinstance(item, Event)
    }

    result = []
    for event in events:
        if event.start is None or event.end is None:
            continue
        if event.status and event.status.upper() == "CANCELLED":
            continue
        if event.uid in cancelled_uids:
            continue
        if (event.uid, event.start) in cancelled_instances:
            continue
        result.append(event)

    return result


def _check_leg(
    previous: Event, following: Event, table: TravelTable
) -> LegConflict | None:
    gap = following.start - previous.end

    origin = (previous.location or "").strip()
    destination = (following.location or "").strip()

    if not origin or not destination:
        return LegConflict(previous, following, gap, None, REASON_UNKNOWN_LOCATION)

    required = table.travel_time(origin, destination, previous.end)
    if required is None:
        return LegConflict(previous, following, gap, None, REASON_UNKNOWN_LEG)

    if gap < required:
        return LegConflict(
            previous, following, gap, required, REASON_INSUFFICIENT_TIME
        )

    return None


def _chains(conflicts: list[LegConflict]) -> list[ConflictChain]:
    chains: list[ConflictChain] = []
    for conflict in conflicts:
        if chains and chains[-1].conflicts[-1].following is conflict.previous:
            chains[-1].conflicts.append(conflict)
        else:
            chains.append(ConflictChain([conflict]))
    return chains
