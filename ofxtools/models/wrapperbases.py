"""
Base classes for OFX message wrappers

These can't be defined in models.base because models.common.STATUS would
create circular imports.
"""

from __future__ import annotations

__all__ = ["TrnRq", "TrnRs", "SyncRqList", "SyncRsList"]


# stdlib imports
from types import SimpleNamespace
from typing import Any

# local imports
from ofxtools.location import get_source_location
from ofxtools.models.base import Aggregate, lenient
from ofxtools.models.common import OFXEXTENSION, STATUS
from ofxtools.Types import Bool, DateTime, String, SubAggregate


class TrnRq(Aggregate):
    """
    Base class for *TRNRQ wrappers.

    OFX section 2.4.6.1
    """

    trnuid = String(36, required=True)
    cltcookie = String(32)
    tan = String(80)
    ofxextension = SubAggregate(OFXEXTENSION)


class TrnRs(Aggregate):
    """
    Base class for *TRNRS wrappers.

    OFX section 2.4.6.1

    A server may return a failed transaction status together with a partially
    populated response aggregate.  Rather than aborting conversion of the whole
    OFX document (and thereby losing the successful sibling accounts), a
    wrapper that cannot be converted in full is retained in degraded form:
    ``status``/``trnuid`` are salvaged where possible, ``raw_partial`` holds
    the original ``ElementTree`` subtree and ``conversion_error`` explains
    what failed.  ``models.response`` classifies such a wrapper as ``partial``.
    """

    trnuid = String(36, required=True)
    status = SubAggregate(STATUS, required=True)
    cltcookie = String(32)
    ofxextension = SubAggregate(OFXEXTENSION)

    @classmethod
    def _convert(cls, elem: Any) -> Aggregate:
        try:
            return super()._convert(elem)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            if not lenient():
                raise
            return cls._convert_degraded(elem, exc)

    @classmethod
    def _convert_degraded(cls, elem: Any, exc: Exception) -> Aggregate:
        """Build a minimal wrapper from a block that failed validation."""
        instance = cls.__new__(cls)
        list.__init__(instance)
        instance.conversion_error = f"{type(exc).__name__}: {exc}"
        instance.raw_partial = elem

        location = get_source_location(elem)
        if location is not None:
            instance.source_location = location

        trnuid = elem.find("TRNUID")
        if trnuid is not None and trnuid.text:
            instance.__dict__["trnuid"] = trnuid.text.strip()

        cltcookie = elem.find("CLTCOOKIE")
        if cltcookie is not None and cltcookie.text:
            instance.__dict__["cltcookie"] = cltcookie.text.strip()

        status_elem = elem.find("STATUS")
        if status_elem is not None:
            try:
                status: Any = Aggregate.from_etree(status_elem)
            except (ValueError, TypeError, KeyError, AttributeError):
                status = _raw_status(status_elem)
            instance.__dict__["status"] = status

        return instance


def _raw_status(elem: Any) -> SimpleNamespace:
    """Best-effort STATUS for blocks that cannot be model-converted."""

    def text(tag: str) -> str | None:
        child = elem.find(tag)
        if child is None or child.text is None:
            return None
        return child.text.strip()

    code_text = text("CODE")
    try:
        code = int(code_text) if code_text is not None else None
    except ValueError:
        code = None
    return SimpleNamespace(
        code=code,
        severity=text("SEVERITY"),
        message=text("MESSAGE"),
    )


class TranList(Aggregate):
    """
    Base class for OFX *TRANLIST

    Cf. OFX section 3.2.7
    """

    dtstart = DateTime(required=True)
    dtend = DateTime(required=True)

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} dtstart='{self.dtstart}' dtend='{self.dtend}' len={len(self)}>"


class SyncRqList(Aggregate):
    """Base class for *SYNCRQ"""

    token = String(10)
    tokenonly = Bool()
    refresh = Bool()
    rejectifmissing = Bool(required=True)

    requiredMutexes = [["token", "tokenonly", "refresh"]]


class SyncRsList(Aggregate):
    """Base class for *SYNCRS"""

    token = String(10, required=True)
    lostsync = Bool()
