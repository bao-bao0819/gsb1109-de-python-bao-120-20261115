"""Conservative transfer of cell data between meshes.

The meshes are assumed to live in the same coordinate system; no rigid-body
transformation is applied.  Only linear cell types approved by the caller take
part in the transfer.

Extensive cell data are cell totals.  A source cell total ``q`` is split among
the target cells in proportion to the *actual* intersection measure,

    q_ij = q_i * |S_i intersect T_j| / |S_i|.

Consequently, the sum of all allocations of ``q_i`` plus its unallocated part
is exactly ``q_i``.  There is deliberately no row normalization; an uncovered
part of the source remains visible as unallocated data.

Intensive cell data are averaged with the intersection measure multiplied by
caller-provided source-cell weights.  Target coverage is always reported
separately, so a partially covered target is never presented as if it had been
covered completely.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

import numpy as np

from ._common import num_nodes_per_cell

__all__ = [
    "CellOverlap",
    "CellTransferResult",
    "cell_overlaps",
    "conservative_transfer",
    "map_cell_data",
    "overlap_volumes",
    "remap_cell_data",
    "transfer_cell_data",
    "transfer_cell_field",
    "transfer_cell_fields",
]


_FACES = {
    "tetra": ((0, 1, 2), (0, 1, 3), (0, 2, 3), (1, 2, 3)),
    "pyramid": (
        (0, 1, 2, 3),
        (0, 1, 4),
        (1, 2, 4),
        (2, 3, 4),
        (3, 0, 4),
    ),
    "wedge": (
        (0, 1, 2),
        (3, 4, 5),
        (0, 1, 4, 3),
        (1, 2, 5, 4),
        (2, 0, 3, 5),
    ),
    "hexahedron": (
        (0, 1, 2, 3),
        (4, 5, 6, 7),
        (0, 1, 5, 4),
        (1, 2, 6, 5),
        (2, 3, 7, 6),
        (3, 0, 4, 7),
    ),
}

_PLANAR_TYPES = {"triangle", "quad", "polygon"}
_LINEAR_TYPES = (
    "line",
    "triangle",
    "quad",
    "polygon",
    "tetra",
    "pyramid",
    "wedge",
    "hexahedron",
)
DEFAULT_CELL_TYPES = _LINEAR_TYPES


class _CellGeometry:
    __slots__ = (
        "dim",
        "measure",
        "bbox_min",
        "bbox_max",
        "polygon",
        "faces",
        "line",
        "plane_normal",
        "plane_offset",
        "planar",
    )

    def __init__(self):
        self.dim = 0
        self.measure = 0.0
        self.bbox_min = None
        self.bbox_max = None
        self.polygon = None
        self.faces = None
        self.line = None
        self.plane_normal = None
        self.plane_offset = 0.0
        self.planar = True


def _newell_normal(points):
    normal = np.zeros(3)
    for k, point in enumerate(points):
        nxt = points[(k + 1) % len(points)]
        normal[0] += (point[1] - nxt[1]) * (point[2] + nxt[2])
        normal[1] += (point[2] - nxt[2]) * (point[0] + nxt[0])
        normal[2] += (point[0] - nxt[0]) * (point[1] + nxt[1])
    return normal


def _canonical_normal(normal):
    normal = np.asarray(normal, dtype=float)
    normal = normal / np.linalg.norm(normal)
    first = int(np.flatnonzero(np.abs(normal) > 1.0e-12)[0])
    if normal[first] < 0.0:
        normal = -normal
    return normal


def _canonical_frame(normal):
    """Build the same in-plane axes for any pair of parallel planes."""
    normal = _canonical_normal(normal)
    axis = np.zeros(3)
    axis[np.argmin(np.abs(normal))] = 1.0
    u = np.cross(normal, axis)
    u /= np.linalg.norm(u)
    v = np.cross(normal, u)
    return normal, u, v


def _project(points, normal):
    if points.shape[1] == 2:
        return points.copy()
    _, u, v = _canonical_frame(normal)
    return np.column_stack((points @ u, points @ v))


def _signed_area(polygon):
    if len(polygon) < 3:
        return 0.0
    x = polygon[:, 0]
    y = polygon[:, 1]
    return 0.5 * (np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def _polygon_area(polygon):
    return abs(_signed_area(polygon))


def _deduplicate_polygon(polygon, eps):
    if len(polygon) == 0:
        return polygon
    out = [polygon[0]]
    for point in polygon[1:]:
        if np.linalg.norm(point - out[-1]) > eps:
            out.append(point)
    if len(out) > 1 and np.linalg.norm(out[0] - out[-1]) <= eps:
        out.pop()
    return np.asarray(out)


def _triangulate(polygon):
    """Triangulate a simple 2D polygon by ear clipping."""
    polygon = _deduplicate_polygon(np.asarray(polygon, dtype=float), 0.0)
    if len(polygon) < 3:
        return []
    if len(polygon) == 3:
        return [polygon]

    if _signed_area(polygon) < 0.0:
        polygon = polygon[::-1]

    extent = max(np.ptp(polygon, axis=0).max(), 1.0)
    eps = 1.0e-12 * extent
    indices = list(range(len(polygon)))
    triangles = []

    while len(indices) > 3:
        found = False
        for position in range(len(indices)):
            ia = indices[position - 1]
            ib = indices[position]
            ic = indices[(position + 1) % len(indices)]
            a, b, c = polygon[ia], polygon[ib], polygon[ic]
            cross = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
            if cross <= eps:
                continue

            others = [
                polygon[index]
                for index in indices
                if index not in (ia, ib, ic)
            ]
            if any(
                _point_in_triangle_strict(point, a, b, c, eps)
                for point in others
            ):
                continue

            triangles.append(np.array([a, b, c]))
            del indices[position]
            found = True
            break

        if not found:
            # Collinear or numerically awkward remaining polygon.
            for position in range(1, len(indices) - 1):
                triangles.append(
                    np.array(
                        [
                            polygon[indices[0]],
                            polygon[indices[position]],
                            polygon[indices[position + 1]],
                        ]
                    )
                )
            return triangles

    triangles.append(np.array([polygon[index] for index in indices]))
    return triangles


def _point_in_triangle_strict(point, a, b, c, eps):
    """Inside test used for ear clipping (boundary points block an ear)."""
    signs = []
    for p, q in ((a, b), (b, c), (c, a)):
        cross = (q[0] - p[0]) * (point[1] - p[1]) - (q[1] - p[1]) * (
            point[0] - p[0]
        )
        signs.append(cross)
    return all(value >= -eps for value in signs) or all(
        value <= eps for value in signs
    )


def _clip_polygon(polygon, a, b, eps):
    edge = b - a
    values = edge[0] * (polygon[:, 1] - a[1]) - edge[1] * (polygon[:, 0] - a[0])
    inside = values >= -eps
    if np.all(inside):
        return polygon
    if not np.any(inside):
        return np.empty((0, 2))

    out = []
    count = len(polygon)
    for k in range(count):
        p = polygon[k]
        q = polygon[(k + 1) % count]
        vp = values[k]
        vq = values[(k + 1) % count]
        if vp >= -eps:
            out.append(p)
        if (vp >= -eps) != (vq >= -eps):
            denominator = vp - vq
            if abs(denominator) > 0.0:
                out.append(p + (vp / denominator) * (q - p))

    if len(out) < 3:
        return np.empty((0, 2))
    return _deduplicate_polygon(np.asarray(out), eps)


def _triangle_intersection_area(subject, clip):
    signed = _signed_area(clip)
    if signed < 0.0:
        clip = clip[::-1]
    if abs(signed) == 0.0:
        return 0.0

    extent = max(
        np.ptp(subject, axis=0).max(),
        np.ptp(clip, axis=0).max(),
        1.0,
    )
    eps = 1.0e-12 * extent

    result = subject
    for k in range(len(clip)):
        result = _clip_polygon(result, clip[k], clip[(k + 1) % len(clip)], eps)
        if len(result) < 3:
            return 0.0
    return _polygon_area(result)


def _is_convex(polygon):
    if len(polygon) < 4:
        return True
    extent = max(np.ptp(polygon, axis=0).max(), 1.0)
    eps = 1.0e-12 * extent
    signs = []
    count = len(polygon)
    for k in range(count):
        a = polygon[k]
        b = polygon[(k + 1) % count]
        c = polygon[(k + 2) % count]
        cross = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
        if abs(cross) <= eps:
            return False
        signs.append(cross > 0.0)
    return all(signs) or not any(signs)


def _intersection_2d(subject_polygon, clip_polygon):
    if _is_convex(subject_polygon) and _is_convex(clip_polygon):
        return _triangle_intersection_area(subject_polygon, clip_polygon)

    subject_triangles = _triangulate(subject_polygon)
    if not subject_triangles:
        return 0.0

    clip_triangles = _triangulate(clip_polygon)
    if not clip_triangles:
        return 0.0

    total = 0.0
    for subject in subject_triangles:
        for clip in clip_triangles:
            total += _triangle_intersection_area(subject, clip)
    return total


def _face_volume(faces):
    faces = [np.asarray(face, dtype=float) for face in faces if len(face) >= 3]
    if not faces:
        return 0.0

    vertices = np.concatenate(faces)
    center = vertices.mean(axis=0)

    volume = 0.0
    for face in faces:
        for k in range(1, len(face) - 1):
            a = face[0] - center
            b = face[k] - center
            c = face[k + 1] - center
            volume += abs(np.dot(a, np.cross(b, c))) / 6.0
    return volume


def _make_geometry(cell_type, vertices, tolerance):
    geometry = _CellGeometry()
    vertices = np.asarray(vertices, dtype=float)

    if cell_type in _FACES:
        if vertices.shape[1] != 3:
            raise ValueError(f"Cell type '{cell_type}' requires 3D coordinates.")
        expected = num_nodes_per_cell[cell_type]
        if vertices.shape[0] != expected:
            raise ValueError(
                f"Cell type '{cell_type}' expects {expected} vertices, "
                f"got {vertices.shape[0]}."
            )
        geometry.dim = 3
        geometry.faces = [vertices[list(face)] for face in _FACES[cell_type]]
        geometry.measure = _face_volume(geometry.faces)
        check = vertices
    elif cell_type in _PLANAR_TYPES:
        if cell_type != "polygon":
            expected = num_nodes_per_cell[cell_type]
            if vertices.shape[0] != expected:
                raise ValueError(
                    f"Cell type '{cell_type}' expects {expected} vertices, "
                    f"got {vertices.shape[0]}."
                )
        elif vertices.shape[0] < 3:
            raise ValueError("Cell type 'polygon' expects at least 3 vertices.")

        if vertices.shape[1] == 2:
            normal = np.array([0.0, 0.0, 1.0])
            geometry.polygon = vertices.copy()
        else:
            raw_normal = _newell_normal(vertices)
            if np.linalg.norm(raw_normal) <= 1.0e-14:
                raise ValueError(
                    f"Cell type '{cell_type}' has degenerate vertices."
                )
            normal = _canonical_normal(raw_normal)
            offsets = vertices @ normal
            geometry.plane_offset = float(offsets.mean())
            extent = max(np.ptp(vertices, axis=0).max(), 1.0)
            geometry.planar = bool(
                np.max(np.abs(offsets - geometry.plane_offset))
                <= tolerance * extent
            )
            geometry.polygon = _project(vertices, normal)

        geometry.dim = 2
        geometry.plane_normal = normal
        geometry.measure = _polygon_area(geometry.polygon)
        check = vertices if vertices.shape[1] == 3 else geometry.polygon
    elif cell_type == "line":
        if vertices.shape[0] != 2:
            raise ValueError(
                f"Cell type 'line' expects 2 vertices, got {vertices.shape[0]}."
            )
        geometry.dim = 1
        geometry.line = vertices
        geometry.measure = float(np.linalg.norm(vertices[1] - vertices[0]))
        check = vertices
    else:
        raise ValueError(
            f"Unsupported cell type '{cell_type}'. Supported types are "
            f"{', '.join(DEFAULT_CELL_TYPES)}."
        )

    if not np.isfinite(geometry.measure):
        raise ValueError(f"Cell type '{cell_type}' has non-finite coordinates.")
    geometry.bbox_min = np.min(check, axis=0)
    geometry.bbox_max = np.max(check, axis=0)
    return geometry


def _planes(faces):
    vertices = np.concatenate([np.asarray(face, dtype=float) for face in faces])
    center = vertices.mean(axis=0)
    planes = []
    for face in faces:
        face = np.asarray(face, dtype=float)
        normal = _newell_normal(face)
        norm = np.linalg.norm(normal)
        if norm <= 1.0e-14:
            continue
        normal = normal / norm
        offset = float(np.dot(normal, face.mean(axis=0)))
        if np.dot(normal, center) > offset:
            normal = -normal
            offset = -offset
        planes.append((normal, offset))
    return planes


def _clip_polygon_3d(polygon, normal, offset, eps):
    values = polygon @ normal - offset
    inside = values <= eps
    if np.all(inside):
        return polygon
    if not np.any(inside):
        return np.empty((0, 3))

    out = []
    count = len(polygon)
    for k in range(count):
        p = polygon[k]
        q = polygon[(k + 1) % count]
        vp = values[k]
        vq = values[(k + 1) % count]
        if vp <= eps:
            out.append(p)
        if (vp <= eps) != (vq <= eps):
            denominator = vp - vq
            if abs(denominator) > 0.0:
                out.append(p + (vp / denominator) * (q - p))
    if len(out) < 3:
        return np.empty((0, 3))
    return _deduplicate_polygon(np.asarray(out), eps)


def _order_on_plane(points, normal):
    center = points.mean(axis=0)
    axis = np.zeros(3)
    axis[np.argmin(np.abs(normal))] = 1.0
    u = np.cross(normal, axis)
    u /= np.linalg.norm(u)
    v = np.cross(normal, u)
    relative = points - center
    angles = np.arctan2(relative @ v, relative @ u)
    return points[np.argsort(angles)]


def _unique_points(points, eps):
    if len(points) == 0:
        return np.empty((0, points.shape[1]))
    out = [points[0]]
    for point in points[1:]:
        if all(np.linalg.norm(point - other) > eps for other in out):
            out.append(point)
    return np.asarray(out)


def _polygon_area_on_plane(points, normal):
    if len(points) < 3:
        return 0.0
    center = points.mean(axis=0)
    area = np.zeros(3)
    for k in range(len(points)):
        area += np.cross(points[k] - center, points[(k + 1) % len(points)] - center)
    return 0.5 * abs(np.dot(area, normal))


def _intersection_3d(subject_faces, clip_faces):
    clip_vertices = np.concatenate([np.asarray(f, dtype=float) for f in clip_faces])
    subject_vertices = np.concatenate([np.asarray(f, dtype=float) for f in subject_faces])
    extent = max(
        np.ptp(np.concatenate((clip_vertices, subject_vertices)), axis=0).max(),
        1.0,
    )
    eps = 1.0e-11 * extent

    faces = [np.asarray(face, dtype=float) for face in subject_faces]
    for normal, offset in _planes(clip_faces):
        distances = [face @ normal - offset for face in faces if len(face) > 0]
        if not distances:
            return 0.0
        minimum = min(float(dist.min()) for dist in distances)
        maximum = max(float(dist.max()) for dist in distances)
        if maximum <= eps:
            continue
        if minimum >= -eps:
            return 0.0

        new_faces = []
        cap_points = []
        for face in faces:
            clipped = _clip_polygon_3d(face, normal, offset, eps)
            if len(clipped) >= 3:
                new_faces.append(clipped)
            on_plane = clipped[np.abs(clipped @ normal - offset) <= 10.0 * eps]
            if len(on_plane):
                cap_points.append(on_plane)

        if cap_points:
            points = _unique_points(np.concatenate(cap_points), 10.0 * eps)
            if len(points) >= 3:
                ordered = _order_on_plane(points, normal)
                if _polygon_area_on_plane(ordered, normal) > eps * eps:
                    new_faces.append(ordered)

        faces = new_faces
        if not faces:
            return 0.0

    return _face_volume(faces)


def _intersection_1d(first, second, tolerance):
    first = np.asarray(first, dtype=float)
    second = np.asarray(second, dtype=float)
    direction = first[1] - first[0]
    length = float(np.linalg.norm(direction))
    if length <= 0.0:
        return 0.0
    direction = direction / length

    for point in second:
        relative = point - first[0]
        perpendicular = relative - np.dot(relative, direction) * direction
        if np.linalg.norm(perpendicular) > tolerance * max(length, 1.0):
            return 0.0

    first_range = np.array([0.0, length])
    second_range = np.sort(
        [
            np.dot(second[0] - first[0], direction),
            np.dot(second[1] - first[0], direction),
        ]
    )
    return float(
        max(
            0.0,
            min(first_range[1], second_range[1])
            - max(first_range[0], second_range[0]),
        )
    )


def _intersection_measure(source, target, tolerance):
    if source.dim != target.dim:
        return 0.0
    if source.dim == 1:
        return _intersection_1d(source.line, target.line, tolerance)
    if source.dim == 2:
        if not source.planar or not target.planar:
            return 0.0
        if not np.allclose(
            source.plane_normal,
            target.plane_normal,
            rtol=0.0,
            atol=tolerance * 10.0,
        ):
            return 0.0
        if abs(source.plane_offset - target.plane_offset) > tolerance * max(
            source.measure**0.5, target.measure**0.5, 1.0
        ):
            return 0.0
        return _intersection_2d(source.polygon, target.polygon)
    if source.dim == 3:
        return _intersection_3d(source.faces, target.faces)
    return 0.0


def _as_name_list(value):
    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    return list(value)


def _cell_lengths(mesh):
    return [len(block) for block in mesh.cells]


def _flatten_block_data(data, lengths):
    if isinstance(data, np.ndarray) and data.ndim == 1 and len(data) == sum(lengths):
        return data
    if len(data) != len(lengths):
        raise ValueError(f"Expected {len(lengths)} cell-data blocks, got {len(data)}.")
    arrays = [np.asarray(item) for item in data]
    for array, length in zip(arrays, lengths):
        if len(array) != length:
            raise ValueError(
                f"Cell-data block has length {len(array)}, expected {length}."
            )
    return np.concatenate(arrays) if arrays else np.empty((0,))


def _split_block_data(data, lengths):
    offsets = np.cumsum([0] + list(lengths))
    return [data[offsets[k] : offsets[k + 1]] for k in range(len(lengths))]


def _normalize_weights(weights, field, lengths, count):
    if weights is None:
        return np.ones(count)
    value = weights.get(field, 1.0) if isinstance(weights, Mapping) else weights
    if np.isscalar(value):
        result = np.full(count, float(value))
    else:
        try:
            candidate = np.asarray(value, dtype=float)
        except (TypeError, ValueError):
            candidate = None
        if candidate is not None and candidate.ndim == 1 and len(candidate) == count:
            result = candidate
        else:
            result = np.asarray(
                _flatten_block_data(value, lengths), dtype=float
            )
            if result.ndim != 1 or len(result) != count:
                raise ValueError(
                    f"Weights for field '{field}' must be scalar or have one "
                    f"value per source cell ({count}), got shape {result.shape}."
                )
    if np.any(result < 0.0) or not np.all(np.isfinite(result)):
        raise ValueError(f"Weights for field '{field}' must be finite and non-negative.")
    return result


class CellOverlap:
    """Pairwise intersection measures between approved source and target cells.

    Cell indices are flattened across the cell blocks of the respective mesh.
    Only pairs with a positive intersection measure are stored.
    """

    def __init__(
        self,
        source_mesh,
        target_mesh,
        source_indices,
        target_indices,
        volumes,
        source_volumes,
        target_volumes,
    ):
        self.source_mesh = source_mesh
        self.target_mesh = target_mesh
        self.source_indices = np.asarray(source_indices, dtype=int)
        self.target_indices = np.asarray(target_indices, dtype=int)
        self.volumes = np.asarray(volumes, dtype=float)
        self.source_volumes = np.asarray(source_volumes, dtype=float)
        self.target_volumes = np.asarray(target_volumes, dtype=float)
        self.source_lengths = _cell_lengths(source_mesh)
        self.target_lengths = _cell_lengths(target_mesh)

    @property
    def rows(self):
        return self.source_indices

    @property
    def cols(self):
        return self.target_indices

    @property
    def shape(self):
        return (len(self.source_volumes), len(self.target_volumes))

    def matrix(self):
        matrix = np.zeros(self.shape)
        np.add.at(matrix, (self.source_indices, self.target_indices), self.volumes)
        return matrix

    def toarray(self):
        return self.matrix()

    def todense(self):
        return self.matrix()

    def __array__(self, dtype=None):
        matrix = self.matrix()
        return matrix if dtype is None else matrix.astype(dtype)

    def __len__(self):
        return self.shape[0]

    def __getitem__(self, key):
        return self.matrix()[key]

    def target_covered_fraction(self):
        covered = np.bincount(
            self.target_indices,
            weights=self.volumes,
            minlength=len(self.target_volumes),
        )
        fraction = np.zeros_like(covered, dtype=float)
        positive = self.target_volumes > 0.0
        fraction[positive] = covered[positive] / self.target_volumes[positive]
        return fraction

    def source_allocated_fraction(self):
        allocated = np.bincount(
            self.source_indices,
            weights=self.volumes,
            minlength=len(self.source_volumes),
        )
        fraction = np.zeros_like(allocated, dtype=float)
        positive = self.source_volumes > 0.0
        fraction[positive] = allocated[positive] / self.source_volumes[positive]
        return fraction


class CellTransferResult(dict):
    """Transferred target cell data plus explicit coverage diagnostics.

    The object is a regular mapping of field names to lists of per-cell-block
    target arrays, so it can be passed directly as ``Mesh.cell_data``.
    Additional attributes document conservation and coverage.
    """

    def __init__(
        self,
        cell_data,
        allocated,
        unallocated,
        target_coverage,
        target_covered,
        target_uncovered_volume,
        overlap,
        source_lengths,
        target_lengths,
        tolerance,
    ):
        super().__init__(cell_data)
        self.allocated = allocated
        self.unallocated = unallocated
        self.target_coverage = target_coverage
        self.target_covered = target_covered
        self.target_uncovered_volume = target_uncovered_volume
        self.overlap = overlap
        self.source_lengths = source_lengths
        self.target_lengths = target_lengths
        self.tolerance = tolerance

    @property
    def target_cell_data(self):
        return dict(self)

    @property
    def cell_data(self):
        return dict(self)

    @property
    def source_allocated(self):
        return self.allocated

    @property
    def source_unallocated(self):
        return self.unallocated

    @property
    def uncovered_targets(self):
        """Per-block indices of target cells with no overlap at all."""
        return [
            np.flatnonzero(coverage <= self.tolerance)
            for coverage in self.target_coverage
        ]

    @property
    def partially_covered_targets(self):
        """Per-block indices of target cells covered only partially."""
        return [
            np.flatnonzero(
                (coverage > self.tolerance)
                & (coverage < 1.0 - 10.0 * self.tolerance)
            )
            for coverage in self.target_coverage
        ]

    @property
    def uncovered(self):
        return self.uncovered_targets

    @property
    def coverage(self):
        return self.target_coverage

    @property
    def covered(self):
        return self.target_covered

    def target_coverage_array(self):
        if not self.target_coverage:
            return np.empty(0)
        return np.concatenate(self.target_coverage)

    def target_uncovered_mask(self):
        """Boolean arrays marking target cells with no overlap at all."""
        return [
            coverage <= self.tolerance for coverage in self.target_coverage
        ]

    def as_cell_data(self):
        return dict(self)

    def apply_to(self, target):
        """Copy the transferred blocks into ``target.cell_data``."""
        for name, blocks in self.items():
            target.cell_data[name] = [
                np.array(block, copy=True) for block in blocks
            ]
        return target


def _validate_cell_types(mesh, allowed, label):
    for block in mesh.cells:
        if block.type not in allowed:
            raise ValueError(
                f"{label} contains cell type '{block.type}', which is not in the "
                f"caller-provided cell_types {tuple(allowed)}."
            )


def _self_overlap(geometries, tolerance, threshold):
    """True if any cell overlaps other cells of the same mesh above threshold."""
    if len(geometries) < 2:
        return False

    bbox_min = np.array([geometry.bbox_min for geometry in geometries])
    bbox_max = np.array([geometry.bbox_max for geometry in geometries])
    scale = max(np.ptp(np.concatenate((bbox_min, bbox_max)), axis=0).max(), 1.0)
    bbox_tolerance = tolerance * scale

    for index, geometry in enumerate(geometries):
        if geometry.measure <= 0.0:
            continue
        candidates = np.flatnonzero(
            np.all(
                (bbox_min <= geometry.bbox_max + bbox_tolerance)
                & (bbox_max + bbox_tolerance >= geometry.bbox_min),
                axis=1,
            )
        )
        candidates = candidates[candidates != index]
        total = 0.0
        for other_index in candidates:
            other = geometries[other_index]
            if other.measure <= 0.0:
                continue
            intersection = _intersection_measure(geometry, other, tolerance)
            pair_threshold = tolerance * max(geometry.measure, other.measure)
            if intersection > pair_threshold:
                total += intersection
                if total > threshold:
                    return True

    return False


def _build_geometries(mesh, tolerance):
    points = np.asarray(mesh.points, dtype=float)
    geometries = []
    for block in mesh.cells:
        for row in block.data:
            connectivity = np.asarray(row, dtype=int)
            if connectivity.ndim != 1:
                raise ValueError(
                    f"Cell block '{block.type}' must contain one connectivity "
                    "row per cell."
                )
            if np.any(connectivity < 0) or np.any(connectivity >= len(points)):
                raise ValueError(
                    f"Cell block '{block.type}' references a point outside the mesh."
                )
            geometries.append(
                _make_geometry(block.type, points[connectivity], tolerance)
            )
    return geometries


def cell_overlaps(
    source,
    target,
    cell_types: Iterable[str] | None = None,
    tolerance: float = 1.0e-12,
) -> CellOverlap:
    """Compute actual source/target intersection measures.

    Parameters
    ----------
    source, target:
        :class:`meshio.Mesh` instances in the same coordinate system.
    cell_types:
        Iterable of linear cell types approved by the caller.  Every source and
        target cell block must be one of these types.  If omitted, the built-in
        linear types (line, triangle, quad, polygon, tetra, pyramid, wedge,
        hexahedron) are allowed.
    tolerance:
        Relative tolerance used for geometric predicates.
    """
    if cell_types is None:
        allowed = DEFAULT_CELL_TYPES
    else:
        allowed = tuple(cell_types)
        unknown = set(allowed) - set(DEFAULT_CELL_TYPES)
        if unknown:
            raise ValueError(
                f"Unsupported cell_types {sorted(unknown)}. Supported types are "
                f"{', '.join(DEFAULT_CELL_TYPES)}."
            )

    _validate_cell_types(source, allowed, "source")
    _validate_cell_types(target, allowed, "target")

    source_points = np.asarray(source.points, dtype=float)
    target_points = np.asarray(target.points, dtype=float)
    if source_points.ndim != 2 or target_points.ndim != 2:
        raise ValueError("Mesh points must be two-dimensional arrays.")
    if source_points.shape[1] not in (2, 3) or target_points.shape[1] not in (2, 3):
        raise ValueError("Mesh points must have two or three coordinates.")
    if source_points.shape[1] != target_points.shape[1]:
        raise ValueError(
            "Source and target meshes must use the same coordinate system; "
            f"got {source_points.shape[1]} and {target_points.shape[1]} coordinates."
        )

    _check_flat_cell_types(source, source_points.shape[1])
    _check_flat_cell_types(target, target_points.shape[1])

    source_geometry = _build_geometries(source, tolerance)
    target_geometry = _build_geometries(target, tolerance)

    source_volumes = np.array([geometry.measure for geometry in source_geometry])
    target_volumes = np.array([geometry.measure for geometry in target_geometry])

    rows = []
    cols = []
    volumes = []
    scale = max(
        np.ptp(source_points, axis=0).max() if len(source_points) else 1.0,
        np.ptp(target_points, axis=0).max() if len(target_points) else 1.0,
        1.0,
    )
    bbox_tolerance = tolerance * scale

    if len(target_geometry):
        target_min = np.array([g.bbox_min for g in target_geometry])
        target_max = np.array([g.bbox_max for g in target_geometry])
    else:
        target_min = target_max = np.empty((0, source_points.shape[1]))

    for source_index, source_cell in enumerate(source_geometry):
        if source_cell.measure <= 0.0:
            continue
        candidates = np.flatnonzero(
            np.all(
                (target_min <= source_cell.bbox_max + bbox_tolerance)
                & (target_max + bbox_tolerance >= source_cell.bbox_min),
                axis=1,
            )
        )
        for target_index in candidates:
            target_cell = target_geometry[target_index]
            if target_cell.measure <= 0.0:
                continue
            volume = _intersection_measure(source_cell, target_cell, tolerance)
            threshold = tolerance * max(source_cell.measure, target_cell.measure)
            if volume > threshold:
                rows.append(int(source_index))
                cols.append(int(target_index))
                volumes.append(float(volume))

    overlap = CellOverlap(
        source,
        target,
        np.asarray(rows, dtype=int),
        np.asarray(cols, dtype=int),
        np.asarray(volumes, dtype=float),
        source_volumes,
        target_volumes,
    )

    allocated = overlap.source_allocated_fraction()
    covered = overlap.target_covered_fraction()
    if np.any(allocated > 1.0 + 100.0 * tolerance) or np.any(
        covered > 1.0 + 100.0 * tolerance
    ):
        raise ValueError(
            "Source or target cells overlap each other; a conservative transfer "
            "of extensive cell totals is not well defined."
        )

    source_measure = float(source_volumes.sum())
    target_measure = float(target_volumes.sum())
    scale = max(source_measure, target_measure, 1.0)
    source_threshold = 100.0 * tolerance * source_measure + tolerance * scale
    target_threshold = 100.0 * tolerance * target_measure + tolerance * scale
    if _self_overlap(source_geometry, tolerance, source_threshold):
        raise ValueError(
            "Source cells overlap each other; a conservative transfer of "
            "extensive cell totals is not well defined."
        )
    if _self_overlap(target_geometry, tolerance, target_threshold):
        raise ValueError(
            "Target cells overlap each other; a conservative transfer is not "
            "well defined."
        )
    return overlap


overlap_volumes = cell_overlaps


def _check_flat_cell_types(mesh, dimension):
    for block in mesh.cells:
        if block.type in _FACES and dimension != 3:
            raise ValueError(
                f"Cell type '{block.type}' requires a 3D coordinate system."
            )


def _resolve_fields(source, extensive, intensive, fields):
    if fields is not None and not isinstance(fields, Mapping):
        extensive = list(fields) if extensive is None else extensive
        fields = None

    if isinstance(fields, Mapping):
        for name, kind in fields.items():
            kind = str(kind).lower()
            if kind in ("extensive", "ext"):
                extensive = ([] if extensive is None else list(extensive)) + [name]
            elif kind in ("intensive", "int"):
                intensive = ([] if intensive is None else list(intensive)) + [name]
            else:
                raise ValueError(
                    f"Unknown field kind '{kind}' for '{name}'; use "
                    "'extensive' or 'intensive'."
                )

    extensive_names = _as_name_list(extensive)
    intensive_names = _as_name_list(intensive)
    if extensive_names is None and intensive_names is None:
        extensive_names = list(source.cell_data)
    extensive_names = [] if extensive_names is None else extensive_names
    intensive_names = [] if intensive_names is None else intensive_names
    return extensive_names, intensive_names


def transfer_cell_data(
    source,
    target,
    cell_types: Iterable[str] | None = None,
    extensive: Iterable[str] | str | None = None,
    intensive: Iterable[str] | str | None = None,
    weights=None,
    tolerance: float = 1.0e-12,
    fields=None,
) -> CellTransferResult:
    """Transfer cell data conservatively onto another mesh.

    Parameters
    ----------
    source, target:
        Meshes in the same coordinate system.  Cell geometry is intersected
        (for the supported linear cell types) to obtain actual overlap
        measures.
    cell_types:
        Caller-approved linear cell types.  All cell blocks in both meshes must
        be listed; otherwise a ``ValueError`` is raised.
    extensive:
        Field names treated as cell totals.  If neither ``extensive`` nor
        ``intensive`` is given, all source cell-data fields are extensive.
    intensive:
        Field names treated as cell averages.
    weights:
        For intensive fields, a scalar, an array with one value per source cell,
        or a mapping field name -> scalar/array.  It multiplies the overlap
        measure in the weighted average.
    tolerance:
        Relative tolerance for overlap detection and coverage classification.
    fields:
        Optional mapping ``name -> "extensive" | "intensive"``, as an
        alternative to the two name lists.  A sequence of names is also
        accepted and treated as all-extensive.

    Returns
    -------
    CellTransferResult
        Mapping of target cell-data blocks.  ``result.unallocated`` holds, for
        each extensive source field, the part of every source-cell total that
        was not assigned to any target cell.  ``result.allocated`` holds the
        assigned part; their sum reproduces the source values exactly.
        ``result.target_coverage`` and ``result.uncovered_targets`` expose target
        coverage without any renormalization.  Intensive values in target cells
        without any overlap are ``nan``.
    """
    extensive_names, intensive_names = _resolve_fields(
        source, extensive, intensive, fields
    )

    missing = [
        name
        for name in extensive_names + intensive_names
        if name not in source.cell_data
    ]
    if missing:
        raise KeyError(f"Source cell_data does not contain fields {missing}.")
    overlap_names = set(extensive_names) & set(intensive_names)
    if overlap_names:
        raise ValueError(
            f"Fields cannot be both extensive and intensive: {sorted(overlap_names)}."
        )

    source_lengths = _cell_lengths(source)
    target_lengths = _cell_lengths(target)
    source_count = sum(source_lengths)
    target_count = sum(target_lengths)

    overlaps = cell_overlaps(
        source, target, cell_types=cell_types, tolerance=tolerance
    )
    source_indices = overlaps.source_indices
    target_indices = overlaps.target_indices
    volumes = overlaps.volumes
    source_volumes = overlaps.source_volumes

    target_cell_data = {}
    allocated_data = {}
    unallocated_data = {}

    for name in extensive_names:
        values = np.asarray(
            _flatten_block_data(source.cell_data[name], source_lengths)
        )
        dtype = np.result_type(values.dtype, np.float64)
        values = values.astype(dtype, copy=False)
        trailing = values.shape[1:]

        target_values = np.zeros((target_count,) + trailing, dtype=dtype)
        allocated = np.zeros((source_count,) + trailing, dtype=dtype)

        if len(volumes):
            fractions = np.zeros(len(volumes), dtype=float)
            positive = source_volumes[source_indices] > 0.0
            fractions[positive] = (
                volumes[positive] / source_volumes[source_indices[positive]]
            )
            # Guard against floating-point over-allocation of a single source
            # cell.  This only clamps down to 100% of the source; it never
            # scales a partially covered source up to full coverage.
            total_fraction = np.bincount(
                source_indices, weights=fractions, minlength=source_count
            )
            excess = total_fraction > 1.0
            if np.any(excess):
                fractions[excess[source_indices]] /= total_fraction[
                    source_indices[excess[source_indices]]
                ]
            contributions = fractions.reshape((-1,) + (1,) * len(trailing))
            contributions = contributions * values[source_indices]
            np.add.at(target_values, target_indices, contributions)
            np.add.at(allocated, source_indices, contributions)

        unallocated = values - allocated
        target_cell_data[name] = _split_block_data(target_values, target_lengths)
        allocated_data[name] = _split_block_data(allocated, source_lengths)
        unallocated_data[name] = _split_block_data(unallocated, source_lengths)

    coverage_flat = overlaps.target_covered_fraction()
    coverage_flat = np.clip(coverage_flat, 0.0, 1.0)
    target_coverage = _split_block_data(coverage_flat, target_lengths)
    target_covered = [
        coverage >= 1.0 - 10.0 * tolerance for coverage in target_coverage
    ]
    uncovered_volume_flat = overlaps.target_volumes * (1.0 - coverage_flat)
    target_uncovered_volume = _split_block_data(
        uncovered_volume_flat, target_lengths
    )

    for name in intensive_names:
        values = np.asarray(
            _flatten_block_data(source.cell_data[name], source_lengths)
        )
        dtype = np.result_type(values.dtype, np.float64)
        values = values.astype(dtype, copy=False)
        trailing = values.shape[1:]

        field_weights = _normalize_weights(
            weights, name, source_lengths, source_count
        )
        denominator = np.bincount(
            target_indices,
            weights=volumes * field_weights[source_indices],
            minlength=target_count,
        )
        numerator = np.zeros((target_count,) + trailing, dtype=dtype)
        if len(volumes):
            pair_weights = volumes * field_weights[source_indices]
            contributions = pair_weights.reshape((-1,) + (1,) * len(trailing))
            contributions = contributions * values[source_indices]
            np.add.at(numerator, target_indices, contributions)

        target_values = np.full((target_count,) + trailing, np.nan, dtype=dtype)
        valid = denominator > 0.0
        if np.any(valid):
            shape = (int(valid.sum()),) + (1,) * len(trailing)
            target_values[valid] = numerator[valid] / denominator[valid].reshape(
                shape
            )
        target_cell_data[name] = _split_block_data(target_values, target_lengths)

    return CellTransferResult(
        target_cell_data,
        allocated_data,
        unallocated_data,
        target_coverage,
        target_covered,
        target_uncovered_volume,
        overlaps,
        source_lengths,
        target_lengths,
        tolerance,
    )


transfer_cell_fields = transfer_cell_data
transfer_cell_field = transfer_cell_data
remap_cell_data = transfer_cell_data
map_cell_data = transfer_cell_data
conservative_transfer = transfer_cell_data
