"""
Per-response diagnostics for OFX statement downloads.

An OFX response may contain several transaction responses, each wrapped in a
``*TRNRS`` aggregate with its own ``STATUS``.  A server can return a failed
status while still including a partially populated statement aggregate.  Such
a block must not be presented as an ordinary, complete statement period.

``StatementResponse`` keeps the raw response, its status, the source location
and a trust scope together.  ``response_scope()`` implements the classification
used by every ``*MSGSRSV1.statements`` property:

``complete``
    The transaction succeeded and a statement is present.  A statement with
    zero transactions is still complete: the zero is an explicit fact from a
    successful response.

``partial``
    The transaction failed (or was degraded) but still carried usable raw data
    (an account, transactions and/or balances).  The data is exposed separately
    and never counts as a formal statement period.

``none``
    The transaction failed and carried no usable account/statement data.
"""

from __future__ import annotations

__all__ = [
    "TRUST_COMPLETE",
    "TRUST_PARTIAL",
    "TRUST_NONE",
    "RawAggregate",
    "RawStatement",
    "ResponseStatus",
    "StatementResponse",
    "format_report",
    "response_scope",
    "responses_of",
    "statement_of",
    "statement_report",
]


import inspect
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from ofxtools.location import SourceLocation, get_source_location
from ofxtools.models.base import Aggregate
from ofxtools.models.wrapperbases import TrnRs

TRUST_COMPLETE = "complete"
TRUST_PARTIAL = "partial"
TRUST_NONE = "none"

# Transaction-response wrapper -> attribute holding its response aggregate.
_STATEMENT_ATTRS = {
    "STMTTRNRS": "stmtrs",
    "CCSTMTTRNRS": "ccstmtrs",
    "INVSTMTTRNRS": "invstmtrs",
    "STMTENDTRNRS": "stmtendrs",
    "CCSTMTENDTRNRS": "ccstmtendrs",
}


def _jsonable(value: Any) -> Any:
    """Convert OFX model values into plain JSON-compatible values."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, SourceLocation):
        return value.to_dict()
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except (TypeError, ValueError):
            pass
    return str(value)


@dataclass(frozen=True)
class ResponseStatus:
    """Snapshot of an OFX ``STATUS`` aggregate."""

    code: int | None = None
    severity: str | None = None
    message: str | None = None

    @classmethod
    def from_status(cls, status: Any) -> ResponseStatus:
        if status is None:
            return cls()
        return cls(
            code=getattr(status, "code", None),
            severity=getattr(status, "severity", None),
            message=getattr(status, "message", None),
        )

    @property
    def ok(self) -> bool:
        """Whether the response is successful enough to be used as a fact."""
        return self.code == 0 and self.severity != "ERROR"

    @property
    def success(self) -> bool:
        return self.ok

    @property
    def failed(self) -> bool:
        return not self.ok

    @property
    def error(self) -> bool:
        return self.failed

    @property
    def warn(self) -> bool:
        return self.severity == "WARN"

    def __str__(self) -> str:
        parts = []
        if self.code is not None:
            parts.append(f"code={self.code}")
        if self.severity is not None:
            parts.append(f"severity={self.severity}")
        if self.message:
            parts.append(f"message={self.message!r}")
        return ", ".join(parts) or "STATUS not provided"

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "ok": self.ok,
            "failed": self.failed,
        }


def _attr(obj: Any, *names: str) -> Any:
    """Look up a real attribute/property without triggering OFX proxies.

    ``Aggregate.__getattr__`` proxies unknown names down the child hierarchy,
    which is useful for user code but unsafe for metadata lookup (it can
    recurse).  Only values actually stored on the instance, or properties
    declared by its class, are considered here.
    """
    if obj is None:
        return None
    for name in names:
        try:
            static = inspect.getattr_static(obj, name)
        except AttributeError:
            continue
        if isinstance(static, property):
            try:
                value = static.fget(obj)  # type: ignore[misc]
            except (AttributeError, TypeError, ValueError):
                continue
        else:
            value = obj.__dict__.get(name)
        if value is not None:
            return value
    return None


def _account_of(statement: Any) -> Any:
    if statement is None:
        return None
    return _attr(
        statement,
        "account",
        "bankacctfrom",
        "ccacctfrom",
        "invacctfrom",
    )


def _account_dict(account: Any) -> dict[str, Any] | None:
    if account is None:
        return None
    return {
        "type": getattr(account, "tag", None) or account.__class__.__name__,
        "acctid": _attr(account, "acctid"),
        "bankid": _attr(account, "bankid"),
        "branchid": _attr(account, "branchid"),
        "accttype": _attr(account, "accttype"),
        "brokerid": _attr(account, "brokerid"),
        "acctkey": _attr(account, "acctkey"),
    }


def _transactions_of(statement: Any) -> Any:
    if statement is None:
        return None
    return _attr(statement, "transactions")


def _period_of(statement: Any) -> dict[str, Any] | None:
    transactions = _transactions_of(statement)
    if transactions is None:
        return None
    dtstart = _attr(transactions, "dtstart")
    dtend = _attr(transactions, "dtend")
    if dtstart is None or dtend is None:
        return None
    return {"dtstart": dtstart, "dtend": dtend}


def _balances_of(statement: Any) -> Any:
    if statement is None:
        return None
    return _attr(statement, "balance", "balances")


def _transaction_dict(transaction: Any) -> dict[str, Any]:
    """Plain-record view of a transaction, whether model or raw stand-in."""
    record: dict[str, Any] = {
        "type": getattr(transaction, "tag", None) or transaction.__class__.__name__,
    }
    for attr in ("trntype", "dtposted", "dtuser", "dtavail", "trnamt", "fitid"):
        record[attr] = _jsonable(_attr(transaction, attr))

    nested = _attr(transaction, "invtran")
    if nested is not None:
        record["invtran"] = {
            "fitid": _jsonable(_attr(nested, "fitid")),
            "dttrade": _jsonable(_attr(nested, "dttrade")),
            "dtsettle": _jsonable(_attr(nested, "dtsettle")),
            "memo": _jsonable(_attr(nested, "memo")),
        }

    for attr in ("name", "memo", "checknum", "refnum"):
        value = _attr(transaction, attr)
        if value is not None:
            record[attr] = _jsonable(value)
    return record


def response_scope(
    status: ResponseStatus,
    statement: Any,
    conversion_error: str | None = None,
) -> str:
    """Classify the usable scope of a single statement response.

    Only a successful response with a statement is ``complete``.  A failed
    response carrying some data is never allowed to masquerade as a complete
    statement period; it is ``partial``.
    """
    if conversion_error is not None:
        return TRUST_PARTIAL if statement is not None else TRUST_NONE
    if status.ok and statement is not None:
        return TRUST_COMPLETE
    if statement is not None:
        if _account_of(statement) is not None:
            return TRUST_PARTIAL
        transactions = _transactions_of(statement)
        if transactions is not None and len(transactions) > 0:
            return TRUST_PARTIAL
        if _balances_of(statement) is not None:
            return TRUST_PARTIAL
    return TRUST_NONE


class RawTransactionList(list[Any]):
    """Raw transaction list recovered without model validation."""

    def __init__(
        self,
        *args: Any,
        dtstart: Any = None,
        dtend: Any = None,
    ) -> None:
        super().__init__(*args)
        self.dtstart = dtstart
        self.dtend = dtend


class RawAggregate:
    """Tagged, attribute-only stand-in for an aggregate that failed validation."""

    def __init__(self, tag: str = "", **kwargs: Any) -> None:
        self.tag = tag or self.__class__.__name__.upper()
        for key, value in kwargs.items():
            setattr(self, key, value)

    def __repr__(self) -> str:
        attrs = ", ".join(
            f"{key}={value!r}"
            for key, value in self.__dict__.items()
            if value is not None
        )
        return f"<{self.tag}({attrs})>"


class RawStatement(RawAggregate):
    """Best-effort statement view recovered from malformed partial markup."""

    def __init__(
        self,
        account: Any = None,
        transactions: RawTransactionList | None = None,
        balance: Any = None,
        location: SourceLocation | None = None,
    ) -> None:
        super().__init__(
            account=account,
            banktranlist=transactions,
            ledgerbal=balance,
            source_location=location,
        )

    @property
    def transactions(self) -> RawTransactionList | None:
        return self.banktranlist

    @property
    def balance(self) -> Any:
        return self.ledgerbal


def _iter_named(elem: Any, tag: str) -> Iterator[Any]:
    """Depth-first search for elements named *tag* below *elem*."""
    for child in elem:
        if child.tag == tag:
            yield child
        yield from _iter_named(child, tag)


def _find_named(elem: Any, tag: str) -> Any:
    return next(_iter_named(elem, tag), None)


def _text(elem: Any, tag: str) -> str | None:
    child = _find_named(elem, tag)
    if child is None or child.text is None:
        return None
    text = child.text.strip()
    return text or None


def _raw_values(elem: Any, attrs: tuple[str, ...]) -> dict[str, str | None]:
    return {attr: _text(elem, attr.upper()) for attr in attrs}


def _raw_account(statement_elem: Any) -> RawAggregate | None:
    for tag in ("BANKACCTFROM", "CCACCTFROM", "INVACCTFROM"):
        elem = _find_named(statement_elem, tag)
        if elem is not None:
            return RawAggregate(
                tag=tag,
                **_raw_values(
                    elem,
                    ("acctid", "bankid", "branchid", "accttype", "brokerid", "acctkey"),
                ),
            )
    return None


def _raw_transactions(statement_elem: Any) -> RawTransactionList | None:
    elem = _find_named(statement_elem, "BANKTRANLIST")
    if elem is None:
        elem = _find_named(statement_elem, "INVTRANLIST")
    if elem is None:
        return None
    transactions = RawTransactionList(
        dtstart=_text(elem, "DTSTART"),
        dtend=_text(elem, "DTEND"),
    )
    for child in elem:
        if child.tag not in ("STMTTRN", "INVBANKTRAN"):
            continue
        transactions.append(
            RawAggregate(
                tag=child.tag,
                **_raw_values(
                    child,
                    ("trntype", "dtposted", "dtuser", "trnamt", "fitid", "name", "memo"),
                ),
            )
        )
    return transactions


def _raw_balance(statement_elem: Any) -> RawAggregate | None:
    for tag in ("LEDGERBAL", "AVAILBAL", "INVBAL"):
        elem = _find_named(statement_elem, tag)
        if elem is not None:
            return RawAggregate(
                tag=tag, **_raw_values(elem, ("balamt", "dtasof"))
            )
    return None


def _raw_statement(elem: Any) -> RawStatement:
    return RawStatement(
        account=_raw_account(elem),
        transactions=_raw_transactions(elem),
        balance=_raw_balance(elem),
        location=get_source_location(elem),
    )


def statement_of(wrapper: Any) -> Any:
    """Return the statement aggregate wrapped by a ``*TRNRS``, if any.

    If the wrapper could not be converted in full during lenient document
    conversion, the statement aggregate is recovered from the retained raw
    subtree.  That is precisely the "failed status with partial data" case.
    """
    if wrapper is None:
        return None
    attr = _STATEMENT_ATTRS.get(wrapper.__class__.__name__)
    if attr is None:
        return None
    statement = wrapper.__dict__.get(attr)
    if statement is not None:
        return statement

    raw = wrapper.__dict__.get("raw_partial")
    if raw is None:
        return None
    elem = raw.find(attr.upper())
    if elem is None:
        return None
    try:
        return Aggregate.from_etree(elem)
    except (ValueError, TypeError, KeyError, AttributeError):
        return _raw_statement(elem)


def responses_of(container: Iterable[Any]) -> Iterator[StatementResponse]:
    """Yield a ``StatementResponse`` for each account statement response.

    Non-statement transaction wrappers (transfers, mail, stop checks, ...) are
    ignored; they have no statement to qualify.
    """
    for child in container:
        if isinstance(child, TrnRs) and child.__class__.__name__ in _STATEMENT_ATTRS:
            yield StatementResponse.from_wrapper(child)


@dataclass
class StatementResponse:
    """A single account/statement response with status and trust metadata."""

    wrapper: Any
    status: ResponseStatus
    statement: Any | None
    kind: str
    trnuid_value: Any = None
    location: SourceLocation | None = None
    statement_location: SourceLocation | None = None
    conversion_error: str | None = None
    raw_partial: Any = None
    _issues: list[str] = field(default_factory=list)

    @classmethod
    def from_wrapper(
        cls,
        wrapper: TrnRs,
        conversion_error: str | None = None,
        raw_partial: Any = None,
        location: SourceLocation | None = None,
    ) -> StatementResponse:
        status = ResponseStatus.from_status(_attr(wrapper, "status"))
        statement = statement_of(wrapper)
        if conversion_error is None:
            conversion_error = wrapper.__dict__.get("conversion_error")
        if raw_partial is None:
            raw_partial = wrapper.__dict__.get("raw_partial")
        if location is None:
            location = wrapper.__dict__.get("source_location")
        statement_location = (
            statement.__dict__.get("source_location") if statement is not None else None
        )
        response = cls(
            wrapper=wrapper,
            status=status,
            statement=statement,
            kind=wrapper.__class__.__name__,
            trnuid_value=_attr(wrapper, "trnuid"),
            location=location,
            statement_location=statement_location,
            conversion_error=conversion_error,
            raw_partial=raw_partial,
        )
        response._annotate()
        return response

    def _annotate(self) -> None:
        """Staple response facts onto the statement for convenient access."""
        statement = self.statement
        if statement is None:
            return
        for name, value in (
            ("response_status", self.status),
            ("trusted_scope", self.trusted_scope),
            ("trust_scope", self.trusted_scope),
            ("formal", self.formal),
            ("usable", self.usable),
            ("partial", self.partial),
            ("complete_period", self.complete_period),
            ("response", self),
        ):
            # Write through ``__dict__``: ``response_status`` is a read-only
            # property on statement models, and descriptors must not block
            # the metadata we're attaching.
            statement.__dict__[name] = value

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------
    @property
    def trnuid(self) -> Any:
        return self.trnuid_value

    @property
    def ok(self) -> bool:
        return self.status.ok and self.conversion_error is None

    @property
    def success(self) -> bool:
        return self.ok

    @property
    def failed(self) -> bool:
        return not self.ok

    @property
    def is_failed(self) -> bool:
        return self.failed

    @property
    def error(self) -> bool:
        return self.failed

    @property
    def is_statement(self) -> bool:
        return self.kind in _STATEMENT_ATTRS

    @property
    def account_response(self) -> bool:
        return self.is_statement

    @property
    def summary(self) -> str:
        scope = self.trusted_scope
        account = self.account_info
        acctid = account.get("acctid") if account else None
        where = str(self.location) if self.location is not None else "<unknown location>"
        return (
            f"[{scope}] {self.kind} trnuid={self.trnuid!r} "
            f"acctid={acctid!r} status=({self.status}) at {where}"
        )

    def __str__(self) -> str:
        return self.summary

    # ------------------------------------------------------------------
    # Trust / scope
    # ------------------------------------------------------------------
    @property
    def trusted_scope(self) -> str:
        return response_scope(self.status, self.statement, self.conversion_error)

    @property
    def trust_scope(self) -> str:
        return self.trusted_scope

    @property
    def trust(self) -> str:
        return self.trusted_scope

    @property
    def trusted_range(self) -> str:
        return self.trusted_scope

    @property
    def scope(self) -> str:
        return self.trusted_scope

    @property
    def trusted(self) -> bool:
        return self.trusted_scope != TRUST_NONE

    @property
    def partial(self) -> bool:
        return self.trusted_scope == TRUST_PARTIAL

    @property
    def is_partial(self) -> bool:
        return self.partial

    @property
    def formal(self) -> bool:
        """Whether this response is a formal, usable statement."""
        return self.trusted_scope == TRUST_COMPLETE

    @property
    def is_formal(self) -> bool:
        return self.formal

    @property
    def usable(self) -> bool:
        return self.formal

    @property
    def is_usable(self) -> bool:
        return self.usable

    @property
    def trusted_fields(self) -> list[str]:
        """Which parts of this response may be relied upon.

        A complete response trusts its account, transactions, period and
        balances.  A partial response only trusts the data actually present;
        completeness of the period is never implied.
        """
        if self.statement is None:
            return []
        fields = []
        if self.account_info is not None:
            fields.append("account")
        if self.transactions is not None:
            fields.append("transactions")
        if self.period is not None:
            fields.append("period")
        if self.balances:
            fields.append("balances")
        return fields

    @property
    def untrusted_fields(self) -> list[str]:
        """Parts explicitly not safe to treat as fact."""
        if self.trusted_scope == TRUST_COMPLETE:
            return []
        untrusted = ["completeness"]
        if self.period is not None:
            untrusted.append("period_coverage")
        if self.transactions is not None:
            untrusted.append("transaction_completeness")
        if self.status.failed:
            untrusted.append("status")
        return untrusted

    @property
    def complete_period(self) -> bool:
        """True only for a successful response with a dated transaction list.

        A failed block that happens to contain an empty or partial transaction
        list is deliberately not a complete period.
        """
        return self.formal and _period_of(self.statement) is not None

    @property
    def complete(self) -> bool:
        return self.formal

    # ------------------------------------------------------------------
    # Account / data
    # ------------------------------------------------------------------
    @property
    def account(self) -> Any:
        return _account_of(self.statement)

    @property
    def account_info(self) -> dict[str, Any] | None:
        return _account_dict(self.account)

    @property
    def transactions(self) -> Any:
        return _transactions_of(self.statement)

    @property
    def formal_transactions(self) -> Any:
        if not self.formal:
            return None
        return self.transactions

    @property
    def partial_transactions(self) -> Any:
        if self.formal:
            return None
        return self.transactions

    @property
    def transaction_count(self) -> int:
        transactions = self.transactions
        return len(transactions) if transactions is not None else 0

    @property
    def period(self) -> dict[str, Any] | None:
        return _period_of(self.statement)

    @property
    def balances(self) -> dict[str, Any] | None:
        """Balance aggregate as a JSON-ready record, if present."""
        balance = _balances_of(self.statement)
        if balance is None:
            return None
        record: dict[str, Any] = {
            "type": getattr(balance, "tag", None) or balance.__class__.__name__,
        }
        for attr in ("balamt", "dtasof"):
            value = _attr(balance, attr)
            if value is not None:
                record[attr] = _jsonable(value)
        if len(record) == 1:
            # Investment ``INVBAL`` exposes several named amounts.
            for attr in (
                "availcash",
                "marginbalance",
                "shortbalance",
                "buypower",
            ):
                value = _attr(balance, attr)
                if value is not None:
                    record[attr] = _jsonable(value)
        return record

    @property
    def balance(self) -> dict[str, Any] | None:
        return self.balances

    @property
    def transaction_records(self) -> list[dict[str, Any]] | None:
        """Transactions rendered as JSON-ready records."""
        transactions = self.transactions
        if transactions is None:
            return None
        return [_transaction_dict(transaction) for transaction in transactions]

    @property
    def location_text(self) -> str | None:
        return str(self.location) if self.location is not None else None

    @property
    def source(self) -> SourceLocation | None:
        return self.location

    @property
    def origin(self) -> SourceLocation | None:
        return self.location

    @property
    def position(self) -> SourceLocation | None:
        return self.location

    @property
    def partial_data(self) -> dict[str, Any] | None:
        """Raw data carried by a failed/degraded response, clearly labelled."""
        if self.formal or self.trusted_scope == TRUST_NONE:
            return None
        return {
            "kind": self.kind,
            "trnuid": self.trnuid,
            "status": self.status.to_dict(),
            "trusted_scope": self.trusted_scope,
            "account": self.account_info,
            "transaction_records": self.transaction_records,
            "transaction_count": self.transaction_count,
            "period": _jsonable(self.period),
            "balances": self.balances,
            "trusted_fields": self.trusted_fields,
            "untrusted_fields": self.untrusted_fields,
            "complete_period": False,
            "location": self.location_text,
            "source_location": self.location.to_dict() if self.location else None,
            "note": "Partial response data from a failed transaction; "
            "not a complete statement period.",
            "reason": "partial_data",
        }

    @property
    def formal_facts(self) -> dict[str, Any] | None:
        """The formal statement facts, only for a successful response."""
        if not self.formal:
            return None
        return {
            "kind": self.kind,
            "trnuid": self.trnuid,
            "status": self.status.to_dict(),
            "trusted_scope": self.trusted_scope,
            "account": self.account_info,
            "transaction_records": self.transaction_records,
            "transaction_count": self.transaction_count,
            "period": _jsonable(self.period),
            "balances": self.balances,
            "trusted_fields": self.trusted_fields,
            "untrusted_fields": self.untrusted_fields,
            "complete_period": self.complete_period,
            "location": self.location_text,
            "source_location": self.location.to_dict() if self.location else None,
        }

    def _make_issues(self) -> list[str]:
        issues = list(self._issues)
        if self.status.failed:
            issues.append(f"Response status failed ({self.status})")
        elif self.status.warn:
            issues.append(f"Response status warning ({self.status})")
        if self.conversion_error:
            issues.append(
                f"Response data could not be fully converted: {self.conversion_error}"
            )
        if self.status.failed:
            if self.statement is None:
                issues.append(
                    "No response data was returned for this failed transaction"
                )
            else:
                count = self.transaction_count
                if count == 0:
                    issues.append(
                        "Failed response contains no transactions; this is not a "
                        "valid zero-activity period"
                    )
                else:
                    issues.append(
                        f"Failed response carries {count} partial transaction(s); "
                        "period coverage is not trustworthy"
                    )
                if self.period is None:
                    issues.append("No trustworthy transaction period is available")
        if self.formal and self.transactions is None:
            issues.append(
                "Successful response has no transaction list; zero activity is not asserted"
            )
        return issues

    @property
    def response_issues(self) -> list[str]:
        return self._make_issues()

    @property
    def issues(self) -> list[str]:
        """Human-readable problems with this response.

        Includes status failure, conversion failure, absent data, and the
        explicit statement that a failed block is not a zero-activity period.
        """
        return self._make_issues()

    @issues.setter
    def issues(self, value: list[str]) -> None:
        self._issues = list(value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "trnuid": self.trnuid,
            "kind": self.kind,
            "is_statement": self.is_statement,
            "status": self.status.to_dict(),
            "ok": self.ok,
            "success": self.success,
            "failed": self.failed,
            "trusted_scope": self.trusted_scope,
            "trust_scope": self.trust_scope,
            "partial": self.partial,
            "formal": self.formal,
            "usable": self.usable,
            "complete_period": self.complete_period,
            "error": self.conversion_error,
            "account": self.account_info,
            "transaction_records": self.transaction_records,
            "transaction_count": self.transaction_count,
            "period": _jsonable(self.period),
            "trusted_fields": self.trusted_fields,
            "untrusted_fields": self.untrusted_fields,
            "location": self.location_text,
            "source_location": self.location.to_dict() if self.location else None,
            "statement_location": (
                str(self.statement_location)
                if self.statement_location is not None
                else None
            ),
            "issues": self._make_issues(),
        }


def statement_report(responses: Iterable[StatementResponse]) -> list[dict[str, Any]]:
    """Render a list of ``StatementResponse`` as JSON-compatible dictionaries."""
    return [response.to_dict() for response in responses]


def format_report(responses: Iterable[StatementResponse]) -> str:
    """One line per response: trust scope, status and original location."""
    return "\n".join(response.summary for response in responses)
