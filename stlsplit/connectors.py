"""Add socket-and-dowel connectors at each cut interface so pieces can be
reassembled and aligned.

Convention: for interface i (between piece i and piece i+1, both already cut
at the plane), BOTH pieces get a matching socket subtracted (depth
`peg_length` mm into their own interior). A separate, standalone dowel is
generated for each socket pair — sized to the socket diameter minus
`peg_clearance` — meant to be printed on its own and glued/pressed into both
sockets at assembly time, rather than being printed as part of either piece.

Dowel cross-section is selectable: round, D-shaped (round with one flat, for
tool-free anti-rotation), square, or hexagonal.

Peg/socket positions are chosen from the *actual* 2D cross-section polygon of
the cut face (via mesh.section), not the piece's bounding box: for
non-convex shapes (limbs, handles, anything that isn't a box) bbox corners
can land outside the solid entirely. Positions are then re-verified in full
3D (not just at the cut plane) by sampling the true surface distance around
and along the socket cavity on both pieces — a 2D check at the cut plane
alone can pass a spot where the wall tapers away a few mm deeper in,
producing a socket that punches through the outer skin.

Cut planes need not be axis-aligned: each interface carries its own
(origin, normal) from a `geometry.ResolvedCut`, and all the placement math
below (section slicing, radial wall sampling, connector orientation) works
directly off that normal rather than assuming one of the world axes.
"""
from __future__ import annotations

import math

import numpy as np
import trimesh
from shapely.geometry import Point, Polygon
from shapely.ops import unary_union

from .geometry import ResolvedCut
from .progress import ProgressReporter

_CANON_AXIS = np.array([0.0, 0.0, 1.0])
_SECTION_EPS = 0.05  # mm inward offset so we slice through the interior, not the flat cap itself
_SHAPE_SIDES = {"square": 4, "hex": 6}
SUPPORTED_SHAPES = ("round", "d", "square", "hex")


def _lateral_basis(normal: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Orthonormal (u, v) in-plane basis perpendicular to `normal`, derived
    from the same rotation used to orient connectors (so ray-sampling
    directions and connector geometry agree)."""
    transform = trimesh.geometry.align_vectors(_CANON_AXIS, normal)
    return transform[:3, 0], transform[:3, 1]


def _project_onto_plane(point: np.ndarray, origin: np.ndarray, normal: np.ndarray) -> np.ndarray:
    return point - normal * np.dot(normal, point - origin)


def _interface_polygon(piece: trimesh.Trimesh, origin: np.ndarray, normal: np.ndarray, inward_sign: float):
    """Cross-section of `piece` near the given plane, as a shapely
    (Multi)Polygon in its own local 2D frame, plus the 4x4 transform mapping
    that local 2D frame back to world 3D coordinates. Slicing exactly at the
    plane is degenerate (it's the piece's own flat cut face, not a crossing
    plane), so we sample `_SECTION_EPS` mm into the piece's interior instead.
    `inward_sign` is +1 if the piece's solid extends toward the normal
    direction from the plane, -1 if it extends the other way. Returns
    (None, None) if the piece doesn't intersect the resulting plane."""
    sample_origin = origin + inward_sign * _SECTION_EPS * normal

    section = piece.section(plane_origin=sample_origin, plane_normal=normal)
    if section is None:
        return None, None

    planar, to_3d = section.to_2D()
    polygons = planar.polygons_full
    if not polygons:
        return None, None
    polygon = unary_union(polygons)
    if polygon.is_empty:
        return None, None
    return polygon, to_3d


def _candidate_positions_2d(polygon, inset: float, min_wall_thickness: float, peg_radius: float, limit: int):
    """Pick up to `limit` 2D points inside `polygon`, ranked by farthest-point
    sampling (most interior first, then spread out). This is a cheap
    pre-filter on the flat cross-section only — full 3D safety is checked
    separately per candidate. Returns a list of (x, y) tuples."""
    required_clearance = min_wall_thickness + peg_radius

    # Erode by the larger of the user's edge inset and the minimum safe
    # clearance. If the part is too slender for the user's inset (e.g. a
    # 9mm-radius leg with an 8mm inset), fall back to just the minimum
    # clearance needed rather than silently finding nothing.
    safe_zone = None
    for erosion in (max(inset, required_clearance), required_clearance):
        candidate_zone = polygon.buffer(-erosion)
        if not candidate_zone.is_empty:
            safe_zone = candidate_zone
            break
    if safe_zone is None:
        return []

    minx, miny, maxx, maxy = safe_zone.bounds
    span = max(maxx - minx, maxy - miny, 1e-6)
    # Scale sampling density to the feature size so slender cross-sections
    # (thin limbs) still get enough candidate points to find a valid spot.
    resolution = int(np.clip(span / max(peg_radius, 1.0) * 3, 10, 48))
    xs = np.linspace(minx, maxx, resolution)
    ys = np.linspace(miny, maxy, resolution)

    candidates = []
    for x in xs:
        for y in ys:
            pt = Point(x, y)
            if not safe_zone.contains(pt):
                continue
            boundary_dist = polygon.boundary.distance(pt)
            candidates.append((x, y, boundary_dist))

    if not candidates:
        return []

    # farthest-point sampling: start from the most interior candidate, then
    # repeatedly pick whichever remaining candidate is farthest from all
    # already-chosen points, so pegs end up spread across the cross-section.
    candidates.sort(key=lambda c: -c[2])
    chosen = [candidates[0]]
    remaining = candidates[1:]
    while remaining and len(chosen) < limit:
        best, best_dist = None, -1.0
        for c in remaining:
            d = min((c[0] - ch[0]) ** 2 + (c[1] - ch[1]) ** 2 for ch in chosen)
            if d > best_dist:
                best, best_dist = c, d
        chosen.append(best)
        remaining.remove(best)

    return [(c[0], c[1]) for c in chosen]


def _is_safe_3d(
    piece_a: trimesh.Trimesh,
    piece_b: trimesh.Trimesh,
    normal: np.ndarray,
    lateral: tuple[np.ndarray, np.ndarray],
    center: np.ndarray,
    socket_radius: float,
    peg_length: float,
    min_wall_thickness: float,
    n_angle: int = 8,
    n_depth: int = 3,
) -> bool:
    """Verify a candidate socket cavity stays at least `min_wall_thickness`
    mm from each piece's outer surface along its *entire* depth, not just at
    the cut plane. A 2D check at the plane alone can pass a spot where the
    wall tapers away deeper in, producing a socket that punches through the
    outer skin a few mm from the joint.

    Measures wall thickness by ray-casting radially outward from the socket
    surface at several depths, rather than nearest-surface-in-any-direction:
    the latter is fooled near the socket mouth, where the nearest surface is
    legitimately the flat cut face itself (the socket's own entrance), not a
    wall that needs protecting.
    """
    depths = np.linspace(1.0, peg_length - 0.5, n_depth) if peg_length > 1.0 else [peg_length / 2.0]
    angles = np.linspace(0, 2 * np.pi, n_angle, endpoint=False)
    u, v = lateral
    lateral_dirs = [math.cos(ang) * u + math.sin(ang) * v for ang in angles]

    for piece, sign in ((piece_a, -1.0), (piece_b, 1.0)):
        origins = []
        directions = []
        for depth in depths:
            base = center + sign * depth * normal
            for d in lateral_dirs:
                origins.append(base + d * socket_radius)
                directions.append(d)
        origins = np.array(origins)
        directions = np.array(directions)

        locations, index_ray, _ = piece.ray.intersects_location(origins, directions)
        if len(np.unique(index_ray)) < len(origins):
            return False  # a ray escaped without hitting anything: outside the solid here

        dists = np.linalg.norm(locations - origins[index_ray], axis=1)
        min_per_ray = np.full(len(origins), np.inf)
        np.minimum.at(min_per_ray, index_ray, dists)
        if min_per_ray.min() < min_wall_thickness:
            return False
    return True


def _normal_transform(normal: np.ndarray, center: np.ndarray) -> np.ndarray:
    """Transform mapping the canonical +Z primitive to the given cut
    plane's normal and position."""
    transform = trimesh.geometry.align_vectors(_CANON_AXIS, normal).copy()
    transform[:3, 3] += center
    return transform


def _regular_polygon_points(n_sides: int, circumradius: float, rotation: float = 0.0):
    return [
        (
            circumradius * math.cos(rotation + 2 * math.pi * i / n_sides),
            circumradius * math.sin(rotation + 2 * math.pi * i / n_sides),
        )
        for i in range(n_sides)
    ]


def _build_profile(
    shape: str,
    diameter: float,
    length: float,
    add_flat: bool = False,
    flat_depth: float = 1.5,
) -> trimesh.Trimesh:
    """Build a connector cross-section prism, centered at the origin, spanning
    [-length/2, length/2] along canonical +Z."""
    if shape not in SUPPORTED_SHAPES:
        raise ValueError(f"Unsupported dowel shape '{shape}'; choose one of {SUPPORTED_SHAPES}")

    radius = diameter / 2.0

    if shape in _SHAPE_SIDES:
        n = _SHAPE_SIDES[shape]
        rotation = math.pi / n if shape == "square" else 0.0
        poly = Polygon(_regular_polygon_points(n, radius, rotation))
        mesh = trimesh.creation.extrude_polygon(poly, height=length)
        mesh.apply_translation([0, 0, -length / 2.0])
        return mesh

    mesh = trimesh.creation.cylinder(radius=radius, height=length, sections=32)
    if shape == "d" or add_flat:
        # shave a chord off one side (in canonical +X, before axis rotation)
        # to break rotational symmetry.
        box = trimesh.creation.box(extents=[diameter, diameter, length * 1.5])
        box.apply_translation([radius - flat_depth + diameter / 2.0, 0, 0])
        mesh = trimesh.boolean.difference([mesh, box], engine="manifold")
    return mesh


def _build_connector(
    shape: str,
    diameter: float,
    length: float,
    normal: np.ndarray,
    center: np.ndarray,
    add_flat: bool,
    flat_depth: float,
    local_offset: float = 0.0,
) -> trimesh.Trimesh:
    """Build a connector prism and place it at `center`. `local_offset` shifts
    the prism along its own (pre-rotation) +Z axis before placement, which
    maps to the world cut-plane normal direction — used to make a socket
    extend into only one piece's interior rather than spanning symmetrically."""
    profile = _build_profile(shape, diameter, length, add_flat, flat_depth)
    if local_offset:
        profile.apply_translation([0, 0, local_offset])
    profile.apply_transform(_normal_transform(normal, center))
    return profile


def add_connectors(
    pieces: list[trimesh.Trimesh],
    resolved_cuts: list[ResolvedCut],
    peg_diameter: float = 7.0,
    peg_length: float = 5.0,
    peg_clearance: float = 0.18,
    inset: float = 8.0,
    n_pegs: int = 4,
    min_wall_thickness: float = 1.2,
    alignment_key: bool = False,
    key_flat_depth: float = 1.5,
    dowel_shape: str = "round",
    progress: ProgressReporter | None = None,
) -> tuple[list[trimesh.Trimesh], list[trimesh.Trimesh]]:
    """Returns (pieces, dowels): `pieces` are the input meshes with sockets
    subtracted at each interface; `dowels` are standalone connector meshes
    meant to be printed separately. `resolved_cuts` must be sorted by
    position and correspond 1:1 to the interfaces between consecutive
    `pieces` (as produced by cutting.cut_mesh from the same list)."""
    if not resolved_cuts:
        return pieces, []

    pieces = list(pieces)
    dowels: list[trimesh.Trimesh] = []
    peg_radius = peg_diameter / 2.0
    socket_diameter = peg_diameter + peg_clearance
    socket_radius = socket_diameter / 2.0
    socket_overshoot = 0.6  # mm the socket crosses past the plane, for a clean boolean seam
    socket_length = peg_length + socket_overshoot
    # shift each socket's center so it extends `peg_length` into its own
    # piece and only `socket_overshoot` past the plane into the other
    socket_offset_a = -(peg_length - socket_overshoot) / 2.0
    socket_offset_b = (peg_length - socket_overshoot) / 2.0
    dowel_length = max(2 * peg_length - 0.5, 1.0)  # slight assembly clearance vs. combined socket depth

    n_interfaces = len(resolved_cuts)

    for i, cut in enumerate(resolved_cuts):
        piece_a = pieces[i]
        piece_b = pieces[i + 1]
        origin, normal = cut.origin, cut.normal
        lateral = _lateral_basis(normal)

        # piece_a is always the lower-index piece, whose solid extends
        # toward the plane's negative-normal side.
        polygon, to_3d = _interface_polygon(piece_a, origin, normal, inward_sign=-1.0)
        if polygon is None:
            if progress:
                progress.step(f"Connectors at interface {i + 1}/{n_interfaces} (skipped)")
            continue  # no flat cut face here; skip connectors for this interface

        candidates_2d = _candidate_positions_2d(
            polygon, inset, min_wall_thickness, peg_radius, limit=max(n_pegs * 6, 24)
        )

        centers = []
        for x, y in candidates_2d:
            c = (to_3d @ np.array([x, y, 0.0, 1.0]))[:3]
            c = _project_onto_plane(c, origin, normal)  # snap back onto the exact cut plane
            if _is_safe_3d(
                piece_a, piece_b, normal, lateral, c, socket_radius, peg_length, min_wall_thickness
            ):
                centers.append(c)
            if len(centers) >= n_pegs:
                break

        if not centers:
            if progress:
                progress.step(f"Connectors at interface {i + 1}/{n_interfaces} (skipped)")
            continue  # cross-section too thin anywhere (in true 3D) for a safe connector

        # A single round peg can't stop rotation about the cut normal; force
        # a flat on every peg at the interface when keying is needed (not
        # just one) so every dowel at a given joint is identical — simpler
        # to print and to tell apart than a mix of round and D-shaped pegs.
        # Non-round shapes are already keyed by their own geometry.
        use_key = dowel_shape == "round" and (alignment_key or len(centers) < 2)

        socket_a_union = []
        socket_b_union = []
        for j, center in enumerate(centers):
            key_this_one = use_key

            socket_a = _build_connector(
                dowel_shape, socket_diameter, socket_length, normal, center,
                key_this_one, key_flat_depth, local_offset=socket_offset_a,
            )
            socket_a_union.append(socket_a)

            socket_b = _build_connector(
                dowel_shape, socket_diameter, socket_length, normal, center,
                key_this_one, key_flat_depth, local_offset=socket_offset_b,
            )
            socket_b_union.append(socket_b)

            dowel = _build_connector(
                dowel_shape, peg_diameter, dowel_length, normal, center, key_this_one, key_flat_depth
            )
            dowels.append(dowel)

        piece_a = trimesh.boolean.difference([piece_a, *socket_a_union], engine="manifold")
        piece_b = trimesh.boolean.difference([piece_b, *socket_b_union], engine="manifold")

        pieces[i] = piece_a
        pieces[i + 1] = piece_b

        if progress:
            progress.step(f"Connectors at interface {i + 1}/{n_interfaces}")

    return pieces, dowels
