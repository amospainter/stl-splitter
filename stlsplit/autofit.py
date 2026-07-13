"""v2: automatically split a mesh (potentially along multiple axes) so every
resulting piece fits within a given print-bed envelope, adding connectors at
each cut as it goes.
"""
from __future__ import annotations

import trimesh

from .connectors import add_connectors
from .cutting import cut_mesh
from .geometry import axis_index, compute_cut_planes, resolve_cuts
from .progress import ProgressReporter

_AXES = ("x", "y", "z")


def _oversized_axis(mesh: trimesh.Trimesh, bed_dims: dict[str, float], eps: float = 1e-6) -> str | None:
    """Return the axis with the largest overage ratio, or None if the mesh
    already fits within bed_dims on every axis."""
    best_axis, best_ratio = None, 1.0 + eps
    for axis in _AXES:
        limit = bed_dims.get(axis)
        if limit is None:
            continue
        extent = mesh.extents[axis_index(axis)]
        ratio = extent / limit
        if ratio > best_ratio:
            best_axis, best_ratio = axis, ratio
    return best_axis


def auto_fit_split(
    mesh: trimesh.Trimesh,
    bed_dims: dict[str, float],
    connector_kwargs: dict | None = None,
    progress: ProgressReporter | None = None,
) -> tuple[list[trimesh.Trimesh], list[trimesh.Trimesh]]:
    """Recursively split `mesh` on whichever axis is most oversized relative
    to `bed_dims` (e.g. {"x": 220, "y": 220, "z": 250}) until every piece
    fits, adding socket connectors at each cut. Axes absent from bed_dims
    are left unconstrained. Pass connector_kwargs=None to cut only, with no
    connectors. The total number of cuts isn't known upfront (it depends on
    the mesh), so progress here is indeterminate. Returns (pieces, dowels)."""
    dowels: list[trimesh.Trimesh] = []
    pieces = _split_recursive(mesh, bed_dims, connector_kwargs, progress, dowels)
    return pieces, dowels


def _split_recursive(
    mesh: trimesh.Trimesh,
    bed_dims: dict[str, float],
    connector_kwargs: dict | None,
    progress: ProgressReporter | None,
    dowels: list[trimesh.Trimesh],
) -> list[trimesh.Trimesh]:
    axis = _oversized_axis(mesh, bed_dims)
    if axis is None:
        return [mesh]

    limit = bed_dims[axis]
    planes = compute_cut_planes(mesh, axis, spacing=limit)
    resolved = resolve_cuts(mesh, axis, planes)
    pieces = cut_mesh(mesh, resolved)
    if connector_kwargs is not None:
        pieces, new_dowels = add_connectors(pieces, resolved, **connector_kwargs)
        dowels.extend(new_dowels)
    if progress:
        progress.step(f"Auto-fit split on {axis} axis into {len(pieces)} piece(s)")

    result = []
    for piece in pieces:
        result.extend(_split_recursive(piece, bed_dims, connector_kwargs, progress, dowels))
    return result
