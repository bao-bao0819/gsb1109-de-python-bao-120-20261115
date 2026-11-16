"""Command line entry point for auditing EDIFACT archives.

Usage::

    python -m pydifact.archive_cli path/to/file.edi
    python -m pydifact.archive_cli --json path/to/directory
    python -m pydifact.archive_cli --strict path/to/archive.zip

Exit codes: ``0`` all interchanges ok, ``1`` mismatch, ``2`` truncated,
``3`` structural error, ``4`` usage/IO error.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence

from pydifact.archive import ArchiveAudit, audit_archive
from pydifact.audit import AuditStatus
from pydifact.exceptions import EDISyntaxError

_EXIT_CODES = {
    AuditStatus.OK: 0,
    AuditStatus.MISMATCH: 1,
    AuditStatus.TRUNCATED: 2,
    AuditStatus.ERROR: 3,
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pydifact.archive",
        description="Audit UNB/UNZ and UNH/UNT envelopes of EDIFACT archives.",
    )
    parser.add_argument("paths", nargs="+", help="Files, directories or ZIP archives")
    parser.add_argument(
        "--text",
        action="store_true",
        help="Treat positional arguments as EDI text instead of paths",
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit JSON instead of a layered text report"
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Raise/return an error for structural faults (exit code 3)",
    )
    parser.add_argument(
        "--encoding", default="iso8859-1", help="File encoding (default: iso8859-1)"
    )
    parser.add_argument(
        "--no-recursive",
        action="store_true",
        help="Do not descend into subdirectories",
    )
    args = parser.parse_args(argv)

    if not args.text:
        missing = [path for path in args.paths if not os.path.exists(path)]
        if missing:
            print(f"error: no such file or directory: {missing[0]}", file=sys.stderr)
            return 4
    sources = args.paths if args.text else [os.fspath(path) for path in args.paths]

    try:
        report = audit_archive(
            sources,
            encoding=args.encoding,
            recursive=not args.no_recursive,
        )
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 4

    if args.strict:
        try:
            report.validate()
        except EDISyntaxError as exc:
            print(f"error: {exc}", file=sys.stderr)
            print(report.format_json() if args.json else report.format_text())
            return _EXIT_CODES[AuditStatus.ERROR]

    print(report.format_json() if args.json else report.format_text())
    return _exit_code(report)


def _exit_code(report: ArchiveAudit) -> int:
    return _EXIT_CODES[report.status]


if __name__ == "__main__":
    raise SystemExit(main())
