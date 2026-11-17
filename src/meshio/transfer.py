"""Conservative transfer of cell data between meshes.

This module re-exports the transfer helpers implemented in
:mod:`meshio._transfer`.

.. code-block:: python

    import meshio

    result = meshio.transfer.transfer_cell_data(
        source,
        target,
        cell_types=["triangle", "quad"],
        extensive=["mass"],
        intensive=["temperature"],
    )
"""

from ._transfer import (
    CellOverlap,
    CellTransferResult,
    cell_overlaps,
    conservative_transfer,
    map_cell_data,
    overlap_volumes,
    remap_cell_data,
    transfer_cell_data,
    transfer_cell_field,
    transfer_cell_fields,
)

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
