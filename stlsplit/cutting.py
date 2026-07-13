"""Split a mesh into pieces along cut planes using manifold boolean ops."""
from __future__ import annotations

import numpy as np
import trimesh

from .geometry import FLOATING_REGION_MIN_VOLUME_MM3, CutPlacementError, ResolvedCut
from .progress import ProgressReporter

_MARGIN = 10.0  # mm of slack added around the mesh bounds for the cutting box
_CANON_Z = np.array([0.0, 0.0, 1.0])


def _halfspace_box(mesh: trimesh.Trimesh, origin: np.ndarray, normal: np.ndarray, keep_positive: bool) -> trimesh.Trimesh:
    """Oversized box approximating one side of the plane through `origin`
    with the given `normal`: {x : normal.(x - origin) >= 0} if
    `keep_positive` else <= 0. Large enough to fully cover `mesh` regardless
    of the plane's orientation, so a tilted cut still clips cleanly."""
    size = float(np.linalg.norm(mesh.extents)) * 4.0 + 2 * _MARGIN
    box = trimesh.creation.box(extents=[size, size, size])
    box.apply_translation([0.0, 0.0, size / 2.0])  # one face at local z=0, spanning [0, size] along +z
    sign = 1.0 if keep_positive else -1.0
    box.apply_transform(trimesh.geometry.align_vectors(_CANON_Z, sign * normal))
    box.apply_translation(origin)
    return box


def _check_no_floating_regions(piece: trimesh.Trimesh, index: int) -> None:
    """Raise if `piece` is actually multiple disconnected chunks of
    meaningful size — i.e. the cut landed at a spot where the geometry
    pinches to nothing, leaving unattached "floating" regions."""
    components = piece.split(only_watertight=False)
    if len(components) <= 1:
        return
    significant = [c for c in components if abs(c.volume) >= FLOATING_REGION_MIN_VOLUME_MM3]
    if len(significant) > 1:
        raise CutPlacementError(
            f"Piece {index + 1} split into {len(significant)} disconnected regions instead of one "
            "solid — a cut plane landed where the model pinches to nothing.",
            piece_index=index,
        )


def cut_mesh(
    mesh: trimesh.Trimesh,
    resolved_cuts: list[ResolvedCut],
    progress: ProgressReporter | None = None,
) -> list[trimesh.Trimesh]:
    """Cut mesh into len(resolved_cuts)+1 pieces at the given (already
    resolved, position-sorted) cut planes, using boolean intersection
    against oriented clipping boxes. Each cut's plane can be tilted
    (ResolvedCut.normal need not be axis-aligned)."""
    if not resolved_cuts:
        return [mesh.copy()]

    n_pieces = len(resolved_cuts) + 1
    pieces = []
    for i in range(n_pieces):
        clip_meshes = [mesh]
        if i > 0:
            lower = resolved_cuts[i - 1]
            clip_meshes.append(_halfspace_box(mesh, lower.origin, lower.normal, keep_positive=True))
        if i < n_pieces - 1:
            upper = resolved_cuts[i]
            clip_meshes.append(_halfspace_box(mesh, upper.origin, upper.normal, keep_positive=False))
        piece = trimesh.boolean.intersection(clip_meshes, engine="manifold")
        if piece.is_empty:
            raise CutPlacementError(f"Cut produced an empty piece {i + 1}.", piece_index=i)
        _check_no_floating_regions(piece, i)
        pieces.append(piece)
        if progress:
            progress.step(f"Cutting piece {i + 1}/{n_pieces}")
    return pieces
