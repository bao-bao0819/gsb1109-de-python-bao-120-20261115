#!/usr/bin/env python
"""Command line front end for :mod:`ofxtools.migrate`."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ofxtools.migrate import MigrationError, StructureError, convert_file


def make_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ofxconvert",
        description="Convert legacy OFXv1 (SGML) documents to OFXv2 (XML)",
    )
    parser.add_argument("source", nargs="+", help="OFX file(s) to convert")
    parser.add_argument(
        "-o",
        "--output",
        help="Output file (only valid with a single source file)",
    )
    parser.add_argument(
        "--version",
        type=int,
        help="Output OFX version (default: 200; source version is recorded)",
    )
    parser.add_argument(
        "--repair",
        action="store_true",
        help="Re-attach aggregates whose end tag was omitted (reported)",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Indent the XML output",
    )
    parser.add_argument(
        "--no-validate",
        action="store_true",
        help="Skip checking required elements and field types",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Re-read each output and compare the business tree (default: on)",
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="Do not re-read the converted output",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="Write a JSON report next to each output file",
    )
    parser.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="Only report errors",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = make_argparser().parse_args(argv)
    if args.output and len(args.source) != 1:
        print("ofxconvert: -o/--output requires exactly one source", file=sys.stderr)
        return 2
    verify = not args.no_verify

    status = 0
    for source in args.source:
        source_path = Path(source)
        destination = (
            Path(args.output) if args.output else source_path.with_suffix(".xml")
        )
        try:
            result = convert_file(
                source_path,
                destination,
                version=args.version,
                repair=args.repair,
                pretty=args.pretty,
                validate=not args.no_validate,
            )
        except (MigrationError, StructureError, OSError) as err:
            print(f"{source}: {err}", file=sys.stderr)
            status = 1
            continue

        if verify:
            try:
                result.verify(source_path)
            except MigrationError as err:
                destination.unlink(missing_ok=True)
                print(f"{source}: conversion did not verify: {err}", file=sys.stderr)
                status = 1
                continue

        if args.report:
            report_path = destination.with_suffix(".report.json")
            report_path.write_text(
                json.dumps(result.report.as_dict(), ensure_ascii=False, indent=2),
                encoding="utf_8",
            )
        if not args.quiet:
            print(f"{source} -> {destination}")
            print(result.report)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
