"""Hollow a solid mesh to a given wall thickness via voxel erosion: voxelize,
fill, erode inward by the wall thickness, remesh the eroded interior, and
boolean-subtract it from the original solid."""
from __future__ import annotations

import trimesh
from scipy import ndimage

_MAX_VOXELS_PER_AXIS = 220


def hollow_mesh(mesh: trimesh.Trimesh, wall_thickness: float, pitch: float | None = None) -> trimesh.Trimesh:
    if wall_thickness <= 0:
        raise ValueError("wall_thickness must be > 0")

    if pitch is None:
        pitch = max(wall_thickness / 3.0, 0.4)

    # cap voxel resolution so pathologically small pitches on large meshes
    # don't blow up memory/runtime
    max_extent = float(mesh.extents.max())
    min_pitch = max_extent / _MAX_VOXELS_PER_AXIS
    if pitch < min_pitch:
        pitch = min_pitch

    vox = mesh.voxelized(pitch=pitch).fill()
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
    dist = ndimage.distance_transform_edt(vox.matrix, sampling=pitch)
    eroded = dist > wall_thickness

    if not eroded.any():
        raise ValueError(
            f"wall_thickness ({wall_thickness}mm) is too large for this mesh; hollowing would remove everything"
        )

    inner_vox = trimesh.voxel.VoxelGrid(eroded, transform=vox.transform)
    inner_mesh = inner_vox.marching_cubes.apply_transform(inner_vox.transform)

    hollowed = trimesh.boolean.difference([mesh, inner_mesh], engine="manifold")
    if not hollowed.is_watertight:
        raise RuntimeError("hollowing produced a non-watertight mesh; try a different wall thickness")
    return hollowed
