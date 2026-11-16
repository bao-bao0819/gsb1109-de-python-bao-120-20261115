import numpy as np
import pytest

import meshio
from meshio._interface import build_interface_table


def test_tetra_pair():
    # two tetrahedra sharing the face {1, 2, 3}
    points = [
        [0.0, 0.0, 0.0],
        [1.0, 0.0, 0.0],
        [0.0, 1.0, 0.0],
        [0.0, 0.0, 1.0],
        [1.0, 1.0, 1.0],
    ]
    cells = [("tetra", [[0, 1, 2, 3], [1, 2, 3, 4]])]
    mesh = meshio.Mesh(points, cells)

    table = build_interface_table(mesh)
    # exactly one shared face; boundary faces are not reported
    assert len(table) == 1
    entry = table[0]
    assert not entry.non_manifold
    assert entry.conforming

    side0, side1 = entry.sides
    assert (side0.block, side0.cell, side0.local_face) == (0, 0, 0)
    assert (side1.block, side1.cell, side1.local_face) == (0, 1, 3)
    assert side0.cell_type == side1.cell_type == "tetra"
    assert set(side0.corners) == set(side1.corners) == {1, 2, 3}
    # the two sides of a conforming interface see the face with opposite
    # orientation
    assert entry.same_orientation(1) is False


def test_mixed_tetra_hexahedron():
    points = [
        # hexahedron 0
        [0.0, 0.0, 0.0],
        [1.0, 0.0, 0.0],
        [1.0, 1.0, 0.0],
        [0.0, 1.0, 0.0],
        [0.0, 0.0, 1.0],
        [1.0, 0.0, 1.0],
        [1.0, 1.0, 1.0],
        [0.0, 1.0, 1.0],
        # hexahedron 1
        [2.0, 0.0, 0.0],
        [2.0, 1.0, 0.0],
        [2.0, 0.0, 1.0],
        [2.0, 1.0, 1.0],
        # tetrahedra
        [3.0, 0.0, 0.0],
        [4.0, 0.0, 0.0],
        [3.0, 1.0, 0.0],
        [3.0, 0.0, 1.0],
        [4.0, 1.0, 1.0],
    ]
    cells = [
        ("hexahedron", [[0, 1, 2, 3, 4, 5, 6, 7], [1, 8, 9, 2, 5, 10, 11, 6]]),
        ("tetra", [[12, 13, 14, 15], [13, 14, 15, 16]]),
    ]
    mesh = meshio.Mesh(points, cells)

    table = build_interface_table(mesh)
    assert len(table) == 2

    by_type = {entry.sides[0].cell_type: entry for entry in table}
    hex_entry = by_type["hexahedron"]
    assert [(s.cell, s.local_face) for s in hex_entry.sides] == [(0, 1), (1, 0)]
    assert set(hex_entry.sides[0].corners) == {1, 2, 5, 6}
    assert hex_entry.conforming
    assert hex_entry.same_orientation(1) is False

    tet_entry = by_type["tetra"]
    assert [(s.cell, s.local_face) for s in tet_entry.sides] == [(0, 0), (1, 3)]
    assert tet_entry.conforming


def test_non_manifold_keeps_all_neighbors():
    # three tetrahedra around the face {1, 2, 3}
    points = [
        [0.0, 0.0, 0.0],
        [1.0, 0.0, 0.0],
        [0.0, 1.0, 0.0],
        [0.0, 0.0, 1.0],
        [1.0, 1.0, 1.0],
        [1.0, 1.0, -1.0],
    ]
    cells = [("tetra", [[0, 1, 2, 3], [1, 2, 3, 4], [2, 1, 3, 5]])]
    mesh = meshio.Mesh(points, cells)

    table = build_interface_table(mesh)
    assert len(table) == 1
    entry = table[0]
    assert entry.non_manifold
    assert len(entry.sides) == 3
    assert {(s.cell, s.local_face) for s in entry.sides} == {
        (0, 0),
        (1, 3),
        (2, 3),
    }


def test_higher_order_mixed_order_not_conforming():
    # a linear tetrahedron and a quadratic tetrahedron sharing the corner
    # nodes {1, 2, 3}: the mid-edge nodes of the tetra10 have no counterpart,
    # so the interface must not be flagged as conforming
    points = [[0.0, 0.0, 0.0]] + [[float(k), 0.0, 0.0] for k in range(1, 11)]
    cells = [
        ("tetra", [[0, 1, 2, 3]]),
        ("tetra10", [[1, 2, 3, 4, 5, 6, 7, 8, 9, 10]]),
    ]
    mesh = meshio.Mesh(points, cells)

    table = build_interface_table(mesh)
    assert len(table) == 1
    entry = table[0]
    assert not entry.conforming
    assert {s.cell_type for s in entry.sides} == {"tetra", "tetra10"}
    # corner nodes still match topologically
    assert set(entry.sides[0].corners) == set(entry.sides[1].corners) == {1, 2, 3}


def test_higher_order_mismatched_mid_nodes_not_conforming():
    # two tetra10 with identical corner nodes but different mid-edge nodes on
    # the shared face
    points = [[float(k), 0.0, 0.0] for k in range(18)]
    cells = [
        (
            "tetra10",
            [
                [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
                [2, 1, 3, 11, 12, 13, 14, 15, 16, 17],
            ],
        )
    ]
    mesh = meshio.Mesh(points, cells)

    table = build_interface_table(mesh)
    assert len(table) == 1
    entry = table[0]
    assert not entry.conforming
    assert set(entry.sides[0].corners) == set(entry.sides[1].corners) == {1, 2, 3}


def test_higher_order_conforming():
    # two tetra10 sharing all six nodes of the face {1, 2, 3, 5, 6, 7}
    points = [[float(k), 0.0, 0.0] for k in range(15)]
    cells = [
        (
            "tetra10",
            [
                [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
                [2, 1, 3, 11, 5, 7, 6, 12, 13, 14],
            ],
        )
    ]
    mesh = meshio.Mesh(points, cells)

    table = build_interface_table(mesh)
    assert len(table) == 1
    entry = table[0]
    assert entry.conforming
    assert entry.same_orientation(1) is False


def test_partition_key():
    points = [
        [0.0, 0.0, 0.0],
        [1.0, 0.0, 0.0],
        [1.0, 1.0, 0.0],
        [0.0, 1.0, 0.0],
        [0.0, 0.0, 1.0],
        [1.0, 0.0, 1.0],
        [1.0, 1.0, 1.0],
        [0.0, 1.0, 1.0],
        [2.0, 0.0, 0.0],
        [2.0, 1.0, 0.0],
        [2.0, 0.0, 1.0],
        [2.0, 1.0, 1.0],
        [3.0, 0.0, 0.0],
        [4.0, 0.0, 0.0],
        [3.0, 1.0, 0.0],
        [3.0, 0.0, 1.0],
        [4.0, 1.0, 1.0],
    ]
    cells = [
        ("hexahedron", [[0, 1, 2, 3, 4, 5, 6, 7], [1, 8, 9, 2, 5, 10, 11, 6]]),
        ("tetra", [[12, 13, 14, 15], [13, 14, 15, 16]]),
    ]
    # the hexes live in different partitions, the tets in the same one
    cell_data = {"partition": [np.array([0, 1]), np.array([1, 1])]}
    mesh = meshio.Mesh(points, cells, cell_data=cell_data)

    table = build_interface_table(mesh, partition_key="partition")
    assert len(table) == 1
    entry = table[0]
    assert entry.sides[0].cell_type == "hexahedron"
    assert [s.partition for s in entry.sides] == [0, 1]

    # without a partition key, both shared faces are reported
    assert len(build_interface_table(mesh)) == 2

    with pytest.raises(KeyError):
        build_interface_table(mesh, partition_key="no_such_data")
