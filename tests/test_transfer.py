import itertools

import numpy as np
import pytest
from numpy.testing import assert_allclose, assert_array_equal

import meshio


def triangle_mesh(points, cells, **kwargs):
    return meshio.Mesh(points, [("triangle", cells)], **kwargs)


def tetra_mesh(points, cells, **kwargs):
    return meshio.Mesh(points, [("tetra", cells)], **kwargs)


def test_triangle_areas_and_full_cover():
    source = triangle_mesh(
        [[0.0, 0.0], [2.0, 0.0], [0.0, 2.0]],
        [[0, 1, 2]],
        cell_data={"q": [[2.0]], "T": [[300.0]]},
    )
    # Subdivision of the source triangle into four equal triangles.
    target = triangle_mesh(
        [[0.0, 0.0], [1.0, 0.0], [0.0, 1.0], [1.0, 1.0], [0.0, 2.0], [2.0, 0.0]],
        [[0, 1, 2], [1, 3, 2], [2, 3, 4], [1, 5, 3]],
    )

    result = meshio.transfer_cell_data(
        source, target, cell_types=["triangle"], extensive=["q"], intensive=["T"]
    )

    assert_allclose(result.target_coverage[0], np.ones(4))
    assert result.uncovered_targets[0].size == 0
    assert_allclose(result["q"][0], np.full(4, 0.5))
    assert_allclose(sum(result["q"][0]), 2.0)
    assert_allclose(result.unallocated["q"][0], [0.0])
    assert_allclose(result["T"][0], np.full(4, 300.0))


def test_partial_source_coverage_is_not_renormalized():
    source = triangle_mesh(
        [[0.0, 0.0], [2.0, 0.0], [0.0, 2.0]],
        [[0, 1, 2]],
        cell_data={"q": [[2.0]]},
    )
    target = triangle_mesh([[0.0, 0.0], [1.0, 0.0], [0.0, 1.0]], [[0, 1, 2]])

    result = meshio.transfer_cell_data(source, target, extensive="q")

    # The target covers only a quarter of the source area.
    assert_allclose(result["q"][0], [0.5])
    assert_allclose(result.allocated["q"][0], [0.5])
    assert_allclose(result.unallocated["q"][0], [1.5])
    assert_allclose(sum(result["q"][0]) + sum(result.unallocated["q"][0]), 2.0)
    assert_allclose(result.target_coverage[0], [1.0])


def test_target_sticking_out_reports_partial_coverage():
    source = triangle_mesh(
        [[0.0, 0.0], [2.0, 0.0], [0.0, 2.0]],
        [[0, 1, 2]],
        cell_data={"q": [[2.0]]},
    )
    target = triangle_mesh([[0.0, 0.0], [3.0, 0.0], [0.0, 1.0]], [[0, 1, 2]])

    result = meshio.transfer_cell_data(source, target, extensive="q")

    # Source area is 2, target area is 1.5, intersection area is 1.25.
    assert_allclose(result["q"][0], [1.25])
    assert_allclose(result.unallocated["q"][0], [0.75])
    assert_allclose(result.target_coverage[0], [1.25 / 1.5])
    assert_array_equal(result.partially_covered_targets[0], [0])
    assert_array_equal(result.uncovered_targets[0], [])


def test_disjoint_target_is_listed_separately():
    source = triangle_mesh(
        [[0.0, 0.0], [1.0, 0.0], [0.0, 1.0]],
        [[0, 1, 2]],
        cell_data={"q": [[2.0]], "T": [[300.0]]},
    )
    target = triangle_mesh([[5.0, 5.0], [6.0, 5.0], [5.0, 6.0]], [[0, 1, 2]])

    result = meshio.transfer_cell_data(
        source, target, extensive="q", intensive="T"
    )

    assert_allclose(result["q"][0], [0.0])
    assert_allclose(result.unallocated["q"][0], [2.0])
    assert_allclose(result.target_coverage[0], [0.0])
    assert_array_equal(result.uncovered_targets[0], [0])
    assert np.isnan(result["T"][0]).all()


def test_quad_split_into_triangles():
    source = meshio.Mesh(
        [[0.0, 0.0], [2.0, 0.0], [2.0, 2.0], [0.0, 2.0]],
        [("quad", [[0, 1, 2, 3]])],
        cell_data={"volume": [[4.0]]},
    )
    target = triangle_mesh(
        [[0.0, 0.0], [2.0, 0.0], [2.0, 2.0], [0.0, 2.0]],
        [[0, 1, 2], [0, 2, 3]],
    )

    result = meshio.transfer_cell_data(
        source, target, cell_types=["quad", "triangle"], extensive="volume"
    )

    assert_allclose(result["volume"][0], [2.0, 2.0])
    assert_allclose(sum(result["volume"][0]), 4.0)


def test_tetrahedron_volume():
    source = tetra_mesh(
        [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
        [[0, 1, 2, 3]],
        cell_data={"q": [[1.0]]},
    )
    smaller = np.array(
        [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 0.5]]
    )
    target = tetra_mesh(smaller, [[0, 1, 2, 3]])

    result = meshio.transfer_cell_data(source, target, extensive="q")

    assert_allclose(result["q"][0], [0.5], rtol=1.0e-12)
    assert_allclose(result.unallocated["q"][0], [0.5], rtol=1.0e-12)
    assert_allclose(result.target_coverage[0], [1.0])


def test_hexahedron_subdivision_conserves_vectors():
    cube = np.array(list(itertools.product([0.0, 1.0], repeat=3)))
    hexahedron = [[0, 1, 3, 2, 4, 5, 7, 6]]

    points = []
    index = {}

    def point_id(point):
        key = tuple(np.round(point, 12))
        if key not in index:
            index[key] = len(points)
            points.append(key)
        return index[key]

    cells = []
    for start in itertools.product([0.0, 0.5], repeat=3):
        low = np.array(start)
        high = low + 0.5
        cells.append(
            [
                point_id([low[0], low[1], low[2]]),
                point_id([high[0], low[1], low[2]]),
                point_id([high[0], high[1], low[2]]),
                point_id([low[0], high[1], low[2]]),
                point_id([low[0], low[1], high[2]]),
                point_id([high[0], low[1], high[2]]),
                point_id([high[0], high[1], high[2]]),
                point_id([low[0], high[1], high[2]]),
            ]
        )

    source = meshio.Mesh(
        cube,
        [("hexahedron", hexahedron)],
        cell_data={"q": [[8.0]], "momentum": [[[1.0, 2.0, 3.0]]]},
    )
    target = meshio.Mesh(np.array(points), [("hexahedron", cells)])

    result = meshio.transfer_cell_data(
        source, target, extensive=["q", "momentum"]
    )

    assert_allclose(result["q"][0], np.ones(8))
    assert_allclose(result["momentum"][0], np.tile([1.0, 2.0, 3.0], (8, 1)) / 8)
    assert_allclose(result["momentum"][0].sum(axis=0), [1.0, 2.0, 3.0])
    assert_allclose(result["q"][0].sum(), 8.0)
    assert_allclose(result.target_coverage[0], np.ones(8))


def test_intensive_uses_given_weights():
    source = triangle_mesh(
        [[0.0, 0.0], [1.0, 0.0], [0.0, 1.0]],
        [[0, 1, 2]],
        cell_data={"T": [[300.0]]},
    )
    target = triangle_mesh(
        [[0.0, 0.0], [1.0, 0.0], [0.0, 1.0]], [[0, 1, 2]]
    )

    for weights in (2.0, np.array([2.0]), [[2.0]], {"T": 2.0}, {"T": [2.0]}):
        result = meshio.transfer_cell_data(
            source, target, intensive="T", weights=weights
        )
        assert_allclose(result["T"][0], [300.0])


def test_line_cells():
    source = meshio.Mesh(
        [[0.0, 0.0], [2.0, 0.0]],
        [("line", [[0, 1]])],
        cell_data={"q": [[3.0]]},
    )
    target = meshio.Mesh(
        [[0.0, 0.0], [0.5, 0.0], [2.0, 0.0]],
        [("line", [[0, 1], [1, 2]])],
    )

    result = meshio.transfer_cell_data(
        source, target, cell_types=["line"], extensive="q"
    )

    assert_allclose(result["q"][0], [0.75, 2.25])
    assert_allclose(result.target_coverage[0], [1.0, 1.0])


def test_planar_cells_embedded_in_3d():
    points = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 1.0]])
    source = triangle_mesh(points, [[0, 1, 2]], cell_data={"q": [[1.0]]})
    midpoint = 0.5 * (points[1] + points[2])
    target = triangle_mesh(
        np.vstack([points, midpoint]), [[0, 1, 3], [0, 3, 2]]
    )

    result = meshio.transfer_cell_data(source, target, extensive="q")

    assert_allclose(result["q"][0], [0.5, 0.5])
    assert_allclose(result.target_coverage[0], [1.0, 1.0])


def test_planar_cells_on_different_planes_do_not_overlap():
    source = triangle_mesh(
        [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
        [[0, 1, 2]],
        cell_data={"q": [[1.0]]},
    )
    target = triangle_mesh(
        [[0.0, 0.0, 1.0], [1.0, 0.0, 1.0], [0.0, 1.0, 1.0]],
        [[0, 1, 2]],
    )

    result = meshio.transfer_cell_data(source, target, extensive="q")

    assert_allclose(result["q"][0], [0.0])
    assert_allclose(result.unallocated["q"][0], [1.0])
    assert_array_equal(result.uncovered_targets[0], [0])


def test_caller_limits_cell_types():
    source = meshio.Mesh(
        [[0.0, 0.0], [2.0, 0.0], [2.0, 1.0], [0.0, 1.0]],
        [("quad", [[0, 1, 2, 3]])],
        cell_data={"q": [[1.0]]},
    )
    target = triangle_mesh(
        [[0.0, 0.0], [2.0, 0.0], [2.0, 1.0], [0.0, 1.0]],
        [[0, 1, 2], [0, 2, 3]],
    )

    with pytest.raises(ValueError, match="quad"):
        meshio.transfer_cell_data(source, target, cell_types=["triangle"], extensive="q")

    with pytest.raises(ValueError, match="Unsupported cell_types"):
        meshio.transfer_cell_data(
            source, target, cell_types=["hexahedron27"], extensive="q"
        )


def test_target_cells_overlapping_each_other_are_rejected():
    source = triangle_mesh(
        [[0.0, 0.0], [2.0, 0.0], [0.0, 2.0]],
        [[0, 1, 2]],
        cell_data={"q": [[1.0]]},
    )
    target = triangle_mesh(
        [[0.0, 0.0], [1.0, 0.0], [0.0, 1.0]],
        [[0, 1, 2], [0, 1, 2]],
    )

    with pytest.raises(ValueError, match="overlap each other"):
        meshio.transfer_cell_data(source, target, extensive="q")


def test_missing_field_and_conflicting_kind():
    source = triangle_mesh(
        [[0.0, 0.0], [1.0, 0.0], [0.0, 1.0]],
        [[0, 1, 2]],
        cell_data={"q": [[1.0]]},
    )
    target = triangle_mesh(
        [[0.0, 0.0], [1.0, 0.0], [0.0, 1.0]], [[0, 1, 2]]
    )

    with pytest.raises(KeyError):
        meshio.transfer_cell_data(source, target, extensive="missing")

    with pytest.raises(ValueError, match="both extensive and intensive"):
        meshio.transfer_cell_data(source, target, extensive="q", intensive="q")


def test_result_can_be_used_as_target_cell_data():
    source = triangle_mesh(
        [[0.0, 0.0], [1.0, 0.0], [0.0, 1.0]],
        [[0, 1, 2]],
        cell_data={"q": [[1.0]]},
    )
    target = triangle_mesh(
        [[0.0, 0.0], [1.0, 0.0], [0.0, 1.0]], [[0, 1, 2]]
    )

    result = meshio.transfer_cell_data(source, target, extensive="q")
    mesh = meshio.Mesh(target.points, target.cells, cell_data=result)
    assert_allclose(mesh.cell_data["q"][0], [1.0])


def test_fields_mapping_form():
    source = triangle_mesh(
        [[0.0, 0.0], [1.0, 0.0], [0.0, 1.0]],
        [[0, 1, 2]],
        cell_data={"q": [[1.0]], "T": [[10.0]]},
    )
    target = triangle_mesh(
        [[0.0, 0.0], [1.0, 0.0], [0.0, 1.0]], [[0, 1, 2]]
    )

    result = meshio.transfer_cell_data(
        source, target, fields={"q": "extensive", "T": "intensive"}
    )
    assert_allclose(result["q"][0], [1.0])
    assert_allclose(result["T"][0], [10.0])
