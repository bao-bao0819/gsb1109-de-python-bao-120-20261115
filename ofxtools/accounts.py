"""
Stable account keys for OFX accounts.

An OFX ``ACCTID`` is only unique within the namespace of the institution
that issued it: two banks may assign the same account number, and
securities accounts live in a different naming domain (``BROKERID``)
than bank accounts (``BANKID``) or credit cards.  This module builds
account keys that are stable across those collisions by qualifying each
account number with its naming domain, the institution identifier as
reported, and the account type.

The original institution and account type are preserved verbatim on the
key; callers may additionally register explicit aliases mapping their
own labels to canonical keys.

Historical account renumbering is modeled with validity periods: each
account number is valid over a half-open interval ``[valid_from,
valid_to)``, and observations are resolved to the canonical (current)
key as of a given date.

Accounts for which no institution evidence is available (no BANKID,
BROKERID, FID, or ORG) are kept in a separate ``unattributed`` list.
Attribution is never guessed from external hints such as file names.
"""

from __future__ import annotations

__all__ = [
    "ACCTDOMAINS",
    "AccountKey",
    "NumberingPeriod",
    "AccountRegistry",
    "scan",
]

# stdlib imports
import dataclasses
import datetime
from typing import Any

# local imports
from ofxtools.models.bank.stmt import BANKACCTFROM, BANKACCTTO, CCACCTFROM, CCACCTTO
from ofxtools.models.invest.acct import INVACCTFROM, INVACCTTO

# Naming domains for account numbers.  ACCTID values are only comparable
# within a single domain.
ACCTDOMAINS = ("BANK", "CREDITCARD", "INVESTMENT")

_BANKACCT_TYPES = (BANKACCTFROM, BANKACCTTO)
_CCACCT_TYPES = (CCACCTFROM, CCACCTTO)
_INVACCT_TYPES = (INVACCTFROM, INVACCTTO)


@dataclasses.dataclass(frozen=True)
class AccountKey:
    """
    Stable identity of an OFX account.

    ``domain`` is the naming domain of the account number, one of
    ``ACCTDOMAINS``.  ``institution`` is the institution identifier as
    reported by the server (BANKID, BROKERID, FID, or ORG), or None if
    the data carries no institution evidence.  ``accttype`` preserves
    the reported account type (e.g. CHECKING, or a USPRODUCTTYPE for
    investment accounts), or None if unknown.
    """

    domain: str
    institution: str | None
    acctid: str
    accttype: str | None = None

    def __post_init__(self) -> None:
        domain = self.domain.upper()
        if domain not in ACCTDOMAINS:
            raise ValueError(
                f"domain must be one of {ACCTDOMAINS}, got {self.domain!r}"
            )
        object.__setattr__(self, "domain", domain)
        if not self.acctid:
            raise ValueError("acctid must not be empty")
        for field in ("institution", "accttype"):
            value = getattr(self, field)
            if value is not None:
                value = value.strip()
                if not value:
                    raise ValueError(f"{field} must not be blank")
                object.__setattr__(self, field, value)

    def __str__(self) -> str:
        return ":".join(
            (
                self.domain,
                self.institution or "",
                self.accttype or "",
                self.acctid,
            )
        )

    @classmethod
    def parse(cls, text: str) -> AccountKey:
        """Inverse of str()."""
        parts = text.split(":", 3)
        if len(parts) != 4:
            raise ValueError(f"Cannot parse {text!r} as an AccountKey")
        domain, institution, accttype, acctid = parts
        return cls(
            domain=domain,
            institution=institution or None,
            accttype=accttype or None,
            acctid=acctid,
        )

    @classmethod
    def from_acctfrom(
        cls,
        acctfrom: Any,
        org: str | None = None,
        fid: str | None = None,
        accttype: str | None = None,
    ) -> AccountKey:
        """
        Build a key from an OFX *ACCTFROM/*ACCTTO aggregate.

        Institution evidence is taken from the aggregate itself where
        available (BANKID for bank accounts, BROKERID for investment
        accounts).  Credit card aggregates carry no institution
        identifier, so ``fid``/``org`` from the signon response (SONRS)
        are used as evidence, in that order of preference.  If no
        evidence is available, ``institution`` is None and the key
        belongs in the unattributed list.

        ``accttype`` overrides the type reported by the aggregate (e.g.
        to supply USPRODUCTTYPE from an INVACCTINFO).
        """
        if isinstance(acctfrom, _BANKACCT_TYPES):
            return cls(
                domain="BANK",
                institution=acctfrom.bankid,
                acctid=acctfrom.acctid,
                accttype=accttype or acctfrom.accttype,
            )
        elif isinstance(acctfrom, _CCACCT_TYPES):
            return cls(
                domain="CREDITCARD",
                institution=fid or org,
                acctid=acctfrom.acctid,
                accttype=accttype,
            )
        elif isinstance(acctfrom, _INVACCT_TYPES):
            return cls(
                domain="INVESTMENT",
                institution=acctfrom.brokerid,
                acctid=acctfrom.acctid,
                accttype=accttype,
            )
        raise TypeError(
            f"Don't know how to key account aggregate of type {type(acctfrom)!r}"
        )


@dataclasses.dataclass(frozen=True)
class NumberingPeriod:
    """
    Validity period of one account number for a logical account.

    Half-open interval [valid_from, valid_to); None means unbounded.
    """

    acctid: str
    valid_from: datetime.date | None = None
    valid_to: datetime.date | None = None

    def __post_init__(self) -> None:
        if not self.acctid:
            raise ValueError("acctid must not be empty")
        if (
            self.valid_from is not None
            and self.valid_to is not None
            and self.valid_from >= self.valid_to
        ):
            raise ValueError(
                f"valid_from {self.valid_from} must precede valid_to {self.valid_to}"
            )

    def covers(self, asof: datetime.date | None) -> bool:
        """True if this period is in effect as of the given date."""
        if asof is None:
            return self.valid_to is None
        if self.valid_from is not None and asof < self.valid_from:
            return False
        if self.valid_to is not None and asof >= self.valid_to:
            return False
        return True


class AccountRegistry:
    """
    Registry of canonical account keys, explicit aliases, and
    renumbering history.

    Aliases map caller-chosen labels to canonical keys; they are only
    ever created explicitly via register_alias().  Renumbering history
    maps (domain, institution, accttype) to the validity periods of the
    account numbers that logical account has used, so that historical
    observations can be resolved to the canonical (current) key.
    """

    def __init__(self) -> None:
        self._keys: dict[str, AccountKey] = {}
        self._aliases: dict[str, AccountKey] = {}
        self._periods: dict[tuple[str, str, str | None], list[NumberingPeriod]] = {}
        self.unattributed: list[AccountKey] = []

    def register(self, key: AccountKey) -> AccountKey:
        """
        Register a key as canonical.  Keys without institution evidence
        go to the separate ``unattributed`` list instead.
        """
        if key.institution is None:
            if key not in self.unattributed:
                self.unattributed.append(key)
            return key
        self._keys[str(key)] = key
        return key

    @property
    def keys(self) -> list[AccountKey]:
        """Canonical keys, sorted by their stable string form."""
        return [self._keys[k] for k in sorted(self._keys)]

    def register_alias(self, alias: str, key: AccountKey) -> None:
        """Map an explicit caller-chosen alias to a canonical key."""
        alias = alias.strip()
        if not alias:
            raise ValueError("alias must not be blank")
        if key.institution is None:
            raise ValueError("Cannot alias an unattributed account key")
        self.register(key)
        self._aliases[alias] = key

    def lookup_alias(self, alias: str) -> AccountKey:
        """Resolve an explicit alias to its canonical key."""
        try:
            return self._aliases[alias]
        except KeyError:
            raise KeyError(f"Unknown account alias {alias!r}") from None

    @property
    def aliases(self) -> dict[str, AccountKey]:
        return dict(self._aliases)

    def record_renumbering(
        self,
        key: AccountKey,
        new_acctid: str,
        effective_date: datetime.date,
    ) -> AccountKey:
        """
        Record that the account identified by ``key`` was renumbered to
        ``new_acctid`` as of ``effective_date``.

        The old number's validity period is closed at ``effective_date``
        and a new period opened from it; the canonical key becomes the
        one carrying ``new_acctid``.  Historical observations of the old
        number resolve to the new canonical key via resolve().
        """
        if key.institution is None:
            raise ValueError("Cannot renumber an unattributed account key")
        history_key = (key.domain, key.institution, key.accttype)
        periods = self._periods.setdefault(
            history_key, [NumberingPeriod(acctid=key.acctid)]
        )
        current = periods[-1]
        if current.acctid != key.acctid:
            raise ValueError(
                f"{key.acctid!r} is not the current number for {history_key}"
            )
        periods[-1] = dataclasses.replace(
            current, valid_to=effective_date
        )
        periods.append(
            NumberingPeriod(acctid=new_acctid, valid_from=effective_date)
        )
        canonical = dataclasses.replace(key, acctid=new_acctid)
        self.register(canonical)
        return canonical

    def resolve(
        self, key: AccountKey, asof: datetime.date | None = None
    ) -> AccountKey:
        """
        Resolve an observed key to its canonical form as of ``asof``
        (None = current), applying recorded renumbering history.
        Returns the key unchanged if no history is recorded for it.
        """
        if key.institution is None:
            return key
        periods = self._periods.get((key.domain, key.institution, key.accttype))
        if not periods:
            return key
        for period in periods:
            if period.acctid == key.acctid and period.covers(asof):
                current = periods[-1]
                return dataclasses.replace(key, acctid=current.acctid)
        return key


def _as_date(dt: Any) -> datetime.date | None:
    if dt is None:
        return None
    return dt.date() if isinstance(dt, datetime.datetime) else dt


def _statement_asof(stmt: Any) -> datetime.date | None:
    """
    Best-effort as-of date for a statement response aggregate.

    NB: attribute access is guarded by membership in the class ``spec``
    because Aggregate.__getattr__ proxies unknown attributes to
    subaggregates (and recurses on missing optional ones).
    """
    spec = type(stmt).spec
    if "dtasof" in spec:
        return _as_date(stmt.dtasof)
    if "banktranlist" in spec:
        tranlist = stmt.banktranlist
        if tranlist is not None:
            return _as_date(tranlist.dtend)
    return None


def scan(ofx: Any, registry: AccountRegistry | None = None) -> tuple[
    list[AccountKey], list[AccountKey]
]:
    """
    Collect stable account keys from the statements of a parsed OFX
    response.

    Institution evidence is taken from each *ACCTFROM aggregate and from
    the signon response (FID/ORG).  Returns (keys, unattributed):
    accounts carrying no institution evidence are reported separately in
    ``unattributed``; their attribution is never guessed from external
    hints such as the source file name.

    If ``registry`` is given, keys are resolved against its renumbering
    history (as of each statement's date) and registered.
    """
    signon = ofx.signon
    fi = signon.fi if "fi" in type(signon).spec else None
    org = fi.org if fi is not None else None
    fid = fi.fid if fi is not None else None

    keys: list[AccountKey] = []
    unattributed: list[AccountKey] = []
    for stmt in ofx.statements:
        if not hasattr(type(stmt), "account"):
            continue
        acctfrom = stmt.account
        if acctfrom is None:
            continue
        key = AccountKey.from_acctfrom(acctfrom, org=org, fid=fid)
        if key.institution is None:
            if key not in unattributed:
                unattributed.append(key)
            if registry is not None:
                registry.register(key)
            continue
        if registry is not None:
            key = registry.resolve(key, asof=_statement_asof(stmt))
            registry.register(key)
        if key not in keys:
            keys.append(key)
    return keys, unattributed
