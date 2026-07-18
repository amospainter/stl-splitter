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
import shapely
import trimesh
from shapely.geometry import Polygon
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


def _piece_side(
    piece: trimesh.Trimesh, cut: ResolvedCut, tol: float = 1e-4, outlier_frac: float = 0.10
) -> str | None:
    """Which side of `cut`'s plane `piece` sits on, if it was actually cut
    there: "a" if the piece touches the plane from the negative-normal side
    (max vertex distance within `tol` of 0, min distance clearly negative),
    "b" for the mirror case, or None if the piece doesn't touch this plane
    at all (was never cut here) or straddles it (wasn't cut here — a bug
    elsewhere, or `cut` doesn't correspond to a real cut on this piece).

    The near-zero-side check uses the `outlier_frac`-percentile of `d`
    rather than a strict max/min. A tilted cut through genuinely non-convex,
    organic geometry (a limb, a fold) can leave a real, non-negligible
    chunk of a piece dipping back across the cut plane locally even though
    the piece is unambiguously "cut here" and correctly one-sided overall --
    this isn't mesh noise, it's what a flat plane slicing through anatomy
    that folds back on itself actually looks like. Measured on a real case:
    up to ~5.8% of a piece's surface AREA (not just a handful of stray
    vertices) sat on the nominally-wrong side at a legitimate, otherwise
    perfectly good interface. A strict max/min let that disqualify the
    ENTIRE interface (find_facing_pairs found no pair at all, costing every
    connector at that cut), even though the piece plainly was cut there.
    10% leaves real margin above the observed 5.8% while still correctly
    rejecting a piece that genuinely doesn't touch this plane at all --
    there, the split runs closer to even (not a small minority on one side),
    so the percentile lands nowhere near zero and this still returns
    None."""
    d = (piece.vertices - cut.origin) @ cut.normal
    dmin = float(d.min())
    near_max = float(np.percentile(d, 100.0 * (1.0 - outlier_frac)))
    near_min = float(np.percentile(d, 100.0 * outlier_frac))
    dmax = float(d.max())
    if near_max <= tol and dmin < -tol:
        return "a"
    if near_min >= -tol and dmax > tol:
        return "b"
    return None


def find_facing_pairs(
    pieces: list[trimesh.Trimesh], cut: ResolvedCut, min_overlap_area: float = 1.0, tol: float = 1e-4
):
    """Return `[(i, j, overlap_polygon)]` for every pair of `pieces` that
    actually face each other across `cut`'s plane — piece `i` on the
    negative-normal ("a") side, piece `j` on the positive ("b") side, with
    their near-plane cross-section polygons overlapping by more than
    `min_overlap_area` mm^2 (not just both happening to touch the same
    plane — in a multi-axis split, two pieces can share a cut plane's axis
    position without ever being adjacent, e.g. diagonal pieces in a 2x2
    grid). `_interface_polygon`'s local 2D (x, y) frame only depends on
    `(origin, normal)`, not which piece or which side was sampled (verified
    empirically: `section.to_2D()`'s rotation and in-plane translation are
    identical regardless of which piece/inward_sign produced the section —
    only the out-of-plane offset differs), so polygons from different
    pieces at the same cut are directly comparable without reprojecting."""
    a_indices = [i for i, p in enumerate(pieces) if _piece_side(p, cut, tol) == "a"]
    b_indices = [i for i, p in enumerate(pieces) if _piece_side(p, cut, tol) == "b"]
    if not a_indices or not b_indices:
        return []

    a_polygons = {i: _interface_polygon(pieces[i], cut.origin, cut.normal, inward_sign=-1.0)[0] for i in a_indices}
    b_polygons = {j: _interface_polygon(pieces[j], cut.origin, cut.normal, inward_sign=1.0)[0] for j in b_indices}

    pairs = []
    for j, b_polygon in b_polygons.items():
        if b_polygon is None:
            continue
        for i, a_polygon in a_polygons.items():
            if a_polygon is None:
                continue
            overlap = a_polygon.intersection(b_polygon)
            if overlap.area > min_overlap_area:
                pairs.append((i, j, overlap))
    return pairs


def _raw_candidate_positions_2d(polygon, inset: float, min_wall_thickness: float, peg_radius: float):
    """All 2D grid points inside `polygon`'s eroded safe zone, each paired
    with its distance to the polygon boundary — the full pool a caller then
    filters for true 3D safety and/or spreads out via
    `_farthest_point_select`. Deliberately does NOT do any spread-selection
    itself: selecting "spread out" candidates from a pool that includes
    positions which later turn out to be 3D-unsafe (near a thin wall a few
    mm deeper than the cut plane) means the eventually-*accepted* subset is
    only spread out relative to points that got dropped, not relative to
    each other — the actual cause of a real observed bug where an interface
    with plenty of usable area still huddled every peg into one corner.
    Returns a list of (x, y, boundary_distance) tuples."""
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

    # Vectorized equivalent of "for x in xs: for y in ys: ..." -- indexing="ij"
    # + ravel() (C order) iterates y fastest within each x, matching the old
    # nested-loop order exactly, which matters for tie-breaking in
    # `_farthest_point_select`'s sort.
    xx, yy = np.meshgrid(xs, ys, indexing="ij")
    flat_x, flat_y = xx.ravel(), yy.ravel()
    inside = shapely.contains_xy(safe_zone, flat_x, flat_y)
    in_x, in_y = flat_x[inside], flat_y[inside]
    if len(in_x) == 0:
        return []
    pts = shapely.points(in_x, in_y)
    dists = shapely.distance(polygon.boundary, pts)
    return list(zip(in_x.tolist(), in_y.tolist(), dists.tolist()))


def _farthest_point_select(candidates: list[tuple[float, float, float]], limit: int, min_spacing: float = 0.0):
    """Greedy farthest-point selection from `candidates` (each
    `(x, y, boundary_distance)`): start from the most interior point, then
    repeatedly pick whichever remaining point maximizes its minimum distance
    to everything already chosen, up to `limit` picks. If `min_spacing` is
    given, a candidate is only accepted once its distance to every already-
    chosen point is at least `min_spacing` — stops early (returning fewer
    than `limit`) rather than ever placing two picks closer than that,
    which matters here specifically because two pegs is not just "close
    together" but their sockets *physically overlapping* once center
    distance drops below the socket diameter. Returns a list of (x, y)
    tuples."""
    if not candidates:
        return []
    candidates = sorted(candidates, key=lambda c: -c[2])
    chosen = [candidates[0]]
    remaining = candidates[1:]
    min_spacing_sq = min_spacing * min_spacing
    while remaining and len(chosen) < limit:
        best, best_dist = None, -1.0
        for c in remaining:
            d = min((c[0] - ch[0]) ** 2 + (c[1] - ch[1]) ** 2 for ch in chosen)
            if d > best_dist:
                best, best_dist = c, d
        if best_dist < min_spacing_sq:
            break  # nothing left is far enough from what's already chosen -- stop rather than crowd pegs together
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


def add_connectors_at_interface(
    piece_a: trimesh.Trimesh,
    piece_b: trimesh.Trimesh,
    cut: ResolvedCut,
    peg_diameter: float = 7.0,
    peg_length: float = 5.0,
    peg_clearance: float = 0.18,
    inset: float = 8.0,
    n_pegs: int = 4,
    min_wall_thickness: float = 1.2,
    alignment_key: bool = False,
    key_flat_depth: float = 1.5,
    dowel_shape: str = "round",
    restrict_polygon=None,
) -> tuple[trimesh.Trimesh, trimesh.Trimesh, list[trimesh.Trimesh]]:
    """Add sockets to `piece_a`/`piece_b` at the single interface `cut`
    between them (piece_a is the lower-index piece, whose solid extends
    toward the plane's negative-normal side). Returns
    (new_piece_a, new_piece_b, dowels_for_this_interface) — the inputs are
    returned unchanged (with `dowels_for_this_interface == []`) when no safe
    connector position exists at this interface.

    `restrict_polygon`, if given, is a shapely polygon (in the same local 2D
    frame `_interface_polygon` would produce for `piece_a`) that further
    restricts candidate positions — used when the two pieces only partially
    face each other across the plane, so pegs land only where both pieces
    actually have material."""
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

    origin, normal = cut.origin, cut.normal
    lateral = _lateral_basis(normal)

    polygon, to_3d = _interface_polygon(piece_a, origin, normal, inward_sign=-1.0)
    if polygon is None:
        return piece_a, piece_b, []  # no flat cut face here; skip connectors for this interface

    if restrict_polygon is not None:
        polygon = polygon.intersection(restrict_polygon)
        if polygon.is_empty:
            return piece_a, piece_b, []

    # Two-stage selection. Stage 1 is a cheap, 2D-only spread pass over the
    # full grid to cut it down to a bounded, still-diverse pool — running
    # the expensive 3D safety check (real ray-casting) against every single
    # grid point (up to ~48x48) doesn't scale. Stage 2 checks 3D safety for
    # that *whole* bounded pool (not stopping at the first `n_pegs`
    # successes) and only THEN spreads the survivors out: selecting "spread
    # out" points from a pool that still includes eventually-rejected ones
    # only spreads the accepted subset relative to points that got dropped,
    # not relative to each other, which is what let a real interface with
    # plenty of usable area huddle every peg into one corner whenever the
    # early, well-spread candidates happened to fail the 3D check (e.g. sit
    # over a thin wall a few mm deeper than the cut plane).
    raw_candidates_2d = _raw_candidate_positions_2d(polygon, inset, min_wall_thickness, peg_radius)
    xy_to_dist = {(x, y): dist for x, y, dist in raw_candidates_2d}
    pool_size = max(n_pegs * 10, 40)
    pool_2d = _farthest_point_select(raw_candidates_2d, pool_size)

    safe_candidates = []
    for x, y in pool_2d:
        c = (to_3d @ np.array([x, y, 0.0, 1.0]))[:3]
        c = _project_onto_plane(c, origin, normal)  # snap back onto the exact cut plane
        if _is_safe_3d(piece_a, piece_b, normal, lateral, c, socket_radius, peg_length, min_wall_thickness):
            safe_candidates.append((x, y, c))

    if not safe_candidates:
        return piece_a, piece_b, []  # cross-section too thin anywhere (in true 3D) for a safe connector

    # Now spread `n_pegs` of the SAFE ones out, enforcing a real minimum
    # center-to-center spacing so sockets can never physically overlap even
    # when the safe area is small/irregular — better to place fewer
    # connectors than to place two whose cavities intersect. Boundary
    # distance is carried through from stage 1 so the first pick is still
    # the most-interior *safe* point, not an arbitrary one.
    xy_to_center = {(x, y): c for x, y, c in safe_candidates}
    min_spacing = socket_diameter + 1.0  # a full socket-width gap between centers, not just non-overlapping
    selected_2d = _farthest_point_select(
        [(x, y, xy_to_dist[(x, y)]) for x, y, _c in safe_candidates], n_pegs, min_spacing=min_spacing
    )
    centers = [xy_to_center[(x, y)] for x, y in selected_2d]
    # `_farthest_point_select` always keeps its first (most-interior) pick,
    # so `centers` is guaranteed non-empty here given `safe_candidates` was.

    # A single round peg can't stop rotation about the cut normal; force
    # a flat on every peg at the interface when keying is needed (not
    # just one) so every dowel at a given joint is identical — simpler
    # to print and to tell apart than a mix of round and D-shaped pegs.
    # Non-round shapes are already keyed by their own geometry.
    use_key = dowel_shape == "round" and (alignment_key or len(centers) < 2)

    dowels: list[trimesh.Trimesh] = []
    socket_a_union = []
    socket_b_union = []
    for center in centers:
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

    new_piece_a = trimesh.boolean.difference([piece_a, *socket_a_union], engine="manifold")
    new_piece_b = trimesh.boolean.difference([piece_b, *socket_b_union], engine="manifold")

    return new_piece_a, new_piece_b, dowels


def dedupe_cuts(cuts: list[ResolvedCut], normal_tol: float = 1e-9, offset_tol: float = 1e-6) -> list[ResolvedCut]:
    """Merge `cuts` that describe the same plane (parallel normals, matching
    plane offset) into one representative each. Used when the same logical,
    user-specified cut plane was recorded once per sub-piece it was applied
    to (e.g. by `pipeline._cut_all_axes` or `autofit._split_recursive`), even
    though it's a single interface. Tolerance-based grouping (rather than
    exact float equality) is used since two sub-pieces' `ResolvedCut.origin`
    can differ slightly off-normal for a tilted plane even though they
    describe the same plane in space."""
    groups: list[ResolvedCut] = []
    for cut in cuts:
        offset = float(np.dot(cut.normal, cut.origin))
        merged = False
        for g in groups:
            g_offset = float(np.dot(g.normal, g.origin))
            if abs(float(np.dot(cut.normal, g.normal))) > 1 - normal_tol and abs(offset - g_offset) < offset_tol:
                merged = True
                break
        if not merged:
            groups.append(cut)
    return groups


def add_connectors_after_cuts(
    pieces: list[trimesh.Trimesh],
    applied_cuts: list[ResolvedCut],
    connector_kwargs: dict,
    progress: ProgressReporter | None = None,
) -> tuple[list[trimesh.Trimesh], list[trimesh.Trimesh]]:
    """Second pass, run once over the FINAL pieces after all cutting (every
    axis, every recursion level) has completed: for every distinct cut plane
    actually used, add sockets between every pair of final pieces that truly
    face each other across it (see `find_facing_pairs` — two pieces sharing a
    cut plane's axis position aren't necessarily adjacent, e.g. diagonal
    pieces in a multi-axis grid). Doing this after all cutting, rather than
    eagerly after each axis's/recursion level's cuts, means a later
    perpendicular cut can never slice through an already-carved socket."""
    pieces = list(pieces)
    dowels: list[trimesh.Trimesh] = []
    deduped = dedupe_cuts(applied_cuts)

    for cut in deduped:
        pairs = find_facing_pairs(pieces, cut)
        for i, j, overlap in pairs:
            piece_i, piece_j, new_dowels = add_connectors_at_interface(
                pieces[i], pieces[j], cut, restrict_polygon=overlap, **connector_kwargs
            )
            pieces[i] = piece_i
            pieces[j] = piece_j
            dowels.extend(new_dowels)
            if progress:
                progress.step(f"Connectors between piece {i + 1} and piece {j + 1}")

    return pieces, dowels


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
    n_interfaces = len(resolved_cuts)

    for i, cut in enumerate(resolved_cuts):
        piece_a, piece_b, new_dowels = add_connectors_at_interface(
            pieces[i], pieces[i + 1], cut,
            peg_diameter=peg_diameter, peg_length=peg_length, peg_clearance=peg_clearance,
            inset=inset, n_pegs=n_pegs, min_wall_thickness=min_wall_thickness,
            alignment_key=alignment_key, key_flat_depth=key_flat_depth, dowel_shape=dowel_shape,
        )
        pieces[i] = piece_a
        pieces[i + 1] = piece_b
        dowels.extend(new_dowels)

        if progress:
            suffix = "" if new_dowels else " (skipped)"
            progress.step(f"Connectors at interface {i + 1}/{n_interfaces}{suffix}")

    return pieces, dowels
