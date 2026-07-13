# STL Splitter — Performance Optimization Plan

Implementation plan for optimizing the splitting pipeline. Phases 1–6 are
performance work, ordered by impact-per-risk; Phase 7 is a known CORRECTNESS fix
(connectors placed before later cuts can slice through them) that must be done
last and as its own change. Each phase is independent — complete and verify one
phase fully (including running tests) before starting the next. Do NOT combine
phases into one change.

Environment notes:
- Use the project venv: `.venv\Scripts\python.exe` (Windows).
- Run tests with: `.venv\Scripts\python.exe -m pytest tests/ -x -q`
- A real test model exists at `standing_figure.stl` in the repo root (use for benchmarks).
- Verified available in this venv: `manifold3d.Manifold.split_by_plane` /
  `trim_by_plane`, `trimesh.intersections.mesh_multiplane` (trimesh 4.12.2),
  `shapely.contains_xy` (shapely 2.1.2).

---

## Phase 0 — Benchmark + behavior-lock harness (do this first)

**Goal:** Make every later phase measurable and prove behavior didn't change.

**Files:** create `tests/bench_split.py` (a plain script, NOT a pytest file).

**Steps:**
1. Write a script that:
   - Loads `standing_figure.stl` via `stlsplit.geometry.load_mesh`.
   - Runs `run_pipeline` with `PipelineParams(axis="z", pieces=4)` and times it
     (`time.perf_counter`).
   - Also times `PipelineParams(axis="z", spacing=<mesh z-extent / 3>)`.
   - Prints for each run: elapsed seconds, number of pieces, number of dowels, and
     for each piece: `piece.volume` rounded to 3 decimals and `len(piece.faces)`.
2. Run it once BEFORE any optimization and save the output to
   `tests/bench_baseline.txt` (commit it so later phases can diff against it).

**Acceptance:** Script runs cleanly; baseline file exists. After every later phase,
re-run and confirm: piece count and dowel count identical; piece volumes within 0.1%
of baseline (tiny float drift from different boolean paths is acceptable; different
piece counts/topology are NOT).

---

## Phase 1 — Replace box-boolean cutting with progressive plane splitting (BIGGEST WIN)

**Problem:** `cut_mesh` in `stlsplit/cutting.py` produces each of N pieces by
boolean-intersecting the FULL original mesh against 1–2 giant box meshes
(`_halfspace_box`). Costs per piece: build 2 box meshes, convert full mesh to
manifold, run 2 full-mesh booleans. Total work grows ~quadratically with piece count
and never shrinks as material is removed.

**Fix:** Convert the mesh to a `manifold3d.Manifold` ONCE, then walk the sorted cut
planes slicing off one piece at a time with `Manifold.split_by_plane`, carrying the
shrinking "remaining" solid forward. No box meshes, one conversion, and each
successive split operates on a smaller solid.

**Files:** `stlsplit/cutting.py` only. Keep the public signature of `cut_mesh`
unchanged. `_halfspace_box` can be deleted once unused.

**Steps:**
1. Add two module-private conversion helpers:
   ```python
   import manifold3d
   import numpy as np

   def _to_manifold(mesh: trimesh.Trimesh) -> "manifold3d.Manifold":
       m = manifold3d.Mesh(
           vert_properties=np.asarray(mesh.vertices, dtype=np.float32),
           tri_verts=np.asarray(mesh.faces, dtype=np.uint32),
       )
       return manifold3d.Manifold(m)

   def _to_trimesh(man: "manifold3d.Manifold") -> trimesh.Trimesh:
       m = man.to_mesh()
       return trimesh.Trimesh(
           vertices=np.asarray(m.vert_properties[:, :3], dtype=np.float64),
           faces=np.asarray(m.tri_verts, dtype=np.int64),
           process=False,
       )
   ```
   (float32 conversion matches what trimesh's own `engine="manifold"` boolean path
   already does today, so this does not lose precision relative to current behavior.)
2. VERIFY the plane-split convention empirically before relying on it. In a scratch
   script, split a unit cube at its center:
   `manifold3d.Manifold.cube((1,1,1)).split_by_plane((0,0,1), 0.5)` returns a pair;
   determine which element is the side where `dot(point, normal) > offset`
   (per manifold docs the FIRST element is the positive/kept side, but confirm).
   Record the answer in a code comment.
3. Rewrite the body of `cut_mesh`:
   ```python
   if not resolved_cuts:
       return [mesh.copy()]
   n_pieces = len(resolved_cuts) + 1
   remaining = _to_manifold(mesh)
   pieces = []
   for i, cut in enumerate(resolved_cuts):
       offset = float(np.dot(cut.normal, cut.origin))
       # positive side = dot(x, normal) > offset (verify in step 2 which tuple slot)
       above, below = remaining.split_by_plane(tuple(cut.normal), offset)
       piece = _to_trimesh(below)          # the slab below/behind this plane
       if piece.is_empty or len(piece.faces) == 0:
           raise CutPlacementError(f"Cut produced an empty piece {i + 1}.", piece_index=i)
       _check_no_floating_regions(piece, i)
       pieces.append(piece)
       remaining = above
       if progress:
           progress.step(f"Cutting piece {i + 1}/{n_pieces}")
   last = _to_trimesh(remaining)
   if last.is_empty or len(last.faces) == 0:
       raise CutPlacementError(f"Cut produced an empty piece {n_pieces}.", piece_index=n_pieces - 1)
   _check_no_floating_regions(last, n_pieces - 1)
   pieces.append(last)
   if progress:
       progress.step(f"Cutting piece {n_pieces}/{n_pieces}")
   ```
   Important details:
   - `resolved_cuts` is already sorted by position and normals point in the +axis
     direction (see `geometry.resolve_cuts`), so slicing off the "below" side in
     order produces pieces in the same order as today. Tilted normals work
     unchanged — `split_by_plane` takes an arbitrary normal.
   - `Manifold.to_mesh()` may return a manifold with zero triangles instead of
     raising; that is why the empty checks test `len(piece.faces) == 0`.
   - An empty `Manifold` also has `.num_tri() == 0`; you may check
     `below.num_tri() == 0` before converting to fail faster.
4. Delete `_halfspace_box`, `_MARGIN`, and `_CANON_Z` if nothing else uses them
   (grep first: `_halfspace_box` is only used inside cutting.py today).

**Acceptance:**
- `pytest tests/ -x -q` passes.
- Bench script (Phase 0): identical piece/dowel counts, volumes within 0.1%,
  and cutting-dominated runs measurably faster (expect several-x on `pieces=4`+).
- Also manually run one job with a tilted cut (e.g.
  `PipelineParams(axis_cuts={"z": [Cut(position=<mid>, tilt_a=15)]})`) and confirm
  two watertight pieces come out (check `piece.is_watertight`).

**Risk:** Medium — this is the core cut path. The empty-piece and floating-region
checks must be preserved exactly, including the error text pattern
`"Cut produced an empty piece {i + 1}."` which the web UI / tests may match on
(grep `tests/` for it first and keep messages identical).

---

## Phase 2 — Halve IPC cost in `_batch_is_safe` (geometry.py)

**Problem:** `_batch_is_safe` in `stlsplit/geometry.py` submits TWO futures per
candidate, and every single future pickles the full `vertices`/`faces` arrays to a
worker process. On a 371k-face mesh that is tens of MB serialized per task — the
pickling often costs more than the check itself.

**Fix (two independent parts, do both):**

1. **One task per candidate, not two.** Add a worker that checks both sides in one
   call:
   ```python
   def _candidate_is_safe_worker(vertices, faces, axis_idx, left_bound, c, right_bound) -> bool:
       mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
       return (_is_single_component(mesh, axis_idx, left_bound, c)
               and _is_single_component(mesh, axis_idx, c, right_bound))
   ```
   Rewrite the executor branch of `_batch_is_safe` to submit exactly one
   `_candidate_is_safe_worker` future per candidate. This halves pickle traffic and
   also gains short-circuiting (right side skipped when left fails), which the
   current parallel version lost. Keep the sequential (executor is None) branch
   exactly as it is. Keep `_is_single_component_worker` only if `_planes_are_safe`
   still uses it (it does — leave that function alone).
2. **Chunk candidates so each worker receives the mesh once per chunk.** Instead of
   one future per candidate, split `candidates` into `min(len(candidates), executor._max_workers)`
   contiguous chunks — but `_max_workers` is private, so instead pass the worker
   count into `_batch_is_safe` (thread it from `compute_cut_planes`, which already
   computes `os.cpu_count() or 1`). Worker signature:
   ```python
   def _candidates_chunk_worker(vertices, faces, axis_idx, left_bound, right_bound, chunk) -> list[bool]:
       mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
       return [
           _is_single_component(mesh, axis_idx, left_bound, c)
           and _is_single_component(mesh, axis_idx, c, right_bound)
           for c in chunk
       ]
   ```
   Reassemble the `dict[float, bool]` from the chunk results in order.
   NOTE: chunking trades per-candidate parallelism for fewer mesh pickles. With
   e.g. 12 candidates and 8 cores, use `n_chunks = min(len(candidates), workers)`
   so all cores still engage.

**Files:** `stlsplit/geometry.py` only.

**Acceptance:** Tests pass; bench output identical (this phase must not change any
chosen cut position — results must be bit-identical to before, since the same checks
run on the same candidates). Time `compute_cut_planes` on `standing_figure.stl`
(spacing mode) before/after; expect a clear improvement on meshes above
`_PARALLEL_FACE_THRESHOLD`.

**Risk:** Low. Pure plumbing; the per-candidate boolean result is unchanged.

---

## Phase 3 — Cache repeated slab-connectivity and section-area checks (geometry.py)

**Problem A:** The spacing-mode retry loop in `_compute_cut_planes_inner` calls
`refine_cut_planes` then `_planes_are_safe` per attempt, and consecutive attempts
re-check many identical or overlapping `(lo, hi)` slabs with the expensive
`_is_single_component`. Nothing is memoized.

**Problem B:** `_neighborhood_area` runs 3 `mesh.section()` calls per candidate, and
the staged window-widening in `refine_cut_planes` re-scores overlapping candidate
sets (the original position `p` is re-scored at every stage; widened windows overlap
narrower ones at the endpoints).

**Fix:**
1. In `_compute_cut_planes_inner`, create two plain dicts before the retry loop:
   `connectivity_cache: dict[tuple, bool]` and `area_cache: dict[float, float]`.
2. Thread them (as optional keyword args defaulting to `None`) through
   `refine_cut_planes`, `_planes_are_safe`, `_batch_is_safe`, `_neighborhood_area`,
   and `_section_area`.
3. Cache keys MUST round coordinates to avoid float-noise misses:
   - connectivity: `(axis_idx, None if lo is None else round(lo, 6), None if hi is None else round(hi, 6))`
   - area: `round(coord, 6)`
4. In `_batch_is_safe`: before submitting, look each candidate up in the cache;
   only submit futures for misses; write results back into the cache after.
   Same pattern in `_planes_are_safe` and the sequential branches.
5. Do NOT cache across different meshes: the caches live only inside one
   `_compute_cut_planes_inner` call (the mesh never changes within it), so no mesh
   identity key is needed. Do not make them module-level globals.

**Files:** `stlsplit/geometry.py` only.

**Acceptance:** Tests pass; bench cut positions identical to Phase 2 output.
Spacing-mode runs that trigger the retry loop (e.g. spacing slightly under half the
z-extent of `standing_figure.stl`) should show fewer worker submissions — add a
temporary counter print while verifying, then remove it.

**Risk:** Low. Same computations, memoized within a single planning call.

---

## Phase 4 — Vectorize 2D candidate sampling in connectors (small, safe win)

**Problem:** `_candidate_positions_2d` in `stlsplit/connectors.py` tests up to
48×48 = 2304 grid points one at a time with `safe_zone.contains(Point(x, y))` plus a
per-point `polygon.boundary.distance(...)` — thousands of individual shapely calls
per interface.

**Fix:** Use shapely 2.x vectorized ops:
```python
xx, yy = np.meshgrid(xs, ys, indexing="ij")
flat_x, flat_y = xx.ravel(), yy.ravel()
import shapely
inside = shapely.contains_xy(safe_zone, flat_x, flat_y)
pts = shapely.points(flat_x[inside], flat_y[inside])
dists = shapely.distance(polygon.boundary, pts)
candidates = list(zip(flat_x[inside], flat_y[inside], dists))
```
Keep the farthest-point sampling loop below it unchanged (candidate counts are small
after filtering; it is not a bottleneck).

**Files:** `stlsplit/connectors.py` only. Import `shapely` at module top
(`import shapely` — the vectorized functions live on the top-level module).

**Acceptance:** Tests pass; bench dowel counts and piece volumes identical.
The candidate list must contain the same points in the same order as before
(grid order was `for x in xs: for y in ys:` — `indexing="ij"` + `ravel()`
reproduces exactly that order; verify on one interface with a debug print, then
remove the print).

**Risk:** Low.

---

## Phase 5 — Reuse one process pool across the auto-fit recursion (autofit.py)

**Problem:** `_split_recursive` in `stlsplit/autofit.py` calls
`compute_cut_planes` once per oversized piece, and for every heavy piece that call
spins up (and tears down) a fresh `ProcessPoolExecutor` — paying full worker-process
startup (fresh trimesh/manifold imports, ~0.5–1s per worker) at every recursion
level.

**Fix:**
1. In `stlsplit/geometry.py`, split `compute_cut_planes` so the pool is injectable:
   add an optional `executor: ProcessPoolExecutor | None = None` parameter. If a
   non-None executor is passed, use it directly and do NOT shut it down; only when
   it is None keep today's create-use-shutdown behavior (existing callers stay
   unchanged).
2. In `stlsplit/autofit.py`:
   - In `auto_fit_split`, decide ONCE whether the (top-level) mesh warrants a pool
     (`len(mesh.faces) >= geometry._PARALLEL_FACE_THRESHOLD`); if so create one
     `ProcessPoolExecutor(max_workers=os.cpu_count() or 1)` in a try/finally that
     shuts it down after `_split_recursive` returns.
   - Pass it down through `_split_recursive` into every `compute_cut_planes` call.
   - Note: sub-pieces smaller than the threshold will now still use the pool.
     That is fine — the sequential path is only preferred to avoid pool *startup*
     cost, which is already paid. To keep it simple, when a pool exists, always
     pass it.

**Files:** `stlsplit/geometry.py` (signature + pool-ownership logic),
`stlsplit/autofit.py`.

**Acceptance:** Tests pass. Bench a bed-dims auto-fit run on `standing_figure.stl`
scaled large enough to need cuts on 2+ axes (e.g. `target_dim=400`,
`bed_dims={"x":220,"y":220,"z":250}`) before/after; cut positions must be identical,
wall-clock lower on multi-level splits.

**Risk:** Low-medium. Be careful that the executor is never shut down by
`compute_cut_planes` when it was passed in from outside.

---

## Phase 6 (OPTIONAL / higher risk — get maintainer sign-off before starting) —
## Graph-based slab connectivity check

**Problem:** `_is_single_component` does `slice_plane(cap=True)` (twice) plus a full
`split()` per check — each O(mesh) with heavy geometry work. It is the innermost hot
function of all of plane refinement.

**Idea:** Within one planning call, precompute the face-adjacency graph once
(`mesh.face_adjacency`, a (m,2) int array) and per-face min/max coordinate along the
cut axis. A slab `[lo, hi]` check then becomes: select faces whose coordinate range
overlaps the slab, build the induced subgraph, and count connected components via
`scipy.sparse.csgraph.connected_components`. No slicing, no capping, no `split()` —
microseconds instead of ~100ms+, and it removes the need for the process pool in
refinement entirely (Phases 2/5 become mostly moot for this path).

**Caveats that make this optional:**
- The current check measures component significance by VOLUME (`>= 1.0 mm³`) of
  capped solids; a graph check can only approximate significance (e.g. component
  face-count or summed face area). Threshold semantics change.
- A component connected only through geometry OUTSIDE the slab correctly counts as
  disconnected in both versions, but degenerate cases (faces exactly tangent to the
  slab boundary, open meshes run with `allow_non_watertight`) may classify
  differently.

**If approved, the required validation is:** a comparison harness that runs BOTH
implementations over every `(lo, hi)` slab queried during planning on at least
`standing_figure.stl` and `stlsplit/static/box.stl` across all three axes and
several piece counts, and reports disagreements. Only swap the implementation in if
disagreement is zero on those models; otherwise keep the graph check as a fast
pre-filter (graph says "connected" → trust it; graph says "disconnected" → confirm
with the existing slice-based check).

---

## Phase 7 (CORRECTNESS FIX, not an optimization — do LAST, after all other phases,
## and get maintainer sign-off first) — Connectors placed before later cuts can be
## sliced through

**Problem:** Both multi-axis paths add connectors too early:

- `stlsplit/autofit.py` `_split_recursive`: cuts on one axis, subtracts sockets at
  those interfaces, THEN recursively cuts the resulting pieces on other axes.
- `stlsplit/pipeline.py` `_cut_all_axes`: same pattern per axis pass — connectors
  are added after each axis's cuts, then those pieces are cut again on the next
  axis.

A later cut on a perpendicular axis can pass through an already-subtracted socket
cavity: the socket becomes an open notch on the new cut face, the matching dowel no
longer has a complete hole to seat in, and `_is_safe_3d`'s wall-thickness guarantee
is voided because it validated against geometry that a later cut then removed.
Single-axis jobs are NOT affected (connectors are added once, after all cutting).

**Fix strategy: cut everything first, add connectors last.** All cutting (every
axis, every recursion level) runs with no connectors, while recording every applied
cut plane. Then, in a second stage over the FINAL pieces, place connectors at every
recorded plane between each pair of pieces that actually face each other across it.
Sockets are then always carved from final geometry, so nothing can cut through them
afterwards, and `_is_safe_3d` validates against the true final walls.

**Files:** `stlsplit/connectors.py`, `stlsplit/pipeline.py`, `stlsplit/autofit.py`.

**Steps:**
1. **Pure refactor of `connectors.py` first (no behavior change, verify tests
   between steps 1 and 2):** extract the body of `add_connectors`'s per-interface
   loop into
   ```python
   def add_connectors_at_interface(piece_a, piece_b, cut: ResolvedCut, *,
                                   peg_diameter=7.0, peg_length=5.0, peg_clearance=0.18,
                                   inset=8.0, n_pegs=4, min_wall_thickness=1.2,
                                   alignment_key=False, key_flat_depth=1.5,
                                   dowel_shape="round") -> tuple[trimesh.Trimesh, trimesh.Trimesh, list[trimesh.Trimesh]]:
   ```
   returning `(new_piece_a, new_piece_b, dowels_for_this_interface)` (returns the
   inputs unchanged plus `[]` when the interface is skipped). Reimplement
   `add_connectors` as a thin loop over it so all existing callers and tests keep
   working unchanged.
2. **Record cuts instead of connecting eagerly.** In `_cut_all_axes`
   (pipeline.py) and `_split_recursive` (autofit.py), stop calling
   `add_connectors` inside the per-axis / recursive loop. Instead append every
   `ResolvedCut` actually applied to a shared `applied_cuts: list[ResolvedCut]`
   list (in the parallel `_process_one_piece` branch, return the resolved cuts —
   as plain `(origin, normal, position)` tuples for picklability — alongside the
   piece arrays, and rebuild `ResolvedCut`s in the parent).
   De-duplicate: multiple pieces are cut by the same user-specified plane; two
   recorded cuts are the same interface if their normals are parallel
   (`abs(dot(n1, n2)) > 1 - 1e-9`) and their plane offsets
   (`dot(normal, origin)`) match within 1e-6.
3. **Pair final pieces across each recorded plane.** For each deduplicated cut and
   each final piece, compute signed distances of the piece's vertices to the plane
   (`(piece.vertices - cut.origin) @ cut.normal`). A piece is on side A if
   `max(dists)` is within `tol = 1e-4` of 0 and `min(dists) < -tol` (it touches
   the plane from the negative side); side B is the mirror. For every (A, B)
   candidate pair, confirm they actually face each other: compute
   `_interface_polygon` for both (piece A with `inward_sign=-1.0`, piece B with
   `inward_sign=+1.0`), transform both into the SAME 2D frame (use piece A's
   `to_3d` and map B's polygon through it — or simpler: intersect the two shapely
   polygons after projecting both pieces' sections with a shared plane basis from
   `_lateral_basis(cut.normal)`), and require `intersection.area > 1.0` (mm²).
   Only place connectors on that shared-overlap polygon region — pass the
   intersection polygon into placement so pegs land where BOTH pieces exist
   (extend `add_connectors_at_interface` with an optional
   `restrict_polygon=None` parameter used to clip the candidate zone).
4. **Wire the second stage in.** After all cutting finishes in `_cut_all_axes` /
   `auto_fit_split`, loop over deduplicated cuts × facing pairs, call
   `add_connectors_at_interface`, replace the two pieces in the final list with
   the returned ones, and collect dowels. Note a piece can gain sockets from
   multiple interfaces (one per neighbor) — always feed the CURRENT version of the
   piece into the next pair's call, not the pre-socket original (keep pieces in a
   list and update in place by index).
5. **Progress reporting:** interface count is now known only after cutting
   completes; emit one `progress.step(...)` per (cut, pair) processed, and update
   the `set_total` estimate in `run_pipeline`'s axis_cuts branch (an estimate is
   fine — see the existing comment there: completion always forces 1.0).
6. **Tests:**
   - Existing single-axis tests must pass completely unchanged (single-axis path
     still calls `add_connectors` exactly as before).
   - New regression test: split `stlsplit/static/box.stl` (or a generated
     `trimesh.creation.box` of e.g. 60×60×60) into 2×2 pieces via
     `axis_cuts={"z": [mid_z], "x": [mid_x]}`. Assert: 4 pieces; every piece
     watertight; dowels exist for BOTH the z-interface pairs and the x-interface
     pairs (with the old eager code, x-interface sockets could intersect the z
     plane region; with the fix, assert additionally that no socket cavity
     intersects another recorded cut plane — e.g. for each dowel, check its
     bounding box does not straddle any OTHER cut's plane by more than the
     socket_overshoot 0.6mm + clearance).

**Acceptance:** All tests pass. `standing_figure.stl` multi-axis bench: piece count
and per-piece watertightness unchanged; dowel count MAY legitimately change (this
phase intentionally changes connector behavior — that is the point). Update
`tests/bench_baseline.txt` dowel counts afterwards and note the phase in the file.

**Risk:** High — this reorders a core behavior and touches both multi-axis paths.
Do it as its own change, after all optimization phases are verified, so performance
regressions and behavior changes are never entangled in one diff.

---

## Explicitly out of scope (do not "fix" while implementing the above)

- Any change to connector placement scoring, peg geometry, or clearances (Phase 7
  changes WHEN and BETWEEN WHICH pieces connectors are placed, not how a single
  interface is scored).
- The web UI / progress-reporting code, except that step counts emitted by
  `cut_mesh` must stay one step per piece (Phase 1 preserves this).
- Switching `_is_single_component`'s `except Exception: return True` policy.
