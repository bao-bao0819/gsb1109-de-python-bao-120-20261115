from .._boundary_catalog import boundary_catalog, catalog_to_json
from .._helpers import read, reader_map


def add_args(parser):
    parser.add_argument(
        "infiles",
        type=str,
        nargs="+",
        help="mesh files to catalog (each file is its own source namespace)",
    )
    parser.add_argument(
        "--input-format",
        "-i",
        type=str,
        choices=sorted(list(reader_map.keys())),
        help="input file format",
        default=None,
    )
    parser.add_argument(
        "--output",
        "-o",
        type=str,
        help="output JSON file (default: stdout)",
        default=None,
    )
    parser.add_argument(
        "--physical-data-key",
        type=str,
        default="gmsh:physical",
        help="cell data key holding group numbers (default: gmsh:physical)",
    )


def boundary_catalog_cmd(args):
    meshes = {
        infile: read(infile, file_format=args.input_format) for infile in args.infiles
    }
    catalog = boundary_catalog(meshes, physical_data_key=args.physical_data_key)
    text = catalog_to_json(catalog)
    if args.output is None:
        print(text)
    else:
        with open(args.output, "w") as f:
            f.write(text + "\n")
    return 0
