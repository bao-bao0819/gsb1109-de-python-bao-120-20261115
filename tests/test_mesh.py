import copy

import numpy as np
import pytest
from numpy.testing import assert_equal

import meshio

from . import helpers


def test_cells_dict():
    mesh = copy.deepcopy(helpers.tri_mesh)
    assert len(mesh.cells_dict) == 1
    assert np.array_equal(mesh.cells_dict["triangle"], [[0, 1, 2], [0, 2, 3]])

    # two cells groups
    mesh = meshio.Mesh(
        [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [1.0, 1.0, 0.0], [0.0, 1.0, 0.0]],
        [("triangle", [[0, 1, 2]]), ("triangle", [[0, 2, 3]])],
        cell_data={"a": [[0.5], [1.3]]},
    )
    assert len(mesh.cells_dict) == 1
    assert_equal(mesh.cells_dict, {"triangle": [[0, 1, 2], [0, 2, 3]]})
    assert_equal(mesh.cell_data_dict, {"a": {"triangle": [0.5, 1.3]}})


def test_merge_cell_blocks():
    mesh = meshio.Mesh(
        [[0.0, 0.0], [1.0, 0.0], [0.0, 1.0], [1.0, 1.0], [2.0, 0.0]],
        [
            ("triangle", [[0, 1, 2]]),
            ("quad", [[0, 1, 3, 2]]),
            ("triangle", [[1, 4, 3]]),
        ],
        cell_data={
            "a": [np.array([1.0]), np.array([5.0]), np.array([2.0])],
            "id": [
                np.array([10], dtype=int),
                np.array([20], dtype=int),
                np.array([30], dtype=int),
            ],
        },
        cell_sets={
            "s": [np.array([0]), np.array([0]), np.array([0])],
            "missing": [np.array([0]), None, np.array([])],
        },
    )

    mapping = mesh.merge_cell_blocks()

    assert [(c.type, len(c)) for c in mesh.cells] == [("triangle", 2), ("quad", 1)]
    assert mapping.block_map == [[0, 2], [1]]
    assert_equal(mapping.row_map[0], [[0, 0], [2, 0]])
    assert_equal(mapping.row_map[1], [[1, 0]])

    assert_equal(mesh.cell_data["a"][0], [1.0, 2.0])
    assert_equal(mesh.cell_data["a"][1], [5.0])
    assert_equal(mesh.cell_data["id"][0], [10, 30])
    assert_equal(mesh.cell_sets["s"][0], [0, 1])
    assert_equal(mesh.cell_sets["s"][1], [0])
    # A set that is absent on one block only contains the remaining rows.
    assert_equal(mesh.cell_sets["missing"][0], [0])
    assert_equal(mesh.cell_sets["missing"][1], [])

    # Original cell 2 is the quad (block 1); original cell 1 is block 2.
    assert mapping.merged_cell_index(0, 0) == 0
    assert mapping.merged_cell_index(2, 0) == 1
    assert mapping.merged_cell_index(1, 0) == 2
    assert_equal(mapping.merged_cell_index_map, [0, 2, 1])
    assert_equal(mapping.original_cell_index_map, [0, 2, 1])
    merged_block, merged_rows = mapping[2]
    assert merged_block == 0
    assert_equal(merged_rows, [1])

    mesh.split_cell_blocks()

    assert [c.type for c in mesh.cells] == ["triangle", "quad", "triangle"]
    assert_equal(mesh.cells[0].data, [[0, 1, 2]])
    assert_equal(mesh.cells[1].data, [[0, 1, 3, 2]])
    assert_equal(mesh.cells[2].data, [[1, 4, 3]])
    assert_equal(mesh.cell_data["a"], [np.array([1.0]), np.array([5.0]), np.array([2.0])])
    assert_equal(mesh.cell_data["id"], [np.array([10]), np.array([20]), np.array([30])])
    assert_equal(mesh.cell_sets["s"], [np.array([0]), np.array([0]), np.array([0])])
    assert mesh.cell_sets["missing"][0] is not None
    assert mesh.cell_sets["missing"][1] is None
    assert mesh.cell_sets["missing"][2] is not None


def test_merge_cell_blocks_missing_data_is_not_zero():
    mesh = meshio.Mesh(
        [[0.0, 0.0], [1.0, 0.0], [0.0, 1.0], [1.0, 1.0], [2.0, 0.0]],
        [
            ("triangle", [[0, 1, 2]]),
            ("triangle", [[1, 4, 3]]),
        ],
        cell_data={
            "float": [np.array([1.0]), None],
            "int": [np.array([7], dtype=int), None],
        },
    )

    mesh.merge_cell_blocks()

    merged_float = mesh.cell_data["float"][0]
    assert merged_float[0] == 1.0
    assert np.isnan(merged_float[1])
    assert merged_float[1] != 0.0

    merged_int = mesh.cell_data["int"][0]
    assert isinstance(merged_int, np.ma.MaskedArray)
    assert merged_int[0] == 7
    assert merged_int.mask[1]

    mesh.split_cell_blocks()

    assert mesh.cell_data["float"][1] is None
    assert mesh.cell_data["int"][1] is None
    assert_equal(mesh.cell_data["float"][0], [1.0])
    assert_equal(mesh.cell_data["int"][0], [7])


def test_split_cell_blocks_without_merge():
    mesh = copy.deepcopy(helpers.tri_mesh)
    cells_before = [c.data.copy() for c in mesh.cells]
    mesh.split_cell_blocks()
    assert [c.type for c in mesh.cells] == ["triangle"]
    assert_equal(mesh.cells[0].data, cells_before[0])


def test_sets_to_int_data():
    mesh = helpers.tri_mesh_5
    mesh = helpers.add_point_sets(mesh)
    mesh = helpers.add_cell_sets(mesh)

    mesh.point_sets_to_data()
    mesh.cell_sets_to_data()

    assert mesh.cell_sets == {}
    assert_equal(mesh.cell_data, {"grain0-grain1": [[0, 0, 1, 1, 1]]})

    assert mesh.point_sets == {}
    assert_equal(mesh.point_data, {"fixed-loose": [0, 0, 0, 1, 1, 1, 1]})

    # now back to set data
    mesh.cell_data_to_sets("grain0-grain1")
    mesh.point_data_to_sets("fixed-loose")

    assert mesh.cell_data == {}
    assert_equal(mesh.cell_sets, {"grain0": [[0, 1]], "grain1": [[2, 3, 4]]})

    assert mesh.point_data == {}
    assert_equal(mesh.point_sets, {"fixed": [0, 1, 2], "loose": [3, 4, 5, 6]})


@pytest.mark.skip
def test_sets_to_int_data_warning():
    mesh = meshio.Mesh(
        [[0.0, 0.0], [0.0, 1.0], [1.0, 0.0], [1.0, 1.0]],
        {"triangle": [[0, 1, 2], [1, 2, 3]]},
        cell_sets={"tag": [[0]]},
    )
    with pytest.warns(UserWarning):
        mesh.cell_sets_to_data()
    assert np.all(mesh.cell_data["tag"] == np.array([[0, -1]]))

    mesh = meshio.Mesh(
        [[0.0, 0.0], [0.0, 1.0], [1.0, 0.0], [1.0, 1.0]],
        {"triangle": [[0, 1, 2], [1, 2, 3]]},
        point_sets={"tag": [[0, 1, 3]]},
    )
    with pytest.warns(UserWarning):
        mesh.point_sets_to_data()

    assert np.all(mesh.point_data["tag"] == np.array([[0, 0, -1, 0]]))


def test_int_data_to_sets():
    mesh = helpers.tri_mesh
    mesh.cell_data = {"grain0-grain1": [np.array([0, 1])]}

    mesh.cell_data_to_sets("grain0-grain1")

    assert_equal(mesh.cell_sets, {"grain0": [[0]], "grain1": [[1]]})


def test_gh_1165():
    mesh = meshio.Mesh(
        [[0.0, 0.0], [1.0, 0.0], [0.0, 1.0], [1.0, 1.0]],
        {
            "triangle": [[0, 1, 2], [1, 2, 3]],
            "line": [[0, 1], [0, 2], [1, 3], [2, 3]],
        },
        cell_sets={
            "test": [[], [1]],
            "sets": [[0, 1], [0, 2, 3]],
        },
    )

    mesh.cell_sets_to_data()
    mesh.cell_data_to_sets("test-sets")

    assert_equal(mesh.cell_sets, {"test": [[], [1]], "sets": [[0, 1], [0, 2, 3]]})


def test_copy():
    mesh = helpers.tri_mesh
    mesh2 = mesh.copy()

    assert np.all(mesh.points == mesh2.points)
    assert not np.may_share_memory(mesh.points, mesh2.points)
