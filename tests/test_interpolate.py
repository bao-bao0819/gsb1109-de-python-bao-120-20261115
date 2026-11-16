import numpy as np
import pytest

import meshio
from meshio import InterpolationError, interpolate_frame


def _frame(temp_values, mat_ids, points=None, cells=None):
    if points is None:
        points = [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [1.0, 1.0, 0.0], [0.0, 1.0, 0.0]]
    if cells is None:
        cells = [("triangle", [[0, 1, 2], [0, 2, 3]])]
    return meshio.Mesh(
        points,
        cells,
        point_data={"temperature": temp_values, "material": mat_ids},
        cell_data={"mat_cell": [np.array([7, 7])]},
    )


def test_basic_midpoint():
    mesh0 = _frame([0.0, 10.0, 20.0, 30.0], [1, 1, 2, 2])
    mesh1 = _frame([10.0, 20.0, 40.0, 50.0], [1, 1, 2, 2])
    out = interpolate_frame(mesh0, mesh1, 0.0, 2.0, 1.0)
    assert np.allclose(
        out.point_data["temperature"], [5.0, 15.0, 30.0, 40.0]
    )
    # discrete data identical in both frames is carried over
    assert np.array_equal(out.point_data["material"], [1, 1, 2, 2])
    assert np.array_equal(out.cell_data["mat_cell"][0], [7, 7])
    assert np.allclose(out.points, mesh0.points)


def test_weights_and_provenance():
    mesh0 = _frame([0.0, 0.0, 0.0, 0.0], [1, 1, 2, 2])
    mesh1 = _frame([8.0, 8.0, 8.0, 8.0], [1, 1, 2, 2])
    out = interpolate_frame(mesh0, mesh1, 1.0, 3.0, 1.5)
    assert np.allclose(out.point_data["temperature"], 2.0)
    # provenance: sources and weights recorded, marked as interpolated
    assert np.array_equal(out.field_data["interpolation:is_interpolated"], [1])
    assert np.array_equal(out.field_data["interpolation:source_times"], [1.0, 3.0])
    assert np.allclose(out.field_data["interpolation:weights"], [0.75, 0.25])
    assert np.array_equal(out.field_data["interpolation:target_time"], [1.5])


def test_reverse_time_order():
    mesh0 = _frame([0.0, 0.0, 0.0, 0.0], [1, 1, 2, 2])
    mesh1 = _frame([8.0, 8.0, 8.0, 8.0], [1, 1, 2, 2])
    out = interpolate_frame(mesh1, mesh0, 3.0, 1.0, 2.0)
    assert np.allclose(out.point_data["temperature"], 4.0)


def test_extrapolation_refused():
    mesh0 = _frame([0.0, 0.0, 0.0, 0.0], [1, 1, 2, 2])
    mesh1 = _frame([1.0, 1.0, 1.0, 1.0], [1, 1, 2, 2])
    with pytest.raises(InterpolationError):
        interpolate_frame(mesh0, mesh1, 0.0, 1.0, 1.5)
    with pytest.raises(InterpolationError):
        interpolate_frame(mesh0, mesh1, 0.0, 0.0, 0.0)


def test_same_count_but_mapping_unclear_refused():
    mesh0 = _frame([0.0, 0.0, 0.0, 0.0], [1, 1, 2, 2])
    moved = [[0.0, 0.0, 0.0], [2.0, 0.0, 0.0], [1.0, 1.0, 0.0], [0.0, 1.0, 0.0]]
    mesh1 = _frame([1.0, 1.0, 1.0, 1.0], [1, 1, 2, 2], points=moved)
    with pytest.raises(InterpolationError, match="mapping is unclear"):
        interpolate_frame(mesh0, mesh1, 0.0, 1.0, 0.5)


def test_point_count_mismatch_refused_without_ids():
    mesh0 = _frame([0.0, 0.0, 0.0, 0.0], [1, 1, 2, 2])
    mesh1 = meshio.Mesh(
        [[0.0, 0.0, 0.0]] * 3,
        [("triangle", [[0, 1, 2]])],
        point_data={"temperature": [1.0, 1.0, 1.0], "material": [1, 1, 2]},
    )
    with pytest.raises(InterpolationError, match="point counts differ"):
        interpolate_frame(mesh0, mesh1, 0.0, 1.0, 0.5)


def test_dimension_mismatch_refused():
    mesh0 = _frame([0.0, 0.0, 0.0, 0.0], [1, 1, 2, 2])
    mesh1 = _frame(
        [1.0, 1.0, 1.0, 1.0], [1, 1, 2, 2],
        points=[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]],
    )
    with pytest.raises(InterpolationError, match="spatial dimension"):
        interpolate_frame(mesh0, mesh1, 0.0, 1.0, 0.5)


def test_topology_mismatch_refused():
    mesh0 = _frame([0.0, 0.0, 0.0, 0.0], [1, 1, 2, 2])
    mesh1 = _frame(
        [1.0, 1.0, 1.0, 1.0], [1, 1, 2, 2],
        cells=[("triangle", [[0, 1, 2], [1, 2, 3]])],
    )
    with pytest.raises(InterpolationError, match="topology"):
        interpolate_frame(mesh0, mesh1, 0.0, 1.0, 0.5)


def test_discrete_data_not_interpolated():
    mesh0 = _frame([0.0, 0.0, 0.0, 0.0], [1, 1, 2, 2])
    mesh1 = _frame([1.0, 1.0, 1.0, 1.0], [1, 2, 2, 2])
    out = interpolate_frame(mesh0, mesh1, 0.0, 1.0, 0.5)
    # differing material ids: dropped, not averaged to 1.5
    assert "material" not in out.point_data
    assert np.allclose(out.point_data["temperature"], 0.5)


def test_discrete_keys_for_float_arrays():
    mesh0 = _frame([0.0, 0.0, 0.0, 0.0], [1, 1, 2, 2])
    mesh1 = _frame([1.0, 1.0, 1.0, 1.0], [1, 1, 2, 2])
    out = interpolate_frame(
        mesh0, mesh1, 0.0, 1.0, 0.5, discrete_keys={"temperature"}
    )
    assert "temperature" not in out.point_data


def test_field_only_in_one_frame_dropped():
    mesh0 = _frame([0.0, 0.0, 0.0, 0.0], [1, 1, 2, 2])
    mesh1 = _frame([1.0, 1.0, 1.0, 1.0], [1, 1, 2, 2])
    mesh1.point_data["extra"] = [5.0, 5.0, 5.0, 5.0]
    out = interpolate_frame(mesh0, mesh1, 0.0, 1.0, 0.5)
    assert "extra" not in out.point_data


def test_nan_restricts_to_common_valid_region():
    mesh0 = _frame([0.0, np.nan, 0.0, 0.0], [1, 1, 2, 2])
    mesh1 = _frame([2.0, 2.0, np.nan, 2.0], [1, 1, 2, 2])
    out = interpolate_frame(mesh0, mesh1, 0.0, 1.0, 0.5)
    temp = out.point_data["temperature"]
    assert temp[0] == 1.0
    assert np.isnan(temp[1])
    assert np.isnan(temp[2])
    assert temp[3] == 1.0


def test_point_id_mapping_reordered():
    points = [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [1.0, 1.0, 0.0], [0.0, 1.0, 0.0]]
    cells = [("triangle", [[0, 1, 2], [0, 2, 3]])]
    mesh0 = _frame([0.0, 10.0, 20.0, 30.0], [1, 1, 2, 2], points, cells)
    mesh0.point_data["pid"] = [10, 11, 12, 13]
    # same mesh, points and cells reordered
    perm = [3, 2, 1, 0]
    mesh1 = meshio.Mesh(
        [points[i] for i in perm],
        [("triangle", [[3, 2, 1], [3, 1, 0]])],
        point_data={
            "temperature": [60.0, 40.0, 20.0, 0.0],
            "material": [2, 2, 1, 1],
            "pid": [13, 12, 11, 10],
        },
        cell_data={"mat_cell": [np.array([7, 7])]},
    )
    out = interpolate_frame(mesh0, mesh1, 0.0, 2.0, 1.0, point_id_key="pid")
    # output follows mesh0's point order
    assert np.array_equal(out.point_data["pid"], [10, 11, 12, 13])
    assert np.allclose(out.point_data["temperature"], [0.0, 15.0, 30.0, 45.0])
    assert np.array_equal(out.point_data["material"], [1, 1, 2, 2])
    assert np.allclose(out.points, points)


def test_point_id_common_region_only():
    points = [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [1.0, 1.0, 0.0], [0.0, 1.0, 0.0]]
    mesh0 = _frame([0.0, 10.0, 20.0, 30.0], [1, 1, 2, 2], points)
    mesh0.point_data["pid"] = [10, 11, 12, 13]
    # after frame: point 13 replaced by 14; second triangle references it
    mesh1 = meshio.Mesh(
        points,
        [("triangle", [[0, 1, 2], [0, 2, 3]])],
        point_data={
            "temperature": [8.0, 18.0, 28.0, 99.0],
            "material": [1, 1, 2, 2],
            "pid": [10, 11, 12, 14],
        },
        cell_data={"mat_cell": [np.array([7, 7])]},
    )
    out = interpolate_frame(mesh0, mesh1, 0.0, 2.0, 1.0, point_id_key="pid")
    # only the 3 common points and the 1 common cell survive
    assert np.array_equal(out.point_data["pid"], [10, 11, 12])
    assert np.allclose(out.point_data["temperature"], [4.0, 14.0, 24.0])
    assert len(out.cells) == 1
    assert np.array_equal(out.cells[0].data, [[0, 1, 2]])
    assert np.array_equal(out.cell_data["mat_cell"][0], [7])


def test_no_common_point_ids_refused():
    mesh0 = _frame([0.0, 0.0, 0.0, 0.0], [1, 1, 2, 2])
    mesh0.point_data["pid"] = [0, 1, 2, 3]
    mesh1 = _frame([1.0, 1.0, 1.0, 1.0], [1, 1, 2, 2])
    mesh1.point_data["pid"] = [4, 5, 6, 7]
    with pytest.raises(InterpolationError, match="no common point ids"):
        interpolate_frame(mesh0, mesh1, 0.0, 1.0, 0.5, point_id_key="pid")


def test_field_dimension_mismatch_refused():
    mesh0 = _frame([0.0, 0.0, 0.0, 0.0], [1, 1, 2, 2])
    mesh1 = _frame([1.0, 1.0, 1.0, 1.0], [1, 1, 2, 2])
    mesh1.point_data["temperature"] = np.ones((4, 3))
    with pytest.raises(InterpolationError, match="inconsistent dimensions"):
        interpolate_frame(mesh0, mesh1, 0.0, 1.0, 0.5)


def test_vector_field_interpolated():
    mesh0 = _frame([0.0, 0.0, 0.0, 0.0], [1, 1, 2, 2])
    mesh1 = _frame([1.0, 1.0, 1.0, 1.0], [1, 1, 2, 2])
    mesh0.point_data["u"] = np.zeros((4, 3))
    mesh1.point_data["u"] = np.ones((4, 3)) * 4.0
    out = interpolate_frame(mesh0, mesh1, 0.0, 4.0, 3.0)
    assert np.allclose(out.point_data["u"], 3.0)
