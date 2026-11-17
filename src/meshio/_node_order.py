"""Explicit node-ordering tables between file formats and meshio.

meshio has one canonical node ordering per cell type (the one documented in
``doc/cell_types.tex``). Every format that orders the mid-side nodes
differently needs an explicit table. This module is the single place where
those tables live, so that reading and writing always use exact inverses of
each other.

A table is a permutation ``p`` with the meaning

    meshio_nodes = format_nodes[:, p]

i.e. entry ``i`` of ``p`` is the slot in the format that belongs to meshio
slot ``i``. Use :func:`node_order` to obtain the permutation for any pair of
formats (including ``"meshio"`` itself) and :func:`apply_node_order` to apply
it to a connectivity array.

Cell types that are not listed for a format are **not** silently treated as
identity. For high-order cells that would reorder the mid-side nodes without
any warning, :class:`~meshio._exceptions.OrderingError` is raised instead.
"""

from __future__ import annotations

import numpy as np

from ._common import num_nodes_per_cell
from ._exceptions import OrderingError, WriteError

# Number of corner (vertex) nodes for each supported cell type. Types not in
# this map are handled as "no known high-order structure".
_num_corners = {
    "vertex": 1,
    "line": 2,
    "triangle": 3,
    "quad": 4,
    "tetra": 4,
    "hexahedron": 8,
    "wedge": 6,
    "pyramid": 5,
    "polygon": None,
}

# Derive the corner count for all high-order names, e.g. "triangle6" -> 3.
def _corner_count(cell_type: str):
    if cell_type in _num_corners:
        return _num_corners[cell_type]
    for base, count in _num_corners.items():
        if cell_type.startswith(base):
            suffix = cell_type[len(base) :]
            if suffix.isdigit() and count is not None:
                return count
    return None


def is_high_order(cell_type: str) -> bool:
    """True if ``cell_type`` has nodes in addition to its corner nodes."""
    corners = _corner_count(cell_type)
    if corners is None:
        return False
    return num_nodes_per_cell.get(cell_type, corners) > corners


# format -> {cell_type: permutation with meshio_nodes = format_nodes[:, p]}
_TABLES = {
    "gmsh": {
        # Gmsh uses the VTK ordering for the linear elements and for
        # line3/triangle6/quad8/quad9, but not for the types below.
        "tetra10": [0, 1, 2, 3, 4, 5, 6, 7, 9, 8],
        "hexahedron20": [
            0, 1, 2, 3, 4, 5, 6, 7, 8, 11, 13,
            9, 16, 18, 19, 17, 10, 12, 14, 15,
        ],
        "hexahedron27": [
            0, 1, 2, 3, 4, 5, 6, 7, 8, 11, 13,
            9, 16, 18, 19, 17, 10, 12, 14, 15,
            22, 23, 21, 24, 20, 25, 26,
        ],
        "wedge15": [0, 1, 2, 3, 4, 5, 6, 9, 7, 12, 14, 13, 8, 10, 11],
        "pyramid13": [0, 1, 2, 3, 4, 5, 8, 10, 6, 7, 9, 11, 12],
        "pyramid14": [0, 1, 2, 3, 4, 5, 8, 10, 6, 7, 9, 11, 12, 13],
    },
    "permas": {
        # PERMAS writes the second-order mid-side nodes in a different order
        # than meshio; see the inverse tables used by the reader.
        "triangle6": [0, 2, 4, 1, 3, 5],
        "tetra10": [0, 2, 4, 9, 1, 3, 5, 6, 7, 8],
        "quad9": [0, 2, 8, 6, 1, 5, 7, 3, 4],
        "wedge15": [0, 2, 4, 9, 11, 13, 1, 3, 5, 6, 7, 8, 10, 12, 14],
    },
    "mdpa": {
        # Kratos/GiD ordering for the second-order hexahedra.
        "hexahedron20": [
            0, 1, 2, 3, 4, 5, 6, 7, 8, 11, 10,
            9, 16, 19, 18, 17, 12, 13, 14, 15,
        ],
        "hexahedron27": [
            0, 1, 2, 3, 4, 5, 6, 7, 8, 11, 10,
            9, 16, 19, 18, 17, 12, 13, 14, 15,
            22, 24, 21, 23, 20, 25, 26,
        ],
    },
}

# Formats whose high-order node ordering is identical to meshio's canonical
# ordering. Only formats where this is a documented/structural property belong
# here; "not listed" must not be confused with "identical".
#
# VTK is deliberately not listed: meshio adopts the Gmsh corner ordering for
# the linear wedge, while VTK swaps the two triangular faces. That linear
# difference is handled by _vtk_common.py; the high-order wedge15 permutation
# is explicit in the table above.
_IDENTITY_FORMATS = {"abaqus", "ansys", "avsucd", "exodus", "med"}

# High-order types for which a format uses meshio's ordering even though it
# also uses explicit tables for other types (e.g. gmsh's line3).
_IDENTITY_TYPES = {
    "gmsh": {"line3", "triangle6", "quad8", "quad9"},
    "permas": {"line3", "quad8", "hexahedron20", "hexahedron27"},
    "mdpa": {"line3", "triangle6", "quad8", "quad9", "tetra10"},
    # VTK shares meshio's ordering for all quadratic types except wedge15,
    # which is in the table above.
    "vtk": {
        "line3",
        "triangle6",
        "quad8",
        "quad9",
        "tetra10",
        "hexahedron20",
        "hexahedron27",
        "pyramid13",
        "pyramid14",
    },
}


def _validate_tables() -> None:
    for fmt, tables in _TABLES.items():
        for cell_type, table in tables.items():
            n = num_nodes_per_cell[cell_type]
            if len(table) != n or sorted(table) != list(range(n)):
                raise RuntimeError(
                    f"Invalid node-order table for {fmt}/{cell_type}: "
                    f"expected a permutation of 0..{n - 1}."
                )
            # The explicit identity set must not silently contradict a table.
            if cell_type in _IDENTITY_TYPES.get(fmt, set()):
                if np.any(np.asarray(table) != np.arange(n)):
                    raise RuntimeError(
                        f"{fmt}/{cell_type} is declared identity but has a table."
                    )


_validate_tables()


def _identity(cell_type: str) -> np.ndarray:
    try:
        n = num_nodes_per_cell[cell_type]
    except KeyError as e:
        raise OrderingError(f"Unknown cell type '{cell_type}'.") from e
    return np.arange(n, dtype=int)


def _format_table(source: str, cell_type: str) -> np.ndarray:
    """Table for ``source`` -> meshio, or None if source has no table."""
    table = _TABLES.get(source, {}).get(cell_type)
    if table is not None:
        return np.asarray(table, dtype=int)

    if source in _IDENTITY_FORMATS or cell_type in _IDENTITY_TYPES.get(source, ()):
        return _identity(cell_type)

    return None


def node_order(source: str, target: str, cell_type: str) -> np.ndarray:
    """Return the permutation converting ``source`` slots into ``target`` slots.

    ``source_nodes[:, node_order(source, target, cell_type)]`` yields
    ``target_nodes``. ``"meshio"`` denotes meshio's canonical ordering.

    The returned array is always an explicit permutation of ``0..n-1``. For
    high-order cell types without a known table, :class:`OrderingError` is
    raised: silently assuming identity would keep the corner nodes right while
    permuting the mid-side nodes.
    """
    if source == target:
        return _identity(cell_type)

    if source != "meshio" and target != "meshio":
        forward = node_order(source, "meshio", cell_type)
        return forward[node_order("meshio", target, cell_type)]

    if target == "meshio":
        table = _format_table(source, cell_type)
        if table is not None:
            return table
        if is_high_order(cell_type):
            raise OrderingError(
                f"No {source} -> meshio node-order table for '{cell_type}'. "
                "Refusing to reorder the mid-side nodes without an explicit table."
            )
        return _identity(cell_type)

    # source == "meshio", target is a file format
    table = _format_table(target, cell_type)
    if table is not None:
        # The table maps target -> meshio; invert it to get meshio -> target.
        return np.argsort(table)
    if is_high_order(cell_type):
        raise OrderingError(
            f"No meshio -> {target} node-order table for '{cell_type}'. "
            "Refusing to permute the mid-side nodes without an explicit table."
        )
    return _identity(cell_type)


def apply_node_order(cell_type: str, data, source: str, target: str) -> np.ndarray:
    """Apply :func:`node_order` to a connectivity array."""
    data = np.asarray(data)
    order = node_order(source, target, cell_type)
    if data.ndim != 2 or data.shape[1] != len(order):
        raise OrderingError(
            f"Connectivity for '{cell_type}' has shape {data.shape}, "
            f"expected [:, {len(order)}]."
        )
    return data[:, order]


def require_lossless(cell_type: str, file_format: str) -> None:
    """Reject writing a high-order cell whose mid-side nodes would be dropped.

    Several formats can only store corner nodes (see their ``meshio_only``
    tables). Silently writing a linear cell while the caller asked for e.g. a
    ``hexahedron20`` misrepresents the element order, so this raises instead.
    """
    if is_high_order(cell_type):
        raise WriteError(
            f"{file_format} cannot store '{cell_type}' without dropping the "
            "mid-side nodes. Convert the cell to its linear form explicitly "
            "before writing."
        )


def node_order_trace(source: str, target: str, cell_type: str) -> dict:
    """JSON-serializable description of a node-order conversion."""
    order = node_order(source, target, cell_type)
    return {
        "cell_type": cell_type,
        "source": source,
        "target": target,
        "node_order": [int(x) for x in order],
        "num_nodes": int(len(order)),
    }
