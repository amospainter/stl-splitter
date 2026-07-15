"""Split a mesh into pieces along cut planes using manifold boolean ops."""
from __future__ import annotations

import manifold3d
import numpy as np
import trimesh

from .geometry import FLOATING_REGION_MIN_VOLUME_MM3, CutPlacementError, ResolvedCut
from .progress import ProgressReporter


def _to_manifold(mesh: trimesh.Trimesh) -> manifold3d.Manifold:
    m = manifold3d.Mesh(
        vert_properties=np.asarray(mesh.vertices, dtype=np.float32),
        tri_verts=np.asarray(mesh.faces, dtype=np.uint32),
    )
    return manifold3d.Manifold(m)


def _to_trimesh(man: manifold3d.Manifold) -> trimesh.Trimesh:
    m = man.to_mesh()
    return trimesh.Trimesh(
        vertices=np.asarray(m.vert_properties[:, :3], dtype=np.float64),
        faces=np.asarray(m.tri_verts, dtype=np.int64),
        process=False,
    )


def _check_no_floating_regions(piece: trimesh.Trimesh, index: int, allow_floating_regions: bool = False) -> None:
    """Raise if `piece` is actually multiple disconnected chunks of
    meaningful size — i.e. the cut landed at a spot where the geometry
    pinches to nothing, leaving unattached "floating" regions.

    `allow_floating_regions` is an escape hatch for models that legitimately
    export as several disjoint solids at a given cut (e.g. a piece with a
    detached accessory, or a mesh the user already knows is disconnected
    there) — skips the raise and lets those regions through as-is rather
    than forcing the user to keep hunting for a plane position that avoids
    them entirely."""
    if allow_floating_regions:
        return
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
    allow_floating_regions: bool = False,
) -> list[trimesh.Trimesh]:
    """Cut mesh into len(resolved_cuts)+1 pieces at the given (already
    resolved, position-sorted) cut planes, by converting to a manifold once
    and progressively slicing pieces off a shrinking "remaining" solid with
    `Manifold.split_by_plane` (rather than boolean-intersecting the full
    original mesh against an oversized clipping box per piece). Each cut's
    plane can be tilted (ResolvedCut.normal need not be axis-aligned).

    `resolved_cuts` are sorted by position with normals pointing in the
    +axis direction (see geometry.resolve_cuts), so slicing off the
    negative-normal ("below") side at each cut in order yields pieces in
    the same low-to-high order as before."""
    if not resolved_cuts:
        return [mesh.copy()]

    n_pieces = len(resolved_cuts) + 1
    remaining = _to_manifold(mesh)
    pieces = []
    for i, cut in enumerate(resolved_cuts):
        offset = float(np.dot(cut.normal, cut.origin))
        # split_by_plane's first result is on the +normal side, second on
        # the -normal side (verified against manifold3d directly) -- so
        # "below" (the piece we cut off here) is the second element.
        above, below = remaining.split_by_plane(tuple(cut.normal), offset)
        if below.num_tri() == 0:
            raise CutPlacementError(f"Cut produced an empty piece {i + 1}.", piece_index=i)
        piece = _to_trimesh(below)
        _check_no_floating_regions(piece, i, allow_floating_regions)
        pieces.append(piece)
        remaining = above
        if progress:
            progress.step(f"Cutting piece {i + 1}/{n_pieces}")

    if remaining.num_tri() == 0:
        raise CutPlacementError(f"Cut produced an empty piece {n_pieces}.", piece_index=n_pieces - 1)
    last = _to_trimesh(remaining)
    _check_no_floating_regions(last, n_pieces - 1, allow_floating_regions)
    pieces.append(last)
    if progress:
        progress.step(f"Cutting piece {n_pieces}/{n_pieces}")
    return pieces
