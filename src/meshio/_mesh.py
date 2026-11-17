from __future__ import annotations

import copy

import numpy as np
from numpy.typing import ArrayLike

from ._common import num_nodes_per_cell, warn

topological_dimension = {
    "line": 1,
    "polygon": 2,
    "triangle": 2,
    "quad": 2,
    "tetra": 3,
    "hexahedron": 3,
    "wedge": 3,
    "pyramid": 3,
    "line3": 1,
    "triangle6": 2,
    "quad9": 2,
    "tetra10": 3,
    "hexahedron27": 3,
    "wedge18": 3,
    "pyramid14": 3,
    "vertex": 0,
    "quad8": 2,
    "hexahedron20": 3,
    "triangle10": 2,
    "triangle15": 2,
    "triangle21": 2,
    "line4": 1,
    "line5": 1,
    "line6": 1,
    "tetra20": 3,
    "tetra35": 3,
    "tetra56": 3,
    "quad16": 2,
    "quad25": 2,
    "quad36": 2,
    "triangle28": 2,
    "triangle36": 2,
    "triangle45": 2,
    "triangle55": 2,
    "triangle66": 2,
    "quad49": 2,
    "quad64": 2,
    "quad81": 2,
    "quad100": 2,
    "quad121": 2,
    "line7": 1,
    "line8": 1,
    "line9": 1,
    "line10": 1,
    "line11": 1,
    "tetra84": 3,
    "tetra120": 3,
    "tetra165": 3,
    "tetra220": 3,
    "tetra286": 3,
    "wedge40": 3,
    "wedge75": 3,
    "hexahedron64": 3,
    "hexahedron125": 3,
    "hexahedron216": 3,
    "hexahedron343": 3,
    "hexahedron512": 3,
    "hexahedron729": 3,
    "hexahedron1000": 3,
    "wedge126": 3,
    "wedge196": 3,
    "wedge288": 3,
    "wedge405": 3,
    "wedge550": 3,
    "VTK_LAGRANGE_CURVE": 1,
    "VTK_LAGRANGE_TRIANGLE": 2,
    "VTK_LAGRANGE_QUADRILATERAL": 2,
    "VTK_LAGRANGE_TETRAHEDRON": 3,
    "VTK_LAGRANGE_HEXAHEDRON": 3,
    "VTK_LAGRANGE_WEDGE": 3,
    "VTK_LAGRANGE_PYRAMID": 3,
}


def _as_masked(entry, length=None):
    """Return an entry as a masked array with an all-``False`` mask, if needed."""
    if np.ma.isMaskedArray(entry):
        return entry
    entry = np.asarray(entry)
    if length is not None and entry.shape[0] != length:
        raise ValueError(
            f"Cell data has {entry.shape[0]} rows, but the cell block has {length}."
        )
    return np.ma.array(entry, mask=np.zeros(entry.shape, dtype=bool), copy=False)


def _take_cell_block_data(data, indices):
    """Take rows from a CellBlock data object.

    Polyhedron blocks store a list of faces, all other blocks store a numpy array.
    """
    if isinstance(data, list):
        return [data[int(i)] for i in indices]
    return data[indices]


def _merge_cell_data_entries(entries, lengths):
    """Concatenate one cell-data attribute over a group of cell blocks.

    ``None`` entries denote that the attribute is not defined on that block. Their
    rows are marked as missing; they are never replaced by real zeros (or any other
    value that could be confused with actual data). Floating-point attributes use
    ``NaN`` for missing rows, other dtypes use a mask. If every entry is missing, the
    result is ``None``.
    """
    if all(entry is None for entry in entries):
        return None

    reference = next(entry for entry in entries if entry is not None)
    reference = _as_masked(reference)
    shape = reference.shape[1:]
    dtype = reference.dtype
    is_float = dtype.kind in "fc"

    chunks = []
    masks = []
    for entry, length in zip(entries, lengths):
        if entry is None:
            if is_float:
                chunks.append(np.full((length,) + shape, np.nan, dtype=dtype))
            else:
                chunks.append(np.zeros((length,) + shape, dtype=dtype))
            masks.append(np.ones((length,) + shape, dtype=bool))
            continue

        entry = _as_masked(entry, length)
        if entry.shape != (length,) + shape:
            raise ValueError(
                "Cannot merge cell data with incompatible shapes: "
                f"expected {(length,) + shape}, got {entry.shape}."
            )
        data = np.ma.getdata(entry)
        mask = np.ma.getmaskarray(entry)
        if is_float and mask.any():
            data = data.astype(dtype, copy=True)
            data[mask] = np.nan
            mask = np.zeros(data.shape, dtype=bool)
        chunks.append(data)
        masks.append(mask)

    data = np.concatenate(chunks, axis=0)
    mask = np.concatenate(masks, axis=0)
    if is_float:
        return data
    if mask.any():
        return np.ma.array(data, mask=mask, copy=False)
    return data


def _take_cell_data_value(value, indices):
    """Select rows from a cell-data value while keeping a possible mask."""
    if value is None:
        return None
    if np.ma.isMaskedArray(value):
        data = np.ma.getdata(value)[indices]
        mask = np.ma.getmaskarray(value)[indices]
        if mask.any():
            return np.ma.array(data, mask=mask, copy=False)
        return data
    return np.asarray(value)[indices]


class CellBlockMapping:
    """Mapping between merged cell blocks and the original block/row layout.

    ``block_map[m]`` lists the original block indices merged into block ``m``.
    ``row_map[m]`` is an ``(n, 2)`` integer array whose rows are
    ``(original_block_index, original_row_index)``. Together they provide a full
    provenance map for every row of every merged block.

    The mapping also records whether an attribute existed on each original block, so a
    split can restore absent attributes as ``None`` instead of inventing values.
    """

    def __init__(
        self,
        merged_types,
        block_map,
        row_map,
        original_cells,
        original_cell_data_present=None,
        original_cell_sets_present=None,
        original_cell_data_dtypes=None,
        original_cell_set_dtypes=None,
    ):
        self.merged_types = list(merged_types)
        self.block_map = [list(indices) for indices in block_map]
        self.row_map = [np.asarray(mapping, dtype=int) for mapping in row_map]
        self.original_cells = list(original_cells)
        self.original_cell_data_present = {
            key: [list(present) for present in groups]
            for key, groups in (original_cell_data_present or {}).items()
        }
        self.original_cell_sets_present = {
            key: [list(present) for present in groups]
            for key, groups in (original_cell_sets_present or {}).items()
        }
        self.original_cell_data_dtypes = {
            key: list(dtypes) for key, dtypes in (original_cell_data_dtypes or {}).items()
        }
        self.original_cell_set_dtypes = {
            key: list(dtypes) for key, dtypes in (original_cell_set_dtypes or {}).items()
        }

    def __len__(self):
        return len(self.block_map)

    def __iter__(self):
        return iter(self.block_map)

    def __getitem__(self, original_block_index):
        """Return ``(merged_block_index, merged_row_indices)`` for an original block.

        The row indices are ordered like the rows of the original block, so
        ``merged_cells[merged][rows]`` reproduces the original cell block.
        """
        for merged_block_index, indices in enumerate(self.block_map):
            if original_block_index in indices:
                mapping = self.row_map[merged_block_index]
                rows = np.where(mapping[:, 0] == original_block_index)[0]
                order = np.argsort(mapping[rows, 1], kind="stable")
                return merged_block_index, rows[order]
        raise IndexError(f"Original cell block {original_block_index} is not mapped.")

    def is_cell_data_present(self, key, merged_block_index, position):
        groups = self.original_cell_data_present.get(key)
        if groups is None:
            return True
        return groups[merged_block_index][position]

    def is_cell_set_present(self, key, merged_block_index, position):
        groups = self.original_cell_sets_present.get(key)
        if groups is None:
            return True
        return groups[merged_block_index][position]

    @property
    def num_original_blocks(self):
        return len(self.original_cells)

    @property
    def inverse_block_map(self):
        """Map each original block index to its merged block index."""
        inverse = [None] * self.num_original_blocks
        for merged_block_index, indices in enumerate(self.block_map):
            for original_block_index in indices:
                inverse[original_block_index] = merged_block_index
        return inverse

    @property
    def original_to_merged(self):
        return self.inverse_block_map

    @property
    def merged_to_original(self):
        return self.block_map

    @property
    def cell_mapping(self):
        return self.row_map

    @property
    def rows(self):
        return self.row_map

    @property
    def block_mapping(self):
        return self.block_map

    @property
    def merged_cell_index_map(self):
        """Global merged cell index -> global original cell index."""
        offsets = np.cumsum([0] + [len(cell) for cell in self.original_cells])
        out = []
        for mapping in self.row_map:
            out.append(offsets[mapping[:, 0]] + mapping[:, 1])
        if not out:
            return np.empty(0, dtype=int)
        return np.concatenate(out)

    @property
    def original_cell_index_map(self):
        """Global original cell index -> global merged cell index."""
        merged = self.merged_cell_index_map
        inverse = np.empty(merged.size, dtype=int)
        inverse[merged] = np.arange(merged.size)
        return inverse

    @property
    def original_to_merged_cell(self):
        return self.original_cell_index_map

    @property
    def merged_to_original_cell(self):
        return self.merged_cell_index_map

    def original_cell_index(self, merged_block_index, merged_row_index):
        """Return the global original cell index for a merged (block, row)."""
        mapping = self.row_map[merged_block_index]
        block, row = mapping[merged_row_index]
        offsets = np.cumsum([0] + [len(cell) for cell in self.original_cells])
        return int(offsets[block] + row)

    def merged_cell_index(self, original_block_index, original_row_index):
        """Return the global merged cell index for an original (block, row)."""
        merged_block_index, rows = self[original_block_index]
        offsets = np.cumsum([0] + [mapping.shape[0] for mapping in self.row_map])
        return int(offsets[merged_block_index] + rows[original_row_index])


class CellBlock:
    def __init__(
        self,
        cell_type: str,
        data: list | np.ndarray,
        tags: list[str] | None = None,
    ):
        self.type = cell_type
        self.data = data

        if cell_type.startswith("polyhedron"):
            self.dim = 3
        else:
            self.data = np.asarray(self.data)
            self.dim = topological_dimension[cell_type]

        self.tags = [] if tags is None else tags

    def __repr__(self):
        items = [
            "meshio CellBlock",
            f"type: {self.type}",
            f"num cells: {len(self.data)}",
            f"tags: {self.tags}",
        ]
        return "<" + ", ".join(items) + ">"

    def __len__(self):
        return len(self.data)


class Mesh:
    def __init__(
        self,
        points: ArrayLike,
        cells: dict[str, ArrayLike] | list[tuple[str, ArrayLike] | CellBlock],
        point_data: dict[str, ArrayLike] | None = None,
        cell_data: dict[str, list[ArrayLike]] | None = None,
        field_data=None,
        point_sets: dict[str, ArrayLike] | None = None,
        cell_sets: dict[str, list[ArrayLike]] | None = None,
        gmsh_periodic=None,
        info=None,
    ):
        self.points = np.asarray(points)
        if isinstance(cells, dict):
            # Let's not deprecate this for now.
            # warn(
            #     "cell dictionaries are deprecated, use list of tuples, e.g., "
            #     '[("triangle", [[0, 1, 2], ...])]',
            #     DeprecationWarning,
            # )
            # old dict, deprecated
            #
            # convert dict to list of tuples
            cells = list(cells.items())

        self.cells = []
        for cell_block in cells:
            if isinstance(cell_block, tuple):
                cell_type, data = cell_block
                cell_block = CellBlock(
                    cell_type,
                    # polyhedron data cannot be converted to numpy arrays
                    # because the sublists don't all have the same length
                    data if cell_type.startswith("polyhedron") else np.asarray(data),
                )
            self.cells.append(cell_block)

        self.point_data = {} if point_data is None else point_data
        self.cell_data = {} if cell_data is None else cell_data
        self.field_data = {} if field_data is None else field_data
        self.point_sets = {} if point_sets is None else point_sets
        self.cell_sets = {} if cell_sets is None else cell_sets
        self.gmsh_periodic = gmsh_periodic
        self.info = info

        # No merge has been performed on this mesh yet. ``merge_cell_blocks`` sets a
        # ``CellBlockMapping`` here that also allows restoring the original layout.
        self.cell_block_mapping = None
        self._cells_merged = False

        # assert point data consistency and convert to numpy arrays
        for key, item in self.point_data.items():
            self.point_data[key] = np.asarray(item)
            if len(self.point_data[key]) != len(self.points):
                raise ValueError(
                    f"len(points) = {len(self.points)}, "
                    f'but len(point_data["{key}"]) = {len(self.point_data[key])}'
                )

        # assert cell data consistency and convert to numpy arrays
        for key, data in self.cell_data.items():
            if len(data) != len(cells):
                raise ValueError(
                    f"Incompatible cell data '{key}'. "
                    f"{len(cells)} cell blocks, but '{key}' has {len(data)} blocks."
                )

            for k in range(len(data)):
                if data[k] is None:
                    # An attribute may be undefined for a particular cell block. None
                    # keeps that information; it must not be turned into real data.
                    continue
                if np.ma.isMaskedArray(data[k]):
                    data[k] = np.ma.asarray(data[k])
                else:
                    data[k] = np.asarray(data[k])
                if len(data[k]) != len(self.cells[k]):
                    raise ValueError(
                        "Incompatible cell data. "
                        + f"Cell block {k} ('{self.cells[k].type}') "
                        + f"has length {len(self.cells[k])}, but "
                        + f"corresponding cell data item has length {len(data[k])}."
                    )

    def __repr__(self):
        lines = ["<meshio mesh object>", f"  Number of points: {len(self.points)}"]
        special_cells = [
            "polygon",
            "polyhedron",
            "VTK_LAGRANGE_CURVE",
            "VTK_LAGRANGE_TRIANGLE",
            "VTK_LAGRANGE_QUADRILATERAL",
            "VTK_LAGRANGE_TETRAHEDRON",
            "VTK_LAGRANGE_HEXAHEDRON",
            "VTK_LAGRANGE_WEDGE",
            "VTK_LAGRANGE_PYRAMID",
        ]
        if len(self.cells) > 0:
            lines.append("  Number of cells:")
            for cell_block in self.cells:
                string = cell_block.type
                if cell_block.type in special_cells:
                    string += f"({cell_block.data.shape[1]})"
                lines.append(f"    {string}: {len(cell_block)}")
        else:
            lines.append("  No cells.")

        if self.point_sets:
            names = ", ".join(self.point_sets.keys())
            lines.append(f"  Point sets: {names}")

        if self.cell_sets:
            names = ", ".join(self.cell_sets.keys())
            lines.append(f"  Cell sets: {names}")

        if self.point_data:
            names = ", ".join(self.point_data.keys())
            lines.append(f"  Point data: {names}")

        if self.cell_data:
            names = ", ".join(self.cell_data.keys())
            lines.append(f"  Cell data: {names}")

        if self.field_data:
            names = ", ".join(self.field_data.keys())
            lines.append(f"  Field data: {names}")

        return "\n".join(lines)

    def copy(self):
        return copy.deepcopy(self)

    def write(self, path_or_buf, file_format: str | None = None, **kwargs):
        # avoid circular import
        from ._helpers import write

        write(path_or_buf, self, file_format, **kwargs)

    def get_cells_type(self, cell_type: str):
        if not any(c.type == cell_type for c in self.cells):
            return np.empty((0, num_nodes_per_cell[cell_type]), dtype=int)
        return np.concatenate([c.data for c in self.cells if c.type == cell_type])

    def get_cell_data(self, name: str, cell_type: str):
        blocks = [
            (c, d)
            for c, d in zip(self.cells, self.cell_data[name])
            if c.type == cell_type
        ]
        if not blocks:
            return np.empty((0,), dtype=float)
        return _merge_cell_data_entries(
            [d for _, d in blocks], [len(c) for c, _ in blocks]
        )

    @property
    def cells_dict(self):
        cells_dict = {}
        for cell_block in self.cells:
            if cell_block.type not in cells_dict:
                cells_dict[cell_block.type] = []
            cells_dict[cell_block.type].append(cell_block.data)
        # concatenate
        for key, value in cells_dict.items():
            cells_dict[key] = np.concatenate(value)
        return cells_dict

    @property
    def cell_data_dict(self):
        cell_data_dict = {}
        for key, value_list in self.cell_data.items():
            cell_data_dict[key] = {}
            for value, cell_block in zip(value_list, self.cells):
                if cell_block.type not in cell_data_dict[key]:
                    cell_data_dict[key][cell_block.type] = []
                cell_data_dict[key][cell_block.type].append(value)

            for cell_type, val in cell_data_dict[key].items():
                if all(entry is None for entry in val):
                    cell_data_dict[key][cell_type] = None
                    continue
                lengths = [
                    len(cell_block)
                    for cell_block in self.cells
                    if cell_block.type == cell_type
                ]
                cell_data_dict[key][cell_type] = _merge_cell_data_entries(val, lengths)
        return cell_data_dict

    @property
    def cell_sets_dict(self):
        sets_dict = {}
        for key, member_list in self.cell_sets.items():
            sets_dict[key] = {}
            offsets = {}
            for members, cells in zip(member_list, self.cells):
                # Advance the offset for every block, even if the set is not defined
                # on this block; otherwise later rows would be shifted.
                if cells.type in offsets:
                    offset = offsets[cells.type]
                    offsets[cells.type] += cells.data.shape[0]
                else:
                    offset = 0
                    offsets[cells.type] = cells.data.shape[0]
                if members is None:
                    continue
                if cells.type in sets_dict[key]:
                    sets_dict[key][cells.type].append(members + offset)
                else:
                    sets_dict[key][cells.type] = [members + offset]
        return {
            key: {
                cell_type: np.concatenate(members)
                for cell_type, members in sets.items()
                if sum(map(np.size, members))
            }
            for key, sets in sets_dict.items()
        }

    def merge_cell_blocks(self):
        """Merge cell blocks of the same type into one block each.

        The order of the merged blocks follows the first occurrence of each cell type.
        All rows of a type are concatenated in their original order, so every merged
        row can be traced back to an original block and row.

        ``cell_data`` is migrated along with the blocks.  If an attribute is missing on
        one of the original blocks (``None``), the corresponding rows in the merged
        array are marked missing: floating-point attributes use ``NaN``, other dtypes
        use a mask. They are never filled with real zeros. ``cell_sets`` are migrated to
        the merged block layout as well.

        The returned :class:`CellBlockMapping` is stored in
        ``mesh.cell_block_mapping`` and can be used with :meth:`split_cell_blocks` to
        restore the original layout.  A second call is a no-op and returns the existing
        mapping.
        """
        if self.cell_block_mapping is not None:
            return self.cell_block_mapping

        original_cells = list(self.cells)

        # Group original blocks by cell type, preserving first-appearance order.
        type_order = []
        type_to_blocks = {}
        for block_index, cell_block in enumerate(original_cells):
            if cell_block.type not in type_to_blocks:
                type_to_blocks[cell_block.type] = []
                type_order.append(cell_block.type)
            type_to_blocks[cell_block.type].append(block_index)

        block_map = []
        row_map = []
        merged_cells = []
        for cell_type in type_order:
            indices = type_to_blocks[cell_type]
            block_map.append(indices)

            lengths = [len(original_cells[i]) for i in indices]
            if cell_type.startswith("polyhedron"):
                merged_data = [
                    row for i in indices for row in original_cells[i].data
                ]
            else:
                merged_data = np.concatenate(
                    [original_cells[i].data for i in indices], axis=0
                )

            mapping = np.empty((sum(lengths), 2), dtype=int)
            offset = 0
            for original_block_index, length in zip(indices, lengths):
                mapping[offset : offset + length, 0] = original_block_index
                mapping[offset : offset + length, 1] = np.arange(length)
                offset += length
            row_map.append(mapping)

            # Tags (if any) of the first block are carried over.
            merged_cells.append(
                CellBlock(cell_type, merged_data, tags=original_cells[indices[0]].tags)
            )

        merged_cell_data = {}
        cell_data_present = {}
        cell_data_dtypes = {}
        for key, entries in self.cell_data.items():
            merged = []
            present = []
            dtypes = []
            for indices in block_map:
                group = [entries[i] for i in indices]
                lengths = [len(original_cells[i]) for i in indices]
                merged.append(_merge_cell_data_entries(group, lengths))
                present.append([entry is not None for entry in group])
                dtypes.append(
                    [
                        None if entry is None else _as_masked(entry).dtype
                        for entry in group
                    ]
                )
            merged_cell_data[key] = merged
            cell_data_present[key] = present
            cell_data_dtypes[key] = dtypes

        merged_cell_sets = {}
        cell_sets_present = {}
        cell_set_dtypes = {}
        for key, member_list in self.cell_sets.items():
            merged = []
            present = []
            dtypes = []
            for indices in block_map:
                offsets = np.cumsum([0] + [len(original_cells[i]) for i in indices])
                parts = []
                for original_block_index, offset in zip(indices, offsets):
                    members = member_list[original_block_index]
                    if members is None:
                        continue
                    parts.append(np.asarray(members, dtype=int) + offset)
                merged.append(
                    np.concatenate(parts) if parts else np.empty(0, dtype=int)
                )
                present.append([member_list[i] is not None for i in indices])
                dtypes.append(
                    [
                        None if member_list[i] is None else np.asarray(member_list[i]).dtype
                        for i in indices
                    ]
                )
            merged_cell_sets[key] = merged
            cell_sets_present[key] = present
            cell_set_dtypes[key] = dtypes

        self.cells = merged_cells
        self.cell_data = merged_cell_data
        self.cell_sets = merged_cell_sets

        self.cell_block_mapping = CellBlockMapping(
            [c.type for c in merged_cells],
            block_map,
            row_map,
            original_cells,
            cell_data_present,
            cell_sets_present,
            cell_data_dtypes,
            cell_set_dtypes,
        )
        self._cells_merged = True
        return self.cell_block_mapping

    def split_cell_blocks(self):
        """Restore the original block layout after :meth:`merge_cell_blocks`.

        ``cell_data`` and ``cell_sets`` are split back so that each original block gets
        its own rows again. Missing attributes are restored as ``None`` for blocks that
        did not have them; genuinely masked values stay masked. If the mesh was not
        merged, this is a no-op.
        """
        mapping = self.cell_block_mapping
        if mapping is None:
            return

        num_original = mapping.num_original_blocks
        cells = [None] * num_original
        cell_data = {key: [None] * num_original for key in self.cell_data}
        cell_sets = {key: [None] * num_original for key in self.cell_sets}

        for merged_block_index, indices in enumerate(mapping.block_map):
            merged_cell = self.cells[merged_block_index]
            rows = mapping.row_map[merged_block_index]
            for position, original_block_index in enumerate(indices):
                selected = np.where(rows[:, 0] == original_block_index)[0]
                order = np.argsort(rows[selected, 1], kind="stable")
                selected = selected[order]

                original_block = mapping.original_cells[original_block_index]
                cells[original_block_index] = CellBlock(
                    original_block.type,
                    _take_cell_block_data(merged_cell.data, selected),
                    tags=original_block.tags,
                )

                for key, values in self.cell_data.items():
                    value = values[merged_block_index]
                    if value is None or not mapping.is_cell_data_present(
                        key, merged_block_index, position
                    ):
                        cell_data[key][original_block_index] = None
                        continue

                    part = _take_cell_data_value(value, selected)
                    dtypes = mapping.original_cell_data_dtypes.get(key)
                    if dtypes is not None:
                        dtype = dtypes[merged_block_index][position]
                        if dtype is not None and not np.ma.isMaskedArray(part):
                            part = np.asarray(part, dtype=dtype)
                        elif dtype is not None:
                            part = np.ma.array(
                                np.ma.getdata(part).astype(dtype, copy=False),
                                mask=np.ma.getmaskarray(part),
                                copy=False,
                            )
                    cell_data[key][original_block_index] = part

                for key, values in self.cell_sets.items():
                    if not mapping.is_cell_set_present(key, merged_block_index, position):
                        cell_sets[key][original_block_index] = None
                        continue
                    value = np.asarray(values[merged_block_index], dtype=int)
                    selected_set = np.where(rows[:, 0] == original_block_index)[0]
                    row_to_local = {int(row): k for k, row in enumerate(selected_set)}
                    cell_sets[key][original_block_index] = np.array(
                        [
                            row_to_local[int(row)]
                            for row in value
                            if int(row) in row_to_local
                        ],
                        dtype=int,
                    )
                    dtypes = mapping.original_cell_set_dtypes.get(key)
                    if dtypes is not None:
                        dtype = dtypes[merged_block_index][position]
                        if dtype is not None:
                            cell_sets[key][original_block_index] = cell_sets[key][
                                original_block_index
                            ].astype(dtype, copy=False)

        self.cells = cells
        self.cell_data = cell_data
        self.cell_sets = cell_sets
        self.cell_block_mapping = None
        self._cells_merged = False
        return self

    # Common alternative names for the block merge/split operations.
    def merge_cells(self):
        """Alias for :meth:`merge_cell_blocks`."""
        return self.merge_cell_blocks()

    def merge_blocks(self):
        """Alias for :meth:`merge_cell_blocks`."""
        return self.merge_cell_blocks()

    def split_cells(self):
        """Alias for :meth:`split_cell_blocks`."""
        return self.split_cell_blocks()

    def split_blocks(self):
        """Alias for :meth:`split_cell_blocks`."""
        return self.split_cell_blocks()

    def unmerge_cell_blocks(self):
        """Alias for :meth:`split_cell_blocks`."""
        return self.split_cell_blocks()

    def restore_cell_blocks(self):
        """Alias for :meth:`split_cell_blocks`."""
        return self.split_cell_blocks()

    @property
    def cell_block_map(self):
        mapping = self.cell_block_mapping
        return None if mapping is None else mapping.block_map

    @property
    def cell_row_map(self):
        mapping = self.cell_block_mapping
        return None if mapping is None else mapping.row_map

    @property
    def block_map(self):
        return self.cell_block_map

    @property
    def row_map(self):
        return self.cell_row_map

    @property
    def original_cell_blocks(self):
        mapping = self.cell_block_mapping
        return None if mapping is None else mapping.original_cells

    @classmethod
    def read(cls, path_or_buf, file_format=None):
        # avoid circular import
        from ._helpers import read

        # 2021-02-21
        warn("meshio.Mesh.read is deprecated, use meshio.read instead")
        return read(path_or_buf, file_format)

    def cell_sets_to_data(self, data_name: str | None = None):
        # If possible, convert cell sets to integer cell data. This is possible if all
        # cells appear exactly in one group.
        default_value = -1
        if len(self.cell_sets) > 0:
            intfun = []
            for k, c in enumerate(zip(*self.cell_sets.values())):
                # Go for -1 as the default value. (NaN is not int.)
                arr = np.full(len(self.cells[k]), default_value, dtype=int)
                for i, cc in enumerate(c):
                    if cc is None:
                        continue
                    arr[cc] = i
                intfun.append(arr)

            for item in intfun:
                num_default = np.sum(item == default_value)
                if num_default > 0:
                    warn(
                        f"{num_default} cells are not part of any cell set. "
                        f"Using default value {default_value}."
                    )
                    break

            if data_name is None:
                data_name = "-".join(self.cell_sets.keys())
            self.cell_data[data_name] = intfun
            self.cell_sets = {}

    def point_sets_to_data(self, join_char: str = "-") -> None:
        # now for the point sets
        # Go for -1 as the default value. (NaN is not int.)
        default_value = -1
        if len(self.point_sets) > 0:
            intfun = np.full(len(self.points), default_value, dtype=int)
            for i, cc in enumerate(self.point_sets.values()):
                intfun[cc] = i

            if np.any(intfun == default_value):
                warn(
                    "Not all points are part of a point set. "
                    f"Using default value {default_value}."
                )

            data_name = join_char.join(self.point_sets.keys())
            self.point_data[data_name] = intfun
            self.point_sets = {}

    # This used to be int_data_to_sets(), converting _all_ cell and point data.
    # This is not useful in many cases, as one usually only wants one
    # particular data array (e.g., "MaterialIDs") converted to sets.
    def cell_data_to_sets(self, key: str):
        """Convert point_data to cell_sets."""
        data = self.cell_data[key]

        # handle all int and uint data
        if not all(v.dtype.kind in ["i", "u"] for v in data):
            raise RuntimeError(f"cell_data['{key}'] is not int data.")

        tags = np.unique(np.concatenate(data))

        # try and get the names by splitting the key along "-" (this is how
        # sets_to_int_data() forms the key)
        names = key.split("-")
        # remove duplicates and preserve order
        # <https://stackoverflow.com/a/7961390/353337>:
        names = list(dict.fromkeys(names))
        if len(names) != len(tags):
            # alternative names
            names = [f"set-{key}-{tag}" for tag in tags]

        # TODO there's probably a better way besides np.where, something from
        # np.unique or np.sort
        for name, tag in zip(names, tags):
            self.cell_sets[name] = [np.where(d == tag)[0] for d in data]

        # remove the cell data
        del self.cell_data[key]

    def point_data_to_sets(self, key: str):
        """Convert point_data to point_sets."""
        data = self.point_data[key]

        # handle all int and uint data
        if not all(v.dtype.kind in ["i", "u"] for v in data):
            raise RuntimeError(f"point_data['{key}'] is not int data.")

        tags = np.unique(data)

        # try and get the names by splitting the key along "-" (this is how
        # sets_to_int_data() forms the key
        names = key.split("-")
        # remove duplicates and preserve order
        # <https://stackoverflow.com/a/7961390/353337>:
        names = list(dict.fromkeys(names))
        if len(names) != len(tags):
            # alternative names
            names = [f"set-key-{tag}" for tag in tags]

        # TODO there's probably a better way besides np.where, something from
        # np.unique or np.sort
        for name, tag in zip(names, tags):
            self.point_sets[name] = np.where(data == tag)[0]

        # remove the cell data
        del self.point_data[key]


def merge_cell_blocks(mesh: Mesh):
    """Merge same-type cell blocks of ``mesh`` in place. See
    :meth:`Mesh.merge_cell_blocks`."""
    return mesh.merge_cell_blocks()


def split_cell_blocks(mesh: Mesh):
    """Restore the original cell-block layout of ``mesh``. See
    :meth:`Mesh.split_cell_blocks`."""
    return mesh.split_cell_blocks()
