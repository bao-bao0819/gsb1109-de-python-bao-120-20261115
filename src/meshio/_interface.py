"""Partition interface tables for (mixed) 3D meshes.

For every cell face of every 3D cell block, find all adjacent cells and record
the local face ids and the relative orientation of the face node orderings on
both sides. Faces shared by more than two cells (non-manifold faces) keep all
of their neighbors.

For higher-order cells, matching corner nodes alone does not imply that the
two sides are fully joined: the mid-edge/mid-face nodes have to agree as well.
Every interface entry therefore carries a `conforming` flag which is only True
if all sides reference exactly the same face nodes.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ._common import warn
from ._mesh import Mesh

# Local face definitions of 3D cell types in meshio's node ordering, see
# <https://github.com/nschloe/meshio/wiki/Node-ordering-in-cells>. All faces
# are oriented such that their normal points out of the cell.
#
# Conventions:
# - tetra: face k is opposite node k.
# - hexahedron: faces 0/1 are the -x/+x faces, 2/3 the -y/+y faces, 4/5 the
#   -z/+z faces (VTK face numbering).
# - wedge: faces 0/1 are the bottom/top triangles, 2-4 the quadrilateral
#   sides.
# - pyramid: face 0 is the quadrilateral base, 1-4 the triangular sides.
#
# For higher-order cells, the corner nodes come first, followed by the
# mid-edge nodes in the order in which the edges are traversed, followed by
# the mid-face node (if any).
_corner_faces = {
    "tetra": [
        (1, 2, 3),
        (0, 3, 2),
        (0, 1, 3),
        (0, 2, 1),
    ],
    "hexahedron": [
        (0, 4, 7, 3),
        (1, 2, 6, 5),
        (0, 1, 5, 4),
        (2, 3, 7, 6),
        (0, 3, 2, 1),
        (4, 5, 6, 7),
    ],
    "wedge": [
        (0, 2, 1),
        (3, 4, 5),
        (0, 1, 4, 3),
        (1, 2, 5, 4),
        (2, 0, 3, 5),
    ],
    "pyramid": [
        (0, 3, 2, 1),
        (0, 1, 4),
        (1, 2, 4),
        (2, 3, 4),
        (3, 0, 4),
    ],
}

# Full face node lists for higher-order cell types (corners first). The
# mid-edge/mid-face node positions follow from meshio's (= VTK's) node
# ordering:
# - tetra10 edges: (0,1), (1,2), (0,2), (0,3), (1,3), (2,3)
# - hexahedron20 edges: (0,1), (1,2), (2,3), (0,3), (4,5), (5,6), (6,7),
#   (4,7), (0,4), (1,5), (2,6), (3,7)
# - hexahedron27: as hexahedron20, plus face centers 20-25 for faces 0-5 and
#   the cell center 26
# - wedge15 edges: (0,1), (3,4), (1,2), (0,3), (2,5), (1,4), (0,2), (4,5),
#   (3,5)
# - wedge18: as wedge15, plus centers 15-17 of the quadrilateral faces 2-4
# - pyramid13 edges: (0,1), (1,2), (2,3), (0,3), (0,4), (1,4), (2,4), (3,4)
# - pyramid14: as pyramid13, plus the center 13 of the base face
_higher_order_faces = {
    "tetra10": [
        (1, 2, 3, 5, 9, 8),
        (0, 3, 2, 7, 9, 6),
        (0, 1, 3, 4, 8, 7),
        (0, 2, 1, 6, 5, 4),
    ],
    "hexahedron20": [
        (0, 4, 7, 3, 16, 15, 19, 11),
        (1, 2, 6, 5, 9, 18, 13, 17),
        (0, 1, 5, 4, 8, 17, 12, 16),
        (2, 3, 7, 6, 10, 19, 14, 18),
        (0, 3, 2, 1, 11, 10, 9, 8),
        (4, 5, 6, 7, 12, 13, 14, 15),
    ],
    "hexahedron27": [
        (0, 4, 7, 3, 16, 15, 19, 11, 20),
        (1, 2, 6, 5, 9, 18, 13, 17, 21),
        (0, 1, 5, 4, 8, 17, 12, 16, 22),
        (2, 3, 7, 6, 10, 19, 14, 18, 23),
        (0, 3, 2, 1, 11, 10, 9, 8, 24),
        (4, 5, 6, 7, 12, 13, 14, 15, 25),
    ],
    "wedge15": [
        (0, 2, 1, 12, 8, 6),
        (3, 4, 5, 7, 13, 14),
        (0, 1, 4, 3, 6, 11, 7, 9),
        (1, 2, 5, 4, 8, 10, 13, 11),
        (2, 0, 3, 5, 12, 9, 14, 10),
    ],
    "wedge18": [
        (0, 2, 1, 12, 8, 6),
        (3, 4, 5, 7, 13, 14),
        (0, 1, 4, 3, 6, 11, 7, 9, 15),
        (1, 2, 5, 4, 8, 10, 13, 11, 16),
        (2, 0, 3, 5, 12, 9, 14, 10, 17),
    ],
    "pyramid13": [
        (0, 3, 2, 1, 8, 7, 6, 5),
        (0, 1, 4, 5, 10, 9),
        (1, 2, 4, 6, 11, 10),
        (2, 3, 4, 7, 12, 11),
        (3, 0, 4, 8, 9, 12),
    ],
    "pyramid14": [
        (0, 3, 2, 1, 8, 7, 6, 5, 13),
        (0, 1, 4, 5, 10, 9),
        (1, 2, 4, 6, 11, 10),
        (2, 3, 4, 7, 12, 11),
        (3, 0, 4, 8, 9, 12),
    ],
}

# Map every supported cell type to a list of faces; each face is a tuple
# (corner local node ids, all local node ids).
_faces = {}
for _cell_type, _face_list in _corner_faces.items():
    _faces[_cell_type] = [(face, face) for face in _face_list]
for _cell_type, _face_list in _higher_order_faces.items():
    _base = _corner_faces[
        {
            "tetra10": "tetra",
            "hexahedron20": "hexahedron",
            "hexahedron27": "hexahedron",
            "wedge15": "wedge",
            "wedge18": "wedge",
            "pyramid13": "pyramid",
            "pyramid14": "pyramid",
        }[_cell_type]
    ]
    assert all(
        tuple(face[: len(corners)]) == corners
        for face, corners in zip(_face_list, _base)
    )
    _faces[_cell_type] = list(zip(_base, _face_list))


@dataclass
class InterfaceSide:
    """One side of an interface face."""

    block: int
    cell: int
    cell_type: str
    local_face: int
    # global corner node ids, in the order of the local face definition
    corners: tuple
    # all global face node ids (incl. higher-order nodes), in local order
    nodes: tuple
    partition: int | None = None


@dataclass
class InterfaceFace:
    """A face shared by two or more cells.

    `sides` holds all adjacent cells; for non-manifold faces it has more than
    two entries. `conforming` is only True if all sides reference exactly the
    same face nodes -- matching corner nodes alone are not sufficient for
    higher-order cells.
    """

    sides: list
    conforming: bool

    @property
    def non_manifold(self) -> bool:
        return len(self.sides) > 2

    def same_orientation(self, k: int) -> bool | None:
        """Compare the corner ordering of side `k` with side 0.

        Returns True if the two orderings are cyclic rotations of each other,
        False if one is the reversal of the other (as expected for the two
        sides of a conforming interface), and None if they cannot be matched.
        """
        return _relative_orientation(self.sides[0].corners, self.sides[k].corners)


def _relative_orientation(corners_a: tuple, corners_b: tuple) -> bool | None:
    n = len(corners_a)
    if n != len(corners_b) or n == 0:
        return None
    try:
        offset = corners_a.index(corners_b[0])
    except ValueError:
        return None
    if all(corners_a[(offset + i) % n] == corners_b[i] for i in range(n)):
        return True
    if all(corners_a[(offset - i) % n] == corners_b[i] for i in range(n)):
        return False
    return None


def build_interface_table(
    mesh: Mesh, partition_key: str | None = None
) -> list[InterfaceFace]:
    """Build the interface table of a (mixed) 3D mesh.

    Every entry of the returned list corresponds to one face shared by at
    least two cells and stores the local face ids of all adjacent cells along
    with their relative orientations. Non-manifold faces keep all of their
    neighbors.

    If `partition_key` is given, it must point to an integer cell-data array
    holding the partition id of every cell; only faces whose sides belong to
    at least two different partitions are then reported.
    """
    if partition_key is not None and partition_key not in mesh.cell_data:
        raise KeyError(f"mesh has no cell data '{partition_key}'")

    skipped_types = set()
    faces = {}
    for block_idx, cell_block in enumerate(mesh.cells):
        if cell_block.type not in _faces:
            skipped_types.add(cell_block.type)
            continue
        data = np.asarray(cell_block.data)
        partitions = None
        if partition_key is not None:
            partitions = np.asarray(mesh.cell_data[partition_key][block_idx])
        for cell_idx, cell in enumerate(data):
            partition = None if partitions is None else int(partitions[cell_idx])
            for local_face, (corner_ids, node_ids) in enumerate(
                _faces[cell_block.type]
            ):
                corners = tuple(int(cell[k]) for k in corner_ids)
                nodes = tuple(int(cell[k]) for k in node_ids)
                key = tuple(sorted(corners))
                side = InterfaceSide(
                    block=block_idx,
                    cell=cell_idx,
                    cell_type=cell_block.type,
                    local_face=local_face,
                    corners=corners,
                    nodes=nodes,
                    partition=partition,
                )
                faces.setdefault(key, []).append(side)

    if skipped_types:
        warn(
            "build_interface_table: skipping cell blocks of type(s) "
            + ", ".join(sorted(skipped_types))
        )

    table = []
    for sides in faces.values():
        if len(sides) < 2:
            continue
        if partition_key is not None:
            if len({side.partition for side in sides}) < 2:
                continue
        node_sets = {tuple(sorted(side.nodes)) for side in sides}
        table.append(InterfaceFace(sides=sides, conforming=len(node_sets) == 1))
    return table
