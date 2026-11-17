"""Concrete writer capability rules.

Only rules that mirror what the writer code in this package actually does are
listed here.  A format name is never treated as evidence that a particular
file is lossless; the preflight checks each block/array against these rules.
"""

from __future__ import annotations

import importlib


def _import(module: str, attribute: str):
    return getattr(importlib.import_module(module), attribute)


def _gmsh_cells():
    return set(_import("meshio.gmsh.common", "_meshio_to_gmsh_type").keys())


def _abaqus_cells():
    return set(_import("meshio.abaqus._abaqus", "meshio_to_abaqus_type").keys())


def _avsucd_cells():
    return set(_import("meshio.avsucd._avsucd", "meshio_to_avsucd_type").keys())


def _exodus_cells():
    return set(_import("meshio.exodus._exodus", "meshio_to_exodus_type").keys())


def _mdpa_cells():
    return set(_import("meshio.mdpa._mdpa", "_meshio_to_mdpa_type").keys())


def _med_cells():
    return set(_import("meshio.med._med", "meshio_to_med_type").keys())


def _nastran_cells():
    return set(_import("meshio.nastran._nastran", "meshio_to_nastran_type").keys())


def _permas_cells():
    return set(_import("meshio.permas._permas", "meshio_to_permas_type").keys())


def _tecplot_cells():
    return set(_import("meshio.tecplot._tecplot", "meshio_to_tecplot_type").keys())


def _xdmf_cells():
    return set(_import("meshio.xdmf.common", "meshio_to_xdmf_type").keys())


def _vtk_cells():
    return set(_import("meshio._vtk_common", "meshio_to_vtk_type").keys()) - {"empty"}


def _vtk_dtypes(version: int):
    module = "meshio.vtk._vtk_42" if version == 42 else "meshio.vtk._vtk_51"
    return set(_import(module, "numpy_to_vtk_dtype").keys())


def _vtu_dtypes():
    return {
        dtype.name for dtype in _import("meshio.vtu._vtu", "numpy_to_vtu_type").keys()
    }


def _xdmf_dtypes():
    return set(_import("meshio.xdmf.common", "numpy_to_xdmf_dtype").keys())


def _exodus_dtypes():
    return set(_import("meshio.exodus._exodus", "numpy_to_exodus_dtype").keys())


def _flac3d_zone_types():
    return set(_import("meshio.flac3d._flac3d", "meshio_only")["zone"])


def _flac3d_collapse():
    supported = _import("meshio.flac3d._flac3d", "meshio_only")
    collapse = {}
    for family, base in (
        (
            "zone",
            {
                "tetra": "tetra",
                "pyramid": "pyramid",
                "wedge": "wedge",
                "hexahedron": "hexahedron",
            },
        ),
        ("face", {"triangle": "triangle", "quad": "quad"}),
    ):
        for cell_type, base_type in supported[family].items():
            if cell_type != base_type:
                collapse[cell_type] = base_type
    return collapse


def _netgen_cells():
    return set(_import("meshio.netgen._netgen", "meshio_to_netgen_pmap").keys())


_PLY_DTYPES = {
    "int8",
    "int16",
    "int32",
    "int64",
    "uint8",
    "uint16",
    "uint32",
    "uint64",
    "float32",
    "float64",
}


def _polygon_by_size(minor: int):
    if minor == 3:
        return "triangle"
    if minor == 4:
        return "quad"
    return None


def _data_all(dtypes=None, max_ndim=2, **kwargs):
    rule = {"mode": "all", "max_ndim": max_ndim}
    if dtypes is not None:
        rule["dtypes"] = dtypes
    rule.update(kwargs)
    return rule


def _data_none(**kwargs):
    rule = {"mode": "none"}
    rule.update(kwargs)
    return rule


def _data_selected(**kwargs):
    rule = {"mode": "selected"}
    rule.update(kwargs)
    return rule


def get_tables(format_name: str) -> dict:
    """Return the concrete rules for a writer-map key."""
    if format_name in ("gmsh", "gmsh22"):
        gmsh41 = format_name == "gmsh"
        point_rule = _data_all(
            max_ndim=2,
            components={1, 3, 9},
            integer_as_real=True,
        )
        cell_rule = _data_all(
            max_ndim=2,
            components={1, 3, 9},
            integer_as_real=True,
        )
        if gmsh41:
            point_rule["special_keys"] = {
                "gmsh:dim_tags": (
                    "preserved",
                    "node entity dimension/tag block",
                )
            }
            cell_rule["constant_keys"] = {"gmsh:physical", "gmsh:geometrical"}
        else:
            point_rule["dropped_keys"] = {"gmsh:dim_tags"}
            cell_rule["special_keys"] = {
                "gmsh:physical": ("preserved", "per-element physical tag"),
                "gmsh:geometrical": ("preserved", "per-element geometrical tag"),
            }
        cell_rule["rejected_keys"] = {"cell_tags"}
        return {
            "label": "Gmsh 4.1" if gmsh41 else "Gmsh 2.2",
            "points": "3d_pad",
            "points_float_only": True,
            "can_widen_points_to_float": True,
            "cells": _gmsh_cells(),
            "duplicate_blocks": "ok",
            "point_sets": "none",
            "cell_sets": "gmsh_bounding" if gmsh41 else "none",
            "point_data": point_rule,
            "cell_data": cell_rule,
            "field_data": "physical_names",
            "gmsh_periodic": True,
            "info": "none",
            "needs_dim_tags_for_multiple_blocks": gmsh41,
            "needs_unique_block_tags": gmsh41,
        }

    if format_name in ("vtk", "vtk42", "vtk51"):
        # Note: the registered "vtk51" key also dispatches to the 4.2 writer.
        use_51 = format_name == "vtk"
        version = 51 if use_51 else 42
        return {
            "label": "VTK 5.1" if use_51 else "VTK 4.2",
            "points": "3d_pad",
            "points_float_only": True,
            "can_widen_points_to_float": True,
            "cells": _vtk_cells(),
            "duplicate_blocks": "adjacent_merge",
            "point_sets": "to_data" if use_51 else "none",
            "cell_sets": "to_data" if use_51 else "none",
            "point_data": _data_all(
                dtypes=_vtk_dtypes(version), max_ndim=2, field_name_no_space=True
            ),
            "cell_data": _data_all(
                dtypes=_vtk_dtypes(version), max_ndim=2, field_name_no_space=True
            ),
            "field_data": "none",
            "gmsh_periodic": False,
            "info": "none",
        }

    if format_name == "vtu":
        return {
            "label": "VTU",
            "points": "3d_pad",
            "points_float_only": True,
            "can_widen_points_to_float": True,
            "cells": _vtk_cells(),
            "polyhedron": True,
            "duplicate_blocks": "adjacent_merge",
            "point_sets": "to_data",
            "cell_sets": "to_data",
            "point_data": _data_all(dtypes=_vtu_dtypes(), max_ndim=2),
            "cell_data": _data_all(dtypes=_vtu_dtypes(), max_ndim=2),
            "field_data": "none",
            "gmsh_periodic": False,
            "info": "none",
        }

    if format_name == "xdmf":
        return {
            "label": "XDMF",
            "points": "max3",
            "points_float_only": True,
            "can_widen_points_to_float": True,
            "cells": _xdmf_cells(),
            "mixed_cells": {
                "vertex",
                "line",
                "triangle",
                "quad",
                "tetra",
                "pyramid",
                "wedge",
                "hexahedron",
                "triangle6",
            },
            "duplicate_blocks": "adjacent_merge",
            "point_sets": "none",
            "cell_sets": "none",
            "point_data": _data_all(
                dtypes=_xdmf_dtypes(), max_ndim=2, components={1, 2, 3, 6, 9}
            ),
            "cell_data": _data_all(
                dtypes=_xdmf_dtypes(), max_ndim=2, components={1, 2, 3, 6, 9}
            ),
            "field_data": "none",
            "gmsh_periodic": False,
            "info": "none",
        }

    if format_name == "exodus":
        return {
            "label": "Exodus",
            "requires": "netCDF4",
            "points": "2d_or_3d",
            "points_float_only": True,
            "can_widen_points_to_float": True,
            "cells": _exodus_cells(),
            "duplicate_blocks": "ok",
            "point_sets": "native",
            "cell_sets": "none",
            "point_data": _data_all(
                dtypes=_exodus_dtypes(), max_ndim=3, name_max_length=32
            ),
            "cell_data": _data_none(),
            "field_data": "none",
            "gmsh_periodic": False,
            "info": "none",
        }

    if format_name == "abaqus":
        return {
            "label": "Abaqus",
            "points": "any",
            "cells": _abaqus_cells() - {"tetra4"},
            "duplicate_blocks": "ok",
            "point_sets": "native",
            "cell_sets": "native",
            "point_data": _data_none(),
            "cell_data": _data_none(),
            "field_data": "none",
            "gmsh_periodic": False,
            "info": "none",
        }

    if format_name == "ansys":
        return {
            "label": "ANSYS msh",
            "points": "2d_or_3d",
            "points_float_only": True,
            "can_widen_points_to_float": True,
            "cells": {"triangle", "tetra", "quad", "hexahedron", "pyramid", "wedge"},
            "duplicate_blocks": "ok",
            "point_sets": "none",
            "cell_sets": "none",
            "point_data": _data_none(),
            "cell_data": _data_none(),
            "field_data": "none",
            "gmsh_periodic": False,
            "info": "none",
        }

    if format_name == "avsucd":
        return {
            "label": "AVS-UCD",
            "points": "3d_pad",
            "points_float_only": True,
            "can_widen_points_to_float": True,
            "cells": _avsucd_cells(),
            "duplicate_blocks": "merge",
            "point_sets": "none",
            "cell_sets": "none",
            "point_data": _data_all(
                max_ndim=2,
                integer_as_real=True,
                real_digits=14,
                sanitize_names=True,
            ),
            "cell_data": _data_all(
                max_ndim=2,
                integer_as_real=True,
                real_digits=14,
                avs_material_from_first_int=True,
            ),
            "field_data": "none",
            "gmsh_periodic": False,
            "info": "none",
        }

    if format_name == "cgns":
        return {
            "label": "CGNS",
            "requires": "h5py",
            "points": "3d_only",
            "points_float_only": True,
            "can_widen_points_to_float": True,
            "cells": {"tetra"},
            "duplicate_blocks": "reject",
            "point_sets": "none",
            "cell_sets": "none",
            "point_data": _data_none(),
            "cell_data": _data_none(),
            "field_data": "none",
            "gmsh_periodic": False,
            "info": "none",
        }

    if format_name == "dolfin-xml":
        return {
            "label": "DOLFIN XML",
            "points": "2d_or_3d",
            "points_float_only": True,
            "can_widen_points_to_float": True,
            "cells": {"triangle", "tetra"},
            "duplicate_blocks": "merge",
            "one_cell_type_priority": ["tetra", "triangle"],
            "point_sets": "none",
            "cell_sets": "none",
            "point_data": _data_none(),
            "cell_data": _data_all(max_ndim=1, single_block_only=True),
            "field_data": "none",
            "gmsh_periodic": False,
            "info": "none",
        }

    if format_name == "flac3d":
        return {
            "label": "FLAC3D",
            "points": "3d_only",
            "points_float_only": True,
            "can_widen_points_to_float": True,
            "cells": _flac3d_zone_types(),
            "cell_collapse": _flac3d_collapse(),
            "duplicate_blocks": "adjacent_merge",
            "point_sets": "none",
            "cell_sets": "native_renamed",
            "point_data": _data_none(),
            "cell_data": _data_none(),
            "field_data": "none",
            "gmsh_periodic": False,
            "info": "none",
        }

    if format_name == "h5m":
        return {
            "label": "H5M",
            "requires": "h5py",
            "points": "any",
            "cells": {"line", "triangle", "tetra"},
            "duplicate_blocks": "reject",
            "point_sets": "none",
            "cell_sets": "none",
            "point_data": _data_all(max_ndim=2),
            "cell_data": _data_none(fatal=True),
            "field_data": "none",
            "gmsh_periodic": False,
            "info": "none",
        }

    if format_name == "hmf":
        return {
            "label": "HMF (experimental)",
            "requires": "h5py",
            "points": "max3",
            "points_float_only": True,
            "can_widen_points_to_float": True,
            "cells": _xdmf_cells(),
            "duplicate_blocks": "reject",
            "point_sets": "none",
            "cell_sets": "none",
            "point_data": _data_all(max_ndim=2),
            "cell_data": _data_all(max_ndim=2),
            "field_data": "none",
            "gmsh_periodic": False,
            "info": "none",
        }

    if format_name == "mdpa":
        return {
            "label": "Kratos MDPA",
            "points": "3d_pad",
            "points_float_only": True,
            "can_widen_points_to_float": True,
            "cells": _mdpa_cells(),
            "duplicate_blocks": "adjacent_merge",
            "point_sets": "none",
            "cell_sets": "none",
            "point_data": _data_all(max_ndim=2),
            "cell_data": _data_all(max_ndim=2),
            "field_data": "none",
            "gmsh_periodic": False,
            "info": "none",
        }

    if format_name == "med":
        return {
            "label": "MED",
            "requires": "h5py",
            "med_tags": True,
            "points": "max3",
            "points_float_only": True,
            "can_widen_points_to_float": True,
            "cells": _med_cells(),
            "duplicate_blocks": "reject",
            "point_sets": "none",
            "cell_sets": "none",
            "point_data": _data_all(
                max_ndim=2,
                integer_as_real=True,
                special_keys={"point_tags": "preserved"},
            ),
            "cell_data": _data_all(
                max_ndim=3,
                integer_as_real=True,
                special_keys={"cell_tags": "preserved"},
                med_elno=True,
                oversized_action="skip",
            ),
            "field_data": "med_names",
            "gmsh_periodic": False,
            "info": "none",
        }

    if format_name == "medit":
        return {
            "label": "Medit",
            "points": "max3",
            "points_dtypes": {"float32", "float64"},
            "cells": {"line", "triangle", "quad", "tetra", "wedge", "pyramid", "hexahedron"},
            "duplicate_blocks": "ok",
            "point_sets": "none",
            "cell_sets": "none",
            "point_data": _data_selected(
                selector="first_int",
                strict_first_int=True,
                required_dtype="integer",
                name_preserved=False,
                roundtrip_keys={"medit:ref"},
                max_ndim=1,
            ),
            "cell_data": _data_selected(
                selector="first_int",
                strict_first_int=True,
                required_dtype="integer",
                name_preserved=False,
                roundtrip_keys={"medit:ref"},
                max_ndim=1,
            ),
            "field_data": "none",
            "gmsh_periodic": False,
            "info": "none",
        }

    if format_name == "nastran":
        return {
            "label": "Nastran",
            "points": "3d_pad",
            "points_float_only": True,
            "can_widen_points_to_float": True,
            "cells": _nastran_cells(),
            "duplicate_blocks": "adjacent_merge",
            "point_sets": "none",
            "cell_sets": "none",
            "point_data": _data_selected(
                allowed_keys={"nastran:ref"},
                preserved_keys={"nastran:ref"},
                max_ndim=1,
            ),
            "cell_data": _data_selected(
                allowed_keys={"nastran:ref"},
                preserved_keys={"nastran:ref"},
                max_ndim=1,
            ),
            "field_data": "none",
            "gmsh_periodic": False,
            "info": "none",
        }

    if format_name == "netgen":
        return {
            "label": "Netgen",
            "points": "max3",
            "points_float_only": True,
            "can_widen_points_to_float": True,
            "cells": _netgen_cells(),
            "duplicate_blocks": "adjacent_merge",
            "point_sets": "none",
            "cell_sets": "none",
            "point_data": _data_none(),
            "cell_data": _data_selected(
                selector="preferred_int",
                preferred="netgen:index",
                required_dtype="integer",
                name_preserved=False,
                preserved_keys={"netgen:index"},
                max_ndim=1,
            ),
            "field_data": "names",
            "gmsh_periodic": False,
            "info": "netgen",
        }

    if format_name == "neuroglancer":
        return {
            "label": "Neuroglancer precomputed",
            "points": "3d_only",
            "points_cast": "float32",
            "cells": {"triangle"},
            "duplicate_blocks": "ok",
            "point_sets": "none",
            "cell_sets": "none",
            "point_data": _data_none(),
            "cell_data": _data_none(),
            "field_data": "none",
            "gmsh_periodic": False,
            "info": "none",
        }

    if format_name == "obj":
        return {
            "label": "Wavefront OBJ",
            "points": "3d_only",
            "points_float_only": True,
            "can_widen_points_to_float": True,
            "cells": {"triangle", "quad", "polygon"},
            "cell_type_change": {"polygon": _polygon_by_size},
            "duplicate_blocks": "merge",
            "point_sets": "none",
            "cell_sets": "none",
            "point_data": _data_selected(
                allowed_keys={"obj:vn", "obj:vt"},
                preserved_keys={"obj:vn", "obj:vt"},
                max_ndim=2,
            ),
            "cell_data": _data_none(),
            "field_data": "none",
            "gmsh_periodic": False,
            "info": "none",
        }

    if format_name == "off":
        return {
            "label": "OFF",
            "points": "3d_pad",
            "points_float_only": True,
            "can_widen_points_to_float": True,
            "cells": {"triangle"},
            "duplicate_blocks": "adjacent_merge",
            "point_sets": "none",
            "cell_sets": "none",
            "point_data": _data_none(),
            "cell_data": _data_none(),
            "field_data": "none",
            "gmsh_periodic": False,
            "info": "none",
        }

    if format_name == "permas":
        return {
            "label": "PERMAS",
            "points": "3d_pad",
            "points_float_only": True,
            "can_widen_points_to_float": True,
            "cells": _permas_cells(),
            "duplicate_blocks": "ok",
            "point_sets": "none",
            "cell_sets": "none",
            "point_data": _data_none(),
            "cell_data": _data_none(),
            "field_data": "none",
            "gmsh_periodic": False,
            "info": "none",
        }

    if format_name == "ply":
        return {
            "label": "PLY",
            "points": "3d_only",
            "points_dtypes": _PLY_DTYPES,
            "points_dedup": False,
            "cells": {"vertex", "line", "triangle", "quad", "polygon"},
            "cell_type_change": {"polygon": _polygon_by_size},
            "duplicate_blocks": "adjacent_merge",
            "cell_index_dtype": "int32",
            "point_sets": "none",
            "cell_sets": "none",
            "point_data": _data_all(
                max_ndim=1, dtypes=_PLY_DTYPES, oversized_action="skip"
            ),
            "cell_data": _data_none(),
            "field_data": "none",
            "gmsh_periodic": False,
            "info": "none",
        }

    if format_name == "stl":
        return {
            "label": "STL",
            "points": "3d_pad",
            "points_float_only": True,
            "can_widen_points_to_float": True,
            "points_cast": "float32",
            "points_cast_binary": True,
            "binary_narrows_cell_data": {"facet_normals": "float32"},
            "points_dedup": True,
            "uses_only_referenced_points": True,
            "cells": {"triangle"},
            "duplicate_blocks": "merge",
            "point_sets": "none",
            "cell_sets": "none",
            "point_data": _data_none(),
            "cell_data": _data_selected(
                allowed_keys={"facet_normals"},
                preserved_keys={"facet_normals"},
                max_ndim=2,
            ),
            "field_data": "none",
            "gmsh_periodic": False,
            "info": "none",
        }

    if format_name == "su2":
        return {
            "label": "SU2",
            "points": "2d_or_3d",
            "points_float_only": True,
            "can_widen_points_to_float": True,
            "cells_by_dim": {
                2: {"line", "triangle", "quad"},
                3: {"tetra", "hexahedron", "wedge", "pyramid", "triangle", "quad"},
            },
            "duplicate_blocks": "merge",
            "boundary_cell_types_by_dim": {
                2: {"line"},
                3: {"triangle", "quad"},
            },
            "point_sets": "none",
            "cell_sets": "none",
            "point_data": _data_none(),
            "cell_data": _data_selected(
                selector="first_int",
                required_dtype="integer",
                name_preserved=False,
                boundary_marker=True,
                max_ndim=1,
            ),
            "field_data": "none",
            "gmsh_periodic": False,
            "info": "none",
        }

    if format_name == "svg":
        return {
            "label": "SVG",
            "points": "2d_or_flat",
            "points_transformed": (
                "view-box scaling and a y-axis flip; original coordinates are not emitted"
            ),
            "cells": {"line", "triangle", "quad"},
            "duplicate_blocks": "ok",
            "point_sets": "none",
            "cell_sets": "none",
            "point_data": _data_none(),
            "cell_data": _data_none(),
            "field_data": "none",
            "gmsh_periodic": False,
            "info": "none",
        }

    if format_name == "tecplot":
        return {
            "label": "Tecplot",
            "points": "2d_or_3d",
            "points_float_only": True,
            "can_widen_points_to_float": True,
            "cells": _tecplot_cells(),
            "tecplot_zone_rules": True,
            "cell_expand": {"pyramid": "hexahedron", "wedge": "hexahedron"},
            "duplicate_blocks": "merge",
            "skips_mixed_dimensions": True,
            "point_sets": "none",
            "cell_sets": "none",
            "point_data": _data_all(
                max_ndim=2,
                forbidden_action="skip",
                forbidden_keys={"X", "Y", "Z", "x", "y", "z"},
                split_components=True,
            ),
            "cell_data": _data_all(
                max_ndim=2,
                forbidden_action="skip",
                forbidden_keys={"X", "Y", "Z", "x", "y", "z"},
                split_components=True,
            ),
            "field_data": "none",
            "gmsh_periodic": False,
            "info": "none",
        }

    if format_name == "tetgen":
        return {
            "label": "TetGen",
            "points": "3d_only",
            "points_float_only": True,
            "can_widen_points_to_float": True,
            "cells": {"tetra"},
            "duplicate_blocks": "reject",
            "point_sets": "none",
            "cell_sets": "none",
            "point_data": _data_all(
                max_ndim=1, integer_as_real=True, name_remap=True
            ),
            "cell_data": _data_all(
                max_ndim=1, integer_as_real=True, name_remap=True
            ),
            "field_data": "none",
            "gmsh_periodic": False,
            "info": "none",
        }

    if format_name == "ugrid":
        return {
            "label": "UGRID",
            "points": "3d_only",
            "points_float_only": True,
            "can_widen_points_to_float": True,
            "cells": {
                "triangle",
                "quad",
                "tetra",
                "pyramid",
                "wedge",
                "hexahedron",
            },
            "duplicate_blocks": "reject",
            "point_sets": "none",
            "cell_sets": "none",
            "point_data": _data_none(),
            "cell_data": _data_selected(
                selector="first_int",
                required_dtype="integer",
                name_preserved=False,
                boundary_only=True,
                max_ndim=1,
                fatal_list_bug=True,
            ),
            "field_data": "none",
            "gmsh_periodic": False,
            "info": "none",
        }

    if format_name == "wkt":
        return {
            "label": "WKT TIN",
            "points": "3d_only",
            "wkt_3d_tuples": True,
            "points_float_only": True,
            "can_widen_points_to_float": True,
            "points_dedup": True,
            "uses_only_referenced_points": True,
            "cells": {"triangle"},
            "duplicate_blocks": "merge",
            "point_sets": "none",
            "cell_sets": "none",
            "point_data": _data_none(),
            "cell_data": _data_none(),
            "field_data": "none",
            "gmsh_periodic": False,
            "info": "none",
        }

    # A writer with no catalogue entry must not be assumed lossless.
    return {
        "label": format_name,
        "points": "unknown",
        "cells": None,
        "duplicate_blocks": "merge",
        "point_sets": "unknown",
        "cell_sets": "unknown",
        "point_data": {"mode": "unknown"},
        "cell_data": {"mode": "unknown"},
        "field_data": "unknown",
        "gmsh_periodic": False,
        "info": "none",
    }
