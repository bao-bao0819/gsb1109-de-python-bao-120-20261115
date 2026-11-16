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


def _overlapping_sets_mesh():
    return meshio.Mesh(
        [[0.0, 0.0], [1.0, 0.0], [0.0, 1.0], [1.0, 1.0], [2.0, 0.0]],
        {"triangle": [[0, 1, 2], [1, 2, 3], [1, 3, 4]]},
        cell_sets={
            "grain0": [[0, 1]],
            "grain1": [[1, 2]],
            "empty": [[]],
        },
    )


def test_sets_to_data_overlap_no_priority():
    mesh = _overlapping_sets_mesh()
    with pytest.raises(ValueError, match="grain0.*grain1"):
        mesh.cell_sets_to_data()


def test_sets_to_data_overlap_priority():
    mesh = _overlapping_sets_mesh()
    mesh.cell_sets_to_data(priority=["grain1", "grain0", "empty"])

    assert mesh.cell_sets == {}
    # cell 1 belongs to grain0 and grain1; grain1 has higher priority
    assert_equal(mesh.cell_data["grain0-grain1-empty"], [[0, 1, 1]])

    # the side table restores all membership relations
    assert "grain0-grain1-empty" in mesh.cell_sets_side_table
    mesh.cell_sets_from_side_table()
    assert_equal(
        mesh.cell_sets,
        {"grain0": [[0, 1]], "grain1": [[1, 2]], "empty": [[]]},
    )


def test_sets_to_data_priority_unresolved():
    mesh = _overlapping_sets_mesh()
    # neither grain0 nor grain1 is covered by the rules, so the conflict
    # between them remains unresolved
    with pytest.raises(ValueError, match="do not resolve"):
        mesh.cell_sets_to_data(priority=["empty"])


def test_sets_to_data_priority_nonexistent_set():
    mesh = _overlapping_sets_mesh()
    with pytest.raises(ValueError, match="non-existent cell set.*typo"):
        mesh.cell_sets_to_data(priority=["grain1", "grain0", "typo"])

    # an empty set, on the other hand, is a valid priority entry
    mesh = _overlapping_sets_mesh()
    mesh.cell_sets_to_data(priority=["empty", "grain1", "grain0"])
    assert_equal(mesh.cell_data["grain0-grain1-empty"], [[0, 1, 1]])


def test_sets_to_data_priority_duplicate():
    mesh = _overlapping_sets_mesh()
    with pytest.raises(ValueError, match="duplicate"):
        mesh.cell_sets_to_data(priority=["grain0", "grain1", "grain0"])


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
