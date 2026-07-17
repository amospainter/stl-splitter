"""Shared geometry helpers: axis handling, cut-plane math, mesh loading."""
from __future__ import annotations

import math
import os
import threading
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass

import numpy as np
import trimesh
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components

from .progress import JobCancelled


def _check_cancelled(cancel_event: "threading.Event | None") -> None:
    """Raise `JobCancelled` if `cancel_event` is set. Checked periodically
    inside `compute_cut_planes`'s search loops (the pinch-avoidance window
    search and the spacing-mode retry loop), which can each run for tens of
    seconds on a large/complex mesh — this is what lets the web UI's
    "Stop" control on a live plane-preview actually abort the computation,
    not just discard the eventual response."""
    if cancel_event is not None and cancel_event.is_set():
        raise JobCancelled("Plane computation cancelled")

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
    executor: "ProcessPoolExecutor | None" = None,
    cancel_event: "threading.Event | None" = None,
) -> list[float]:
    """Return sorted list of coordinate positions (along axis) where cuts occur.

    Cuts are interior planes only (not the mesh's own bounding extremes).
    Planes start out evenly spaced (symmetric piece sizes), then, unless
    `smart=False`, each is nudged within a bounded local window toward a
    cross-section of larger area — steering cuts away from thin necks and
    slivers, and away from spots where the piece on either side would come
    apart into disconnected floating regions.

    `executor`, if given, is used directly for any parallel connectivity
    checks and is NOT shut down here — the caller owns its lifetime (e.g.
    `autofit.auto_fit_split`, which reuses one pool across its whole
    recursion instead of paying pool-startup cost at every level). When
    `executor` is None (the default), this function decides for itself
    whether the mesh is heavy enough to warrant a pool, creates one, and
    shuts it down before returning — unchanged from before this parameter
    existed.

    `cancel_event`, if given, is checked periodically during the search below
    (see `_check_cancelled`) and raises `progress.JobCancelled` as soon as
    it's set, rather than only after the whole (potentially tens-of-seconds)
    computation finishes — used by the web UI's live plane-preview "Stop"
    control (`stlsplit/web.py`'s `/plane_preview` endpoint), which has no
    other way to interrupt this synchronous, CPU-bound search once started.
    """
    if spacing is None and pieces is None:
        raise ValueError("Specify one of --spacing or --pieces")
    if spacing is not None and pieces is not None:
        raise ValueError("Specify only one of --spacing or --pieces")

    idx = axis_index(axis)
    lo, hi = mesh.bounds[0][idx], mesh.bounds[1][idx]
    span = hi - lo

    # Scoped to this single compute_cut_planes call (one mesh, one axis):
    # memoizes connectivity/section-area results across every refine_cut_planes
    # and _planes_are_safe call made below, including every escalating attempt
    # of the spacing-mode retry loop, which otherwise re-checks many identical
    # or overlapping (lo, hi) slabs and coordinates.
    connectivity_cache: dict[tuple, bool] = {}
    area_cache: dict[tuple, float] = {}
    max_workers = os.cpu_count() or 1
    # Phase 6 (OPTIMIZATION_PLAN.md): a fast graph-based connectivity
    # pre-filter, built once per (mesh, axis) and reused by every
    # connectivity check below. Only ever used to trust a cheap "disconnected"
    # answer outright (never a "connected" one — see
    # `_is_single_component_prefiltered` for why), so it can only make checks
    # faster, never change which planes are chosen.
    graph_index = build_graph_connectivity_index(mesh, idx)

    if executor is not None:
        # Caller-owned pool: use it as-is, don't shut it down.
        return _compute_cut_planes_inner(
            mesh, idx, lo, hi, span, spacing, pieces, smart, executor, max_workers,
            connectivity_cache, area_cache, graph_index, cancel_event,
        )

    # A live process pool, reused across every refine_cut_planes/
    # _planes_are_safe call this function makes (all the spacing-mode
    # retry's escalating attempts included) — spawning it once here and
    # sharing it avoids paying process-startup cost per candidate batch.
    # Only worth it once each connectivity check is itself expensive; below
    # the threshold, everything just runs serially as before (executor=None
    # is threaded through and behaves identically to before this existed).
    use_parallel = len(mesh.faces) >= _PARALLEL_FACE_THRESHOLD
    owned_executor = ProcessPoolExecutor(max_workers=max_workers) if use_parallel else None
    try:
        return _compute_cut_planes_inner(
            mesh, idx, lo, hi, span, spacing, pieces, smart, owned_executor, max_workers,
            connectivity_cache, area_cache, graph_index, cancel_event,
        )
    finally:
        if owned_executor is not None:
            owned_executor.shutdown(wait=True)


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
    max_workers: int,
    connectivity_cache: dict[tuple, bool],
    area_cache: dict[tuple, float],
    graph_index: "GraphConnectivityIndex | None" = None,
    cancel_event: "threading.Event | None" = None,
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
            planes = refine_cut_planes(
                mesh, idx, planes, max_piece_size=None, executor=executor, max_workers=max_workers,
                connectivity_cache=connectivity_cache, area_cache=area_cache, graph_index=graph_index,
                cancel_event=cancel_event,
            )
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
        _check_cancelled(cancel_event)
        step = span / n_pieces
        planes = [lo + step * i for i in range(1, n_pieces)]
        if not smart or not planes:
            return planes
        refined = refine_cut_planes(
            mesh, idx, planes, max_piece_size=spacing, executor=executor, max_workers=max_workers,
            connectivity_cache=connectivity_cache, area_cache=area_cache, graph_index=graph_index,
            cancel_event=cancel_event,
        )
        if first_attempt is None:
            first_attempt = refined
        if _planes_are_safe(
            mesh, idx, refined, lo, hi, executor=executor, cache=connectivity_cache, graph_index=graph_index
        ):
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


def _round_bound(x: float | None) -> float | None:
    return None if x is None else round(x, 6)


def _section_area(
    mesh: trimesh.Trimesh, axis_idx: int, coord: float, cache: dict[tuple, float] | None = None
) -> float:
    """Cross-sectional area of `mesh` at the plane axis_idx=coord (0 if the
    plane misses the mesh entirely). `cache`, if given, memoizes by
    (axis_idx, rounded coord) — scoped to one compute_cut_planes call, where
    the same coordinate is often re-scored across the retry loop and the
    staged window-widening in refine_cut_planes."""
    key = (axis_idx, round(coord, 6))
    if cache is not None and key in cache:
        return cache[key]
    normal = [0.0, 0.0, 0.0]
    normal[axis_idx] = 1.0
    origin = [0.0, 0.0, 0.0]
    origin[axis_idx] = coord
    section = mesh.section(plane_origin=origin, plane_normal=normal)
    if section is None:
        area = 0.0
    else:
        try:
            planar, _ = section.to_planar()
            area = float(planar.area)
        except Exception:
            area = 0.0
    if cache is not None:
        cache[key] = area
    return area


def _neighborhood_area(
    mesh: trimesh.Trimesh,
    axis_idx: int,
    coord: float,
    probe: float,
    cache: dict[tuple, float] | None = None,
) -> float:
    """Worst-case (minimum) cross-sectional area sampled at coord and just to
    either side of it. A single-point area can look fine while sitting right
    next to a near-zero pinch (a sliver); sampling a small neighborhood
    catches that."""
    return min(
        _section_area(mesh, axis_idx, coord - probe, cache),
        _section_area(mesh, axis_idx, coord, cache),
        _section_area(mesh, axis_idx, coord + probe, cache),
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


# Phase 6 (OPTIMIZATION_PLAN.md): an experimental, much faster approximation
# of `_is_single_component` below, using precomputed face-adjacency
# connectivity instead of slicing+capping+splitting. NOT used by
# `refine_cut_planes`/`_planes_are_safe` yet — see the comparison harness in
# `tests/verify_graph_connectivity.py`, which must show zero disagreement
# against `_is_single_component` on real models before this can be trusted as
# a drop-in replacement rather than a pre-filter.


@dataclass(frozen=True)
class GraphConnectivityIndex:
    """Precomputed structures for `is_single_component_graph`, built once per
    (mesh, axis) pair a single planning call works with — avoids recomputing
    face adjacency/areas/per-face axis ranges on every candidate check."""

    face_lo: np.ndarray
    face_hi: np.ndarray
    adjacency: np.ndarray
    face_areas: np.ndarray


def build_graph_connectivity_index(mesh: trimesh.Trimesh, axis_idx: int) -> GraphConnectivityIndex:
    coords = mesh.vertices[mesh.faces][:, :, axis_idx]
    return GraphConnectivityIndex(
        face_lo=coords.min(axis=1),
        face_hi=coords.max(axis=1),
        adjacency=mesh.face_adjacency,
        face_areas=mesh.area_faces,
    )


# Area-based proxy for "significant" component, standing in for
# `_is_single_component`'s volume-based `FLOATING_REGION_MIN_VOLUME_MM3`
# threshold — the graph check only has access to face area, not capped
# solid volume, so this is a coarse proxy, not an exact equivalence. Whether
# it tracks the volume-based threshold closely enough is exactly what the
# comparison harness measures.
GRAPH_FLOATING_REGION_MIN_AREA_MM2 = 1.0


def is_single_component_graph(index: GraphConnectivityIndex, lo: float | None, hi: float | None) -> bool:
    """Fast approximation of `_is_single_component`: whether the faces whose
    axis-range overlaps `[lo, hi]` form a single connected component (via
    face adjacency), ignoring any face-adjacency component too small to be a
    real floating region. No slicing, no capping, no `split()` — just a
    masked connected-components pass over a graph built once per planning
    call."""
    lo_bound = -np.inf if lo is None else lo
    hi_bound = np.inf if hi is None else hi
    mask = (index.face_hi >= lo_bound) & (index.face_lo <= hi_bound)
    if not mask.any():
        return False  # slab misses the mesh entirely

    kept = np.nonzero(mask)[0]
    remap = np.full(len(mask), -1, dtype=np.int64)
    remap[kept] = np.arange(len(kept))

    a, b = index.adjacency[:, 0], index.adjacency[:, 1]
    edge_mask = mask[a] & mask[b]
    if not edge_mask.any():
        n_components = len(kept)
        labels = np.arange(len(kept))
    else:
        a2, b2 = remap[a[edge_mask]], remap[b[edge_mask]]
        graph = coo_matrix((np.ones(len(a2)), (a2, b2)), shape=(len(kept), len(kept)))
        n_components, labels = connected_components(graph, directed=False)

    if n_components <= 1:
        return True
    comp_area = np.bincount(labels, weights=index.face_areas[kept], minlength=n_components)
    significant = int(np.count_nonzero(comp_area >= GRAPH_FLOATING_REGION_MIN_AREA_MM2))
    return significant <= 1


def _is_single_component_prefiltered(
    mesh: trimesh.Trimesh,
    axis_idx: int,
    lo: float | None,
    hi: float | None,
    graph_index: "GraphConnectivityIndex | None",
) -> bool:
    """`_is_single_component`, optionally pre-filtered by the fast graph-based
    approximation (Phase 6, OPTIMIZATION_PLAN.md). The comparison harness
    (`tests/verify_graph_connectivity.py`) found the graph check's
    disagreements are overwhelmingly one-directional: it can incorrectly
    report "connected" for a slab that's actually empty/disconnected (a face
    exactly tangent to the slab boundary gets counted as material present),
    but essentially never incorrectly reports "disconnected" for a slab
    that's actually safe. So only the "disconnected" answer is trusted
    outright — it's cheap and safe to skip the real check on. A "connected"
    answer is never trusted alone; it always falls through to the real,
    authoritative `_is_single_component` check. This is the inverse of the
    pre-filter direction the plan originally proposed ("trust connected,
    verify disconnected") — that direction turned out to be the unsafe one
    for this mesh/error profile, so it was flipped based on the measured
    data rather than followed as originally written."""
    if graph_index is not None and not is_single_component_graph(graph_index, lo, hi):
        return False
    return _is_single_component(mesh, axis_idx, lo, hi)


def _is_single_component_worker(
    vertices,
    faces,
    axis_idx: int,
    lo: float | None,
    hi: float | None,
    graph_index: "GraphConnectivityIndex | None" = None,
) -> bool:
    """Worker body for `_planes_are_safe` below, run in a separate process.
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
    return _is_single_component_prefiltered(mesh, axis_idx, lo, hi, graph_index)


def _connectivity_cache_lookup(
    cache: dict[tuple, bool] | None, axis_idx: int, lo: float | None, hi: float | None
) -> bool | None:
    if cache is None:
        return None
    return cache.get((axis_idx, _round_bound(lo), _round_bound(hi)))


def _connectivity_cache_store(
    cache: dict[tuple, bool] | None, axis_idx: int, lo: float | None, hi: float | None, value: bool
) -> None:
    if cache is not None:
        cache[(axis_idx, _round_bound(lo), _round_bound(hi))] = value


def _is_single_component_cached(
    mesh: trimesh.Trimesh,
    axis_idx: int,
    lo: float | None,
    hi: float | None,
    cache: dict[tuple, bool] | None = None,
    graph_index: "GraphConnectivityIndex | None" = None,
) -> bool:
    cached = _connectivity_cache_lookup(cache, axis_idx, lo, hi)
    if cached is not None:
        return cached
    result = _is_single_component_prefiltered(mesh, axis_idx, lo, hi, graph_index)
    _connectivity_cache_store(cache, axis_idx, lo, hi, result)
    return result


def _candidates_chunk_worker(
    vertices,
    faces,
    axis_idx: int,
    left_bound: float,
    right_bound: float,
    chunk: list[float],
    graph_index: "GraphConnectivityIndex | None" = None,
) -> list[tuple[bool, bool | None]]:
    """Worker body for `_batch_is_safe`'s parallel path, run in a separate
    process. Checks a whole `chunk` of candidates in one call (so the mesh
    is pickled to the worker once per chunk, not once per candidate — the
    dominant cost on a heavy mesh, since vertices/faces can be tens of MB).
    Returns one (left_safe, right_safe) pair per candidate in `chunk`, in
    order; `right_safe` is None when `left_safe` was False, since the right
    side is never checked in that case (short-circuits the same way the old
    purely-sequential version did, just per-candidate instead of globally)."""
    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
    results = []
    for c in chunk:
        left_safe = _is_single_component_prefiltered(mesh, axis_idx, left_bound, c, graph_index)
        right_safe = _is_single_component_prefiltered(mesh, axis_idx, c, right_bound, graph_index) if left_safe else None
        results.append((left_safe, right_safe))
    return results


def _chunk_candidates(candidates: list[float], n_chunks: int) -> list[list[float]]:
    """Split `candidates` into up to `n_chunks` contiguous chunks (fewer if
    there aren't enough candidates), so a batch check submits one task per
    worker instead of one per candidate."""
    n_chunks = max(1, min(n_chunks, len(candidates)))
    chunk_size = -(-len(candidates) // n_chunks)  # ceil division
    return [candidates[i : i + chunk_size] for i in range(0, len(candidates), chunk_size)]


def _batch_is_safe(
    executor: "ProcessPoolExecutor | None",
    mesh: trimesh.Trimesh,
    axis_idx: int,
    candidates: list[float],
    left_bound: float,
    right_bound: float,
    max_workers: int = 1,
    cache: dict[tuple, bool] | None = None,
    graph_index: "GraphConnectivityIndex | None" = None,
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
    (small/fast meshes — see `_PARALLEL_FACE_THRESHOLD`).

    `cache`, if given, memoizes individual (axis_idx, lo, hi) connectivity
    results across the whole compute_cut_planes call this batch is part of
    (see `_is_single_component_cached`) — a cache hit skips the check for
    that side entirely, sequential or parallel."""
    result: dict[float, bool] = {}
    pending: list[float] = []
    for c in candidates:
        left_cached = _connectivity_cache_lookup(cache, axis_idx, left_bound, c)
        if left_cached is False:
            result[c] = False
            continue
        if left_cached is True:
            right_cached = _connectivity_cache_lookup(cache, axis_idx, c, right_bound)
            if right_cached is not None:
                result[c] = right_cached
                continue
        pending.append(c)

    if not pending:
        return result

    if executor is None:
        for c in pending:
            safe = _is_single_component_cached(
                mesh, axis_idx, left_bound, c, cache, graph_index
            ) and _is_single_component_cached(mesh, axis_idx, c, right_bound, cache, graph_index)
            result[c] = safe
        return result

    vertices, faces = mesh.vertices, mesh.faces
    chunks = _chunk_candidates(pending, max_workers)
    futures = [
        executor.submit(
            _candidates_chunk_worker, vertices, faces, axis_idx, left_bound, right_bound, chunk, graph_index
        )
        for chunk in chunks
    ]
    for chunk, future in zip(chunks, futures):
        for c, (left_safe, right_safe) in zip(chunk, future.result()):
            _connectivity_cache_store(cache, axis_idx, left_bound, c, left_safe)
            if right_safe is not None:
                _connectivity_cache_store(cache, axis_idx, c, right_bound, right_safe)
                result[c] = left_safe and right_safe
            else:
                result[c] = False
    return result


def _planes_are_safe(
    mesh: trimesh.Trimesh,
    axis_idx: int,
    planes: list[float],
    lo: float,
    hi: float,
    executor: "ProcessPoolExecutor | None" = None,
    cache: dict[tuple, bool] | None = None,
    graph_index: "GraphConnectivityIndex | None" = None,
) -> bool:
    """Whether every slab between consecutive `planes` (and the mesh's own
    bounds) is connectivity-safe, i.e. `refine_cut_planes` found a genuinely
    safe spot for every cut rather than falling back to its best-effort
    (possibly unsafe) candidate. Checks every slab boundary concurrently via
    `executor` when one was given, rather than one at a time. `cache`, if
    given, skips slab boundaries already answered elsewhere in the same
    compute_cut_planes call (e.g. by `refine_cut_planes` itself)."""
    bounds = [lo, *sorted(planes), hi]
    pairs = [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)]

    result: dict[tuple, bool] = {}
    pending = []
    for pair in pairs:
        cached = _connectivity_cache_lookup(cache, axis_idx, pair[0], pair[1])
        if cached is not None:
            result[pair] = cached
        else:
            pending.append(pair)

    if pending:
        if executor is None:
            for pair in pending:
                result[pair] = _is_single_component_cached(mesh, axis_idx, pair[0], pair[1], cache, graph_index)
        else:
            vertices, faces = mesh.vertices, mesh.faces
            futures = [
                executor.submit(_is_single_component_worker, vertices, faces, axis_idx, a, b, graph_index)
                for a, b in pending
            ]
            for pair, future in zip(pending, futures):
                value = future.result()
                _connectivity_cache_store(cache, axis_idx, pair[0], pair[1], value)
                result[pair] = value

    return all(result[pair] for pair in pairs)


def refine_cut_planes(
    mesh: trimesh.Trimesh,
    axis_idx: int,
    planes: list[float],
    search_frac: float = 0.4,
    min_gap_frac: float = 0.12,
    samples: int = 11,
    max_piece_size: float | None = None,
    executor: "ProcessPoolExecutor | None" = None,
    max_workers: int = 1,
    connectivity_cache: dict[tuple, bool] | None = None,
    area_cache: dict[tuple, float] | None = None,
    graph_index: "GraphConnectivityIndex | None" = None,
    cancel_event: "threading.Event | None" = None,
) -> list[float]:
    """Nudge each plane, within a bounded local window, to the CLOSEST
    position that yields connectivity-safe pieces — avoiding the disconnected
    "floating region" pieces that result from cutting where the model pinches
    to nothing (a thin neck, or across the gap of a free-hanging limb). A
    plane whose even position is already safe isn't moved at all, and one
    that must move takes the smallest step that reaches a safe spot rather
    than jumping to the widest cross-section available: steering toward
    maximum area drags cuts toward whatever is bulkiest (a statue's base, a
    torso's hips) and pulls neighbouring planes together into thin slices,
    which defeats the point of an even split. `max_piece_size` (e.g. from
    --spacing) additionally caps how far a plane may drift so no piece grows
    past that limit — but if no safe spot exists within the initial (small)
    search window, the window is progressively widened, up to that cap,
    before giving up: a piece coming out smaller than the requested
    spacing/bed size is always acceptable, a disconnected floating region
    never is.

    NOTE: this is a greedy left-to-right placement — each plane is finalized
    using the previous plane's already-chosen position as its left bound and
    the whole material above as its right feasibility check. For most piece
    counts that produces clean, near-even cuts, but on a mesh with a large
    genuinely-disconnected band (a limb that hangs free over a long span) a
    high piece count can strand a later plane with no safe spot its greedy
    predecessors left room for, falling back to a best-effort cut that
    cutting.py may then reject. That's inherent to the mesh's topology on
    that axis, not a spacing the search can smooth away.

    `executor`, if given, is a live `ProcessPoolExecutor` used to check a
    whole batch of candidates' connectivity-safety concurrently instead of
    one at a time (see `_batch_is_safe`) — pass one in for heavy meshes
    where each check is itself expensive; `compute_cut_planes` decides when
    that's worth it and owns the executor's lifetime. `max_workers` controls
    how many chunks a batch is split into when `executor` is given.
    `connectivity_cache`/`area_cache`, if given, memoize connectivity and
    section-area results by (axis_idx, coord) across every call sharing the
    same dicts — scope them to one compute_cut_planes call (the mesh doesn't
    change within it), not across different meshes.
    """
    lo, hi = mesh.bounds[0][axis_idx], mesh.bounds[1][axis_idx]
    bounds = [lo, *sorted(planes), hi]
    refined: list[float] = []
    for i, p in enumerate(sorted(planes)):
        _check_cancelled(cancel_event)
        # left_bound must be the PREVIOUS plane's actual chosen position, not
        # its original (pre-refinement) slot in `bounds` -- each plane is
        # otherwise scored against where its neighbors *started*, not where
        # they actually end up, and since every plane searches independently,
        # two adjacent planes can each get nudged toward the other's original
        # spot and cross over. That inverts their order (plane i+1 ends up
        # left of plane i) and produces a near-zero-thickness sliver between
        # them once sorted back for cutting -- exactly the "very thin Z
        # layers" bug this guards against. right_bound is deliberately left
        # as the original two-planes-ahead position (unchanged): that's the
        # intentional wide look-ahead that lets a plane route around a pinch
        # using a neighbor's original slot, and remains safe as an upper
        # bound only, since the next iteration's left_bound is now pinned to
        # THIS plane's real chosen position regardless of how far right it
        # moved.
        left_bound = max(bounds[i], refined[-1]) if refined else bounds[i]
        right_bound = bounds[i + 2]
        # `p` (this plane's original, pre-refinement position) can now sit
        # to the left of `left_bound` if the previous plane got nudged past
        # it -- clamp before using it to center the search window/gap below,
        # so a pathologically squeezed previous plane can't produce a
        # negative gap here (which would otherwise invert full_min_c/
        # full_max_c and reopen the crossing bug this guards against).
        p_clamped = min(max(p, left_bound), right_bound)
        gap = min(p_clamped - left_bound, right_bound - p_clamped)
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
            full_min_c = full_max_c = p_clamped

        # A cut at `c` is "safe" when the piece to its LEFT, [left_bound, c],
        # is a single connected solid AND the entire remaining material above
        # it, [c, hi], is still connected. Two deliberate choices here:
        #
        #  * The right side is checked against `hi` (the mesh top), NOT
        #    against `right_bound` (the next plane's original, pre-refinement
        #    slot). That slot can sit inside a region that only reconnects
        #    higher up — e.g. a statue's arm that hangs free and is anchored
        #    only at the shoulder — making [c, right_bound] disconnected for
        #    EVERY c and leaving the plane no safe spot, so it falls back to
        #    the widest cross-section (the base) and slices off a wafer.
        #    Checking [c, hi] instead asks the correct question: "is the
        #    material above this cut still all one piece?" The actual next
        #    piece [c, next_cut] gets validated in its own right when the
        #    next plane is placed (as that plane's left-piece check), so
        #    every real piece is still covered.
        #  * left_bound is the PREVIOUS plane's refined position, so
        #    [left_bound, c] is exactly the piece being finalized now.
        def _is_safe(c: float) -> bool:
            return _is_single_component_cached(
                mesh, axis_idx, left_bound, c, connectivity_cache, graph_index
            ) and _is_single_component_cached(
                mesh, axis_idx, c, hi, connectivity_cache, graph_index
            )

        # Fast path / minimum-displacement anchor: if the plane's own
        # (evenly-spaced) position is already safe, keep it exactly — most
        # planes don't sit anywhere near a pinch, and the search below only
        # exists to route the few that do around a floating-region cut.
        # Keeping the rest put is what preserves uniform piece thicknesses.
        if _is_safe(p_clamped):
            refined.append(p_clamped)
            continue

        chosen = p_clamped
        # Widen the search in stages: try a small band around p first so
        # symmetric spacing is preserved when nothing's wrong, then expand
        # toward the full allowed range only if that wasn't enough to find
        # a safe spot.
        for frac in (search_frac, search_frac * 3, 1.0):
            _check_cancelled(cancel_event)
            half_width = gap * frac
            lo_c = max(full_min_c, p_clamped - half_width)
            hi_c = min(full_max_c, p_clamped + half_width)
            if hi_c <= lo_c:
                if frac >= 1.0:
                    lo_c, hi_c = full_min_c, full_max_c
                else:
                    continue

            candidates = list(np.linspace(lo_c, hi_c, samples))
            if p_clamped not in candidates:
                candidates.append(p_clamped)

            scored = sorted(
                ((c, _neighborhood_area(mesh, axis_idx, c, probe, area_cache)) for c in candidates),
                key=lambda t: t[1],
                reverse=True,
            )
            max_area = scored[0][1] if scored else 0.0

            # Among connectivity-safe candidates, take the one CLOSEST to the
            # plane's even position — the minimum move that escapes the
            # floating-region cut — rather than the largest cross-section.
            # Steering toward maximum area drags cuts toward whatever is
            # widest (a statue's base, a torso's hips), which on a figure
            # pulls neighboring planes together into thin slices even though
            # a safe cut existed much closer to where the plane started. The
            # connectivity check already rules out the disconnected cuts;
            # once a cut is safe, keeping spacing uniform matters more than
            # squeezing out extra joint area. `by_proximity` is the whole
            # candidate set ordered by distance from p; `_batch_is_safe`
            # checks them concurrently on a heavy mesh (see its docstring),
            # so first-safe-by-proximity is just an ordered scan of that
            # result, not a serialized per-candidate wait.
            by_proximity = sorted((c for c, _a in scored), key=lambda c: abs(c - p_clamped))
            # Right side checked against `hi`, not `right_bound` — see the
            # `_is_safe` comment above for why the next plane's original slot
            # is the wrong bound for the connectivity question.
            safety = _batch_is_safe(
                executor, mesh, axis_idx, by_proximity, left_bound, hi,
                max_workers=max_workers, cache=connectivity_cache, graph_index=graph_index,
            )
            found = False
            for c in by_proximity:
                if safety[c]:
                    chosen = c
                    found = True
                    break
            if found:
                break

            # Nothing safe anywhere in this window (by_proximity already
            # covered the whole candidate set). Remember the largest-area
            # candidate as the best effort and widen to the next stage —
            # cutting.py's post-cut check still catches a genuine floating
            # region and raises a clear error rather than silently shipping
            # one.
            if scored:
                chosen = scored[0][0]

        # If every widened window was exhausted with nothing connectivity-safe,
        # `chosen` is still the best-area candidate found; cutting.py's
        # post-cut check will catch a genuine floating-region result and
        # raise a clear, actionable error rather than silently producing one.
        refined.append(chosen)
    return refined
