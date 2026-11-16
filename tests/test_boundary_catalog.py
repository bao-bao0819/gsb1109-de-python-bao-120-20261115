import json

import numpy as np
import pytest

import meshio
from meshio._boundary_catalog import (
    boundary_catalog,
    catalog_to_json,
    rename_group,
    write_boundary_catalog,
)


def _base_mesh():
    points = [
        [0.0, 0.0, 0.0],
        [1.0, 0.0, 0.0],
        [1.0, 1.0, 0.0],
        [0.0, 1.0, 0.0],
    ]
    cells = [
        ("line", [[0, 1], [1, 2]]),
        ("triangle", [[0, 1, 2], [0, 2, 3]]),
    ]
    return points, cells


def test_groups_carry_namespace_dim_and_original_name():
    points, cells = _base_mesh()
    mesh = meshio.Mesh(
        points,
        cells,
        # number 1 reused across dimensions
        field_data={"bottom": [1, 1], "surface": [1, 2]},
        cell_sets={
            "bottom": [np.array([0]), None],
            "surface": [None, np.array([0])],
        },
    )
    catalog = boundary_catalog({"meshA": mesh})

    groups = {
        (g["namespace"], g["dim"], g["number"]): g for g in catalog["groups"]
    }
    assert set(groups) == {("meshA", 1, 1), ("meshA", 2, 1)}
    assert groups[("meshA", 1, 1)]["name"] == "bottom"
    assert groups[("meshA", 2, 1)]["name"] == "surface"
    assert groups[("meshA", 1, 1)]["cells"] == {"line": [0]}
    assert groups[("meshA", 2, 1)]["cells"] == {"triangle": [0]}
    assert catalog["unknown"] == []


def test_same_number_in_different_meshes_stays_distinct():
    points, cells = _base_mesh()
    mesh_a = meshio.Mesh(
        points,
        cells,
        field_data={"wall": [1, 2]},
        cell_sets={"wall": [None, np.array([0])]},
    )
    # second mesh reuses group number 1 for something unrelated
    mesh_b = meshio.Mesh(
        points,
        cells,
        field_data={"wall": [1, 2]},
        cell_sets={"wall": [None, np.array([1])]},
    )
    catalog = boundary_catalog({"meshA": mesh_a, "meshB": mesh_b})

    groups = {
        (g["namespace"], g["dim"], g["number"]): g for g in catalog["groups"]
    }
    assert set(groups) == {("meshA", 2, 1), ("meshB", 2, 1)}
    assert groups[("meshA", 2, 1)]["cells"] == {"triangle": [0]}
    assert groups[("meshB", 2, 1)]["cells"] == {"triangle": [1]}


def test_cell_can_belong_to_multiple_groups():
    points, cells = _base_mesh()
    mesh = meshio.Mesh(
        points,
        cells,
        field_data={"inlet": [1, 2], "roof": [2, 2]},
        cell_sets={
            "inlet": [None, np.array([0, 1])],
            "roof": [None, np.array([1])],
        },
    )
    catalog = boundary_catalog({"ns": mesh})

    groups = {g["name"]: g for g in catalog["groups"]}
    assert groups["inlet"]["cells"] == {"triangle": [0, 1]}
    assert groups["roof"]["cells"] == {"triangle": [1]}


def test_rename_keeps_identity_and_original_name():
    points, cells = _base_mesh()
    mesh = meshio.Mesh(
        points,
        cells,
        field_data={"wall": [3, 2]},
        cell_sets={"wall": [None, np.array([0])]},
    )
    catalog = boundary_catalog({"ns": mesh})

    entry = rename_group(catalog, "ns", 2, 3, "Wand (renamed)")
    assert entry["display_name"] == "Wand (renamed)"
    # original name and identity are untouched
    assert entry["name"] == "wall"
    assert (entry["namespace"], entry["dim"], entry["number"]) == ("ns", 2, 3)
    assert entry["cells"] == {"triangle": [0]}

    with pytest.raises(KeyError):
        rename_group(catalog, "ns", 1, 3, "nope")


def test_display_name_alias_does_not_create_new_group():
    points, cells = _base_mesh()
    # two names for the same (dim, number): a rename, not a new group
    mesh = meshio.Mesh(
        points,
        cells,
        field_data={"wall": [1, 2], "wall_v2": [1, 2]},
        cell_sets={
            "wall": [None, np.array([0])],
            "wall_v2": [None, np.array([1])],
        },
    )
    catalog = boundary_catalog({"ns": mesh})

    assert len(catalog["groups"]) == 1
    group = catalog["groups"][0]
    assert group["name"] == "wall"
    assert group["aliases"] == ["wall_v2"]
    # membership is the union over both display names
    assert group["cells"] == {"triangle": [0, 1]}


def test_unknown_cell_data_tag_listed_separately():
    points, cells = _base_mesh()
    mesh = meshio.Mesh(
        points,
        cells,
        field_data={"wall": [1, 2]},
        cell_data={
            "gmsh:physical": [
                np.array([0, 0]),
                np.array([1, 7]),
            ]
        },
    )
    catalog = boundary_catalog({"ns": mesh})

    groups = {(g["dim"], g["number"]): g for g in catalog["groups"]}
    assert groups[(2, 1)]["cells"] == {"triangle": [0]}

    assert len(catalog["unknown"]) == 1
    unknown = catalog["unknown"][0]
    assert unknown["namespace"] == "ns"
    assert unknown["dim"] == 2
    assert unknown["number"] == 7
    assert unknown["cells"] == {"triangle": [1]}


def test_no_cross_dimension_fallback():
    points, cells = _base_mesh()
    # tag 7 exists as a 1D group; the 2D cells tagged 7 must not be
    # completed with the 1D group of the same number
    mesh = meshio.Mesh(
        points,
        cells,
        field_data={"edge7": [7, 1]},
        cell_data={
            "gmsh:physical": [
                np.array([7, 0]),
                np.array([7, 0]),
            ]
        },
    )
    catalog = boundary_catalog({"ns": mesh})

    groups = {(g["dim"], g["number"]): g for g in catalog["groups"]}
    # the 1D group only contains the line cell
    assert groups[(1, 7)]["cells"] == {"line": [0]}
    assert (2, 7) not in groups

    # the 2D reference is unknown and listed separately
    assert len(catalog["unknown"]) == 1
    unknown = catalog["unknown"][0]
    assert unknown["dim"] == 2
    assert unknown["number"] == 7
    assert unknown["cells"] == {"triangle": [0]}


def test_unknown_cell_set_listed_separately():
    points, cells = _base_mesh()
    mesh = meshio.Mesh(
        points,
        cells,
        field_data={"wall": [1, 2]},
        cell_sets={
            "wall": [None, np.array([0])],
            "ghost": [np.array([1]), None],
        },
    )
    catalog = boundary_catalog({"ns": mesh})

    assert len(catalog["groups"]) == 1
    assert len(catalog["unknown"]) == 1
    unknown = catalog["unknown"][0]
    assert unknown["namespace"] == "ns"
    assert unknown["name"] == "ghost"
    assert unknown["source"] == "cell_set"
    assert unknown["cells"] == {"line": [1]}


def test_single_mesh_default_namespace_and_json_roundtrip(tmp_path):
    points, cells = _base_mesh()
    mesh = meshio.Mesh(
        points,
        cells,
        field_data={"wall": [1, 2]},
        cell_sets={"wall": [None, np.array([0, 1])]},
    )
    catalog = boundary_catalog(mesh)
    assert catalog["groups"][0]["namespace"] == "default"

    text = catalog_to_json(catalog)
    assert json.loads(text) == catalog

    out = tmp_path / "catalog.json"
    write_boundary_catalog(catalog, out)
    assert json.loads(out.read_text()) == catalog


def test_cli(tmp_path):
    points, cells = _base_mesh()
    mesh = meshio.Mesh(
        points,
        cells,
        field_data={"wall": [1, 2]},
        cell_data={"gmsh:physical": [np.array([0, 0]), np.array([1, 0])]},
    )
    mesh_path = tmp_path / "mesh.msh"
    meshio.write(mesh_path, mesh, file_format="gmsh22")

    out_path = tmp_path / "catalog.json"
    ret = meshio._cli.main(
        ["boundary-catalog", str(mesh_path), "--output", str(out_path)]
    )
    assert ret == 0

    catalog = json.loads(out_path.read_text())
    assert len(catalog["groups"]) == 1
    group = catalog["groups"][0]
    # the source namespace is the file as given on the command line
    assert group["namespace"] == str(mesh_path)
    assert (group["dim"], group["number"], group["name"]) == (2, 1, "wall")
    assert group["cells"] == {"triangle": [0]}
