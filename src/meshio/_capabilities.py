"""Concrete, data-driven preflight for mesh writes.

The report is built from the mesh's actual cell blocks, attribute dtypes and
shapes, and set relationships.  It never upgrades a whole file to "lossless"
merely because the target format knows a cell-type or attribute name.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ._exceptions import WriteError

PRESERVED = "preserved"
DEGRADED = "degraded"
UNREPRESENTABLE = "unrepresentable"

@dataclass(frozen=True)
class FeatureResult:
    """Result for one concrete feature instance."""

    feature: str
    status: str
    block: str | None = None
    detail: str = ""
    remedy: str | None = None

    @property
    def preserved(self) -> bool:
        return self.status == PRESERVED

    @property
    def degraded(self) -> bool:
        return self.status == DEGRADED

    @property
    def unrepresentable(self) -> bool:
        return self.status == UNREPRESENTABLE

    def __str__(self) -> str:
        where = f" [{self.block}]" if self.block else ""
        text = f"{self.status}: {self.feature}{where}"
        if self.detail:
            text += f" - {self.detail}"
        if self.remedy:
            text += f" (authorize with {self.remedy})"
        return text


@dataclass
class CapabilityReport:
    """A concrete capability report for one mesh and one target format."""

    file_format: str
    format_label: str
    results: list[FeatureResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add(
        self,
        feature: str,
        status: str,
        block: str | None = None,
        detail: str = "",
        remedy: str | None = None,
    ) -> None:
        if status == DEGRADED and remedy is None:
            remedy = "--allow-degraded"
        self.results.append(
            FeatureResult(feature, status, block=block, detail=detail, remedy=remedy)
        )

    def _select(self, status: str):
        return [r for r in self.results if r.status == status]

    @property
    def preserved(self) -> list[FeatureResult]:
        return self._select(PRESERVED)

    @property
    def degraded(self) -> list[FeatureResult]:
        return self._select(DEGRADED)

    @property
    def unrepresentable(self) -> list[FeatureResult]:
        return self._select(UNREPRESENTABLE)

    def ok(self, allow_degraded: bool = False) -> bool:
        if self.unrepresentable:
            return False
        if self.degraded and not allow_degraded:
            return False
        return True

    def worst(self) -> str:
        if self.unrepresentable:
            return UNREPRESENTABLE
        if self.degraded:
            return DEGRADED
        return PRESERVED

    def describe(self) -> str:
        lines = [f"Target format: {self.format_label} ({self.file_format})"]
        for title, items in (
            ("Preservable", self.preserved),
            ("Requires explicit degradation", self.degraded),
            ("Cannot be represented", self.unrepresentable),
        ):
            lines.append(f"{title} ({len(items)}):")
            if items:
                lines.extend(f"  - {item}" for item in items)
            else:
                lines.append("  - none")
        if self.notes:
            lines.append("Notes:")
            lines.extend(f"  - {note}" for note in self.notes)
        return "\n".join(lines)

    def reason(self, allow_degraded: bool = False) -> str:
        if self.unrepresentable:
            return (
                "Conversion rejected: the listed features cannot be represented "
                f"by {self.format_label}."
            )
        if self.degraded and not allow_degraded:
            return (
                "Conversion rejected: the listed degradations are not authorized. "
                "Re-run with --allow-degraded to accept them."
            )
        return ""


def _grouping_key(block):
    data = block.data
    width = None if isinstance(data, list) else getattr(data, "shape", (None, None))[1]
    return block.type, width


def _block_label(index: int, block) -> str:
    return f"cells[{index}] type={block.type} count={len(block)}"


def _feature(base: str, key: str) -> str:
    return f"{base} '{key}'"


def _dtype(dtype) -> str:
    return np.dtype(dtype).name


def _point_set_relation(mesh) -> tuple[bool, bool]:
    """Return (disjoint, complete) for the point-set family."""
    seen: set[int] = set()
    disjoint = True
    count = 0
    for arr in mesh.point_sets.values():
        values = {int(v) for v in np.asarray(arr).reshape(-1).tolist()}
        if seen & values:
            disjoint = False
        seen |= values
        count += len(values)
    complete = seen == set(range(len(mesh.points))) and count == len(seen)
    return disjoint, complete


def _cell_set_relation(mesh) -> tuple[bool, bool]:
    """Return (disjoint, complete) for the cell-set family, block by block."""
    disjoint = True
    complete = True
    for index, block in enumerate(mesh.cells):
        seen: set[int] = set()
        for members in mesh.cell_sets.values():
            if index >= len(members) or members[index] is None:
                continue
            values = {
                int(v) for v in np.asarray(members[index]).reshape(-1).tolist()
            }
            if seen & values:
                disjoint = False
            seen |= values
        if seen != set(range(len(block))):
            complete = False
    return disjoint, complete


# ---------------------------------------------------------------------------
# Points
# ---------------------------------------------------------------------------


def _check_points(report: CapabilityReport, mesh, tables: dict) -> None:
    points = np.asarray(mesh.points)
    if points.ndim != 2 or points.shape[1] == 0:
        report.add("points", UNREPRESENTABLE, detail=f"shape {points.shape}")
        return
    dim = points.shape[1]
    rule = tables.get("points", "unknown")

    if dim <= 3:
        report.add("points", PRESERVED, detail=f"dimension {dim}")
    else:
        report.add(
            "points",
            UNREPRESENTABLE,
            detail=f"dimension {dim}; coordinates cannot be represented",
        )
        return

    if rule == "3d_only" and dim != 3:
        report.add(
            "points dimension",
            UNREPRESENTABLE,
            detail=f"dimension {dim}; the writer requires 3D coordinates",
        )
        return
    elif rule == "3d_pad" and dim == 2:
        report.add(
            "points dimension 2D -> 3D",
            DEGRADED,
            detail="z=0 is appended, so the file no longer records a 2D mesh",
        )
    elif rule == "2d_or_flat":
        if dim == 3 and not np.allclose(points[:, 2], 0.0, rtol=0.0, atol=1.0e-14):
            report.add(
                "points",
                UNREPRESENTABLE,
                detail="3D points with non-zero z cannot be flattened to SVG",
            )
    elif rule == "dim2" and dim != 2:
        report.add(
            "points dimension",
            UNREPRESENTABLE,
            detail=f"dimension {dim}; the writer requires 2D coordinates",
        )
    elif rule == "2d_or_3d" and dim not in (2, 3):
        report.add(
            "points dimension",
            UNREPRESENTABLE,
            detail=f"dimension {dim}; the writer requires 2D or 3D coordinates",
        )
    elif rule == "max3" and dim > 3:
        report.add(
            "points dimension",
            UNREPRESENTABLE,
            detail=f"dimension {dim}; at most 3 coordinates are writable",
        )

    kind = points.dtype.kind
    if kind not in "fiu":
        report.add(
            "points dtype",
            UNREPRESENTABLE,
            detail=f"{_dtype(points.dtype)} coordinates are not numeric",
        )
        return
    if tables.get("points_float_only") and kind != "f":
        if tables.get("can_widen_points_to_float"):
            values = points.astype(np.float64)
            exact = np.array_equal(
                values.astype(points.dtype), points, equal_nan=True
            )
            if exact:
                report.add(
                    "points dtype",
                    PRESERVED,
                    detail=f"{_dtype(points.dtype)} integers widen exactly to float64",
                )
            else:
                report.add(
                    "points dtype",
                    DEGRADED,
                    detail=f"{_dtype(points.dtype)} is approximated as float64",
                )
        else:
            report.add(
                "points dtype",
                UNREPRESENTABLE,
                detail=f"{_dtype(points.dtype)} coordinates are not accepted",
            )
            return
    if kind == "f" and points.dtype.itemsize < 8:
        report.add(
            "points precision",
            PRESERVED,
            detail=f"{_dtype(points.dtype)} widens losslessly to float64",
        )

    allowed = tables.get("points_dtypes")
    if allowed is not None and _dtype(points.dtype) not in allowed:
        report.add(
            "points dtype",
            UNREPRESENTABLE,
            detail=f"{_dtype(points.dtype)} is not in {sorted(allowed)}",
        )

    transform = tables.get("points_transformed")
    if transform:
        report.add("coordinate values", DEGRADED, detail=transform)

    if tables.get("points_dedup"):
        unique = np.unique(points, axis=0)
        if len(unique) != len(points):
            report.add(
                "duplicate coordinates",
                UNREPRESENTABLE,
                detail=(
                    f"{len(points) - len(unique)} duplicate point rows would be "
                    "collapsed into one vertex each"
                ),
            )

    if tables.get("uses_only_referenced_points"):
        referenced = _referenced_points(mesh)
        if len(referenced) != len(points):
            report.add(
                "unreferenced points",
                UNREPRESENTABLE,
                detail=(
                    f"{len(points) - len(referenced)} points are not referenced by "
                    "any written cell and would be dropped"
                ),
            )


def _num_nodes_for_block(where: str) -> int | None:
    import re

    match = re.search(r"type=([A-Za-z0-9_]+)", where)
    if match is None:
        return None
    from ._common import num_nodes_per_cell

    return num_nodes_per_cell.get(match.group(1))


def _referenced_points(mesh) -> set[int]:
    used: set[int] = set()
    for block in mesh.cells:
        data = block.data
        if isinstance(data, list):
            for cell in data:
                for face in cell:
                    used.update(int(v) for v in np.asarray(face).reshape(-1))
        else:
            used.update(int(v) for v in np.asarray(data).reshape(-1))
    return used


def _precision_check(report, feature, array, lossy_transform, block=None):
    array = np.asarray(array)
    if array.dtype.kind != "f":
        return
    transformed = lossy_transform(array)
    if not np.array_equal(array, transformed, equal_nan=True):
        report.add(
            feature,
            DEGRADED,
            block,
            detail="values change in the target text representation",
        )


def _check_point_precision(report: CapabilityReport, mesh, tables: dict, options: dict) -> None:
    points = np.asarray(mesh.points)
    digits = tables.get("points_precision_digits")
    if digits is not None and points.dtype.kind == "f":

        def fmt(array):
            return np.vectorize(
                lambda x: float(np.format_float_scientific(x, precision=digits))
            )(array)

        _precision_check(report, "points precision", points, fmt)

    narrowed = tables.get("binary_narrows_cell_data", {})
    if narrowed and options.get("binary"):
        for name, target in narrowed.items():
            if name in mesh.cell_data:
                report.add(
                    f"cell data '{name}' precision",
                    DEGRADED,
                    detail=f"binary output stores values as {target}",
                )

    cast = tables.get("points_cast")
    binary_only = tables.get("points_cast_binary", False)
    if cast is not None and points.dtype.kind == "f":
        target = np.dtype(cast)
        if points.dtype.itemsize > target.itemsize:
            binary = options.get("binary")
            when_binary = binary_only and (binary is None or binary)
            if not when_binary:
                if binary_only:
                    report.add(
                        "points precision",
                        PRESERVED,
                        detail=(
                            f"ASCII text preserves {_dtype(points.dtype)}; "
                            f"binary would store {cast}"
                        ),
                    )
                else:
                    report.add(
                        "points precision",
                        DEGRADED,
                        detail=f"{_dtype(points.dtype)} is stored as {cast}",
                    )


# ---------------------------------------------------------------------------
# Cells
# ---------------------------------------------------------------------------


def _check_cells(report: CapabilityReport, mesh, tables: dict) -> None:
    cells = list(mesh.cells)
    if not cells:
        report.add("cells", PRESERVED, detail="no cells")
        return

    dim = np.asarray(mesh.points).shape[1]
    if "cells_by_dim" in tables:
        supported = tables["cells_by_dim"].get(dim)
        if supported is None:
            report.add(
                "cells",
                UNREPRESENTABLE,
                detail=f"no cell rule for {dim}D meshes",
            )
            return
    else:
        supported = tables.get("cells")

    if supported is None:
        for index, block in enumerate(cells):
            report.add(
                _feature("cell block", block.type),
                UNREPRESENTABLE,
                _block_label(index, block),
                detail=(
                    "writer has no declared support for this cell type; the file "
                    "cannot be written without dropping this block"
                ),
            )
        return

    polyhedron_blocks = [b for b in cells if b.type.startswith("polyhedron")]
    if polyhedron_blocks:
        if not tables.get("polyhedron"):
            for index, block in enumerate(cells):
                if block.type.startswith("polyhedron"):
                    report.add(
                        _feature("cells", block.type),
                        UNREPRESENTABLE,
                        _block_label(index, block),
                        detail="writer cannot serialize polyhedral cells",
                    )
            return
        if len(cells) > 1:
            report.add(
                "polyhedral cells",
                UNREPRESENTABLE,
                detail=(
                    "VTU cannot mix polyhedral cells with any other cell block"
                ),
            )

    if tables.get("needs_dim_tags_for_multiple_blocks") and len(cells) > 1:
        missing = []
        if "gmsh:dim_tags" not in mesh.point_data:
            missing.append("point_data['gmsh:dim_tags']")
        if "gmsh:geometrical" not in mesh.cell_data:
            missing.append("cell_data['gmsh:geometrical']")
        if missing:
            report.add(
                "multiple cell blocks",
                UNREPRESENTABLE,
                detail=(
                    "Gmsh needs " + " and ".join(missing) + " to write more than "
                    "one cell block"
                ),
            )
        elif len(mesh.cell_data["gmsh:geometrical"]) != len(cells):
            report.add(
                "multiple cell blocks",
                UNREPRESENTABLE,
                detail=(
                    "cell_data['gmsh:geometrical'] has "
                    f"{len(mesh.cell_data['gmsh:geometrical'])} entries for "
                    f"{len(cells)} cell blocks"
                ),
            )

    boundary_types = tables.get("boundary_cell_types_by_dim")
    if boundary_types is not None:
        present = boundary_types.get(dim, set())
        for index, block in enumerate(cells):
            if block.type in present:
                report.add(
                    _feature("cells", block.type),
                    UNREPRESENTABLE,
                    _block_label(index, block),
                    detail=(
                        "SU2 treats this block as boundary markers, but the "
                        "current writer cannot serialize boundary CellBlocks"
                    ),
                )

    mixed = tables.get("mixed_cells")
    if mixed is not None and len(cells) > 1:
        for index, block in enumerate(cells):
            if block.type not in mixed:
                report.add(
                    _feature("cell block", block.type),
                    UNREPRESENTABLE,
                    _block_label(index, block),
                    detail="XDMF mixed topology cannot encode this cell type",
                )

    tecplot_mixed_base = None
    if tables.get("tecplot_zone_rules"):
        _check_tecplot_cells(report, mesh, tables, supported)
        supported = None

    if supported is not None:
        counts: dict[tuple, int] = {}
        for index, block in enumerate(cells):
            key = _grouping_key(block)
            counts[key] = counts.get(key, 0) + 1
            label = _block_label(index, block)
            if block.type not in supported:
                if not (block.type.startswith("polyhedron") and tables.get("polyhedron")):
                    report.add(
                        _feature("cells", block.type),
                        UNREPRESENTABLE,
                        label,
                        detail=(
                            "not accepted by this writer; the file cannot be written "
                            "without dropping this block"
                        ),
                    )
                    continue
            collapse = tables.get("cell_collapse", {}).get(block.type)
            expand = tables.get("cell_expand", {}).get(block.type)
            type_change = tables.get("cell_type_change", {}).get(block.type)
            if collapse is not None:
                report.add(
                    _feature("cells", block.type),
                    DEGRADED,
                    label,
                    detail=f"written as {collapse}; higher-order nodes are discarded",
                )
            elif expand is not None:
                report.add(
                    _feature("cells", block.type),
                    DEGRADED,
                    label,
                    detail=f"topology is expanded to {expand}",
                )
            elif type_change is not None:
                target = type_change(minor=block.data.shape[1])
                if target is None:
                    report.add(_feature("cells", block.type), PRESERVED, label)
                else:
                    report.add(
                        _feature("cells", block.type),
                        DEGRADED,
                        label,
                        detail=f"read back as {target}",
                    )
            else:
                report.add(_feature("cells", block.type), PRESERVED, label)

        duplicates = {k: v for k, v in counts.items() if v > 1}
        if duplicates:
            detail = ", ".join(
                f"{k[0]}{'' if k[1] is None else f'({k[1]})'} x{v}"
                for k, v in sorted(duplicates.items(), key=lambda kv: str(kv[0]))
            )
            mode = tables.get("duplicate_blocks", "ok")
            if mode == "ok" and tables.get("needs_unique_block_tags"):
                geom = mesh.cell_data.get("gmsh:geometrical")
                tags = []
                if geom is not None and len(geom) == len(cells):
                    tags = [int(np.asarray(g).reshape(-1)[0]) for g in geom]
                if len(set(tags)) != len(tags):
                    report.add(
                        "cell block grouping",
                        UNREPRESENTABLE,
                        detail=(
                            f"repeated blocks ({detail}) need distinct "
                            "gmsh:geometrical entity tags; the reader and entity "
                            "block are ambiguous otherwise"
                        ),
                    )
                    mode = None
            if mode is None:
                pass
            elif mode == "reject":
                report.add(
                    "cell block grouping",
                    UNREPRESENTABLE,
                    detail=f"repeated blocks ({detail}) would overwrite one another",
                )
            elif mode == "ok":
                report.add(
                    "cell block grouping",
                    PRESERVED,
                    detail=f"repeated blocks ({detail}) keep their boundaries",
                )
            else:
                report.add(
                    "cell block grouping",
                    DEGRADED,
                    detail=f"repeated blocks ({detail}) are merged into one block",
                )

    _check_one_cell_type(report, mesh, tables)

def _check_one_cell_type(report, mesh, tables):
    priority = tables.get("one_cell_type_priority")
    if not priority:
        return
    present = {block.type for block in mesh.cells}
    chosen = next((t for t in priority if t in present), None)
    if chosen is None:
        return
    for index, block in enumerate(mesh.cells):
        if block.type != chosen:
            report.add(
                _feature("cells", block.type),
                UNREPRESENTABLE,
                _block_label(index, block),
                detail=(
                    f"writer keeps only {chosen} cells and discards this block; "
                    "the resulting file no longer matches the input mesh"
                ),
            )


def _check_tecplot_cells(report, mesh, tables, supported) -> None:
    # The writer keeps one zone type; mixed topologies are expanded into the
    # zone's base type, and mixed 2D/3D drops all 2D blocks.
    blocks = [(i, b) for i, b in enumerate(mesh.cells) if b.type in supported]
    dims = {2 if b.dim == 2 else 3 if b.dim == 3 else 1 for _, b in blocks}
    two_d = []
    three_d = []
    for index, block in enumerate(mesh.cells):
        if block.type not in supported:
            report.add(
                _feature("cells", block.type),
                UNREPRESENTABLE,
                _block_label(index, block),
                detail="Tecplot does not support this cell type",
            )
        elif block.dim == 2:
            two_d.append((index, block))
        elif block.dim == 3:
            three_d.append((index, block))

    if two_d and three_d:
        for index, block in two_d:
            report.add(
                _feature("cells", block.type),
                UNREPRESENTABLE,
                _block_label(index, block),
                detail="Tecplot skips all 2D blocks when 3D blocks are present",
            )
        active = three_d
        base = "hexahedron"
    elif three_d:
        active = three_d
        base = "hexahedron"
    elif two_d:
        active = two_d
        base = "quad"
    else:
        active = [(i, b) for i, b in enumerate(mesh.cells) if b.type in supported]
        base = "line"

    unique_types = {b.type for _, b in active}
    mixed = len(unique_types) > 1
    for index, block in active:
        label = _block_label(index, block)
        if block.type == base:
            report.add(_feature("cells", block.type), PRESERVED, label)
        elif block.type == "pyramid" or block.type == "wedge":
            report.add(
                _feature("cells", block.type),
                DEGRADED,
                label,
                detail=f"written as {base}; topology changes",
            )
        elif mixed:
            report.add(
                _feature("cells", block.type),
                DEGRADED,
                label,
                detail=f"expanded into the common {base} zone",
            )
        else:
            report.add(_feature("cells", block.type), PRESERVED, label)


def _check_cell_indices(report: CapabilityReport, mesh, tables: dict) -> None:
    target = tables.get("cell_index_dtype")
    if target is None:
        return
    info = np.iinfo(target)
    dtypes = set()
    for index, block in enumerate(mesh.cells):
        if block.type.startswith("polyhedron"):
            continue
        data = np.asarray(block.data)
        if not data.size:
            continue
        dtypes.add(data.dtype)
        label = _block_label(index, block)
        if data.dtype.itemsize > np.dtype(target).itemsize and data.max() > info.max:
            report.add(
                "cell indices",
                UNREPRESENTABLE,
                label,
                detail=f"indices exceed the {np.dtype(target).name} range",
            )
        elif data.dtype.itemsize > np.dtype(target).itemsize:
            report.add(
                "cell indices",
                DEGRADED,
                label,
                detail=(
                    f"{_dtype(data.dtype)} indices are cast to "
                    f"{np.dtype(target).name}"
                ),
            )
    if len(dtypes) > 1:
        report.add(
            "cell connectivity dtype",
            UNREPRESENTABLE,
            detail=f"writer requires one connectivity dtype, got {sorted(map(_dtype, dtypes))}",
        )


# ---------------------------------------------------------------------------
# Sets
# ---------------------------------------------------------------------------


def _check_sets(report: CapabilityReport, mesh, tables: dict) -> None:
    point_rule = tables.get("point_sets", "unknown")
    for name, values in mesh.point_sets.items():
        feature = _feature("point set", name)
        values_arr = np.asarray(values).reshape(-1)
        count = len(values_arr)
        preview = ", ".join(str(int(v)) for v in values_arr[:8])
        if count > 8:
            preview += ", ..."
        point_note = f"points: [{preview}]" if count else "points: []"
        if point_rule == "native":
            values = np.asarray(values)
            if values.dtype.kind not in "iu":
                report.add(
                    feature,
                    UNREPRESENTABLE,
                    detail=(
                        f"{values.dtype} point ids; the writer formats ids as "
                        "integers; " + point_note
                    ),
                )
            else:
                report.add(feature, PRESERVED, detail=f"{count} points; {point_note}")
        elif point_rule == "to_data":
            disjoint, complete = _point_set_relation(mesh)
            detail = (
                "the set object is replaced by integer point data "
                "(name and set type change)"
            )
            if disjoint and complete:
                detail += "; this particular partition is reversible"
            else:
                problems = []
                if not disjoint:
                    problems.append("sets overlap")
                if not complete:
                    problems.append("some points belong to no set")
                detail += "; " + " and ".join(problems) + " cannot be recovered"
            report.add(feature, DEGRADED, detail=detail + "; " + point_note)
        elif point_rule == "none":
            report.add(
                feature,
                DEGRADED,
                detail="set is dropped; the writer has no point set channel; "
                + point_note,
            )
        else:
            report.add(
                feature,
                UNREPRESENTABLE,
                detail="writer support is not declared" + block_note,
            )

    cell_rule = tables.get("cell_sets", "unknown")
    for name, members in mesh.cell_sets.items():
        feature = _feature("cell set", name)
        members = list(members)
        total = sum(0 if m is None else len(np.asarray(m)) for m in members)
        blocks = [
            _block_label(index, block)
            for index, (block, member) in enumerate(zip(mesh.cells, members))
            if member is not None and len(np.asarray(member)) > 0
        ]
        block_note = "; blocks: " + ", ".join(blocks) if blocks else ""
        if cell_rule == "native":
            if any(m is None for m in members):
                report.add(
                    feature,
                    UNREPRESENTABLE,
                    detail="set has missing block entries (None)",
                )
            else:
                report.add(feature, PRESERVED, detail=f"{total} memberships{block_note}")
        elif cell_rule == "native_renamed":
            report.add(
                feature,
                DEGRADED,
                detail=(
                    "memberships survive, but the set name is rewritten "
                    "(for example zone:<name>:1)"
                ),
            )
        elif cell_rule == "gmsh_bounding":
            if name != "gmsh:bounding_entities":
                report.add(
                    feature,
                    DEGRADED,
                    detail=(
                        "Gmsh 4.1 only writes cell_sets['gmsh:bounding_entities']; "
                        "this set family is dropped" + block_note
                    ),
                )
            elif any(m is None for m in members):
                report.add(
                    feature,
                    UNREPRESENTABLE,
                    detail="bounding entity list has missing block entries"
                    + block_note,
                )
            elif "gmsh:dim_tags" not in mesh.point_data:
                report.add(
                    feature,
                    DEGRADED,
                    detail=(
                        "the writer skips the entire Entities block without "
                        "point_data['gmsh:dim_tags'], so these memberships are dropped"
                        + block_note
                    ),
                )
            else:
                report.add(
                    feature,
                    PRESERVED,
                    detail="written as Gmsh bounding entities" + block_note,
                )
        elif cell_rule == "to_data":
            disjoint, complete = _cell_set_relation(mesh)
            detail = (
                "the set object is replaced by integer cell data "
                "(name and set type change)"
            )
            if disjoint and complete:
                detail += "; this particular partition is reversible"
            else:
                problems = []
                if not disjoint:
                    problems.append("sets overlap")
                if not complete:
                    problems.append("some cells belong to no set")
                detail += "; " + " and ".join(problems) + " cannot be recovered"
            report.add(feature, DEGRADED, detail=detail + block_note)
        elif cell_rule == "none":
            report.add(
                feature,
                DEGRADED,
                detail="set is dropped; the writer has no cell set channel"
                + block_note,
            )
        else:
            report.add(
                feature,
                UNREPRESENTABLE,
                detail="writer support is not declared",
            )

    point_tags = getattr(mesh, "point_tags", None)
    cell_tags = getattr(mesh, "cell_tags", None)
    if point_tags:
        if tables.get("med_tags"):
            report.add("point_tags", PRESERVED, detail=f"{len(point_tags)} families")
        else:
            report.add(
                "point_tags",
                DEGRADED,
                detail="dropped; only the MED writer understands point_tags",
            )
    if cell_tags:
        if tables.get("med_tags"):
            report.add("cell_tags", PRESERVED, detail=f"{len(cell_tags)} families")
        else:
            report.add(
                "cell_tags",
                DEGRADED,
                detail="dropped; only the MED writer understands cell_tags",
            )


# ---------------------------------------------------------------------------
# Attribute data
# ---------------------------------------------------------------------------


def _where_point(name, array) -> str:
    array = np.asarray(array)
    return f"point_data['{name}'] shape={array.shape} dtype={_dtype(array.dtype)}"


def _where_cell(name, index, block, array) -> str:
    array = np.asarray(array)
    return (
        f"cell_data['{name}'][{index}] for {_block_label(index, block)} "
        f"shape={array.shape} dtype={_dtype(array.dtype)}"
    )


def _select_key(keys, data, rule):
    allowed = rule.get("allowed_keys")
    if allowed is not None:
        return next((k for k in keys if k in allowed), None)
    if rule.get("strict_first_int"):
        for key in keys:
            arrays = data[key] if isinstance(data[key], list) else [data[key]]
            if arrays and all(np.asarray(a).dtype.kind in "iu" for a in arrays):
                return key
        return None
    preserved = rule.get("preserved_keys")
    if preserved:
        for key in keys:
            if key in preserved:
                return key
    preferred = rule.get("preferred")
    if preferred is not None and preferred in keys:
        return preferred
    for key in keys:
        arrays = data[key] if isinstance(data[key], list) else [data[key]]
        if arrays and all(np.asarray(a).dtype.kind in "iu" for a in arrays):
            return key
    return None


def _evaluate_array(report, base, name, array, rule, where) -> None:
    array = np.asarray(array)
    feature = _feature(base, name)
    mode = rule.get("mode", "unknown")

    if rule.get("fatal"):
        report.add(
            feature,
            UNREPRESENTABLE,
            where,
            detail="the writer raises instead of serializing this data class",
        )
        return
    if mode == "none":
        report.add(
            feature,
            DEGRADED,
            where,
            detail="attribute is dropped; the writer has no channel for this data class",
        )
        return
    if mode != "all":
        report.add(
            feature,
            UNREPRESENTABLE,
            where,
            detail="writer support is not declared",
        )
        return

    allowed_dtypes = rule.get("dtypes")
    if allowed_dtypes is not None and _dtype(array.dtype) not in allowed_dtypes:
        report.add(
            feature,
            UNREPRESENTABLE,
            where,
            detail=f"dtype {_dtype(array.dtype)} is not supported",
        )
        return

    max_ndim = rule.get("max_ndim", 3)
    if array.ndim > max_ndim:
        if rule.get("oversized_action") == "skip":
            report.add(
                feature,
                DEGRADED,
                where,
                detail=(
                    f"{array.ndim} dimensions exceed the writer limit {max_ndim}; "
                    "the writer skips this attribute"
                ),
            )
        else:
            report.add(
                feature,
                UNREPRESENTABLE,
                where,
                detail=f"{array.ndim} dimensions exceed the writer limit {max_ndim}",
            )
        return

    components = rule.get("components")
    if components is not None:
        if array.ndim == 1:
            ncomp = 1
        elif array.ndim == 2:
            ncomp = array.shape[1]
        else:
            ncomp = array.shape[-1]
        if ncomp not in components:
            report.add(
                feature,
                UNREPRESENTABLE,
                where,
                detail=f"{ncomp} components; allowed {sorted(components)}",
            )
            return

    if rule.get("field_name_no_space") and " " in name:
        report.add(
            feature,
            UNREPRESENTABLE,
            where,
            detail="field names cannot contain spaces",
        )
        return

    forbidden = rule.get("forbidden_keys")
    if forbidden and name in forbidden:
        if rule.get("forbidden_action") == "skip":
            report.add(
                feature,
                DEGRADED,
                where,
                detail="the writer skips attributes named like coordinates",
            )
        else:
            report.add(
                feature,
                UNREPRESENTABLE,
                where,
                detail="Tecplot reserves coordinate variable names",
            )
        return

    name_max = rule.get("name_max_length")
    if name_max is not None and len(name) > name_max:
        report.add(
            feature,
            UNREPRESENTABLE,
            where,
            detail=f"name length {len(name)} exceeds the format limit {name_max}",
        )
        return

    if rule.get("sanitize_names") and (" " in name or "," in name):
        report.add(
            feature,
            DEGRADED,
            where,
            detail="spaces/commas in the attribute name are rewritten on read-back",
        )
        return

    if rule.get("split_components") and array.ndim == 2 and array.shape[1] > 1:
        report.add(
            feature,
            DEGRADED,
            where,
            detail=(
                f"{array.shape[1]} components become separate scalar variables "
                f"{name}_0..{name}_{array.shape[1] - 1}"
            ),
        )
        return

    if rule.get("integer_as_real") and array.dtype.kind in "iu":
        report.add(
            feature,
            DEGRADED,
            where,
            detail=(
                f"{_dtype(array.dtype)} values are stored as real and read back "
                "as floating-point"
            ),
        )
        return

    if rule.get("required_dtype") == "integer" and array.dtype.kind not in "iu":
        report.add(
            feature,
            UNREPRESENTABLE,
            where,
            detail=f"{_dtype(array.dtype)} cannot be stored in the integer label slot",
        )
        return

    if rule.get("med_elno") and array.ndim == 3:
        if array.shape[1] != _num_nodes_for_block(where):
            report.add(
                feature,
                DEGRADED,
                where,
                detail=(
                    "3D cell data whose middle dimension is not the cell's node "
                    "count becomes MED ELGA, which this writer silently skips"
                ),
            )
            return

    if rule.get("avs_material_from_first_int") and rule.get("_is_material_key"):
        report.add(
            feature,
            DEGRADED,
            where,
            detail=(
                "this first integer cell array becomes avsucd:material and loses "
                "its original name"
            ),
        )
        return

    report.add(feature, PRESERVED, where)


def _check_point_data(report, mesh, rule):
    if not mesh.point_data:
        return
    special = rule.get("special_keys", {})
    dropped = rule.get("dropped_keys", set()) | rule.get("rejected_keys", set())
    ordinary = {}
    for name, data in mesh.point_data.items():
        where = _where_point(name, data)
        if name in dropped:
            report.add(
                _feature("point data", name),
                DEGRADED,
                where,
                detail="attribute is explicitly dropped by this writer",
            )
        elif name in special:
            value = special[name]
            if isinstance(value, tuple):
                status, detail = value
            else:
                status, detail = value, ""
            report.add(_feature("point data", name), status, where, detail=detail)
        else:
            ordinary[name] = data

    if rule.get("mode") == "selected":
        selected = _select_key(list(ordinary), ordinary, rule)

        for name, data in ordinary.items():
            where = _where_point(name, data)
            if name != selected:
                report.add(
                    _feature("point data", name),
                    DEGRADED,
                    where,
                    detail="attribute is dropped; the writer keeps one selected array",
                )
                continue
            if name in rule.get("preserved_keys", set()) | rule.get(
                "roundtrip_keys", set()
            ):
                report.add(
                    _feature("point data", name),
                    PRESERVED,
                    where,
                    detail="writer's dedicated key for this attribute is preserved",
                )
            elif rule.get("name_preserved") is False:
                report.add(
                    _feature("point data", name),
                    DEGRADED,
                    where,
                    detail="values are written, but the attribute name is not preserved",
                )
            elif rule.get("repurposed"):
                report.add(
                    _feature("point data", name),
                    DEGRADED,
                    where,
                    detail=f"values are repurposed as {rule['repurposed']}",
                )
            else:
                _evaluate_array(report, "point data", name, data, rule, where)
        return

    for name, data in ordinary.items():
        _evaluate_array(report, "point data", name, data, rule, _where_point(name, data))

    if rule.get("name_remap"):
        for name in ordinary:
            report.add(
                _feature("point data name", name),
                DEGRADED,
                _where_point(name, ordinary[name]),
                detail="name is replaced by positional tetgen:attrN keys",
            )

    _check_data_precision(report, "point data", ordinary, rule)


def _check_cell_data(report, mesh, rule):
    if not mesh.cell_data:
        return
    special = rule.get("special_keys", {})
    dropped = rule.get("dropped_keys", set()) | rule.get("rejected_keys", set())
    ordinary = {}
    for name, blocks in mesh.cell_data.items():
        if name in dropped:
            for index, block in enumerate(mesh.cells):
                report.add(
                    _feature("cell data", name),
                    DEGRADED,
                    _where_cell(name, index, block, blocks[index]),
                    detail="attribute is explicitly dropped by this writer",
                )
        elif name in special:
            for index, block in enumerate(mesh.cells):
                where = _where_cell(name, index, block, blocks[index])
                report.add(
                    _feature("cell data", name),
                    PRESERVED,
                    where,
                    detail=special[name]
                    if isinstance(special[name], str)
                    else "",
                )
        else:
            ordinary[name] = blocks

    if rule.get("fatal_list_bug") and ordinary:
        for name, blocks in ordinary.items():
            for index, block in enumerate(mesh.cells):
                report.add(
                    _feature("cell data", name),
                    UNREPRESENTABLE,
                    _where_cell(name, index, block, blocks[index]),
                    detail="the UGRID writer raises on any per-block cell data",
                )
        return

    if rule.get("mode") == "selected":
        selected = _select_key(list(ordinary), ordinary, rule)

        for name, blocks in ordinary.items():
            for index, (block, data) in enumerate(zip(mesh.cells, blocks)):
                where = _where_cell(name, index, block, data)
                array = np.asarray(data)
                feature = _feature("cell data", name)
                if name != selected:
                    report.add(
                        feature,
                        DEGRADED,
                        where,
                        detail="attribute is dropped; the writer keeps one selected array",
                    )
                    continue
                if array.ndim > rule.get("max_ndim", 1):
                    report.add(
                        feature,
                        UNREPRESENTABLE,
                        where,
                        detail=f"writer accepts at most {rule.get('max_ndim', 1)}D values",
                    )
                elif rule.get("boundary_only") and block.type not in {"triangle", "quad"}:
                    report.add(
                        feature,
                        UNREPRESENTABLE,
                        where,
                        detail="writer only stores boundary tags for triangle/quad blocks",
                    )
                elif name in rule.get("preserved_keys", set()) | rule.get(
                    "roundtrip_keys", set()
                ):
                    report.add(
                        feature,
                        PRESERVED,
                        where,
                        detail="writer's dedicated key for this attribute is preserved",
                    )
                elif rule.get("name_preserved") is False:
                    report.add(
                        feature,
                        DEGRADED,
                        where,
                        detail="values are written, but the attribute name is not preserved",
                    )
                elif rule.get("boundary_marker"):
                    report.add(
                        feature,
                        DEGRADED,
                        where,
                        detail=(
                            "values become SU2 boundary-marker tags; non-boundary "
                            "cells are counted separately"
                        ),
                    )
                elif rule.get("repurposed"):
                    report.add(
                        feature,
                        DEGRADED,
                        where,
                        detail=f"values are repurposed as {rule['repurposed']}",
                    )
                else:
                    _evaluate_array(report, "cell data", name, array, rule, where)
        return

    if rule.get("mode") == "all":
        material_key = None
        if rule.get("avs_material_from_first_int"):
            for name, blocks in ordinary.items():
                if blocks and all(np.asarray(b).dtype.kind in "iu" for b in blocks):
                    material_key = name
                    break
        for name, blocks in ordinary.items():
            local_rule = dict(rule)
            local_rule["_is_material_key"] = name == material_key
            for index, (block, data) in enumerate(zip(mesh.cells, blocks)):
                where = _where_cell(name, index, block, data)
                array = np.asarray(data)
                constant = rule.get("constant_keys", set())
                if name in constant:
                    unique = np.unique(array)
                    if len(unique) <= 1:
                        report.add(
                            _feature("cell data", name),
                            PRESERVED,
                            where,
                            detail="one constant entity tag per block",
                        )
                    else:
                        report.add(
                            _feature("cell data", name),
                            DEGRADED,
                            where,
                            detail=(
                                f"{len(unique)} distinct values; the writer stores "
                                "one entity tag per block"
                            ),
                        )
                    continue
                _evaluate_array(report, "cell data", name, array, local_rule, where)
        _check_data_precision(report, "cell data", ordinary, rule)
        return

    for name, blocks in ordinary.items():
        for index, (block, data) in enumerate(zip(mesh.cells, blocks)):
            if rule.get("fatal"):
                report.add(
                    _feature("cell data", name),
                    UNREPRESENTABLE,
                    _where_cell(name, index, block, data),
                    detail="the H5M writer raises on per-block cell data",
                )
            else:
                report.add(
                    _feature("cell data", name),
                    DEGRADED,
                    _where_cell(name, index, block, data),
                    detail="attribute is dropped; the writer has no channel for this data class",
                )


def _check_data_precision(report, base, data, rule):
    digits = rule.get("real_digits")
    if digits is None:
        return
    for name, value in data.items():
        arrays = value if isinstance(value, list) else [value]
        for index, array in enumerate(arrays):
            array = np.asarray(array)
            if array.dtype.kind != "f":
                continue
            where = _where_point(name, array) if base == "point data" else (
                f"cell_data['{name}'][{index}]"
            )
            fmt = np.vectorize(lambda x: float(f"{x:.{digits}e}"))
            _precision_check(
                report,
                _feature(base + " precision", name),
                array,
                fmt,
                where,
            )


def _check_field_data(report, mesh, rule):
    for name, value in mesh.field_data.items():
        feature = _feature("field data", name)
        arr = np.asarray(value)
        if rule == "none":
            report.add(
                feature,
                DEGRADED,
                detail="field data is dropped; the writer has no field data channel",
            )
        elif rule == "physical_names":
            if arr.shape == (2,) and arr.dtype.kind in "iu":
                report.add(feature, PRESERVED, detail=f"physical tag {arr.tolist()}")
            else:
                report.add(
                    feature,
                    DEGRADED,
                    detail=(
                        "Gmsh skips field data that is not a [number, dimension] "
                        f"integer pair; got shape {arr.shape} dtype {arr.dtype}"
                    ),
                )
        elif rule == "names":
            ok = (
                arr.size == 2
                and arr.dtype.kind in "iu"
                or (arr.ndim == 2 and arr.shape[1] == 2)
            )
            if ok:
                report.add(feature, PRESERVED, detail=f"name/index pair {arr.tolist()}")
            else:
                report.add(
                    feature,
                    UNREPRESENTABLE,
                    detail=f"expected [index, dimension] pairs, got {arr.shape}",
                )
        elif rule == "med_names":
            if name == "med:nom":
                report.add(feature, PRESERVED, detail="component names array")
            else:
                report.add(
                    feature,
                    UNREPRESENTABLE,
                    detail=(
                        "MED only reads component names from field_data['med:nom']; "
                        "other field data is ignored"
                    ),
                )
        else:
            report.add(
                feature,
                UNREPRESENTABLE,
                detail="writer field data support is not declared",
            )


# ---------------------------------------------------------------------------
# Miscellaneous mesh attributes
# ---------------------------------------------------------------------------


def _check_misc(report, mesh, tables, options):
    if getattr(mesh, "gmsh_periodic", None) is not None:
        if tables.get("gmsh_periodic"):
            report.add("gmsh periodic links", PRESERVED, detail="gmsh_periodic")
        else:
            report.add(
                "gmsh periodic links",
                UNREPRESENTABLE,
                detail="only Gmsh writers can encode gmsh_periodic",
            )

    info = getattr(mesh, "info", None)
    if info:
        mode = tables.get("info", "none")
        if mode == "netgen":
            keys = set(info)
            if keys <= {"netgen:identifications", "netgen:identificationtypes"}:
                report.add("mesh.info", PRESERVED, detail=", ".join(sorted(keys)))
            else:
                report.add(
                    "mesh.info",
                    UNREPRESENTABLE,
                    detail=f"Netgen stores only identification records, got {sorted(keys)}",
                )
        else:
            report.add(
                "mesh.info",
                UNREPRESENTABLE,
                detail=f"{report.format_label} has no info channel",
            )

    for attr, label in (("points_id", "point IDs"), ("cells_id", "cell IDs")):
        value = getattr(mesh, attr, None)
        if value is None:
            continue
        if attr == "points_id":
            expected = np.arange(1, len(mesh.points) + 1)
        else:
            expected = [
                np.arange(1, len(block) + 1) for block in mesh.cells
            ]
        if attr == "points_id":
            sequential = np.array_equal(np.asarray(value), expected)
        else:
            sequential = all(
                np.array_equal(np.asarray(v), e)
                for v, e in zip(value, expected)
            )
        if tables.get("label") == "Nastran" and sequential:
            report.add(label, PRESERVED, detail="sequential IDs are renumbered identically")
        else:
            report.add(
                label,
                DEGRADED if tables.get("label") == "Nastran" else UNREPRESENTABLE,
                detail="writer ignores original IDs and renumbers entities",
            )


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def capability_report(mesh, file_format: str, options: dict | None = None) -> CapabilityReport:
    """Compute a concrete preflight report for ``mesh`` -> ``file_format``.

    ``options`` may contain writer keyword arguments that change storage, e.g.
    ``{"binary": True}`` for STL.
    """
    from ._capability_tables import get_tables
    from ._helpers import _writer_map

    if file_format not in _writer_map:
        raise KeyError(
            f"Unknown output format '{file_format}'. Pick one of "
            f"{sorted(_writer_map)}"
        )

    options = {} if options is None else options
    tables = get_tables(file_format)
    report = CapabilityReport(
        file_format=file_format,
        format_label=tables.get("label", file_format),
    )

    required = tables.get("requires")
    if required:
        import importlib.util

        if importlib.util.find_spec(required) is None:
            report.add(
                "writer dependency",
                UNREPRESENTABLE,
                detail=f"Python module '{required}' is not installed",
            )
            return report

    if file_format == "xdmf":
        import importlib.util

        data_format = options.get("data_format", "HDF")
        if data_format == "HDF" and importlib.util.find_spec("h5py") is None:
            report.add(
                "writer dependency",
                UNREPRESENTABLE,
                detail="XDMF HDF output needs h5py; use data_format='XML'",
            )
            return report
        if data_format == "XML" and options.get("compression") is not None:
            report.add(
                "compression",
                DEGRADED,
                detail="XML output ignores HDF compression settings",
            )

    _check_points(report, mesh, tables)
    _check_point_precision(report, mesh, tables, options)
    _check_cells(report, mesh, tables)
    _check_cell_indices(report, mesh, tables)
    _check_sets(report, mesh, tables)
    _check_point_data(report, mesh, tables.get("point_data", {"mode": "unknown"}))
    _check_cell_data(report, mesh, tables.get("cell_data", {"mode": "unknown"}))
    _check_field_data(report, mesh, tables.get("field_data", "unknown"))
    _check_misc(report, mesh, tables, options)

    return report


def require_writable(
    mesh,
    file_format: str,
    allow_degraded: bool = False,
    options: dict | None = None,
) -> CapabilityReport:
    """Return a report, or raise :class:`WriteError` when loss is not authorized."""
    report = capability_report(mesh, file_format, options=options)
    if not report.ok(allow_degraded=allow_degraded):
        raise WriteError(_blocked_message(report, allow_degraded))
    return report


def write_with_preflight(
    filename,
    mesh,
    file_format: str | None = None,
    allow_degraded: bool = False,
    options: dict | None = None,
    **kwargs,
):
    """Preflight and write with the regular writer."""
    from ._helpers import _filetypes_from_path, write

    target = file_format
    if target is None:
        from pathlib import Path

        target = _filetypes_from_path(Path(filename))[0]

    merged_options = dict(options or {})
    merged_options.update(kwargs)
    require_writable(
        mesh,
        target,
        allow_degraded=allow_degraded,
        options=merged_options,
    )
    return write(filename, mesh, file_format=target, **kwargs)


def _blocked_message(report: CapabilityReport, allow_degraded: bool) -> str:
    lines = [report.reason(allow_degraded), "", report.describe()]
    if report.unrepresentable:
        lines.append("")
        lines.append(
            "Unrepresentable features cannot be authorized away; convert or remove "
            "the listed blocks first."
        )
    elif report.degraded and not allow_degraded:
        lines.append("")
        lines.append("Re-run with --allow-degraded to accept the listed degradations.")
    return "\n".join(lines)


def report_to_dict(report: CapabilityReport) -> dict[str, Any]:
    def encode(items):
        return [
            {
                "feature": item.feature,
                "status": item.status,
                "block": item.block,
                "detail": item.detail,
                "remedy": item.remedy,
            }
            for item in items
        ]

    return {
        "format": report.file_format,
        "label": report.format_label,
        "status": report.worst(),
        "preserved": encode(report.preserved),
        "degraded": encode(report.degraded),
        "unrepresentable": encode(report.unrepresentable),
        "notes": report.notes,
    }
