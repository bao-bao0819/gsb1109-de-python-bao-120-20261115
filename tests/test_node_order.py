import copy

import numpy as np
import pytest

import meshio

from . import helpers


HIGH_ORDER = [
    "line3",
    "triangle6",
    "quad8",
    "quad9",
    "tetra10",
    "hexahedron20",
    "hexahedron27",
    "wedge15",
    "pyramid13",
]


def _permutation_cell(cell_type):
    n = meshio._common.num_nodes_per_cell[cell_type]
    return meshio.Mesh(
        np.zeros((n, 3)),
        [(cell_type, [list(range(n))])],
    )


@pytest.mark.parametrize("cell_type", HIGH_ORDER)
def test_node_order_is_involution_pair(cell_type):
    forward = meshio.node_order("gmsh", "meshio", cell_type)
    backward = meshio.node_order("meshio", "gmsh", cell_type)
    n = len(forward)
    assert sorted(forward) == list(range(n))
    assert np.array_equal(forward[backward], np.arange(n))
    assert np.array_equal(backward[forward], np.arange(n))


@pytest.mark.parametrize("cell_type", HIGH_ORDER)
def test_node_order_trace_round_trip(cell_type):
    trace = meshio.node_order_trace("gmsh", "meshio", cell_type)
    assert trace["cell_type"] == cell_type
    assert trace["source"] == "gmsh"
    assert trace["target"] == "meshio"
    assert trace["num_nodes"] == len(trace["node_order"])


def test_unknown_high_order_type_is_rejected():
    with pytest.raises(meshio.OrderingError):
        meshio.node_order("gmsh", "meshio", "triangle10")


def test_mesh_convert_node_order_records_trace():
    from tests import helpers

    mesh = helpers.add_point_data(helpers.hex20_mesh, 1, seed=4)
    converted = mesh.convert_node_order("meshio", "gmsh")
    restored = converted.convert_node_order("gmsh", "meshio")

    assert np.array_equal(restored.cells[0].data, mesh.cells[0].data)
    assert np.array_equal(restored.point_data["a"], mesh.point_data["a"])
    trace = converted.info["node_order"]
    assert trace["0:hexahedron20"]["source"] == "meshio"
    assert trace["0:hexahedron20"]["target"] == "gmsh"
    assert len(trace["0:hexahedron20"]["node_order"]) == 20


def test_unknown_linear_type_defaults_to_identity():
    assert np.array_equal(
        meshio.node_order("gmsh", "meshio", "triangle"), np.arange(3)
    )


@pytest.mark.parametrize("cell_type", HIGH_ORDER)
def test_gmsh_round_trip_identity(cell_type, tmp_path):
    mesh = _permutation_cell(cell_type)
    for version in ["2.2", "4.1"]:
        p = tmp_path / f"test-{version}.msh"
        meshio.gmsh.write(p, mesh, fmt_version=version, binary=False)
        out = meshio.gmsh.read(p)
        assert out.cells[0].type == cell_type
        assert np.array_equal(out.cells[0].data, mesh.cells[0].data)


@pytest.mark.parametrize("cell_type", ["triangle6", "quad9", "tetra10", "wedge15"])
def test_permas_round_trip_identity(cell_type, tmp_path):
    mesh = _permutation_cell(cell_type)
    p = tmp_path / "test.post"
    meshio.permas.write(p, mesh)
    out = meshio.permas.read(p)
    assert out.cells[0].type == cell_type
    assert np.array_equal(out.cells[0].data, mesh.cells[0].data)


@pytest.mark.parametrize("cell_type", ["hexahedron20", "hexahedron27"])
def test_mdpa_round_trip_identity(cell_type, tmp_path):
    mesh = _permutation_cell(cell_type)
    p = tmp_path / "test.mdpa"
    meshio.mdpa.write(p, mesh)
    out = meshio.mdpa.read(p)
    assert out.cells[0].type == cell_type
    assert np.array_equal(out.cells[0].data, mesh.cells[0].data)


def test_hexahedron27_mid_nodes_are_not_reversed(tmp_path):
    """Regression: corner nodes matched but the face/centre nodes were permuted."""
    mesh = helpers.hex20_mesh
    # Unique value per corner/mid node in the cell, so any mix-up is visible.
    mesh = copy.deepcopy(mesh)
    mesh.cells[0].data[0] = np.arange(20)

    p = tmp_path / "test.mdpa"
    meshio.mdpa.write(p, mesh)
    out = meshio.mdpa.read(p)
    assert np.array_equal(out.cells[0].data, mesh.cells[0].data)


@pytest.mark.parametrize(
    "writer,extension,cell_type",
    [
        (meshio.flac3d.write, ".f3grid", "hexahedron20"),
        (meshio.medit.write, ".mesh", "triangle6"),
        (meshio.su2.write, ".su2", "triangle6"),
        (meshio.tecplot.write, ".dat", "triangle6"),
        (meshio.ugrid.write, ".ugrid", "triangle6"),
    ],
)
def test_lossy_writer_rejects_high_order(writer, extension, cell_type, tmp_path):
    mesh = _permutation_cell(cell_type)
    with pytest.raises(meshio.WriteError) as excinfo:
        writer(tmp_path / ("test" + extension), mesh)
    assert "mid-side" in str(excinfo.value)


def test_lossy_writer_still_accepts_linear_cells(tmp_path):
    mesh = helpers.tri_mesh
    meshio.medit.write(tmp_path / "test.mesh", mesh)
    out = meshio.medit.read(tmp_path / "test.mesh")
    assert out.cells[0].type == "triangle"


def test_gmsh41_node_data_is_aligned_with_point_order(tmp_path):
    """NodeData is indexed by tag; points may be stored in another order."""
    # Two node blocks: storage order is tag 1, then tags 3, 2.
    content = """$MeshFormat
4.1 0 8
$EndMeshFormat
$Nodes
2 3 1 3
0 1 0 1
1
0 0 0
1 2 0 2
3
2
1 0 0
0 1 0
$EndNodes
$Elements
1 1 1 1
0 1 15 1
1
0
$EndElements
$NodeData
1
"id"
1
0
3
0
1
3
1 10
2 20
3 30
$EndNodeData
"""
    p = tmp_path / "test.msh"
    p.write_text(content)
    mesh = meshio.gmsh.read(p)
    # Storage order has x = 0, 1, 0 for tags 1, 3, 2; NodeData says
    # id = 10 * tag.
    assert np.array_equal(mesh.point_data["id"], [10.0, 30.0, 20.0])
    assert np.array_equal(mesh.points[:, 0], [0.0, 1.0, 0.0])


def test_gmsh22_node_data_is_aligned_with_node_tags(tmp_path):
    # Node tags 10, 30, 20 in storage order; NodeData is keyed by tag.
    content = """$MeshFormat
2.2 0 8
$EndMeshFormat
$Nodes
3
10 0 0 0
30 1 0 0
20 0 1 0
$EndNodes
$Elements
1
1 1 2 1 10 30 20
$EndElements
$NodeData
1
"id"
1
0
3
0
1
3
10 1
30 3
20 2
$EndNodeData
"""
    p = tmp_path / "test22.msh"
    p.write_text(content)
    mesh = meshio.gmsh.read(p)
    assert np.array_equal(mesh.point_data["id"], [1.0, 3.0, 2.0])
    assert np.array_equal(mesh.points[:, 0], [0.0, 1.0, 0.0])
