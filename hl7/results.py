"""Versioned history chains for laboratory results.

A result may arrive more than once with different statuses: a forecast
(``PRELIMINARY``), the official result (``FINAL``), one or several
corrections (``CORRECTION``) and ultimately a withdrawal (``CANCELLED``).
This module keeps every received version so the full history stays
queryable, links the versions exactly as the source declares them and
resolves the currently effective result(s).

Observation identifiers are only unique within an order, so every chain is
keyed by the ``(order_id, observation_id)`` pair.

Design rules:

* Re-delivering the exact same version never creates a new entry; the
  existing one is returned.
* A correction points at the version it replaces. Two corrections that
  cannot be ordered relative to each other (neither declares the other as
  its predecessor) are both retained and surface as a conflict.
* An official report naturally succeeds the forecast for the same
  observation; when the source gives no explicit predecessor the registry
  links it to the single outstanding preliminary root.
* A withdrawal terminates its branch: a head that is itself a
  ``CANCELLED`` version has no effective value. Re-delivering the old
  original afterwards is swallowed by duplicate handling and cannot
  resurrect the branch; only an explicit new report can do that.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from .exceptions import HL7Exception


class ResultStatus(Enum):
    """Lifecycle status of a laboratory result version."""

    #: Forecast / preliminary report that may still change.
    PRELIMINARY = "PRELIMINARY"
    #: Official, confirmed result.
    FINAL = "FINAL"
    #: Corrects a previously reported version.
    CORRECTION = "CORRECTION"
    #: Withdraws the result; the chain has no effective value afterwards.
    CANCELLED = "CANCELLED"


class ResultError(HL7Exception):
    """Base class for errors raised while building a result chain."""


class DuplicateResultError(ResultError):
    """Raised when a version id is reused with different content."""


class UnknownTargetError(ResultError):
    """Raised when a version references a predecessor that does not exist."""


class InvalidResultError(ResultError):
    """Raised when a result version is missing mandatory information."""


@dataclass(frozen=True)
class ResultVersion:
    """One immutable version of a laboratory result.

    ``version_id`` is the source-assigned identifier of this delivery.
    ``declared_replaces_id`` is the predecessor the source named (``None``
    when it named none), while ``replaces_id`` is the predecessor used for
    the version graph: they differ only when an official report implicitly
    succeeds the single outstanding forecast. ``received_at`` records when
    the delivery was accepted and is not part of version identity, so it
    does not affect duplicate detection.
    """

    version_id: str
    status: ResultStatus
    value: Any
    order_id: str
    observation_id: str
    replaces_id: Optional[str] = None
    received_at: Any = None
    payload: Any = None
    declared_replaces_id: Optional[str] = None


@dataclass(frozen=True)
class ResultChain:
    """The complete version history of one ``(order_id, observation_id)``."""

    order_id: str
    observation_id: str
    versions: Tuple[ResultVersion, ...]
    #: Graph tips that no other version replaced; several means a conflict.
    heads: Tuple[str, ...]
    #: Withdrawal tips; a branch ending here has no effective value.
    cancelled_heads: Tuple[str, ...]

    def history(self) -> Tuple[ResultVersion, ...]:
        """All received versions, in delivery order."""
        return self.versions

    def get(self, version_id: str) -> ResultVersion:
        """Return the version with ``version_id`` or raise ``KeyError``."""
        for version in self.versions:
            if version.version_id == version_id:
                return version
        raise KeyError(version_id)

    def contains(self, version_id: str) -> bool:
        """Whether ``version_id`` is part of this chain."""
        return any(version.version_id == version_id for version in self.versions)

    @property
    def is_cancelled(self) -> bool:
        """Whether every current branch of the chain has been withdrawn."""
        return bool(self.heads) and len(self.heads) == len(self.cancelled_heads)

    @property
    def cancelled_by(self) -> Optional[str]:
        """Identifier of the withdrawal when the whole chain is withdrawn."""
        return self.cancelled_heads[0] if self.is_cancelled else None

    @property
    def has_conflict(self) -> bool:
        """Whether two or more unordered versions compete for the head."""
        return len(self.effective_versions()) > 1

    def lineage(self, version_id: str) -> Tuple[ResultVersion, ...]:
        """Return ``version_id`` and all of its ancestors, nearest first."""
        by_id = {version.version_id: version for version in self.versions}
        if version_id not in by_id:
            raise KeyError(version_id)
        lineage = []
        seen = set()
        current_id: Optional[str] = version_id
        while current_id is not None:
            if current_id in seen:
                raise ResultError(
                    f"cyclic version relationship at {current_id!r} in chain "
                    f"({self.order_id!r}, {self.observation_id!r})"
                )
            seen.add(current_id)
            current = by_id[current_id]
            lineage.append(current)
            current_id = current.replaces_id
        return tuple(lineage)

    def effective_versions(self) -> Tuple[ResultVersion, ...]:
        """Versions that currently compete for the effective value.

        Returns an empty tuple when every branch has been withdrawn, a
        single element in the normal case, or several elements when
        unordered corrections create a conflict that the source never
        resolved. Withdrawn branches are always excluded.
        """
        active_heads = [head for head in self.heads if head not in self.cancelled_heads]
        return tuple(self.get(head) for head in active_heads)

    @property
    def effective(self) -> Optional[ResultVersion]:
        """The single currently effective version, or ``None``.

        ``None`` covers both the withdrawn case and an unresolved conflict,
        which is inspected through :meth:`effective_versions` or
        :attr:`has_conflict`.
        """
        candidates = self.effective_versions()
        if len(candidates) == 1:
            return candidates[0]
        return None


class ResultRegistry:
    """Collects result deliveries and keeps the history per observation."""

    def __init__(self) -> None:
        self._chains: Dict[Tuple[str, str], List[ResultVersion]] = {}
        self._index: Dict[Tuple[str, str], Dict[str, int]] = {}

    def add(
        self,
        version_id: str,
        status: ResultStatus,
        value: Any,
        order_id: str,
        observation_id: str,
        replaces_id: Optional[str] = None,
        received_at: Any = None,
        payload: Any = None,
    ) -> ResultVersion:
        """Record one delivered result version.

        Re-delivering an already known ``version_id`` returns the stored
        version unchanged; duplicate deliveries therefore never grow the
        history. This also means replaying an old original report after a
        withdrawal cannot resurrect anything. A reused id with different
        content raises :class:`DuplicateResultError`.

        A ``CORRECTION`` or ``CANCELLED`` version must name its predecessor
        through ``replaces_id`` and that predecessor must already be known;
        otherwise :class:`InvalidResultError` or :class:`UnknownTargetError`
        is raised. A new explicit report after a withdrawal (a fresh
        ``version_id``) starts a new branch and can make the observation
        effective again.
        """
        if not version_id:
            raise InvalidResultError("version_id is required")
        if not order_id or not observation_id:
            raise InvalidResultError("order_id and observation_id are required")
        if not isinstance(status, ResultStatus):
            raise InvalidResultError(f"unsupported status {status!r}")

        key = (order_id, observation_id)
        versions = self._chains.setdefault(key, [])
        index = self._index.setdefault(key, {})

        if version_id in index:
            existing = versions[index[version_id]]
            if self._same(existing, status, value, replaces_id, payload):
                return existing
            raise DuplicateResultError(
                f"version_id {version_id!r} reused with different content in "
                f"chain ({order_id!r}, {observation_id!r})"
            )

        if status in (ResultStatus.CORRECTION, ResultStatus.CANCELLED):
            if not replaces_id:
                raise InvalidResultError(
                    f"{status.name} version {version_id!r} must reference the "
                    "version it replaces"
                )
        if replaces_id is not None and replaces_id not in index:
            raise UnknownTargetError(
                f"version {version_id!r} references unknown predecessor "
                f"{replaces_id!r}"
            )

        effective_replaces = replaces_id
        if status == ResultStatus.FINAL and replaces_id is None:
            effective_replaces = self._implicit_predecessor(versions)

        version = ResultVersion(
            version_id=version_id,
            status=status,
            value=value,
            order_id=order_id,
            observation_id=observation_id,
            replaces_id=effective_replaces,
            received_at=received_at,
            payload=payload,
            declared_replaces_id=replaces_id,
        )
        index[version_id] = len(versions)
        versions.append(version)
        return version

    def chain(self, order_id: str, observation_id: str) -> ResultChain:
        """Build the immutable history chain for one observation."""
        key = (order_id, observation_id)
        versions = tuple(self._chains.get(key, ()))
        replaced = {
            version.replaces_id
            for version in versions
            if version.replaces_id is not None
        }
        heads = tuple(
            version.version_id
            for version in versions
            if version.version_id not in replaced
        )
        cancelled_heads = tuple(
            head
            for head in heads
            if self._get(versions, head).status == ResultStatus.CANCELLED
        )
        return ResultChain(
            order_id=order_id,
            observation_id=observation_id,
            versions=versions,
            heads=heads,
            cancelled_heads=cancelled_heads,
        )

    def chains(self) -> List[ResultChain]:
        """All known chains, ordered by their first delivery."""
        return [
            self.chain(order_id, observation_id)
            for order_id, observation_id in self._chains
        ]

    def history(
        self, order_id: str, observation_id: str
    ) -> Tuple[ResultVersion, ...]:
        """Convenience accessor for the full version history of one observation."""
        return self.chain(order_id, observation_id).history()

    def effective(
        self, order_id: str, observation_id: str
    ) -> Optional[ResultVersion]:
        """Convenience accessor for the current effective version."""
        return self.chain(order_id, observation_id).effective

    @staticmethod
    def _same(
        existing: ResultVersion,
        status: ResultStatus,
        value: Any,
        replaces_id: Optional[str],
        payload: Any,
    ) -> bool:
        return (
            existing.status == status
            and existing.value == value
            and existing.declared_replaces_id == replaces_id
            and existing.payload == payload
        )

    @staticmethod
    def _implicit_predecessor(
        versions: List[ResultVersion],
    ) -> Optional[str]:
        """Find the single live preliminary root a final report succeeds.

        Returns ``None`` when there is no preliminary root (the final starts
        a new branch) or when several unordered preliminary roots compete
        (the source must give an explicit predecessor rather than guessing).
        """
        replaced = {
            version.replaces_id
            for version in versions
            if version.replaces_id is not None
        }
        preliminary_roots = [
            version.version_id
            for version in versions
            if version.replaces_id is None
            and version.status == ResultStatus.PRELIMINARY
            and version.version_id not in replaced
        ]
        if len(preliminary_roots) == 1:
            return preliminary_roots[0]
        return None

    @staticmethod
    def _get(
        versions: List[ResultVersion], version_id: str
    ) -> ResultVersion:
        for version in versions:
            if version.version_id == version_id:
                return version
        raise KeyError(version_id)
