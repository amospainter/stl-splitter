"""Hollow a solid mesh to a given wall thickness via voxel erosion: voxelize,
fill, erode inward by the wall thickness, remesh the eroded interior, and
boolean-subtract it from the original solid."""
from __future__ import annotations

import numpy as np
import trimesh
from scipy import ndimage

_MAX_VOXELS_PER_AXIS = 600

# Independent of wall_thickness: real meshes can have narrow features (a
# crease, a limb resting against the torso) far smaller than any reasonable
# wall thickness. Pitch must stay at or below this regardless of how thick a
# wall is requested, or those features get bridged/misclassified during
# voxelization. See the note in hollow_mesh() for why this isn't just
# min_pitch from the per-mesh voxel budget.
_MAX_USEFUL_PITCH = 5.0

# The erosion step below can only resolve a wall as fine as a handful of
# voxel-widths — the distance transform's smallest possible nonzero reading
# for a voxel one grid-step from the boundary is `pitch` itself, so once
# `pitch` gets close to (or worse, exceeds) `wall_thickness`, the erosion
# threshold `dist > wall_thickness` stops meaningfully distinguishing "inside
# the wall" from "past it": on a real case (a ~500mm mesh hollowed to a 2mm
# wall, which forces pitch up to ~2.27mm under the voxel-count cap below),
# this didn't just leave the wall a bit uneven -- it silently ate nearly the
# entire model (0.02 mm^3 left out of ~7.7 million mm^3, split into
# disconnected scraps), because almost every interior voxel one step from the
# boundary already reads a distance of `pitch` > `wall_thickness` and gets
# eroded away. A `pitch` that's merely somewhat too coarse produces the
# milder version of the same failure: uneven thickness with real holes in
# some regions, not everywhere. This margin requires pitch to stay
# comfortably below wall_thickness before proceeding at all.
_MIN_PITCH_SAFETY_MARGIN = 3.0


def hollow_mesh(mesh: trimesh.Trimesh, wall_thickness: float, pitch: float | None = None) -> trimesh.Trimesh:
    if wall_thickness <= 0:
        raise ValueError("wall_thickness must be > 0")

    # cap voxel resolution so pathologically small pitches on large meshes
    # don't blow up memory/runtime
    max_extent = float(mesh.extents.max())
    min_pitch = max_extent / _MAX_VOXELS_PER_AXIS

    if pitch is None:
        # `wall_thickness / 3` alone is a poor pitch target for thick walls:
        # it has nothing to do with how much *geometric* detail the mesh
        # actually has. Real meshes have narrow features — creases, a limb
        # resting against the torso — whose physical gap is unrelated to the
        # requested wall thickness; a pitch coarser than that gap bridges it
        # during `voxelized().fill()` and misclassifies material there,
        # producing holes that `is_watertight` won't catch. Cap pitch at
        # `_MAX_USEFUL_PITCH` regardless of how thick a wall was requested,
        # so a 40mm-wall request on a huge mesh doesn't fall back to a grid
        # too coarse to see a 5-10mm gap under an arm. (Uncapped use of the
        # full per-mesh voxel budget was tried and rejected: on large,
        # simple-geometry meshes it forces pitch far finer than the mesh has
        # detail to justify, and `voxelized()`'s internal triangle
        # subdivision can exceed its own iteration limit before reaching
        # that pitch on a mesh with large flat faces.)
        pitch = max(min(wall_thickness / 3.0, _MAX_USEFUL_PITCH), 0.4)

    if pitch < min_pitch:
        # The cap would force a pitch too coarse for this wall_thickness on a
        # mesh this large — proceeding would silently produce corrupted
        # geometry (see the module docstring note above), not just a slower
        # or lower-fidelity result. Fail clearly instead: an honest error the
        # caller can act on (a thicker wall) beats a mesh that looks fine in
        # `is_watertight` but has holes or is mostly gone.
        #
        # Note this threshold only depends on the wall_thickness/max_extent
        # *ratio*, not absolute size — hollowing the same mesh before scaling
        # it up hits the identical limit at a proportionally thinner wall, so
        # that isn't a workaround; a genuinely thicker wall (relative to the
        # model's size) is the only fix within this tool's resolution.
        if min_pitch * _MIN_PITCH_SAFETY_MARGIN >= wall_thickness:
            max_safe_wall = min_pitch * _MIN_PITCH_SAFETY_MARGIN
            raise ValueError(
                f"wall_thickness ({wall_thickness}mm) is too thin to hollow reliably on a mesh this large "
                f"(largest dimension {max_extent:.1f}mm): the erosion grid would need a resolution finer "
                f"than this tool allows, and proceeding anyway would produce uneven thickness or holes "
                f"rather than a clean result. Try a wall_thickness of at least ~{max_safe_wall:.2f}mm at "
                f"this size."
            )
        pitch = min_pitch

    vox = mesh.voxelized(pitch=pitch).fill()
    # `.voxelized().fill()` fills the grid exactly to the mesh's own
    # bounding box, with no empty margin — every face of `vox.matrix` can
    # come out fully True, meaning cells right at the true surface have no
    # neighboring "outside" (False) cell for the distance transform to
    # measure against. `distance_transform_edt` on such an array has no
    # local reference for those cells and returns wildly-too-large
    # distances there (measured directly: up to 619mm on a mesh whose
    # largest true distance-to-surface is ~100mm) — those cells then
    # incorrectly pass the `dist > wall_thickness` erosion test even though
    # they're right at the boundary, which is what let entire walls
    # disappear rather than just come out uneven. Padding with one voxel of
    # explicit False on every side gives every surface cell a real "outside"
    # neighbor to measure against, so distances come out physically correct
    # (verified: max distance drops from 619mm to the geometrically correct
    # ~103mm on the same case).
    padded = np.pad(vox.matrix, 1, mode="constant", constant_values=False)
    padding_offset = trimesh.transformations.translation_matrix([-1, -1, -1])
    padded_transform = vox.transform @ padding_offset

    # Erode via a Euclidean distance transform (distance in mm from each
    # filled voxel to the nearest empty one), not scipy's binary_erosion:
    # binary_erosion's default structuring element only counts face-adjacent
    # (6-connected) voxel hops, which is a Manhattan/Chebyshev-ish distance,
    # not a true radius — it erodes much less aggressively along diagonals
    # than along the voxel axes. On a curved or angled surface that made the
    # actual wall thickness vary by up to ~2x direction-to-direction (as
    # little as half the requested thickness on the thinnest side), which is
    # exactly what let sockets/features punch through as unintended holes.
    # The EDT gives an isotropic offset, so wall thickness holds uniformly
    # regardless of the local surface angle relative to the voxel grid.
    dist = ndimage.distance_transform_edt(padded, sampling=pitch)
    eroded = dist > wall_thickness

    if not eroded.any():
        raise ValueError(
            f"wall_thickness ({wall_thickness}mm) is too large for this mesh; hollowing would remove everything"
        )

    inner_vox = trimesh.voxel.VoxelGrid(eroded, transform=padded_transform)
    inner_mesh = inner_vox.marching_cubes.apply_transform(inner_vox.transform)

    hollowed = trimesh.boolean.difference([mesh, inner_mesh], engine="manifold")
    if not hollowed.is_watertight:
        raise RuntimeError("hollowing produced a non-watertight mesh; try a different wall thickness")

    # Defense in depth against the failure mode above: `is_watertight` is a
    # purely topological check (every edge shared by exactly two consistently
    # wound faces) and stays True even when the erosion grid was too coarse
    # to resolve the wall — the observed real case kept 0.02 mm^3 out of
    # ~7.7 million mm^3 while still reporting `is_watertight`. The pre-check
    # above should already prevent this by refusing to run at all, but if
    # some other combination of parameters slips a corrupted result through
    # anyway, catch a near-total material loss here rather than silently
    # handing back a mesh that's basically gone.
    #
    # NOT checked here: `len(hollowed.split(only_watertight=False))` — a
    # correctly hollowed shell is topologically TWO disjoint watertight
    # surfaces (the outer skin and the inner cavity's boundary, which share
    # no vertices/edges with each other) by nature, not evidence of a
    # problem; `split()` reporting 2 (or more, for a shape with multiple
    # separate cavities) components here is the expected, correct result.
    if hollowed.volume < mesh.volume * 0.001:
        raise RuntimeError(
            "hollowing removed nearly the entire model instead of leaving a shell -- the requested "
            "wall_thickness is likely too thin for this mesh's size/detail; try a thicker wall"
        )
    return hollowed
