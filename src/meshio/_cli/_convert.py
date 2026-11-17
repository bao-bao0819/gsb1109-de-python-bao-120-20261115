import json
from pathlib import Path

import numpy as np

from .._capabilities import capability_report, report_to_dict
from .._common import error
from .._exceptions import ReadError
from .._helpers import _writer_map, read, reader_map, write


def add_args(parser):
    parser.add_argument("infile", type=str, help="mesh file to be read from")
    parser.add_argument(
        "--input-format",
        "-i",
        type=str,
        choices=sorted(list(reader_map.keys())),
        help="input file format",
        default=None,
    )
    parser.add_argument(
        "--output-format",
        "-o",
        type=str,
        choices=sorted(list(_writer_map.keys())),
        help="output file format",
        default=None,
    )
    parser.add_argument(
        "--ascii",
        "-a",
        action="store_true",
        help="write in ASCII format variant (where applicable, default: binary)",
    )
    parser.add_argument("outfile", type=str, help="mesh file to be written to")
    parser.add_argument(
        "--float-format",
        "-f",
        type=str,
        help="float format used in output ASCII files (default: .16e)",
    )
    parser.add_argument(
        "--sets-to-int-data",
        "-s",
        action="store_true",
        help="if possible, convert sets to integer data (useful if the output type does not support sets)",
    )
    parser.add_argument(
        "--int-data-to-sets",
        "-d",
        action="store_true",
        help="if possible, convert integer data to sets (useful if the output type does not support integer data)",
    )
    parser.add_argument(
        "--report",
        "-r",
        action="store_true",
        help="print the concrete capability report and do not write anything",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the capability report as JSON (implies behavior of --report)",
    )
    parser.add_argument(
        "--allow-degraded",
        action="store_true",
        help="authorize only the explicitly listed degradations; unrepresentable features still abort",
    )


def _print_report(report, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report_to_dict(report), indent=2, sort_keys=False))
    else:
        print(report.describe())


def convert(args):
    # read mesh data
    mesh = read(args.infile, file_format=args.input_format)

    # Some converters (like VTK) require `points` to be contiguous.
    mesh.points = np.ascontiguousarray(mesh.points)

    if args.sets_to_int_data:
        mesh.point_sets_to_data()
        mesh.cell_sets_to_data()

    if args.int_data_to_sets:
        for key in mesh.point_data:
            mesh.point_data_to_sets(key)
        for key in mesh.cell_data:
            mesh.cell_data_to_sets(key)

    file_format = args.output_format
    if file_format is None:
        from .._helpers import _filetypes_from_path

        try:
            file_format = _filetypes_from_path(Path(args.outfile))[0]
        except ReadError as exc:
            error(str(exc))
            return 1

    writer_options = {}
    if args.ascii:
        writer_options["binary"] = False
    report = capability_report(mesh, file_format, options=writer_options)

    if args.report or args.json:
        _print_report(report, args.json)
        return 0 if report.ok(allow_degraded=False) else 1

    # List the concrete preflight result before any bytes are written.
    print(report.describe())

    if not report.ok(allow_degraded=args.allow_degraded):
        error(report.reason(allow_degraded=args.allow_degraded))
        if report.unrepresentable:
            error(
                "Unrepresentable features cannot be authorized away; convert or "
                "remove the listed blocks first."
            )
        return 1

    # write it out
    kwargs = {"file_format": file_format}
    if args.float_format is not None:
        kwargs["float_fmt"] = args.float_format
    if args.ascii:
        kwargs["binary"] = False

    write(args.outfile, mesh, **kwargs)
    return 0
