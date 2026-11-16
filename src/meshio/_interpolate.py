"""Fill a missing frame of a time series by linear interpolation between the
adjacent frames.

The interpolation is deliberately conservative:

* Point identity, topology, and dimensionality of the two frames are verified
  before anything is blended.
* Only the common valid region is interpolated: fields must exist in both
  frames, and NaN entries propagate into the result.
* Discrete data (integer/bool dtypes, e.g. material ids, or anything listed in
  ``discrete_keys``) is never interpolated. It is carried over only if both
  frames agree exactly, otherwise it is dropped.
* If the two frames merely have the same number of points but the point
  mapping is unclear (coordinates differ and no ``point_id_key`` is given),
  interpolation is refused.
* The returned mesh carries provenance (source times and weights) in
  ``field_data`` so the synthetic frame cannot be mistaken for measured data.
"""

from __future__ import annotations

import numpy as np

from ._common import warn
from ._mesh import CellBlock, Mesh

__all__ = ["InterpolationError", "interpolate_frame"]


class InterpolationError(ValueError):
    """Raised when two frames cannot be interpolated safely."""


def _check_times(t_before: float, t_after: float, t: float) -> tuple[float, float]:
    if t_before == t_after:
        raise InterpolationError(
            f"source frames share the same time ({t_before}); cannot interpolate"
        )
    lo, hi = min(t_before, t_after), max(t_before, t_after)
    if not lo <= t <= hi:
        raise InterpolationError(
            f"target time {t} is outside [{lo}, {hi}]; extrapolation is refused"
        )
    w_after = (t - t_before) / (t_after - t_before)
    return 1.0 - w_after, w_after


def _point_correspondence(
    mesh_before: Mesh,
    mesh_after: Mesh,
    point_id_key: str | None,
    rtol: float,
    atol: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Return index arrays (into mesh_before/mesh_after points) of the common
    points, in output order."""
    points_before = mesh_before.points
    points_after = mesh_after.points

    if point_id_key is None:
        if len(points_before) != len(points_after):
            raise InterpolationError(
                f"point counts differ ({len(points_before)} vs "
                f"{len(points_after)}) and no point_id_key was given; "
                "point mapping is unclear"
            )
        if not np.allclose(points_before, points_after, rtol=rtol, atol=atol):
            raise InterpolationError(
                "point counts match but coordinates differ and no "
                "point_id_key was given; point mapping is unclear, "
                "refusing to interpolate"
            )
        idx = np.arange(len(points_before))
        return idx, idx

    for name, mesh in (("before", mesh_before), ("after", mesh_after)):
        if point_id_key not in mesh.point_data:
            raise InterpolationError(
                f"point_id_key '{point_id_key}' missing in {name} frame"
            )
        ids = np.asarray(mesh.point_data[point_id_key])
        if ids.ndim != 1:
            raise InterpolationError(
                f"point ids '{point_id_key}' in {name} frame are not 1D"
            )
        if len(np.unique(ids)) != len(ids):
            raise InterpolationError(
                f"point ids '{point_id_key}' in {name} frame are not unique; "
                "point mapping is unclear"
            )

    ids_before = np.asarray(mesh_before.point_data[point_id_key])
    ids_after = np.asarray(mesh_after.point_data[point_id_key])
    after_pos = {pid: i for i, pid in enumerate(ids_after)}
    keep_before = [i for i, pid in enumerate(ids_before) if pid in after_pos]
    if not keep_before:
        raise InterpolationError(
            f"no common point ids '{point_id_key}' between the frames"
        )
    keep_after = [after_pos[ids_before[i]] for i in keep_before]
    return np.asarray(keep_before), np.asarray(keep_after)


def _remap_cells(
    mesh: Mesh, point_map: np.ndarray
) -> dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]]:
    """Remap connectivity to output point indices, dropping cells that
    reference points outside the common region.

    Returns {cell_type: (connectivity, block_indices, row_indices)} where
    block/row indices locate each kept cell in the original mesh.
    """
    out: dict[str, list] = {}
    for block_idx, block in enumerate(mesh.cells):
        if block.type.startswith("polyhedron"):
            raise InterpolationError(
                "polyhedron cell blocks are not supported for interpolation"
            )
        data = np.asarray(block.data)
        remapped = point_map[data]
        valid = np.all(remapped >= 0, axis=1)
        if not np.any(valid):
            continue
        entry = out.setdefault(block.type, [[], [], []])
        entry[0].append(remapped[valid])
        rows = np.nonzero(valid)[0]
        entry[1].append(np.full(len(rows), block_idx, dtype=int))
        entry[2].append(rows)
    return {
        cell_type: (
            np.concatenate(conn_lists),
            np.concatenate(block_lists),
            np.concatenate(row_lists),
        )
        for cell_type, (conn_lists, block_lists, row_lists) in out.items()
    }


def _check_topology(
    mesh_before: Mesh,
    mesh_after: Mesh,
    keep_before: np.ndarray,
    keep_after: np.ndarray,
) -> tuple[list[CellBlock], dict[str, tuple[np.ndarray, np.ndarray]]]:
    """Verify that both frames have identical topology on the common points.

    Returns the output cell blocks (in mesh_before's order) and, per cell
    type, the (block/row) source locations in each frame for cell_data
    alignment.
    """
    map_before = np.full(len(mesh_before.points), -1, dtype=int)
    map_before[keep_before] = np.arange(len(keep_before))
    map_after = np.full(len(mesh_after.points), -1, dtype=int)
    map_after[keep_after] = np.arange(len(keep_after))

    topo_before = _remap_cells(mesh_before, map_before)
    topo_after = _remap_cells(mesh_after, map_after)

    if set(topo_before) != set(topo_after):
        raise InterpolationError(
            "cell types differ between frames on the common points: "
            f"{sorted(topo_before)} vs {sorted(topo_after)}"
        )

    out_blocks = []
    sources: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for cell_type, (conn_before, blocks_before, rows_before) in topo_before.items():
        conn_after, blocks_after, rows_after = topo_after[cell_type]
        if conn_before.shape != conn_after.shape:
            raise InterpolationError(
                f"topology of cell type '{cell_type}' differs between frames: "
                f"{conn_before.shape[0]} vs {conn_after.shape[0]} cells on the "
                "common points"
            )
        # align frame-after rows to frame-before rows via connectivity lookup
        lookup = {}
        for i, row in enumerate(conn_after):
            key = tuple(row)
            if key in lookup:
                raise InterpolationError(
                    f"duplicate '{cell_type}' cell {key} in after frame; "
                    "cell mapping is ambiguous"
                )
            lookup[key] = i
        try:
            align_after = np.array([lookup[tuple(row)] for row in conn_before])
        except KeyError:
            raise InterpolationError(
                f"topology of cell type '{cell_type}' differs between frames"
            ) from None

        out_blocks.append(CellBlock(cell_type, conn_before))
        sources[cell_type] = (
            (blocks_before, rows_before),
            (blocks_after[align_after], rows_after[align_after]),
        )
    return out_blocks, sources


def _gather(data_list, block_ids, row_ids):
    """Pick per-cell values from a cell_data block list given each kept
    cell's source (block, row), preserving the kept-cell order."""
    return np.concatenate(
        [
            np.asarray(data_list[block_id])[row_ids[block_ids == block_id]]
            for block_id in dict.fromkeys(block_ids)
        ]
    )


def _is_discrete(key: str, arrays, discrete_keys) -> bool:
    return key in discrete_keys or any(
        np.asarray(a).dtype.kind in "biu" for a in arrays
    )


def _blend(values_before, values_after, w_before, w_after, key: str, what: str):
    """Linear blend of two float arrays; NaN in either frame propagates so
    only the common valid region carries values."""
    a = np.asarray(values_before)
    b = np.asarray(values_after)
    if a.shape != b.shape:
        raise InterpolationError(
            f"{what} '{key}' has inconsistent dimensions between frames: "
            f"{a.shape} vs {b.shape}"
        )
    return w_before * a.astype(float) + w_after * b.astype(float)


def interpolate_frame(
    mesh_before: Mesh,
    mesh_after: Mesh,
    t_before: float,
    t_after: float,
    t: float,
    *,
    point_id_key: str | None = None,
    discrete_keys=(),
    rtol: float = 1.0e-9,
    atol: float = 1.0e-12,
) -> Mesh:
    """Interpolate a synthetic frame at time ``t`` between two mesh frames.

    Parameters
    ----------
    mesh_before, mesh_after : Mesh
        The adjacent frames bracketing the missing one.
    t_before, t_after, t : float
        Times of the source frames and of the frame to synthesize. ``t`` must
        lie between ``t_before`` and ``t_after``; extrapolation is refused.
    point_id_key : str, optional
        Name of a point_data array holding unique point ids. Required whenever
        the point mapping is not trivially given by identical coordinates.
    discrete_keys : iterable of str, optional
        Names of data arrays that must never be interpolated (e.g. material
        ids). Integer and boolean arrays are always treated as discrete.
    rtol, atol : float
        Tolerances for the coordinate identity check when no ``point_id_key``
        is given.

    Returns
    -------
    Mesh
        The interpolated frame. Its ``field_data`` records the source times
        and interpolation weights so the frame is not mistaken for measured
        data.
    """
    w_before, w_after = _check_times(t_before, t_after, t)

    if mesh_before.points.ndim != 2 or mesh_after.points.ndim != 2:
        raise InterpolationError("points arrays must be two-dimensional")
    if mesh_before.points.shape[1] != mesh_after.points.shape[1]:
        raise InterpolationError(
            "spatial dimension differs between frames: "
            f"{mesh_before.points.shape[1]} vs {mesh_after.points.shape[1]}"
        )

    keep_before, keep_after = _point_correspondence(
        mesh_before, mesh_after, point_id_key, rtol, atol
    )

    out_blocks, cell_sources = _check_topology(
        mesh_before, mesh_after, keep_before, keep_after
    )

    out_points = (
        w_before * mesh_before.points[keep_before]
        + w_after * mesh_after.points[keep_after]
    )

    # point data
    out_point_data = {}
    if point_id_key is not None:
        out_point_data[point_id_key] = np.asarray(
            mesh_before.point_data[point_id_key]
        )[keep_before]
    for key in mesh_before.point_data:
        if key == point_id_key:
            continue
        if key not in mesh_after.point_data:
            warn(f"point data '{key}' only present in before frame; dropped")
            continue
        a = np.asarray(mesh_before.point_data[key])[keep_before]
        b = np.asarray(mesh_after.point_data[key])[keep_after]
        if _is_discrete(key, (a, b), discrete_keys):
            if np.array_equal(a, b):
                out_point_data[key] = a
            else:
                warn(
                    f"point data '{key}' is discrete and differs between "
                    "frames; not interpolated, dropped"
                )
            continue
        out_point_data[key] = _blend(a, b, w_before, w_after, key, "point data")
    for key in mesh_after.point_data:
        if key != point_id_key and key not in mesh_before.point_data:
            warn(f"point data '{key}' only present in after frame; dropped")

    # cell data
    out_cell_data: dict[str, list] = {}
    for key in mesh_before.cell_data:
        if key not in mesh_after.cell_data:
            warn(f"cell data '{key}' only present in before frame; dropped")
            continue
        blocks = []
        for block in out_blocks:
            (blocks_b, rows_b), (blocks_a, rows_a) = cell_sources[block.type]
            a = _gather(mesh_before.cell_data[key], blocks_b, rows_b)
            b = _gather(mesh_after.cell_data[key], blocks_a, rows_a)
            if _is_discrete(key, (a, b), discrete_keys):
                if np.array_equal(a, b):
                    blocks.append(a)
                else:
                    warn(
                        f"cell data '{key}' is discrete and differs between "
                        "frames; not interpolated, dropped"
                    )
                    break
            else:
                blocks.append(_blend(a, b, w_before, w_after, key, "cell data"))
        else:
            out_cell_data[key] = blocks
    for key in mesh_after.cell_data:
        if key not in mesh_before.cell_data:
            warn(f"cell data '{key}' only present in after frame; dropped")

    if mesh_before.point_sets or mesh_after.point_sets:
        warn("point sets are not carried over to the interpolated frame")
    if mesh_before.cell_sets or mesh_after.cell_sets:
        warn("cell sets are not carried over to the interpolated frame")

    out = Mesh(
        points=out_points,
        cells=out_blocks,
        point_data=out_point_data,
        cell_data=out_cell_data,
    )
    # provenance: mark the frame as interpolated, with sources and weights
    out.field_data = {
        "interpolation:is_interpolated": np.array([1]),
        "interpolation:source_times": np.array([t_before, t_after]),
        "interpolation:weights": np.array([w_before, w_after]),
        "interpolation:target_time": np.array([t]),
    }
    return out
