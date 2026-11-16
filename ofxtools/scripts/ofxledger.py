#!/usr/bin/env python
"""
Settlement ledger CLI - three-book reporting for OFX investment statements.

Reads an OFX file, replays it through :class:`ofxtools.ledger.SettlementLedger`
and prints equity holdings, available securities and pending cash.
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from decimal import Decimal
from typing import Any

from ofxtools.ledger import (
    SettlementLedger,
    SettlementRules,
    SettlementStatus,
    Snapshot,
    event_from_dict,
)
from ofxtools.Parser import OFXTree


def _parse_date(value: str) -> datetime.date:
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"invalid date {value!r}; use YYYYMMDD")


def _parse_known(value: str) -> tuple[str, datetime.date]:
    if "=" not in value:
        raise argparse.ArgumentTypeError(
            f"invalid --known-fitid {value!r}; use FITID=YYYYMMDD"
        )
    fitid, date_ = value.split("=", 1)
    return fitid.strip(), _parse_date(date_)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ofxledger",
        description="Replay OFX investment statements into settlement books",
    )
    parser.add_argument("ofx", help="OFX/QFX file to read ('-' for stdin)")
    parser.add_argument(
        "command",
        nargs="?",
        default="asof",
        choices=("asof", "trades", "pending", "events"),
        help="report to produce (default: asof)",
    )
    parser.add_argument(
        "-d",
        "--date",
        type=_parse_date,
        help="effective as-of date (default: latest event date)",
    )
    parser.add_argument(
        "-k",
        "--known",
        type=_parse_date,
        help="knowledge cutoff date (default: as-of date)",
    )
    parser.add_argument(
        "--known-fitid",
        action="append",
        default=[],
        type=_parse_known,
        metavar="FITID=DATE",
        help="receipt date of a late confirmation (repeatable)",
    )
    parser.add_argument(
        "--default-known",
        type=_parse_date,
        help="receipt date for events not named by --known-fitid",
    )
    parser.add_argument(
        "--confirmations",
        metavar="JSON",
        help=(
            "JSON file of settlement confirmations (list of event objects, "
            "or {'events': [...]}) for partial settlements, failures, "
            "cancellations and late receipts"
        ),
    )
    parser.add_argument(
        "--rules",
        metavar="JSON",
        help=(
            "JSON object mapping instrument/security type to business days, "
            "e.g. '{\"MUNICIPAL\": 2, \"DEBT\": 3}' (order matters)"
        ),
    )
    parser.add_argument(
        "--status",
        choices=[status.value for status in SettlementStatus],
        help="filter 'trades' output by settlement status",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit JSON instead of text"
    )
    return parser


def _read_ofx(source: str) -> Any:
    tree = OFXTree()
    if source == "-":
        tree.parse(sys.stdin.buffer)
    else:
        tree.parse(source)
    return tree.convert()


def _load_json(path: str) -> Any:
    if path == "-":
        return json.load(sys.stdin)
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)


def _load_confirmations(path: str) -> list[Any]:
    payload = _load_json(path)
    if isinstance(payload, dict):
        payload = payload.get("events")
    if not isinstance(payload, list):
        raise SystemExit(
            "--confirmations must hold a list of events, or {'events': [...]}"
        )
    return payload


def _load_rules(path: str | None) -> SettlementRules | None:
    if path is None:
        return None
    payload = _load_json(path)
    if not isinstance(payload, dict):
        raise SystemExit("--rules must hold a JSON object of {name: business_days}")
    return SettlementRules.from_mapping(payload)


def _decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _snapshot_payload(snapshot: Snapshot) -> dict[str, Any]:
    equity = []
    available = []
    unsettled = []
    for (account, security_id), position in sorted(snapshot.securities.items()):
        equity.append(
            {
                "account": account,
                "security_id": security_id,
                "security_type": position.security_type,
                "units": _decimal(position.held),
            }
        )
        available.append(
            {
                "account": account,
                "security_id": security_id,
                "security_type": position.security_type,
                "units": _decimal(position.available),
            }
        )
        unsettled.append(
            {
                "account": account,
                "security_id": security_id,
                "security_type": position.security_type,
                "units": _decimal(position.unsettled),
                "receivable": _decimal(position.receivable),
                "deliverable_pending": _decimal(position.deliverable_pending),
            }
        )
    cash = []
    for (account, currency), cash_position in sorted(snapshot.cash.items()):
        cash.append(
            {
                "account": account,
                "currency": currency,
                "settled": _decimal(cash_position.settled),
                "pending": _decimal(cash_position.pending),
                "total": _decimal(cash_position.total),
            }
        )
    return {
        "as_of": snapshot.as_of.isoformat(),
        "known_as_of": snapshot.known_as_of.isoformat(),
        "equity": equity,
        "available": available,
        "unsettled": unsettled,
        "cash": cash,
    }


def _print_snapshot(snapshot: Snapshot) -> None:
    print(f"As of {snapshot.as_of} (known {snapshot.known_as_of})")
    print()
    print("Equity holdings (trade date)")
    if snapshot.equity:
        for (account, security_id), units in sorted(snapshot.equity.items()):
            print(f"  {account:12} {security_id:20} {_decimal(units):>18}")
    else:
        print("  (none)")
    print()
    print("Available securities (settled)")
    if snapshot.securities:
        for (account, security_id), position in sorted(snapshot.securities.items()):
            print(
                f"  {account:12} {security_id:20} "
                f"{_decimal(position.available):>18}  "
                f"to receive {_decimal(position.receivable):>14}  "
                f"to deliver {_decimal(position.deliverable_pending):>14}"
            )
    else:
        print("  (none)")
    print()
    print("Cash")
    if snapshot.cash:
        for (account, currency), cash_position in sorted(snapshot.cash.items()):
            print(
                f"  {account:12} {currency:5} "
                f"settled {_decimal(cash_position.settled):>16}  "
                f"pending {_decimal(cash_position.pending):>16}"
            )
    else:
        print("  (none)")


def _trade_payload(record: Any) -> dict[str, Any]:
    return {
        "fitid": record.fitid,
        "account": record.account,
        "security_id": record.security_id,
        "units": _decimal(record.units),
        "amount": _decimal(record.amount),
        "trade_date": record.trade_date.isoformat(),
        "settlement_date": record.settlement_date.isoformat(),
        "status": record.status.value,
        "settled_units": _decimal(record.settled_units),
        "failed_units": _decimal(record.failed_units),
        "remaining_units": _decimal(record.remaining_units),
        "settled_amount": _decimal(record.settled_amount),
        "failed_amount": _decimal(record.failed_amount),
        "remaining_amount": _decimal(record.remaining_amount),
        "pieces": list(record.pieces),
    }


def _event_payload(event: Any) -> dict[str, Any]:
    return {
        "fitid": event.fitid,
        "event_type": event.event_type.value,
        "effective_date": event.effective_date.isoformat(),
        "known_date": event.known_date.isoformat() if event.known_date else None,
        "trade_date": event.trade_date.isoformat() if event.trade_date else None,
        "settlement_date": (
            event.settlement_date.isoformat() if event.settlement_date else None
        ),
        "reference": event.reference,
        "account": event.account,
        "currency": event.currency,
        "security_id": event.security_id,
        "units": _decimal(event.units),
        "amount": _decimal(event.amount),
    }


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)

    ofx = _read_ofx(args.ofx)
    known_dates = dict(args.known_fitid)
    ledger = SettlementLedger.from_statements(
        ofx.statements,
        known_dates=known_dates or None,
        default_known_date=args.default_known,
        rules=_load_rules(args.rules),
    )
    if args.confirmations:
        ledger.extend(
            event_from_dict(payload)
            for payload in _load_confirmations(args.confirmations)
        )

    date, known = ledger.default_cutoffs(args.date, args.known)

    if args.command == "asof":
        snapshot = ledger.as_of(date, known)
        if args.json:
            print(json.dumps(_snapshot_payload(snapshot), indent=2))
        else:
            _print_snapshot(snapshot)
        return

    if args.command == "trades":
        records = [
            record
            for record in ledger.trade_records(date, known)
            if record.status is not SettlementStatus.CANCELLED
        ]
        if args.status:
            wanted = SettlementStatus(args.status)
            records = [record for record in records if record.status is wanted]
        if args.json:
            print(json.dumps([_trade_payload(record) for record in records], indent=2))
        else:
            for record in records:
                print(
                    f"{record.fitid:12} {record.account:12} "
                    f"trade {record.trade_date} settle {record.settlement_date} "
                    f"{record.status.value:9} "
                    f"units {_decimal(record.units):>14} "
                    f"settled {_decimal(record.settled_units):>14} "
                    f"failed {_decimal(record.failed_units):>14} "
                    f"remaining {_decimal(record.remaining_units):>14} "
                    f"pieces {','.join(record.pieces) or '-'}"
                )
        return

    if args.command == "pending":
        records = ledger.pending_trades(date, known)
        if args.json:
            print(json.dumps([_trade_payload(record) for record in records], indent=2))
        else:
            for record in records:
                print(
                    f"{record.fitid:12} {record.account:12} "
                    f"settle {record.settlement_date} "
                    f"{record.status.value:9} "
                    f"remaining units {_decimal(record.remaining_units):>14}"
                )
        return

    events = [
        event
        for event in ledger.events
        if event.effective_date <= date
        and event.known_date is not None
        and event.known_date <= known
    ]
    if args.json:
        print(json.dumps([_event_payload(event) for event in events], indent=2))
    else:
        for event in events:
            print(
                f"{event.effective_date} {event.event_type.value:7} "
                f"{event.fitid:12} trade {event.trade_date} "
                f"settle {event.settlement_date} "
                f"known {event.known_date} "
                f"units {_decimal(event.units):>14} "
                f"cash {_decimal(event.amount):>14}"
            )


if __name__ == "__main__":
    main()
