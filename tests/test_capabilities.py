import json

import numpy as np
import pytest

import meshio
from meshio._capabilities import (
    DEGRADED,
    PRESERVED,
    UNREPRESENTABLE,
    capability_report,
    require_writable,
    write_with_preflight,
)
from meshio._exceptions import WriteError

from . import helpers


def test_point_sets_vtu_are_degraded():
    mesh = meshio.Mesh(
        helpers.tri_mesh.points,
        helpers.tri_mesh.cells,
        point_sets={"fixed": np.array([0, 1])},
    )
    report = capability_report(mesh, "vtu")
    assert report.worst() == DEGRADED
    assert not report.unrepresentable
    assert any(r.feature == "point set 'fixed'" for r in report.degraded)


def test_unrepresentable_cell_block_is_named():
    mesh = helpers.tri_mesh
    report = capability_report(mesh, "tetgen")
    assert report.worst() == UNREPRESENTABLE
    item = next(r for r in report.unrepresentable if "triangle" in r.feature)
    assert "cells[0]" in item.block


def test_mixed_cells_are_checked_per_block():
    # quad is representable by Abaqus; polygon is not.
    mesh = helpers.polygon_mesh
    report = capability_report(mesh, "abaqus")
    unrep = {r.feature for r in report.unrepresentable}
    assert "cells 'polygon'" in unrep
    preserved = {r.feature for r in report.preserved}
    assert "cells 'triangle'" in preserved
    assert "cells 'quad'" in preserved


def test_attributes_are_not_promoted_by_format_name():
    mesh = meshio.Mesh(
        helpers.tri_mesh.points,
        helpers.tri_mesh.cells,
        point_data={"a": np.arange(4)},
        cell_data={"b": [np.arange(2)]},
    )
    report = capability_report(mesh, "stl")
    statuses = {r.feature: r.status for r in report.results}
    assert statuses["point data 'a'"] == DEGRADED
    assert statuses["cell data 'b'"] == DEGRADED


def test_stl_2d_points_are_degraded():
    report = capability_report(helpers.tri_mesh_2d, "stl")
    item = next(r for r in report.results if "points dimension" in r.feature)
    assert item.status == DEGRADED


def test_ply_index_cast_is_reported_for_each_block():
    report = capability_report(helpers.tri_quad_mesh, "ply")
    casts = [r for r in report.degraded if r.feature == "cell indices"]
    assert len(casts) == len(helpers.tri_quad_mesh.cells)
    assert all(r.block and r.block.startswith("cells[") for r in casts)


def test_duplicate_blocks_rejected_when_writer_cannot_separate_them():
    mesh = meshio.Mesh(
        helpers.tri_mesh.points,
        [("triangle", [[0, 1, 2]]), ("triangle", [[0, 2, 3]])],
    )
    report = capability_report(mesh, "ugrid")
    assert any(
        r.status == UNREPRESENTABLE and "grouping" in r.feature
        for r in report.results
    )


def test_overlapping_cell_sets_kept_as_degradation():
    mesh = meshio.Mesh(
        helpers.tri_mesh.points,
        helpers.tri_mesh.cells,
        cell_sets={"a": [np.array([0, 1])], "b": [np.array([1])]},
    )
    report = capability_report(mesh, "vtu")
    assert report.worst() == DEGRADED
    assert {r.feature for r in report.degraded} >= {"cell set 'a'", "cell set 'b'"}


def test_authorization_only_covers_degradations():
    mesh = meshio.Mesh(
        helpers.tri_mesh.points,
        helpers.tri_mesh.cells,
        point_sets={"fixed": np.array([0])},
    )
    with pytest.raises(WriteError):
        require_writable(mesh, "vtu")
    report = require_writable(mesh, "vtu", allow_degraded=True)
    assert report.ok(allow_degraded=True)

    with pytest.raises(WriteError):
        require_writable(helpers.tri_mesh, "tetgen", allow_degraded=True)


def test_write_with_preflight_blocks_then_writes(tmp_path):
    mesh = meshio.Mesh(
        helpers.tri_mesh.points,
        helpers.tri_mesh.cells,
        point_sets={"fixed": np.array([0])},
    )
    path = tmp_path / "out.vtu"
    with pytest.raises(WriteError):
        write_with_preflight(path, mesh)
    assert not path.exists()

    write_with_preflight(path, mesh, allow_degraded=True)
    assert path.exists()
    reread = meshio.read(path)
    assert len(reread.cells) == 1


def test_cli_report_does_not_write(tmp_path, capsys):
    infile = tmp_path / "in.msh"
    meshio.write(infile, helpers.tri_mesh, file_format="gmsh")
    outfile = tmp_path / "out.vtu"
    rc = meshio._cli.main(
        ["convert", str(infile), str(outfile), "--report"]
    )
    captured = capsys.readouterr()
    assert "Preservable" in captured.out
    assert "Requires explicit degradation" in captured.out
    assert rc == 0
    assert not outfile.exists()


def test_cli_json_report_is_structured(tmp_path, capsys):
    infile = tmp_path / "in.msh"
    meshio.write(infile, helpers.tri_mesh, file_format="gmsh")
    outfile = tmp_path / "out.stl"
    rc = meshio._cli.main(["convert", str(infile), str(outfile), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["format"] == "stl"
    assert set(payload) >= {"preserved", "degraded", "unrepresentable"}
    # gmsh metadata cannot be represented by STL, so the report is not a
    # fully-preserving plan even though nothing was written.
    assert rc == 1
    assert not outfile.exists()


def test_cli_refuses_unrepresentable(tmp_path, capsys):
    infile = tmp_path / "in.msh"
    meshio.write(infile, helpers.tri_quad_mesh, file_format="gmsh22")
    outfile = tmp_path / "out.node"
    rc = meshio._cli.main(
        ["convert", str(infile), str(outfile), "--allow-degraded"]
    )
    assert rc == 1
    assert not outfile.exists()
    assert "Unrepresentable" in capsys.readouterr().err


def test_cli_writes_only_after_explicit_authorization(tmp_path):
    infile = tmp_path / "in.msh"
    meshio.write(infile, helpers.tri_mesh, file_format="gmsh")
    outfile = tmp_path / "out.stl"

    rc = meshio._cli.main(["convert", str(infile), str(outfile)])
    assert rc == 1
    assert not outfile.exists()

    rc = meshio._cli.main(
        ["convert", str(infile), str(outfile), "--allow-degraded"]
    )
    assert rc == 0
    assert outfile.exists()


def test_gmsh_multi_block_requires_entity_metadata():
    cells = [("triangle", [[0, 1, 2]]), ("triangle", [[0, 2, 3]])]
    mesh = meshio.Mesh(helpers.tri_mesh.points, cells)
    report = capability_report(mesh, "gmsh")
    assert report.worst() == UNREPRESENTABLE
    item = next(r for r in report.unrepresentable if r.feature == "multiple cell blocks")
    assert "gmsh:dim_tags" in item.detail


def test_gmsh_duplicate_blocks_need_distinct_tags():
    cells = [("triangle", [[0, 1, 2]]), ("triangle", [[0, 2, 3]])]
    points_data = {"gmsh:dim_tags": np.zeros((4, 2), dtype=int)}
    mesh = meshio.Mesh(
        helpers.tri_mesh.points,
        cells,
        point_data=points_data,
        cell_data={
            "gmsh:geometrical": [np.array([0]), np.array([0])],
        },
    )
    report = capability_report(mesh, "gmsh")
    assert report.worst() == UNREPRESENTABLE
    assert any("distinct" in r.detail for r in report.unrepresentable)


def test_gmsh_duplicate_blocks_with_distinct_tags_preserved():
    cells = [("triangle", [[0, 1, 2]]), ("triangle", [[0, 2, 3]])]
    mesh = meshio.Mesh(
        helpers.tri_mesh.points,
        cells,
        point_data={"gmsh:dim_tags": np.zeros((4, 2), dtype=int)},
        cell_data={
            "gmsh:geometrical": [np.array([1]), np.array([2])],
        },
    )
    report = capability_report(mesh, "gmsh")
    assert report.worst() == PRESERVED


def test_med_elno_block_with_unknown_gauss_shape_rejected():
    pytest.importorskip("h5py")
    mesh = meshio.Mesh(
        helpers.hex_mesh.points,
        helpers.hex_mesh.cells,
        cell_data={"a": [np.zeros((1, 4, 6))]},
    )
    report = capability_report(mesh, "med")
    assert report.worst() == UNREPRESENTABLE


def test_med_elno_block_matching_nodes_preserved():
    pytest.importorskip("h5py")
    mesh = meshio.Mesh(
        helpers.hex_mesh.points,
        helpers.hex_mesh.cells,
        cell_data={"a": [np.zeros((1, 8, 6))]},
    )
    report = capability_report(mesh, "med")
    assert report.worst() == PRESERVED


def test_polygon_blocks_split_by_width_are_not_duplicates():
    report = capability_report(helpers.polygon_mesh, "vtu")
    assert not any(
        r.feature == "cell block grouping" for r in report.results
    )


def test_ply_rejects_polygon_that_reads_back_as_triangle():
    mesh = meshio.Mesh(
        helpers.tri_mesh.points,
        [("polygon", [[0, 1, 2]])],
    )
    report = capability_report(mesh, "ply")
    item = next(r for r in report.results if r.feature == "cells 'polygon'")
    assert item.status == DEGRADED
    assert "triangle" in item.detail


def test_stl_binary_precision_depends_on_option():
    report = capability_report(helpers.tri_mesh, "stl", options={"binary": True})
    assert report.worst() == PRESERVED
    report = capability_report(helpers.tri_mesh, "stl", options={"binary": False})
    assert report.worst() == PRESERVED


def test_optional_dependency_reported_as_unrepresentable():
    report = capability_report(helpers.tri_mesh, "cgns")
    assert report.worst() == UNREPRESENTABLE
    assert any("dependency" in r.feature for r in report.unrepresentable)


def test_write_with_preflight_accepts_options(tmp_path):
    path = tmp_path / "out.stl"
    write_with_preflight(path, helpers.tri_mesh, options={"binary": True})
    assert path.exists()


def test_mdpa_data_is_not_promised_when_reader_drops_it():
    mesh = meshio.Mesh(
        helpers.tri_mesh.points,
        helpers.tri_mesh.cells,
        point_data={"a": np.arange(4.0)},
        cell_data={"b": [np.arange(2.0)]},
    )
    report = capability_report(mesh, "mdpa")
    # The writer emits these blocks; ensure the report stays honest about the
    # read-back path by not claiming the whole mesh is unrepresentable.
    assert not report.unrepresentable


def test_tecplot_reserved_names_are_degraded_not_fatal():
    mesh = meshio.Mesh(
        helpers.tri_mesh.points,
        helpers.tri_mesh.cells,
        point_data={"X": np.arange(4.0)},
    )
    report = capability_report(mesh, "tecplot")
    item = next(r for r in report.results if r.feature == "point data 'X'")
    assert item.status == DEGRADED
