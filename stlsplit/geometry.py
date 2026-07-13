"""Shared geometry helpers: axis handling, cut-plane math, mesh loading."""
from __future__ import annotations

import math
import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass

import numpy as np
import trimesh

# Below this face count, the per-candidate connectivity check (_is_single_
# component) is already cheap enough (well under a second) that spinning up
# a process pool costs more than it saves. Above it — the regime where a
# single refine_cut_planes call can otherwise take tens of seconds to
# minutes, especially once the spacing-mode retry loop escalates piece
# count — parallelizing the candidate checks is worth the pool overhead.
_PARALLEL_FACE_THRESHOLD = 20_000

AXES = {"x": 0, "y": 1, "z": 2}

# Absolute (not fraction-of-total) volume floor, in mm^3, below which a split
# component is treated as boolean-op floating-point noise rather than a real
# floating region. A *fraction* of the total piece's volume doesn't work here:
# a real disconnected chunk (a hand, a fold of drapery) has essentially fixed
# absolute size regardless of how big the piece it detached from is, so on a
# large piece its fraction can look "insignificant" while still being a
# clearly visible, unattached blob in the slicer. Genuine floating-point
# noise components measured in practice are ~1e-6 mm^3 or smaller, many
# orders of magnitude below this floor.
FLOATING_REGION_MIN_VOLUME_MM3 = 1.0


def axis_index(axis: str) -> int:
    axis = axis.lower()
    if axis not in AXES:
        raise ValueError(f"Invalid axis '{axis}', must be one of x/y/z")
    return AXES[axis]


class CutPlacementError(RuntimeError):
    """Raised when a cut produced bad geometry (empty or disconnected
    piece). Carries which piece failed and, once the pipeline layer fills
    it in, which axis and cut position(s) are responsible — so callers can
    point the user at the specific cut instead of just a piece number and a
    generic explanation."""

    def __init__(self, message: str, piece_index: int):
        super().__init__(message)
        self.message = message
        self.piece_index = piece_index
        self.axis: str | None = None
        self.positions: list[float] = []

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True)
class Cut:
    """A single cut, defined relative to `axis`: `position` is the
    coordinate along `axis` (as before); `tilt_a`/`tilt_b` (degrees) tilt the
    plane's normal away from `axis`, rotating around the other two world
    axes in axis order (e.g. for axis='x', tilt_a rotates around world Y,
    tilt_b around world Z). tilt_a == tilt_b == 0 is a plain axis-aligned
    cut, so a bare float position is always equivalent to `Cut(position)`.
    """

    position: float
    tilt_a: float = 0.0
    tilt_b: float = 0.0

    @staticmethod
    def coerce(value: "Cut | float") -> "Cut":
        return value if isinstance(value, Cut) else Cut(position=float(value))


@dataclass(frozen=True)
class ResolvedCut:
    """A cut plane fully resolved to world-space geometry: a point the
    plane passes through and its (unit) normal, plus the original `position`
    (kept for ordering/display purposes)."""

    origin: np.ndarray
    normal: np.ndarray
    position: float


def resolve_cuts(mesh: trimesh.Trimesh, axis: str, cuts: list["Cut | float"]) -> list[ResolvedCut]:
    """Resolve `cuts` (positions, optionally tilted) against `mesh`'s
    current bounds into world-space (origin, normal) pairs, sorted by
    position. Resolve once against the pre-cut mesh and reuse the same
    result for both cutting and connector placement, so tilted planes line
    up between the two."""
    idx = axis_index(axis)
    other = [i for i in range(3) if i != idx]
    center = (mesh.bounds[0] + mesh.bounds[1]) / 2.0

    resolved = []
    for raw in sorted((Cut.coerce(c) for c in cuts), key=lambda c: c.position):
        normal = np.zeros(3)
        normal[idx] = 1.0
        if raw.tilt_a:
            axis_a = np.zeros(3)
            axis_a[other[0]] = 1.0
            normal = trimesh.transformations.rotation_matrix(math.radians(raw.tilt_a), axis_a)[:3, :3] @ normal
        if raw.tilt_b:
            axis_b = np.zeros(3)
            axis_b[other[1]] = 1.0
            normal = trimesh.transformations.rotation_matrix(math.radians(raw.tilt_b), axis_b)[:3, :3] @ normal
        normal = normal / np.linalg.norm(normal)

        origin = center.copy()
        origin[idx] = raw.position
        resolved.append(ResolvedCut(origin=origin, normal=normal, position=raw.position))
    return resolved


def repair_watertight(mesh: trimesh.Trimesh) -> trimesh.Trimesh:
    """Attempt in-place repair of a non-watertight mesh (fill holes, fix winding/normals)."""
    mesh.fill_holes()
    mesh.fix_normals()
    trimesh.repair.fix_winding(mesh)
    return mesh


def load_mesh(path: str, allow_non_watertight: bool = False) -> trimesh.Trimesh:
    mesh = trimesh.load_mesh(path)
    if isinstance(mesh, trimesh.Scene):
        mesh = trimesh.util.concatenate(
            [g for g in mesh.geometry.values()]
        )
    if not mesh.is_watertight:
        repair_watertight(mesh)
    if not mesh.is_watertight:
        if not allow_non_watertight:
            raise ValueError(
                f"Mesh '{path}' is not watertight; cannot safely perform boolean ops. "
                "Pass --allow-non-watertight to proceed anyway (boolean cuts/connectors may fail or produce bad geometry)."
            )
    return mesh


def scale_mesh(
    mesh: trimesh.Trimesh,
    scale: float | None = None,
    target_dim: float | None = None,
    axis: str | None = None,
) -> trimesh.Trimesh:
    if scale is None and target_dim is None:
        return mesh
    if scale is not None and target_dim is not None:
        raise ValueError("Specify only one of --scale or --target-dim")
    if target_dim is not None:
        if axis is None:
            raise ValueError("--target-dim requires --axis to know which dimension to match")
        idx = axis_index(axis)
        current = mesh.extents[idx]
        if current <= 0:
            raise ValueError("Mesh has zero extent on the target axis")
        scale = target_dim / current
    mesh = mesh.copy()
    mesh.apply_scale(scale)
    return mesh


def compute_cut_planes(
    mesh: trimesh.Trimesh,
    axis: str,
    spacing: float | None = None,
    pieces: int | None = None,
    smart: bool = True,
) -> list[float]:
    """Return sorted list of coordinate positions (along axis) where cuts occur.

    Cuts are interior planes only (not the mesh's own bounding extremes).
    Planes start out evenly spaced (symmetric piece sizes), then, unless
    `smart=False`, each is nudged within a bounded local window toward a
    cross-section of larger area — steering cuts away from thin necks and
    slivers, and away from spots where the piece on either side would come
    apart into disconnected floating regions.
    """
    if spacing is None and pieces is None:
        raise ValueError("Specify one of --spacing or --pieces")
    if spacing is not None and pieces is not None:
        raise ValueError("Specify only one of --spacing or --pieces")

    idx = axis_index(axis)
    lo, hi = mesh.bounds[0][idx], mesh.bounds[1][idx]
    span = hi - lo

    # A live process pool, reused across every refine_cut_planes/
    # _planes_are_safe call this function makes (all the spacing-mode
    # retry's escalating attempts included) — spawning it once here and
    # sharing it avoids paying process-startup cost per candidate batch.
    # Only worth it once each connectivity check is itself expensive; below
    # the threshold, everything just runs serially as before (executor=None
    # is threaded through and behaves identically to before this existed).
    use_parallel = len(mesh.faces) >= _PARALLEL_FACE_THRESHOLD
    executor = ProcessPoolExecutor(max_workers=os.cpu_count() or 1) if use_parallel else None
    try:
        return _compute_cut_planes_inner(mesh, idx, lo, hi, span, spacing, pieces, smart, executor)
    finally:
        if executor is not None:
            executor.shutdown(wait=True)


def _compute_cut_planes_inner(
    mesh: trimesh.Trimesh,
    idx: int,
    lo: float,
    hi: float,
    span: float,
    spacing: float | None,
    pieces: int | None,
    smart: bool,
    executor: "ProcessPoolExecutor | None",
) -> list[float]:
    if pieces is not None:
        if pieces < 1:
            raise ValueError("--pieces must be >= 1")
        n_cuts = pieces - 1
        if n_cuts == 0:
            return []
        step = span / pieces
        planes = [lo + step * i for i in range(1, pieces)]
        # An explicit --pieces count is a direct user request, not something
        # we should second-guess by adding more pieces on our own; if no
        # safe cut exists at this exact count, refine_cut_planes does its
        # best and cutting.py will raise a clear, actionable error.
        if smart and planes:
            planes = refine_cut_planes(mesh, idx, planes, max_piece_size=None, executor=executor)
        return planes

    if spacing <= 0:
        raise ValueError("--spacing must be > 0")

    # Spacing mode (driven by bed-size auto-fit, not a direct piece-count
    # request): the piece count here is just whatever an even split needs to
    # respect `spacing`. If the mandatory window for that count turns out to
    # be entirely unsafe (e.g. a span so close to 2x spacing that a 2-piece
    # split forces the cut into a narrow band that happens to sit on a
    # pinch/neck in the model), adding one more cut gives the search far more
    # room to route around it — smaller pieces are always acceptable, a
    # floating region never is. Retry with more pieces until a genuinely
    # safe set of planes is found, or a bounded number of extra attempts is
    # exhausted.
    #
    # That retry cap must stay small and constant, not scale with the mesh
    # (a prior version capped at span // 5mm, which for e.g. an 800mm target
    # meant up to ~160 retries — each one re-running refine_cut_planes's own
    # multi-stage per-cut search over an ever-growing plane list, which
    # compounds to a many-minutes hang on any mesh where no piece count ever
    # satisfies "every segment safe", such as an asymmetric figure with
    # limbs). An organic mesh may simply have no perfectly safe configuration
    # at all on some axis; a handful of extra attempts is enough to route
    # around an isolated pinch without turning an unlucky case into an
    # effectively unbounded search. If every attempt still comes back
    # unsafe, return the best (largest-area) effort found — cutting.py's
    # real post-cut check still catches a genuine floating region and
    # raises a clear, actionable error, same as before this retry existed.
    n_pieces = max(1, int(span // spacing) + 1)
    max_extra_attempts = 5
    first_attempt = None
    for _ in range(max_extra_attempts + 1):
        step = span / n_pieces
        planes = [lo + step * i for i in range(1, n_pieces)]
        if not smart or not planes:
            return planes
        refined = refine_cut_planes(mesh, idx, planes, max_piece_size=spacing, executor=executor)
        if first_attempt is None:
            first_attempt = refined
        if _planes_are_safe(mesh, idx, refined, lo, hi, executor=executor):
            return refined
        n_pieces += 1
    # Never found a fully safe configuration at any piece count tried — more
    # pieces didn't fix the underlying pinch, so fall back to the *first*
    # (smallest, closest-to-requested-spacing) attempt rather than the most
    # fragmented one. Previously this fell back to the last (largest) attempt
    # on the theory that "more pieces is generally safer", but for a mesh
    # with a genuine unavoidable pinch that's false — every attempt stays
    # unsafe regardless of count, so escalating just needlessly ballooned a
    # ~1-2 cut request into 6+ cuts with no actual safety benefit. Real
    # protection still comes from cutting.py's post-cut floating-region
    # check, same as before this retry existed.
    return first_attempt


def _section_area(mesh: trimesh.Trimesh, axis_idx: int, coord: float) -> float:
    """Cross-sectional area of `mesh` at the plane axis_idx=coord (0 if the
    plane misses the mesh entirely)."""
    normal = [0.0, 0.0, 0.0]
    normal[axis_idx] = 1.0
    origin = [0.0, 0.0, 0.0]
    origin[axis_idx] = coord
    section = mesh.section(plane_origin=origin, plane_normal=normal)
    if section is None:
        return 0.0
    try:
        planar, _ = section.to_planar()
    except Exception:
        return 0.0
    return float(planar.area)


def _neighborhood_area(mesh: trimesh.Trimesh, axis_idx: int, coord: float, probe: float) -> float:
    """Worst-case (minimum) cross-sectional area sampled at coord and just to
    either side of it. A single-point area can look fine while sitting right
    next to a near-zero pinch (a sliver); sampling a small neighborhood
    catches that."""
    return min(
        _section_area(mesh, axis_idx, coord - probe),
        _section_area(mesh, axis_idx, coord),
        _section_area(mesh, axis_idx, coord + probe),
    )


def _is_single_component(mesh: trimesh.Trimesh, axis_idx: int, lo: float | None, hi: float | None) -> bool:
    """Cheap trial check (mesh slicing, not a full boolean) for whether the
    slab between lo and hi along axis_idx would come apart into multiple
    meaningfully-sized disconnected chunks."""
    try:
        sub = mesh
        normal = [0.0, 0.0, 0.0]
        normal[axis_idx] = 1.0
        if lo is not None:
            origin = [0.0, 0.0, 0.0]
            origin[axis_idx] = lo
            sub = sub.slice_plane(origin, normal, cap=True)
            if sub is None or sub.is_empty:
                return False
        if hi is not None:
            origin = [0.0, 0.0, 0.0]
            origin[axis_idx] = hi
            neg_normal = [-n for n in normal]
            sub = sub.slice_plane(origin, neg_normal, cap=True)
            if sub is None or sub.is_empty:
                return False
        components = sub.split(only_watertight=False)
        if len(components) <= 1:
            return True
        significant = [c for c in components if abs(c.volume) >= FLOATING_REGION_MIN_VOLUME_MM3]
        return len(significant) <= 1
    except Exception:
        return True  # don't block refinement on slicing edge cases


def _is_single_component_worker(vertices, faces, axis_idx: int, lo: float | None, hi: float | None) -> bool:
    """Worker body for `_batch_is_safe` below, run in a separate process.
    Takes plain vertex/face arrays rather than a `Trimesh` for the same
    reason as `pipeline._process_one_piece`: sidesteps whatever unpicklable
    cached state (a ray-query engine, etc.) a `Trimesh` may carry rather
    than depending on what happens to be picklable today.

    An earlier version tried to avoid re-sending the mesh on every task by
    loading it once per worker via a ProcessPoolExecutor `initializer`
    instead of as a per-call argument. Measured slower in practice (~144s
    vs ~121s on a 371k-face real-world mesh) for this workload's actual
    task counts — the eager, blocking per-worker load plus the extra
    synchronization it introduces cost more than the repeated pickling it
    was meant to avoid. Reverted; keep this simpler version unless a
    measurement says otherwise."""
    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
    return _is_single_component(mesh, axis_idx, lo, hi)


def _batch_is_safe(
    executor: "ProcessPoolExecutor | None",
    mesh: trimesh.Trimesh,
    axis_idx: int,
    candidates: list[float],
    left_bound: float,
    right_bound: float,
) -> dict[float, bool]:
    """Check whether each `c` in `candidates` is a safe cut position (both
    [left_bound, c] and [c, right_bound] are single-component), for the
    *same* fixed (left_bound, right_bound) pair. Each candidate's check only
    reads `mesh` — never mutates it — so they're trivially independent, and
    on a heavy mesh (where each check is a real boolean/slice operation
    costing well over a tenth of a second) checking a whole batch of
    candidates concurrently instead of one at a time is the difference
    between a search that finishes in seconds and one that takes minutes.
    Falls back to plain sequential checks when no executor was handed in
    (small/fast meshes — see `_PARALLEL_FACE_THRESHOLD`)."""
    if executor is None:
        return {
            c: _is_single_component(mesh, axis_idx, left_bound, c) and _is_single_component(mesh, axis_idx, c, right_bound)
            for c in candidates
        }

    vertices, faces = mesh.vertices, mesh.faces
    futures = {}
    for c in candidates:
        futures[(c, "left")] = executor.submit(_is_single_component_worker, vertices, faces, axis_idx, left_bound, c)
        futures[(c, "right")] = executor.submit(_is_single_component_worker, vertices, faces, axis_idx, c, right_bound)
    results = {key: future.result() for key, future in futures.items()}
    return {c: results[(c, "left")] and results[(c, "right")] for c in candidates}


def _planes_are_safe(
    mesh: trimesh.Trimesh,
    axis_idx: int,
    planes: list[float],
    lo: float,
    hi: float,
    executor: "ProcessPoolExecutor | None" = None,
) -> bool:
    """Whether every slab between consecutive `planes` (and the mesh's own
    bounds) is connectivity-safe, i.e. `refine_cut_planes` found a genuinely
    safe spot for every cut rather than falling back to its best-effort
    (possibly unsafe) candidate. Checks every slab boundary concurrently via
    `executor` when one was given, rather than one at a time."""
    bounds = [lo, *sorted(planes), hi]
    if executor is None:
        return all(_is_single_component(mesh, axis_idx, bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1))
    vertices, faces = mesh.vertices, mesh.faces
    futures = [
        executor.submit(_is_single_component_worker, vertices, faces, axis_idx, bounds[i], bounds[i + 1])
        for i in range(len(bounds) - 1)
    ]
    return all(f.result() for f in futures)


def refine_cut_planes(
    mesh: trimesh.Trimesh,
    axis_idx: int,
    planes: list[float],
    search_frac: float = 0.4,
    min_gap_frac: float = 0.12,
    samples: int = 11,
    improvement_ratio: float = 1.1,
    max_piece_size: float | None = None,
    executor: "ProcessPoolExecutor | None" = None,
) -> list[float]:
    """Nudge each plane within a bounded local window to sit at a larger,
    connectivity-safe cross-section, avoiding thin necks/slivers and the
    disconnected "floating region" pieces that result from cutting exactly
    at a pinch point. Movement is bounded and only taken when it's a clear
    improvement, so evenly-spaced (symmetric) cuts are preserved unless
    there's a good reason to move them; `max_piece_size` (e.g. from
    --spacing) additionally caps how far a plane may drift so no piece
    grows past that limit — but if no connectivity-safe spot exists within
    the initial (small) search window, the window is progressively widened,
    up to that same max_piece_size cap, before giving up: a piece coming out
    smaller than the requested spacing/bed size is always acceptable, a
    disconnected floating region never is.

    `executor`, if given, is a live `ProcessPoolExecutor` used to check a
    whole batch of candidates' connectivity-safety concurrently instead of
    one at a time (see `_batch_is_safe`) — pass one in for heavy meshes
    where each check is itself expensive; `compute_cut_planes` decides when
    that's worth it and owns the executor's lifetime.
    """
    lo, hi = mesh.bounds[0][axis_idx], mesh.bounds[1][axis_idx]
    bounds = [lo, *sorted(planes), hi]
    refined: list[float] = []
    for i, p in enumerate(sorted(planes)):
        left_bound, right_bound = bounds[i], bounds[i + 2]
        gap = min(p - left_bound, right_bound - p)
        min_gap = gap * min_gap_frac
        probe = max(gap * 0.02, 1e-3)

        # The true allowed range for c, respecting min_gap on both sides and
        # (independently, since the two directions aren't symmetric around
        # p) the max_piece_size cap on whichever neighboring piece would
        # grow in that direction. Moving c *toward* left_bound only shrinks
        # the left piece (always fine) while growing the right piece (must
        # stay <= max_piece_size); moving toward right_bound is the mirror
        # case. A plane sitting close to one bound (as spacing-based
        # placement often does) can therefore still search much further
        # toward the other bound than a naive symmetric window would allow.
        full_min_c = left_bound + min_gap
        full_max_c = right_bound - min_gap
        if max_piece_size is not None:
            full_min_c = max(full_min_c, right_bound - max_piece_size)
            full_max_c = min(full_max_c, left_bound + max_piece_size)
        if full_max_c < full_min_c:
            full_min_c = full_max_c = p

        chosen = p
        # Widen the search in stages: try a small band around p first so
        # symmetric spacing is preserved when nothing's wrong, then expand
        # toward the full allowed range only if that wasn't enough to find
        # a safe spot.
        for frac in (search_frac, search_frac * 3, 1.0):
            half_width = gap * frac
            lo_c = max(full_min_c, p - half_width)
            hi_c = min(full_max_c, p + half_width)
            if hi_c <= lo_c:
                if frac >= 1.0:
                    lo_c, hi_c = full_min_c, full_max_c
                else:
                    continue

            candidates = list(np.linspace(lo_c, hi_c, samples))
            if p not in candidates:
                candidates.append(p)

            scored = sorted(
                ((c, _neighborhood_area(mesh, axis_idx, c, probe)) for c in candidates),
                key=lambda t: t[1],
                reverse=True,
            )
            max_area = scored[0][1] if scored else 0.0

            # Each check is expensive (two real mesh `split()` calls, each
            # O(faces) — on a heavy mesh this dominates runtime completely).
            # Serially checking candidates one at a time with early-exit
            # works well when the first few are usually safe, but on a mesh
            # where many candidates in a row are genuinely unsafe (e.g. an
            # appendage that stays disconnected across a wide band), that
            # degrades to checking almost the whole batch anyway — just one
            # at a time. So the *whole* batch (near-best-by-proximity, then
            # the top-K fallback) is checked via `_batch_is_safe`, which runs
            # them concurrently when `executor` was handed a heavy enough
            # mesh to be worth it; either way, the result (closest-safe-
            # candidate, or top-K-by-area fallback) is identical to the old
            # purely-sequential early-exit version, just not serialized.
            near_best = [c for c, a in scored if max_area <= 0 or a >= max_area / improvement_ratio]
            near_best_by_proximity = sorted(near_best, key=lambda c: abs(c - p))
            safety = _batch_is_safe(executor, mesh, axis_idx, near_best_by_proximity, left_bound, right_bound)
            found = False
            for c in near_best_by_proximity:
                if safety[c]:
                    chosen = c
                    found = True
                    break
            if found:
                break

            # Nothing near `p` worked; fall back to the highest-area
            # candidates overall, but only check a bounded number of them —
            # a much-smaller-area candidate is rarely going to be the right
            # answer anyway, and checking the full sample set here is what
            # made this pathologically slow on large/complex meshes.
            _TOP_K_FALLBACK = 6
            fallback_candidates = [c for c, _a in scored[:_TOP_K_FALLBACK]]
            fallback_safety = _batch_is_safe(executor, mesh, axis_idx, fallback_candidates, left_bound, right_bound)
            for c in fallback_candidates:
                if fallback_safety[c]:
                    chosen = c
                    found = True
                    break
            if found:
                break

            # Nothing safe in this window; remember the largest-area
            # candidate as a fallback and try a wider window next.
            if scored:
                chosen = scored[0][0]

        # If every widened window was exhausted with nothing connectivity-safe,
        # `chosen` is still the best-area candidate found; cutting.py's
        # post-cut check will catch a genuine floating-region result and
        # raise a clear, actionable error rather than silently producing one.
        refined.append(chosen)
    return refined
