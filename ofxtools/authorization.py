"""
Local authorization ledger - reconcile bank debit flows against
standing authorizations (mandates).

Each authorization fixes a currency, a per-transaction cap, a
per-period cumulative cap, and a validity window.  The ledger enforces
the following rules:

* Every debit flow must carry an explicit authorization reference.
  A flow without a reference is rejected; the ledger never picks
  another authorization that happens to have remaining capacity.
* The referenced authorization must be in force on the posting date,
  be denominated in the same currency, accept the single-flow amount
  under its per-transaction cap, and retain enough period capacity.
* Reversals release only the period and the amount originally
  consumed - even when the reversal posts in a later period or after
  the authorization has been amended.
* Authorization amendments take effect at their effective boundary;
  earlier consumption remains bound to the version in force when the
  original flow posted.
* A repeated flow identifier is recognized as a duplicate and never
  consumes capacity a second time.
"""

from __future__ import annotations

__all__ = [
    "PeriodGranularity",
    "FlowType",
    "FlowStatus",
    "FindingLevel",
    "Authorization",
    "Flow",
    "PeriodKey",
    "FlowResult",
    "Finding",
    "AuthorizationConsumption",
    "ReconciliationReport",
    "AuthorizationLedger",
    "LedgerError",
    "RegistrationError",
    "MissingAuthorizationError",
    "UnknownAuthorizationError",
    "AuthorizationNotInForceError",
    "CurrencyMismatchError",
    "PerTransactionLimitExceededError",
    "PeriodLimitExceededError",
    "DuplicateFlowError",
    "ConflictingDuplicateError",
    "ReversalError",
    "OrphanReversalError",
    "ReversalCurrencyMismatchError",
    "ReversalAuthorizationMismatchError",
    "ReversalAmountExceededError",
    "flow_from_stmttrn",
]


# stdlib imports
import datetime
import enum
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from ofxtools.models.bank.stmt import STMTTRN


class PeriodGranularity(enum.Enum):
    """Granularity of the period cumulative cap."""

    DAY = "DAY"
    WEEK = "WEEK"
    MONTH = "MONTH"
    YEAR = "YEAR"


class FlowType(enum.Enum):
    """Bank flow direction relative to the authorization."""

    DEBIT = "DEBIT"
    REVERSAL = "REVERSAL"


class FlowStatus(enum.Enum):
    """Lifecycle state of a processed flow."""

    POSTED = "POSTED"
    PARTIALLY_REVERSED = "PARTIALLY_REVERSED"
    REVERSED = "REVERSED"
    REVERSAL = "REVERSAL"
    DUPLICATE = "DUPLICATE"
    REJECTED = "REJECTED"


class FindingLevel(enum.Enum):
    """Severity of a reconciliation finding."""

    ERROR = "ERROR"
    WARNING = "WARNING"


@dataclass(frozen=True)
class Authorization:
    """
    A standing authorization mandate.

    ``valid_to`` is inclusive; ``None`` denotes an open-ended mandate.
    """

    auth_id: str
    currency: str
    per_transaction_limit: Decimal
    period_limit: Decimal
    valid_from: datetime.date
    period: PeriodGranularity = PeriodGranularity.MONTH
    valid_to: datetime.date | None = None

    def __post_init__(self) -> None:
        auth_id = self.auth_id.strip()
        if not auth_id:
            raise ValueError("Authorization auth_id may not be blank")
        currency = self.currency.strip().upper()
        if not currency:
            raise ValueError("Authorization currency may not be blank")
        per_transaction_limit = Decimal(self.per_transaction_limit)
        period_limit = Decimal(self.period_limit)
        if per_transaction_limit <= 0:
            raise ValueError("per_transaction_limit must be positive")
        if period_limit <= 0:
            raise ValueError("period_limit must be positive")
        if period_limit < per_transaction_limit:
            raise ValueError(
                "period_limit may not be smaller than per_transaction_limit"
            )
        if self.valid_to is not None and self.valid_to < self.valid_from:
            raise ValueError("valid_to may not precede valid_from")
        object.__setattr__(self, "auth_id", auth_id)
        object.__setattr__(self, "currency", currency)
        object.__setattr__(self, "per_transaction_limit", per_transaction_limit)
        object.__setattr__(self, "period_limit", period_limit)

    def in_force_on(self, day: datetime.date) -> bool:
        """Whether the authorization version covers ``day`` (inclusive ends)."""
        if day < self.valid_from:
            return False
        return self.valid_to is None or day <= self.valid_to

    def replaced_on(self, effective: datetime.date) -> Authorization:
        """Return a copy whose validity window closes the day before."""
        return Authorization(
            auth_id=self.auth_id,
            currency=self.currency,
            per_transaction_limit=self.per_transaction_limit,
            period_limit=self.period_limit,
            valid_from=self.valid_from,
            period=self.period,
            valid_to=effective - datetime.timedelta(days=1),
        )


@dataclass(frozen=True)
class Flow:
    """
    A single bank statement flow.

    ``auth_ref`` identifies the authorization explicitly referenced by
    the flow.  A blank reference means the flow cannot be matched -
    the ledger must not substitute another authorization.
    ``reversal_of`` links a reversal to the original flow identifier.
    """

    flow_id: str
    posted: datetime.datetime
    amount: Decimal
    currency: str
    auth_ref: str | None = None
    kind: FlowType = FlowType.DEBIT
    reversal_of: str | None = None

    def __post_init__(self) -> None:
        flow_id = self.flow_id.strip()
        if not flow_id:
            raise ValueError("Flow flow_id may not be blank")
        amount = Decimal(self.amount)
        if amount <= 0:
            raise ValueError("Flow amount must be positive")
        currency = self.currency.strip().upper()
        if not currency:
            raise ValueError("Flow currency may not be blank")
        auth_ref = self.auth_ref.strip() if self.auth_ref else None
        auth_ref = auth_ref or None
        reversal_of = self.reversal_of.strip() if self.reversal_of else None
        reversal_of = reversal_of or None
        if self.kind is FlowType.REVERSAL and not reversal_of:
            raise ValueError("Reversal flow must identify the original flow_id")
        if self.kind is FlowType.DEBIT and reversal_of:
            raise ValueError("Debit flow may not carry reversal_of")
        object.__setattr__(self, "flow_id", flow_id)
        object.__setattr__(self, "amount", amount)
        object.__setattr__(self, "currency", currency)
        object.__setattr__(self, "auth_ref", auth_ref)
        object.__setattr__(self, "reversal_of", reversal_of)


class PeriodKey(NamedTuple):
    """Identifier of the accumulation period an amount is booked into."""

    granularity: PeriodGranularity
    year: int
    month: int | None = None
    day: int | None = None
    week: int | None = None


def _period_key(granularity: PeriodGranularity, day: datetime.date) -> PeriodKey:
    if granularity is PeriodGranularity.DAY:
        return PeriodKey(granularity, day.year, day.month, day.day)
    if granularity is PeriodGranularity.WEEK:
        iso = day.isocalendar()
        return PeriodKey(granularity, iso.year, week=iso.week)
    if granularity is PeriodGranularity.MONTH:
        return PeriodKey(granularity, day.year, day.month)
    return PeriodKey(granularity, day.year)


@dataclass(frozen=True)
class FlowResult:
    """Outcome of processing one flow."""

    flow_id: str
    status: FlowStatus
    currency: str
    amount: Decimal
    auth_id: str | None
    version_seq: int | None
    period: PeriodKey | None
    period_limit: Decimal | None
    period_consumed: Decimal | None
    available: Decimal | None
    released: Decimal = Decimal(0)
    note: str | None = None


@dataclass(frozen=True)
class Finding:
    """An audit observation emitted during reconciliation."""

    level: FindingLevel
    code: str
    flow_id: str | None
    message: str


@dataclass(frozen=True)
class AuthorizationConsumption:
    """Capacity snapshot for one authorization version and period."""

    auth_id: str
    version_seq: int
    currency: str
    period: PeriodKey
    per_transaction_limit: Decimal
    period_limit: Decimal
    consumed: Decimal
    available: Decimal
    valid_from: datetime.date
    valid_to: datetime.date | None


@dataclass(frozen=True)
class ReconciliationReport:
    """Result of reconciling a batch of flows."""

    results: tuple[FlowResult, ...]
    findings: tuple[Finding, ...]
    consumption: tuple[AuthorizationConsumption, ...]

    @property
    def errors(self) -> tuple[Finding, ...]:
        return tuple(
            finding
            for finding in self.findings
            if finding.level is FindingLevel.ERROR
        )

    @property
    def warnings(self) -> tuple[Finding, ...]:
        return tuple(
            finding
            for finding in self.findings
            if finding.level is FindingLevel.WARNING
        )

    @property
    def is_clean(self) -> bool:
        return not self.errors

    def result_for(self, flow_id: str) -> FlowResult:
        for result in self.results:
            if result.flow_id == flow_id:
                return result
        raise KeyError(f"No result for flow {flow_id!r}")

    def consumption_for(
        self, auth_id: str, day: datetime.date
    ) -> AuthorizationConsumption | None:
        """Consumption of the version in force on ``day`` for its period."""
        for item in self.consumption:
            if item.auth_id != auth_id:
                continue
            if item.valid_from <= day and (
                item.valid_to is None or day <= item.valid_to
            ) and _period_key(item.period.granularity, day) == item.period:
                return item
        return None

    def available_for(self, auth_id: str, day: datetime.date) -> Decimal:
        item = self.consumption_for(auth_id, day)
        if item is None:
            raise KeyError(f"No active authorization {auth_id!r} on {day}")
        return item.available


class LedgerError(ValueError):
    """Base class for ledger processing errors."""

    code = "LEDGER_ERROR"
    level = FindingLevel.ERROR

    def __init__(self, message: str, flow: Flow | None = None) -> None:
        super().__init__(message)
        self.flow = flow


class RegistrationError(LedgerError):
    """An authorization could not be registered or amended."""

    code = "REGISTRATION_ERROR"


class MissingAuthorizationError(LedgerError):
    """A debit flow carries no authorization reference."""

    code = "MISSING_AUTH_REF"


class UnknownAuthorizationError(LedgerError):
    """The referenced authorization identifier is not on the ledger."""

    code = "UNKNOWN_AUTH"


class AuthorizationNotInForceError(LedgerError):
    """No version of the referenced authorization is in force on posting."""

    code = "AUTH_NOT_IN_FORCE"


class CurrencyMismatchError(LedgerError):
    """Flow currency differs from the authorization currency."""

    code = "CURRENCY_MISMATCH"


class PerTransactionLimitExceededError(LedgerError):
    """Single flow amount exceeds the per-transaction cap."""

    code = "TXN_LIMIT_EXCEEDED"


class PeriodLimitExceededError(LedgerError):
    """The flow would breach the period cumulative cap."""

    code = "PERIOD_LIMIT_EXCEEDED"


class DuplicateFlowError(LedgerError):
    """The flow identifier was already processed; no capacity is consumed."""

    code = "DUPLICATE_FLOW"
    level = FindingLevel.WARNING


class ConflictingDuplicateError(LedgerError):
    """A repeated flow identifier carries different content."""

    code = "DUPLICATE_CONFLICT"


class ReversalError(LedgerError):
    """Base class for reversal processing errors."""

    code = "REVERSAL_ERROR"


class OrphanReversalError(ReversalError):
    """The reversal references an unknown original flow."""

    code = "REVERSAL_NO_ORIGINAL"


class ReversalCurrencyMismatchError(ReversalError):
    """Reversal currency differs from the original flow."""

    code = "REVERSAL_CURRENCY_MISMATCH"


class ReversalAuthorizationMismatchError(ReversalError):
    """Reversal reference points at another authorization."""

    code = "REVERSAL_WRONG_AUTH"


class ReversalAmountExceededError(ReversalError):
    """Cumulative reversals exceed the original flow amount."""

    code = "REVERSAL_OVER_AMOUNT"


@dataclass
class _FlowRecord:
    flow: Flow
    kind: FlowType
    auth_id: str | None
    version_seq: int | None
    period: PeriodKey | None
    outstanding: Decimal
    original_amount: Decimal
    reversed_amount: Decimal
    status: FlowStatus


@dataclass(frozen=True)
class _ConsumptionKey:
    auth_id: str
    version_seq: int
    period: PeriodKey


class AuthorizationLedger:
    """
    Book debit flows against versioned authorization mandates.

    Authorizations form a version chain per ``auth_id``: registering a
    new version (or calling :meth:`amend_authorization`) fixes an
    effective boundary, and consumption always stays with the version
    that was in force when the flow posted.
    """

    def __init__(self, authorizations: Iterable[Authorization] = ()) -> None:
        self._versions: dict[str, list[Authorization]] = {}
        self._flows: dict[str, _FlowRecord] = {}
        self._consumption: dict[_ConsumptionKey, Decimal] = {}
        for authorization in authorizations:
            self.register_authorization(authorization)

    # ------------------------------------------------------------------
    # Authorization registration / amendment
    # ------------------------------------------------------------------
    def register_authorization(self, authorization: Authorization) -> None:
        """Register an authorization version, validating the version chain."""
        chain = self._versions.setdefault(authorization.auth_id, [])
        for version in chain:
            if version.valid_from == authorization.valid_from:
                raise RegistrationError(
                    f"Authorization {authorization.auth_id!r} already has a "
                    f"version effective on {authorization.valid_from}"
                )
        if chain:
            previous = max(chain, key=lambda version: version.valid_from)
            if authorization.valid_from <= previous.valid_from:
                raise RegistrationError(
                    f"New version of {authorization.auth_id!r} must postdate "
                    f"the latest version ({previous.valid_from})"
                )
            if previous.valid_to is None:
                raise RegistrationError(
                    f"Close authorization {authorization.auth_id!r} at "
                    f"{authorization.valid_from - datetime.timedelta(days=1)} "
                    "before registering the new version"
                )
            expected = previous.valid_to + datetime.timedelta(days=1)
            if authorization.valid_from != expected:
                raise RegistrationError(
                    f"Version gap/overlap for {authorization.auth_id!r}: new "
                    f"version starts {authorization.valid_from}, expected {expected}"
                )
        chain.append(authorization)
        chain.sort(key=lambda version: version.valid_from)

    def amend_authorization(
        self,
        auth_id: str,
        effective: datetime.date,
        *,
        currency: str | None = None,
        per_transaction_limit: Decimal | None = None,
        period_limit: Decimal | None = None,
        period: PeriodGranularity | None = None,
        valid_to: datetime.date | None = None,
    ) -> Authorization:
        """
        Apply an amendment at ``effective``.

        The previous version is closed at ``effective - 1`` and a new
        version carrying the changed terms starts at ``effective``.
        Flows posted before the boundary keep binding the old version.
        """
        chain = self._versions.get(auth_id)
        if not chain:
            raise RegistrationError(f"Unknown authorization {auth_id!r}")
        previous = chain[-1]
        if effective <= previous.valid_from:
            raise RegistrationError(
                f"Amendment for {auth_id!r} must postdate {previous.valid_from}"
            )
        if previous.valid_to is not None:
            expected = previous.valid_to + datetime.timedelta(days=1)
            if effective != expected:
                raise RegistrationError(
                    f"Amendment boundary for {auth_id!r} must be {expected}"
                )
        else:
            closed = previous.replaced_on(effective)
            self._versions[auth_id][-1] = closed
            previous = closed
        amended = Authorization(
            auth_id=auth_id,
            currency=currency if currency is not None else previous.currency,
            per_transaction_limit=(
                per_transaction_limit
                if per_transaction_limit is not None
                else previous.per_transaction_limit
            ),
            period_limit=(
                period_limit
                if period_limit is not None
                else previous.period_limit
            ),
            valid_from=effective,
            period=period if period is not None else previous.period,
            valid_to=valid_to,
        )
        self._versions[auth_id].append(amended)
        return amended

    def versions(self, auth_id: str) -> tuple[Authorization, ...]:
        """Return the ordered version chain of an authorization."""
        chain = self._versions.get(auth_id)
        if not chain:
            raise UnknownAuthorizationError(f"Unknown authorization {auth_id!r}")
        return tuple(chain)

    def _resolve(
        self, auth_ref: str, day: datetime.date
    ) -> tuple[Authorization, int]:
        chain = self._versions.get(auth_ref)
        if not chain:
            raise UnknownAuthorizationError(
                f"Unknown authorization reference {auth_ref!r}"
            )
        for seq, version in enumerate(chain):
            if version.in_force_on(day):
                return version, seq
        latest = chain[-1]
        if day < latest.valid_from:
            raise AuthorizationNotInForceError(
                f"Authorization {auth_ref!r} is not effective until "
                f"{latest.valid_from}"
            )
        raise AuthorizationNotInForceError(
            f"Authorization {auth_ref!r} expired on {latest.valid_to}"
        )

    # ------------------------------------------------------------------
    # Flow processing
    # ------------------------------------------------------------------
    def process(self, flow: Flow) -> FlowResult:
        """Process one flow strictly, raising on any rule violation."""
        return self._process(flow)

    def reconcile(self, flows: Iterable[Flow]) -> ReconciliationReport:
        """
        Process a batch, collecting violations as findings instead of
        raising.  The ledger state is updated in processing order, so
        duplicates, reversals and period accumulation remain consistent.
        """
        ordered = sorted(flows, key=lambda item: (item.posted, item.flow_id))
        results: list[FlowResult] = []
        findings: list[Finding] = []
        for flow in ordered:
            try:
                results.append(self._process(flow))
            except DuplicateFlowError as error:
                # A duplicate occupies no capacity; it is a warning, not a
                # rejection of the original (already posted) flow.
                findings.append(self._finding(error, flow.flow_id))
                results.append(
                    self._result_for_record(
                        self._flows[flow.flow_id], FlowStatus.DUPLICATE
                    )
                )
            except LedgerError as error:
                findings.append(self._finding(error, flow.flow_id))
                results.append(
                    FlowResult(
                        flow_id=flow.flow_id,
                        status=FlowStatus.REJECTED,
                        currency=flow.currency,
                        amount=flow.amount,
                        auth_id=flow.auth_ref,
                        version_seq=None,
                        period=None,
                        period_limit=None,
                        period_consumed=None,
                        available=None,
                        note=str(error),
                    )
                )
        return ReconciliationReport(
            results=tuple(results),
            findings=tuple(findings),
            consumption=self._consumption_snapshot(),
        )

    @staticmethod
    def _finding(error: LedgerError, flow_id: str) -> Finding:
        return Finding(
            level=error.level,
            code=error.code,
            flow_id=getattr(error.flow, "flow_id", None) or flow_id,
            message=str(error),
        )

    def _process(self, flow: Flow) -> FlowResult:
        existing = self._flows.get(flow.flow_id)
        if existing is not None:
            self._handle_duplicate(flow, existing)
        if flow.kind is FlowType.REVERSAL:
            return self._apply_reversal(flow)
        return self._apply_debit(flow)

    def _handle_duplicate(self, flow: Flow, existing: _FlowRecord) -> None:
        if flow != existing.flow:
            raise ConflictingDuplicateError(
                f"Flow {flow.flow_id!r} reappears with different content",
                flow=flow,
            )
        raise DuplicateFlowError(
            f"Duplicate flow {flow.flow_id!r}; no additional capacity consumed",
            flow=flow,
        )

    def _apply_debit(self, flow: Flow) -> FlowResult:
        if not flow.auth_ref:
            raise MissingAuthorizationError(
                f"Flow {flow.flow_id!r} has no authorization reference; "
                "no alternative authorization may be assigned",
                flow=flow,
            )
        day = flow.posted.date()
        authorization, seq = self._resolve(flow.auth_ref, day)
        if flow.currency != authorization.currency:
            raise CurrencyMismatchError(
                f"Flow {flow.flow_id!r} currency {flow.currency} does not "
                f"match authorization {authorization.auth_id!r} "
                f"currency {authorization.currency}",
                flow=flow,
            )
        if flow.amount > authorization.per_transaction_limit:
            raise PerTransactionLimitExceededError(
                f"Flow {flow.flow_id!r} amount {flow.amount} exceeds "
                f"per-transaction limit "
                f"{authorization.per_transaction_limit} of "
                f"{authorization.auth_id!r}",
                flow=flow,
            )
        period = _period_key(authorization.period, day)
        key = _ConsumptionKey(authorization.auth_id, seq, period)
        consumed = self._consumption.get(key, Decimal(0))
        if consumed + flow.amount > authorization.period_limit:
            raise PeriodLimitExceededError(
                f"Flow {flow.flow_id!r} would raise period consumption to "
                f"{consumed + flow.amount} over the period limit "
                f"{authorization.period_limit} of {authorization.auth_id!r}",
                flow=flow,
            )
        consumed += flow.amount
        self._consumption[key] = consumed
        record = _FlowRecord(
            flow=flow,
            kind=FlowType.DEBIT,
            auth_id=authorization.auth_id,
            version_seq=seq,
            period=period,
            outstanding=flow.amount,
            original_amount=flow.amount,
            reversed_amount=Decimal(0),
            status=FlowStatus.POSTED,
        )
        self._flows[flow.flow_id] = record
        return FlowResult(
            flow_id=flow.flow_id,
            status=FlowStatus.POSTED,
            currency=flow.currency,
            amount=flow.amount,
            auth_id=authorization.auth_id,
            version_seq=seq,
            period=period,
            period_limit=authorization.period_limit,
            period_consumed=consumed,
            available=authorization.period_limit - consumed,
        )

    def _apply_reversal(self, flow: Flow) -> FlowResult:
        assert flow.reversal_of is not None
        original_record = self._flows.get(flow.reversal_of)
        if original_record is None or original_record.kind is not FlowType.DEBIT:
            raise OrphanReversalError(
                f"Reversal {flow.flow_id!r} references unknown original "
                f"flow {flow.reversal_of!r}",
                flow=flow,
            )
        if flow.currency != original_record.flow.currency:
            raise ReversalCurrencyMismatchError(
                f"Reversal {flow.flow_id!r} currency {flow.currency} differs "
                f"from original flow currency "
                f"{original_record.flow.currency}",
                flow=flow,
            )
        if (
            flow.auth_ref is not None
            and flow.auth_ref != original_record.auth_id
        ):
            raise ReversalAuthorizationMismatchError(
                f"Reversal {flow.flow_id!r} references {flow.auth_ref!r} but "
                f"the original flow consumed {original_record.auth_id!r}",
                flow=flow,
            )
        already_reversed = original_record.reversed_amount
        releasable = original_record.original_amount - already_reversed
        if flow.amount > releasable:
            raise ReversalAmountExceededError(
                f"Reversal {flow.flow_id!r} amount {flow.amount} exceeds the "
                f"remaining reversible amount {releasable} of "
                f"{flow.reversal_of!r}",
                flow=flow,
            )
        assert original_record.period is not None
        assert original_record.version_seq is not None
        assert original_record.auth_id is not None
        key = _ConsumptionKey(
            original_record.auth_id,
            original_record.version_seq,
            original_record.period,
        )
        consumed = self._consumption.get(key, Decimal(0)) - flow.amount
        self._consumption[key] = consumed
        original_record.reversed_amount += flow.amount
        original_record.outstanding -= flow.amount
        if original_record.outstanding == 0:
            original_record.status = FlowStatus.REVERSED
        else:
            original_record.status = FlowStatus.PARTIALLY_REVERSED
        reversal_record = _FlowRecord(
            flow=flow,
            kind=FlowType.REVERSAL,
            auth_id=original_record.auth_id,
            version_seq=original_record.version_seq,
            period=original_record.period,
            outstanding=Decimal(0),
            original_amount=flow.amount,
            reversed_amount=flow.amount,
            status=FlowStatus.REVERSAL,
        )
        self._flows[flow.flow_id] = reversal_record
        authorization = self._versions[original_record.auth_id][
            original_record.version_seq
        ]
        return FlowResult(
            flow_id=flow.flow_id,
            status=FlowStatus.REVERSAL,
            currency=flow.currency,
            amount=flow.amount,
            auth_id=original_record.auth_id,
            version_seq=original_record.version_seq,
            period=original_record.period,
            period_limit=authorization.period_limit,
            period_consumed=consumed,
            available=authorization.period_limit - consumed,
            released=flow.amount,
        )

    def _result_for_record(
        self, record: _FlowRecord, status: FlowStatus
    ) -> FlowResult:
        if record.auth_id is None or record.version_seq is None:
            return FlowResult(
                flow_id=record.flow.flow_id,
                status=status,
                currency=record.flow.currency,
                amount=record.flow.amount,
                auth_id=None,
                version_seq=None,
                period=None,
                period_limit=None,
                period_consumed=None,
                available=None,
            )
        assert record.period is not None
        key = _ConsumptionKey(record.auth_id, record.version_seq, record.period)
        authorization = self._versions[record.auth_id][record.version_seq]
        consumed = self._consumption.get(key, Decimal(0))
        return FlowResult(
            flow_id=record.flow.flow_id,
            status=status,
            currency=record.flow.currency,
            amount=record.flow.amount,
            auth_id=record.auth_id,
            version_seq=record.version_seq,
            period=record.period,
            period_limit=authorization.period_limit,
            period_consumed=consumed,
            available=authorization.period_limit - consumed,
        )

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------
    def consumed(self, auth_id: str, day: datetime.date) -> Decimal:
        """Net consumption of the version in force on ``day``."""
        authorization, seq = self._resolve(auth_id, day)
        period = _period_key(authorization.period, day)
        key = _ConsumptionKey(auth_id, seq, period)
        return self._consumption.get(key, Decimal(0))

    def available(self, auth_id: str, day: datetime.date) -> Decimal:
        """Remaining period capacity of the version in force on ``day``."""
        authorization, _ = self._resolve(auth_id, day)
        return authorization.period_limit - self.consumed(auth_id, day)

    def _consumption_snapshot(self) -> tuple[AuthorizationConsumption, ...]:
        snapshot: list[AuthorizationConsumption] = []
        for key, consumed in sorted(
            self._consumption.items(),
            key=lambda item: (
                item[0].auth_id,
                item[0].version_seq,
                item[0].period,
            ),
        ):
            authorization = self._versions[key.auth_id][key.version_seq]
            snapshot.append(
                AuthorizationConsumption(
                    auth_id=key.auth_id,
                    version_seq=key.version_seq,
                    currency=authorization.currency,
                    period=key.period,
                    per_transaction_limit=authorization.per_transaction_limit,
                    period_limit=authorization.period_limit,
                    consumed=consumed,
                    available=authorization.period_limit - consumed,
                    valid_from=authorization.valid_from,
                    valid_to=authorization.valid_to,
                )
            )
        return tuple(snapshot)

    def __iter__(self) -> Iterator[str]:
        return iter(self._versions)


def flow_from_stmttrn(
    stmttrn: STMTTRN,
    *,
    auth_ref: str | None = None,
    reversal_of: str | None = None,
    fallback_currency: str | None = None,
) -> Flow:
    """
    Adapt an OFX ``STMTTRN`` debit transaction into a ledger :class:`Flow`.

    The transaction ``REFNUM`` is used as the authorization reference
    when no explicit ``auth_ref`` is given.  ``trnamt`` must be negative
    (a debit); its absolute value becomes the flow amount.  Reversals
    are not inferred from OFX correction tags - pass ``reversal_of``
    explicitly when the statement flow reverses an earlier flow.
    """
    if stmttrn.trnamt is None or stmttrn.trnamt >= 0:
        raise ValueError(
            f"STMTTRN {stmttrn.fitid!r} is not a debit (trnamt={stmttrn.trnamt})"
        )
    if stmttrn.currency is not None:
        currency = stmttrn.currency.cursym
    elif stmttrn.origcurrency is not None:
        currency = stmttrn.origcurrency.cursym
    elif fallback_currency is not None:
        currency = fallback_currency
    else:
        raise ValueError(
            f"STMTTRN {stmttrn.fitid!r} has no currency; pass fallback_currency"
        )
    reference = auth_ref if auth_ref is not None else stmttrn.refnum
    kind = FlowType.REVERSAL if reversal_of else FlowType.DEBIT
    return Flow(
        flow_id=stmttrn.fitid,
        posted=stmttrn.dtposted,
        amount=abs(stmttrn.trnamt),
        currency=currency,
        auth_ref=reference,
        kind=kind,
        reversal_of=reversal_of,
    )
