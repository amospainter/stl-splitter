"""v2: automatically split a mesh (potentially along multiple axes) so every
resulting piece fits within a given print-bed envelope, adding connectors
once cutting is complete.
"""
from __future__ import annotations

import itertools
import os
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import trimesh

from .connectors import add_connectors_after_cuts
from .cutting import cut_mesh
from .geometry import _PARALLEL_FACE_THRESHOLD, ResolvedCut, axis_index, compute_cut_planes, resolve_cuts
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


def _axis_permutation_rotations() -> list[np.ndarray]:
    """Every 90-degree-multiple, orientation-preserving (proper rotation,
    determinant +1 — never a mirror) reassignment of which world axis a
    mesh's local X/Y/Z extents land on: 6 matrices total (3 even
    permutations, already proper rotations, plus the 3 odd permutations —
    axis swaps — each paired with a sign flip on one axis to cancel the
    mirroring and restore a proper rotation). Used to check whether simply
    reorienting a piece, not cutting it further, would let it fit the bed."""
    rotations = []
    for perm in itertools.permutations(range(3)):
        m = np.zeros((3, 3))
        for i, p in enumerate(perm):
            m[p, i] = 1.0
        if np.linalg.det(m) < 0:
            m[0, :] *= -1.0  # flip one axis to cancel the mirror, restoring det=+1
        rotations.append(m)
    return rotations


_AXIS_PERMUTATION_ROTATIONS = _axis_permutation_rotations()


def _best_fit_rotation(mesh: trimesh.Trimesh, bed_dims: dict[str, float]) -> np.ndarray | None:
    """Return whichever axis-permutation rotation (see
    `_axis_permutation_rotations`) lets `mesh`, as-is, fit entirely within
    `bed_dims` without any further cutting — or None if no 90-degree
    reorientation achieves that. When more than one works, prefers the one
    changing the fewest axes from the mesh's current orientation (the
    identity, i.e. "no rotation needed", always wins if it already fits),
    so a piece is never rotated more than necessary."""
    limits = [bed_dims.get(axis) for axis in _AXES]
    extents = mesh.extents
    best: tuple[int, np.ndarray] | None = None
    for rot in _AXIS_PERMUTATION_ROTATIONS:
        # Each row of a signed-permutation matrix has exactly one nonzero
        # (+-1) entry, so |rot| @ extents reassigns old-axis extents onto
        # new axes without the sign flips affecting their (always
        # nonnegative) magnitude.
        new_extents = np.abs(rot) @ extents
        if all(limits[j] is None or new_extents[j] <= limits[j] + 1e-6 for j in range(3)):
            changed = int(np.count_nonzero(~np.isclose(rot, np.eye(3))))
            if best is None or changed < best[0]:
                best = (changed, rot)
    return best[1] if best is not None else None


def auto_fit_split(
    mesh: trimesh.Trimesh,
    bed_dims: dict[str, float],
    connector_kwargs: dict | None = None,
    progress: ProgressReporter | None = None,
    allow_rotation: bool = True,
    allow_floating_regions: bool = False,
) -> tuple[list[trimesh.Trimesh], list[trimesh.Trimesh]]:
    """Recursively split `mesh` on whichever axis is most oversized relative
    to `bed_dims` (e.g. {"x": 220, "y": 220, "z": 250}) until every piece
    fits. Axes absent from bed_dims are left unconstrained. Pass
    connector_kwargs=None to cut only, with no connectors. The total number
    of cuts isn't known upfront (it depends on the mesh), so progress here
    is indeterminate. Returns (pieces, dowels).

    Cutting happens first, across the whole recursion, with no connectors;
    sockets are added in one pass over the FINAL pieces afterward (see
    `connectors.add_connectors_after_cuts`). A recursion level cuts on
    whichever axis is currently most oversized — the same axis can recur at
    different depths on different branches — so adding sockets eagerly after
    each level's cut could let a later level's cut (on the same or a
    different axis) slice straight through an already-carved socket on a
    sibling piece's shared plane.

    `allow_rotation` (default True): before cutting a piece that doesn't fit
    in its current orientation, check whether some 90-degree-multiple
    reorientation would let it fit as-is (see `_best_fit_rotation`) — a long
    thin piece lying the "wrong" way relative to the bed is a common case
    where a rotation avoids an otherwise-unnecessary cut entirely. Since a
    rotation is only ever taken when it strictly avoids a cut that would
    otherwise happen, this can only reduce piece count, never increase it;
    pass False to restore the original cut-only-in-place behavior."""
    # One process pool shared across the whole recursion, rather than
    # compute_cut_planes spinning up (and tearing down) a fresh one at every
    # oversized piece it's asked to plan for — worker-process startup (fresh
    # trimesh/manifold3d imports, ~0.5-1s per worker) would otherwise be paid
    # once per recursion level instead of once total. Only worth creating when
    # the top-level mesh is heavy enough that compute_cut_planes would have
    # opted into a pool anyway; smaller meshes keep the serial path (executor
    # stays None), unchanged from before this existed.
    executor = (
        ProcessPoolExecutor(max_workers=os.cpu_count() or 1)
        if len(mesh.faces) >= _PARALLEL_FACE_THRESHOLD
        else None
    )
    applied_cuts: list[ResolvedCut] = []
    try:
        pieces = _split_recursive(
            mesh, bed_dims, progress, applied_cuts, executor, allow_rotation, allow_floating_regions,
        )
    finally:
        if executor is not None:
            executor.shutdown(wait=True)

    if connector_kwargs is None:
        return pieces, []
    return add_connectors_after_cuts(pieces, applied_cuts, connector_kwargs, progress)


def _split_recursive(
    mesh: trimesh.Trimesh,
    bed_dims: dict[str, float],
    progress: ProgressReporter | None,
    applied_cuts: list[ResolvedCut],
    executor: "ProcessPoolExecutor | None",
    allow_rotation: bool,
    allow_floating_regions: bool = False,
) -> list[trimesh.Trimesh]:
    axis = _oversized_axis(mesh, bed_dims)
    if axis is None:
        return [mesh]

    if allow_rotation:
        rotation = _best_fit_rotation(mesh, bed_dims)
        if rotation is not None:
            rotated = mesh.copy()
            transform = np.eye(4)
            transform[:3, :3] = rotation
            rotated.apply_transform(transform)
            if progress:
                progress.step("Rotated a piece to fit the bed without an extra cut")
            return [rotated]

    limit = bed_dims[axis]
    planes = compute_cut_planes(
        mesh, axis, spacing=limit, executor=executor,
        cancel_event=progress.cancel_event if progress else None,
    )
    resolved = resolve_cuts(mesh, axis, planes)
    pieces = cut_mesh(mesh, resolved, allow_floating_regions=allow_floating_regions)
    applied_cuts.extend(resolved)
    if progress:
        progress.step(f"Auto-fit split on {axis} axis into {len(pieces)} piece(s)")

    result = []
    for piece in pieces:
        result.extend(_split_recursive(
            piece, bed_dims, progress, applied_cuts, executor, allow_rotation, allow_floating_regions,
        ))
    return result
