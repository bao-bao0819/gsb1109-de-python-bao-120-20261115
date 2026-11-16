"""Export a boundary catalog for one or more meshes.

Group numbers (e.g. gmsh physical tags) are only unique per source mesh and
per dimension: two meshes may reuse the same number, and one mesh may use the
same number for, say, a 1D and a 2D group. The catalog therefore keys every
group by the identity tuple

    (namespace, dim, number)

where ``namespace`` identifies the source mesh. The group name is a display
attribute only; renaming a group never changes its identity, and the original
name is always preserved in the catalog entry.

A cell may belong to any number of groups, so membership is collected as
sets of cell indices (from ``cell_sets`` and integer cell data), never
collapsed into a single per-cell value.

References that cannot be resolved within their own namespace and dimension
(unknown cell sets, or integer tags without a matching group of the same
dimension) are listed separately under ``"unknown"``. A tag is never matched
against a group carrying the same number in another dimension.
"""

from __future__ import annotations

import json

import numpy as np

from ._mesh import Mesh


def _iter_meshes(meshes, default_namespace: str):
    if isinstance(meshes, Mesh):
        yield default_namespace, meshes
    elif isinstance(meshes, dict):
        yield from meshes.items()
    else:
        # iterable of (namespace, mesh) pairs
        yield from meshes


def _new_group(namespace: str, dim: int, number: int, name: str) -> dict:
    return {
        "namespace": namespace,
        "dim": dim,
        "number": number,
        # original group name; never changes, part of the exported record
        "name": name,
        # display attribute; may be changed via rename_group()
        "display_name": name,
        # further display names that were mapped to this same identity
        "aliases": [],
        "cells": {},
    }


def _add_cells(entry: dict, cell_type: str, indices) -> None:
    cells = entry["cells"].setdefault(cell_type, set())
    cells.update(int(i) for i in indices)


def boundary_catalog(
    meshes,
    physical_data_key: str = "gmsh:physical",
    default_namespace: str = "default",
) -> dict:
    """Build a boundary catalog from one or more meshes.

    Parameters
    ----------
    meshes:
        A single :class:`meshio.Mesh`, a dict mapping namespace to mesh, or
        an iterable of ``(namespace, mesh)`` pairs. The namespace identifies
        the source mesh; group numbers are only unique per namespace and
        dimension.
    physical_data_key:
        Name of the integer cell data holding group numbers (e.g.
        ``"gmsh:physical"``). Tags are resolved against groups of the same
        cell-block dimension only.
    default_namespace:
        Namespace used when a single mesh is passed without a namespace.

    Returns
    -------
    A JSON-serializable dict with keys ``"groups"`` and ``"unknown"``.
    """
    # group identity: (namespace, dim, number) -> entry
    groups: dict[tuple[str, int, int], dict] = {}
    # unknown integer-tag references, keyed to merge across cell blocks
    unknown_tags: dict[tuple[str, int, int, str], dict] = {}
    # unknown named references (cell sets without field data)
    unknown_sets: list[dict] = []

    for namespace, mesh in _iter_meshes(meshes, default_namespace):
        if not isinstance(mesh, Mesh):
            raise TypeError(f"Expected a meshio.Mesh for namespace '{namespace}'.")

        # Register all named groups. Names mapping to the same (dim, number)
        # are display names of one and the same group.
        for name, value in mesh.field_data.items():
            number, dim = int(value[0]), int(value[1])
            key = (namespace, dim, number)
            if key in groups:
                groups[key]["aliases"].append(name)
            else:
                groups[key] = _new_group(namespace, dim, number, name)

        # Cell sets: a cell may appear in any number of sets. Cell sets with
        # a "gmsh:" prefix are reader-internal bookkeeping (e.g.
        # "gmsh:bounding_entities"), not group references.
        for set_name, member_list in mesh.cell_sets.items():
            if set_name.startswith("gmsh:"):
                continue
            if set_name in mesh.field_data:
                number, dim = (
                    int(mesh.field_data[set_name][0]),
                    int(mesh.field_data[set_name][1]),
                )
                entry = groups[(namespace, dim, number)]
                for members, cell_block in zip(member_list, mesh.cells):
                    if members is None:
                        continue
                    _add_cells(entry, cell_block.type, np.asarray(members).flat)
            else:
                # Unknown reference: no group of this name in the same
                # namespace. Do not guess a number or dimension.
                entry = {
                    "namespace": namespace,
                    "name": set_name,
                    "source": "cell_set",
                    "cells": {},
                }
                for members, cell_block in zip(member_list, mesh.cells):
                    if members is None:
                        continue
                    _add_cells(entry, cell_block.type, np.asarray(members).flat)
                unknown_sets.append(entry)

        # Integer cell data: resolve tags within the cell block's dimension.
        if physical_data_key in mesh.cell_data:
            source = f"cell_data:{physical_data_key}"
            for values, cell_block in zip(
                mesh.cell_data[physical_data_key], mesh.cells
            ):
                values = np.asarray(values)
                for tag in np.unique(values):
                    number = int(tag)
                    key = (namespace, cell_block.dim, number)
                    indices = np.where(values == tag)[0]
                    if key in groups:
                        _add_cells(groups[key], cell_block.type, indices)
                    elif number == 0:
                        # 0 conventionally means "not in any group"
                        continue
                    else:
                        # Unknown reference. Never fall back to a group with
                        # the same number in another dimension.
                        ukey = (namespace, cell_block.dim, number, source)
                        if ukey not in unknown_tags:
                            unknown_tags[ukey] = {
                                "namespace": namespace,
                                "dim": cell_block.dim,
                                "number": number,
                                "source": source,
                                "cells": {},
                            }
                        _add_cells(unknown_tags[ukey], cell_block.type, indices)

    def finalize(entry: dict) -> dict:
        entry["cells"] = {
            cell_type: sorted(indices)
            for cell_type, indices in sorted(entry["cells"].items())
            if indices
        }
        return entry

    catalog = {
        "groups": [
            finalize(groups[key])
            for key in sorted(groups, key=lambda k: (k[0], k[1], k[2]))
        ],
        "unknown": [finalize(entry) for entry in unknown_sets]
        + [
            finalize(unknown_tags[key])
            for key in sorted(unknown_tags, key=lambda k: (k[0], k[1], k[2], k[3]))
        ],
    }
    return catalog


def rename_group(
    catalog: dict, namespace: str, dim: int, number: int, new_display_name: str
) -> dict:
    """Change a group's display name without touching its identity.

    The identity ``(namespace, dim, number)`` and the original ``name`` stay
    untouched; only ``display_name`` is updated. Returns the updated entry.
    """
    for entry in catalog["groups"]:
        if (
            entry["namespace"] == namespace
            and entry["dim"] == dim
            and entry["number"] == number
        ):
            entry["display_name"] = new_display_name
            return entry
    raise KeyError(f"No group with identity ({namespace!r}, {dim}, {number}).")


def catalog_to_json(catalog: dict, **kwargs) -> str:
    """Serialize a boundary catalog to a JSON string."""
    kwargs.setdefault("indent", 2)
    return json.dumps(catalog, **kwargs)


def write_boundary_catalog(catalog: dict, path, **kwargs) -> None:
    """Write a boundary catalog to a JSON file."""
    with open(path, "w") as f:
        f.write(catalog_to_json(catalog, **kwargs))
        f.write("\n")
