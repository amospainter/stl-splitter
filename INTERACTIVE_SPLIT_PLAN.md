# Interactive Cut-Tree Splitting — Implementation Plan

## Goal

Today the web UI is "configure a flat list of cuts per axis, submit once, get final
pieces back" (`stlsplit/web.py`'s `POST /jobs`, `stlsplit/static/js/state.js`'s
`useSplitForm`). The same `cut_planes_x/y/z` list is applied to *every* piece at that
axis's turn — there is no way to cut one specific resulting piece differently from
its siblings.

This plan adds a second, additive UI mode ("Interactive split") where the user:
1. Draws cut(s) on one axis for the whole mesh → sees the resulting pieces as
   separate objects.
2. Picks one of those pieces.
3. Draws cut(s) on a (possibly different) axis for *that piece only* → sees its
   sub-pieces.
4. Repeats on any leaf piece, then hits "Finish" to add connectors (across the whole
   cut tree) and export.

**The existing "Quick split" flow is not touched or replaced.** This is a new mode
added alongside it, selected by a toggle. This is a deliberate scope decision to
de-risk the plan — the existing flow has real users and real tests; breaking it to
build this is not an acceptable tradeoff.

**Estimated effort: ~7–11 developer-days for a solid v1** (see phase breakdown
below for where the time goes). Phases are ordered so each is independently
testable/mergeable; do not skip ahead or combine phases.

## Key existing primitives this reuses (read these before starting)

- `stlsplit/geometry.py`: `Cut`, `ResolvedCut`, `resolve_cuts(mesh, axis, cuts)`,
  `compute_cut_planes(mesh, axis, spacing=, pieces=, cancel_event=)`.
- `stlsplit/cutting.py`: `cut_mesh(mesh, resolved_cuts, progress=)` — cuts ONE mesh
  into N pieces at the given resolved cut planes. This is exactly the primitive a
  single "cut this piece" step needs; no new geometry code is required here.
- `stlsplit/connectors.py`: `dedupe_cuts(cuts)`, `add_connectors_after_cuts(pieces,
  applied_cuts, connector_kwargs, progress=)`, `find_facing_pairs(pieces, cut)`.
  These already implement "collect every cut made anywhere in a multi-level tree,
  then place connectors once at the end, only between pieces that actually face
  each other across a given plane" — this is precisely what an interactive,
  arbitrary-depth cut tree needs, and it already exists (built for Phase 7 of
  `OPTIMIZATION_PLAN.md`). **Do not reimplement this logic.**
- `stlsplit/progress.py`: `ProgressReporter`, `JobCancelled` — reuse for the
  `/finish` step (which can be slow: connectors + export) and for the "Stop" affordance
  on any single cut operation.
- `stlsplit/web.py`: `Job` dataclass + `_JOBS`/`_JOBS_LOCK` + `stream_job` (SSE) +
  `_run_job` pattern — reuse this exact pattern for `/finish` rather than inventing a
  second progress-streaming mechanism.
- `stlsplit/export.py`: `export_pieces(pieces, basename, format, dowels=, bed_size=)`.
- Frontend: `stlsplit/static/js/viewer.js`'s `makeViewer()` (one three.js scene per
  mounted piece), `components/PieceGrid.js` + `PieceCard.js` (grid of small
  per-piece viewers — reuse directly for "here are the current leaf pieces, click one
  to select it"), `components/AxisPlaneEditor.js` (the drag-to-place-cuts UI — reuse
  its *template/interaction pattern*, not the component itself, since it's coupled to
  `state.js`'s three-simultaneous-axis-controllers model).

## Out of scope for this plan (do not build)

- Multi-select / batch-cutting several pieces at once in one step.
- An "explode view" that offsets pieces spatially for clarity — pieces render at
  their real post-cut position; this is a nice-to-have, not required for v1.
- Editing/regenerating a *specific* past cut in place (only whole-subtree undo, see
  Phase 1).
- Any change to the connector-placement algorithm itself — this plan only changes
  *when* the user triggers cuts, not how sockets/dowels are computed.

---

## Phase 1 — Backend: in-memory session / piece-tree store (no HTTP yet) [DONE]

**Result:** Implemented as planned in `stlsplit/sessions.py`, matching the sketch
almost exactly. `tests/test_sessions.py` (9 tests) all passed on the first run,
including the cascading-undo case. Full suite (30 tests) passes.



**Goal:** All the new state-management logic, in one plain-Python module with no
FastAPI dependency, so it's unit-testable in isolation before any HTTP plumbing
exists.

**File:** create `stlsplit/sessions.py`.

**Data model:**

```python
"""In-memory session/piece-tree store for the interactive split UI.

A Session holds every piece ever produced by cutting, as a tree: the
originally-uploaded (scaled) mesh is the root; cutting a leaf piece replaces
it with its children in the "leaves" set but keeps the piece itself in
`pieces` (as an internal/non-leaf node) so the tree can be inspected, undone,
or re-rendered after a page refresh. Only `leaves` are eligible to be cut
further or included in the final connector/export pass.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field

import trimesh

from .geometry import ResolvedCut

_SESSION_TTL_SECONDS = 30 * 60  # idle sessions are swept after this long


@dataclass
class PieceNode:
    id: str
    mesh: trimesh.Trimesh
    parent_id: str | None
    # The cut-group that produced this piece as a child (None for the root).
    # Every child produced by the same /cut call shares one cut_group_id, so
    # undo can remove exactly (and only) that operation's effects.
    cut_group_id: str | None
    children_ids: list[str] = field(default_factory=list)


@dataclass
class CutGroup:
    id: str
    piece_id: str  # the piece that was cut to produce this group's children
    child_ids: list[str]
    resolved_cuts: list[ResolvedCut]  # what to remove from Session.applied_cuts on undo


@dataclass
class Session:
    id: str
    filename: str
    scale_factor: float
    pieces: dict[str, PieceNode] = field(default_factory=dict)
    leaves: set[str] = field(default_factory=set)
    cut_groups: dict[str, CutGroup] = field(default_factory=dict)
    applied_cuts: list[ResolvedCut] = field(default_factory=list)
    last_activity: float = field(default_factory=time.time)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def touch(self) -> None:
        self.last_activity = time.time()


_SESSIONS: dict[str, Session] = {}
_SESSIONS_LOCK = threading.Lock()


def create_session(mesh: trimesh.Trimesh, filename: str, scale_factor: float) -> Session:
    session_id = uuid.uuid4().hex
    root = PieceNode(id="root", mesh=mesh, parent_id=None, cut_group_id=None)
    session = Session(id=session_id, filename=filename, scale_factor=scale_factor)
    session.pieces["root"] = root
    session.leaves.add("root")
    with _SESSIONS_LOCK:
        _SESSIONS[session_id] = session
    return session


def get_session(session_id: str) -> Session | None:
    with _SESSIONS_LOCK:
        session = _SESSIONS.get(session_id)
    if session is not None:
        session.touch()
    return session


def close_session(session_id: str) -> None:
    with _SESSIONS_LOCK:
        _SESSIONS.pop(session_id, None)


def sweep_expired_sessions(now: float | None = None) -> int:
    """Remove sessions idle longer than _SESSION_TTL_SECONDS. Returns the
    count removed. Call this periodically (see Phase 2's background sweep
    thread) — full meshes live in server memory for the session's lifetime,
    so unbounded sessions is a real memory leak, not a theoretical one."""
    now = now if now is not None else time.time()
    with _SESSIONS_LOCK:
        expired = [sid for sid, s in _SESSIONS.items() if now - s.last_activity > _SESSION_TTL_SECONDS]
        for sid in expired:
            del _SESSIONS[sid]
    return len(expired)
```

**Core operations (add to `stlsplit/sessions.py`):**

```python
from .cutting import cut_mesh
from .geometry import CutPlacementError, Cut, axis_index, resolve_cuts


def cut_piece(
    session: Session, piece_id: str, axis: str, cuts: list["Cut | float"], progress=None
) -> list[PieceNode]:
    """Cut the leaf piece `piece_id` along `axis` at `cuts`, replacing it with
    its children in `session.leaves`. Raises KeyError if `piece_id` isn't a
    current leaf (stale client state — e.g. it was already cut or undone by
    another request), CutPlacementError if the cut itself fails (same as the
    existing single-shot pipeline — surface this to the user unchanged)."""
    if piece_id not in session.leaves:
        raise KeyError(f"'{piece_id}' is not a current leaf piece")
    node = session.pieces[piece_id]
    resolved = resolve_cuts(node.mesh, axis, cuts)
    sub_pieces = cut_mesh(node.mesh, resolved, progress=progress)  # raises CutPlacementError on failure

    group_id = uuid.uuid4().hex
    child_nodes = []
    for sub in sub_pieces:
        child_id = uuid.uuid4().hex
        child = PieceNode(id=child_id, mesh=sub, parent_id=piece_id, cut_group_id=group_id)
        session.pieces[child_id] = child
        node.children_ids.append(child_id)
        child_nodes.append(child)

    session.cut_groups[group_id] = CutGroup(
        id=group_id, piece_id=piece_id, child_ids=[c.id for c in child_nodes], resolved_cuts=resolved
    )
    session.leaves.discard(piece_id)
    session.leaves.update(c.id for c in child_nodes)
    session.applied_cuts.extend(resolved)
    session.touch()
    return child_nodes


def undo_cut(session: Session, piece_id: str) -> None:
    """Undo the cut that produced `piece_id`'s children: remove the children
    (and, recursively, anything cut from them — a piece can't be un-cut while
    its own children still exist, so this cascades down first), restore
    `piece_id` as a leaf, and remove that cut's ResolvedCuts from
    session.applied_cuts. Raises KeyError if `piece_id` has no children (never
    cut, or already undone)."""
    node = session.pieces[piece_id]
    if not node.children_ids:
        raise KeyError(f"'{piece_id}' has no cut to undo")

    # Cascade: recursively undo any grandchildren first (a child that was
    # itself cut further must be un-cut before it can be removed).
    for child_id in list(node.children_ids):
        child = session.pieces.get(child_id)
        if child and child.children_ids:
            undo_cut(session, child_id)

    group_id = session.pieces[node.children_ids[0]].cut_group_id
    group = session.cut_groups.pop(group_id)
    for child_id in group.child_ids:
        session.pieces.pop(child_id, None)
        session.leaves.discard(child_id)
    node.children_ids.clear()
    session.leaves.add(piece_id)

    # Remove exactly this group's ResolvedCuts (by identity, not equality —
    # ResolvedCut isn't hashable/orderable in a way that's safe to dedupe by
    # value here) from applied_cuts.
    removed_ids = {id(c) for c in group.resolved_cuts}
    session.applied_cuts = [c for c in session.applied_cuts if id(c) not in removed_ids]
    session.touch()


def tree_summary(session: Session) -> dict:
    """JSON-serializable snapshot of the whole tree, for GET /sessions/{id}
    (reconstructing UI state after a page refresh) — piece meshes themselves
    are NOT included here (too large); the piece-preview endpoint fetches an
    individual piece's STL bytes on demand."""
    return {
        "session_id": session.id,
        "leaves": sorted(session.leaves),
        "pieces": {
            pid: {
                "parent_id": node.parent_id,
                "children_ids": node.children_ids,
                "is_leaf": pid in session.leaves,
                "extents": node.mesh.extents.tolist(),
                "volume": float(node.mesh.volume),
            }
            for pid, node in session.pieces.items()
        },
    }
```

**Steps:**
1. Create the file with the code above (adjust imports to match current module
   layout — verify `Cut`, `CutPlacementError`, `resolve_cuts`, `cut_mesh` import
   paths against the actual current source, not just this plan, since other phases
   of prior work may have shifted names slightly).
2. Write `tests/test_sessions.py` (plain pytest, no FastAPI):
   - `create_session` then `cut_piece(session, "root", "z", [0.0])` on a
     `trimesh.creation.box(extents=[20,20,60])` → assert 2 new leaves, `"root"` no
     longer in `leaves`, `session.applied_cuts` has 1 entry, `session.pieces["root"]`
     still exists with `children_ids` of length 2.
   - Cut one of those children on `"x"` → assert 3 total current leaves, tree has 4
     total pieces (root + 2 + ... wait: root's 2 children, one of which now has 2 of
     its own — 1 + 2 + 2 = 5 total pieces, 3 current leaves). Get this right in the
     test by actually running it, not just reasoning about it.
   - `undo_cut` on the twice-cut piece → back to 2 leaves, cut_groups/applied_cuts
     shrink back down correctly.
   - Cutting a non-leaf `piece_id` (e.g. `"root"` after it's been cut) raises
     `KeyError`.
   - `undo_cut` cascades: cut root → cut one child → undo on ROOT (not the child)
     should raise `KeyError`("root has no *direct* cut to undo"? — actually root DOES
     have a direct cut in this scenario) — re-derive this test case by actually
     running the scenario, don't guess the exact exception semantics from this plan
     alone.
   - `sweep_expired_sessions`: create a session, manually set
     `session.last_activity` to `time.time() - 3600`, call `sweep_expired_sessions()`,
     assert it's gone from `get_session`.

**Acceptance:** New test file passes. No other file is modified in this phase — it's
purely additive.

---

## Phase 2 — Backend: HTTP endpoints [DONE]

**Result:** Implemented directly in `stlsplit/web.py` (not a separate router file —
the circular-import cost of splitting it out at this size wasn't worth it; noted as
a "real code wins over the plan's sketch" deviation). `_build_job_result` extracted
from `_run_job` first and verified behavior-unchanged (full suite passed) before
adding anything new. All 8 endpoints from the sketch implemented as planned:
`POST /sessions`, `GET /sessions/{id}`, `GET .../pieces/{id}/preview`,
`POST .../pieces/{id}/plane_preview`, `POST .../pieces/{id}/cut`,
`POST .../pieces/{id}/undo`, `DELETE /sessions/{id}`, `POST /sessions/{id}/finish`
(reusing the existing `Job`/SSE mechanism exactly as planned). Added `httpx` to the
`web` extra in `pyproject.toml` (needed for `fastapi.testclient.TestClient`).
Fixed one real bug found during implementation, not anticipated in the plan: the
periodic session-sweep timer must be an explicit daemon thread, or it blocks clean
process exit (including every test run that imports `stlsplit.web`).
`tests/test_session_endpoints.py` (8 tests, using `TestClient`) all pass, including
the exact end-to-end user story (cut root on Z → cut one child on X → finish →
3 final pieces). Full suite (38 tests) passes.



**Goal:** Expose Phase 1's session store over HTTP, reusing the existing
Job/SSE-progress pattern for the one genuinely slow operation (`/finish`).

**File:** add to `stlsplit/web.py` (or, if it's getting long, a new
`stlsplit/session_web.py` module that defines its own `APIRouter` and gets
`app.include_router(...)`'d from `web.py` — check `stlsplit/web.py`'s current
line count before deciding; if it's grown past ~600 lines, prefer the router file).

**Endpoints:**

```python
@app.post("/sessions")
async def create_session_endpoint(
    file: UploadFile,
    scale: str | None = Form(None),
    target_dim: str | None = Form(None),
    scale_axis: str = Form("z"),
    allow_non_watertight: bool = Form(False),
):
    """Upload + scale, creating a new interactive session whose root piece is
    the scaled mesh. Returns {session_id, piece_id: "root", extents, volume,
    scale_factor} — NOT the mesh preview itself; call
    GET /sessions/{id}/pieces/{piece_id}/preview for that (kept separate so a
    client that already has last-known geometry, e.g. after a page
    reconnect, doesn't have to re-download it)."""
    raw = await file.read()
    filename = file.filename or "input.stl"

    def _prepare():
        from .geometry import scale_mesh
        mesh = load_mesh_from_bytes(raw, filename, allow_non_watertight=allow_non_watertight)
        scale_val, target_dim_val = _parse_float(scale), _parse_float(target_dim)
        mesh = scale_mesh(mesh, scale=scale_val, target_dim=target_dim_val, axis=scale_axis)
        factor = scale_val if scale_val is not None else 1.0  # target_dim case: see /plane_preview for the exact formula to reuse
        return mesh, factor

    try:
        mesh, scale_factor = await asyncio.to_thread(_prepare)
    except ValueError as e:
        return JSONResponse({"detail": str(e)}, status_code=400)

    from .sessions import create_session
    session = create_session(mesh, filename, scale_factor)
    root = session.pieces["root"]
    return {
        "session_id": session.id, "piece_id": "root",
        "extents": root.mesh.extents.tolist(), "volume": float(root.mesh.volume),
        "bounds": root.mesh.bounds.tolist(), "scale_factor": scale_factor,
    }


@app.get("/sessions/{session_id}/pieces/{piece_id}/preview")
async def piece_preview(session_id: str, piece_id: str):
    """STL bytes (base64) for one piece — called on demand when the frontend
    needs to (re)render a specific piece, rather than every session mutation
    pushing full mesh bytes for every piece it touches."""
    from .sessions import get_session
    session = get_session(session_id)
    if session is None:
        return JSONResponse({"detail": "Unknown session"}, status_code=404)
    node = session.pieces.get(piece_id)
    if node is None:
        return JSONResponse({"detail": "Unknown piece"}, status_code=404)
    data = export_pieces([node.mesh], "piece", "stl")[0][1]
    return {"data_base64": base64.b64encode(data).decode()}


@app.get("/sessions/{session_id}")
async def session_tree(session_id: str):
    from .sessions import get_session, tree_summary
    session = get_session(session_id)
    if session is None:
        return JSONResponse({"detail": "Unknown session"}, status_code=404)
    return tree_summary(session)


@app.post("/sessions/{session_id}/pieces/{piece_id}/plane_preview")
async def piece_plane_preview(
    session_id: str, piece_id: str,
    axis: str = Form("z"), spacing: str | None = Form(None), pieces: str | None = Form(None),
):
    """Same idea as the existing /plane_preview, but scoped to one specific
    piece's current mesh instead of re-uploading/re-scaling the whole file
    every time -- the piece's geometry already lives in the session."""
    from .sessions import get_session
    from .geometry import compute_cut_planes
    session = get_session(session_id)
    if session is None:
        return JSONResponse({"detail": "Unknown session"}, status_code=404)
    node = session.pieces.get(piece_id)
    if node is None or piece_id not in session.leaves:
        return JSONResponse({"detail": "Piece is not a current leaf"}, status_code=400)

    spacing_val, pieces_val = _parse_float(spacing), _parse_int(pieces)

    def _compute():
        return compute_cut_planes(node.mesh, axis, spacing=spacing_val, pieces=pieces_val)

    try:
        planes = await asyncio.to_thread(_compute) if (spacing_val is not None or pieces_val is not None) else []
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"detail": f"Unexpected error: {e}"}, status_code=400)
    return {"planes": planes, "bounds": node.mesh.bounds.tolist()}


@app.post("/sessions/{session_id}/pieces/{piece_id}/cut")
async def cut_piece_endpoint(session_id: str, piece_id: str, axis: str = Form("z"), cut_planes: str = Form("")):
    from .sessions import get_session, cut_piece
    session = get_session(session_id)
    if session is None:
        return JSONResponse({"detail": "Unknown session"}, status_code=404)

    try:
        cuts = _parse_cut_planes(cut_planes)  # reuse the existing helper — already in web.py
    except ValueError as e:
        return JSONResponse({"detail": str(e)}, status_code=400)
    if not cuts:
        return JSONResponse({"detail": "No cut planes given"}, status_code=400)

    def _do_cut():
        with session.lock:
            return cut_piece(session, piece_id, axis, cuts)

    try:
        children = await asyncio.to_thread(_do_cut)
    except KeyError as e:
        return JSONResponse({"detail": str(e)}, status_code=400)
    except CutPlacementError as e:
        return JSONResponse({"detail": str(e), "axis": e.axis, "positions": e.positions}, status_code=400)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"detail": f"Unexpected error: {e}"}, status_code=400)

    return {
        "children": [
            {"piece_id": c.id, "extents": c.mesh.extents.tolist(), "volume": float(c.mesh.volume)}
            for c in children
        ]
    }


@app.post("/sessions/{session_id}/pieces/{piece_id}/undo")
async def undo_cut_endpoint(session_id: str, piece_id: str):
    from .sessions import get_session, undo_cut
    session = get_session(session_id)
    if session is None:
        return JSONResponse({"detail": "Unknown session"}, status_code=404)
    try:
        with session.lock:
            undo_cut(session, piece_id)
    except KeyError as e:
        return JSONResponse({"detail": str(e)}, status_code=400)
    return {"status": "ok"}


@app.delete("/sessions/{session_id}")
async def delete_session_endpoint(session_id: str):
    from .sessions import close_session
    close_session(session_id)
    return {"status": "ok"}
```

**`/finish` — reuse the existing Job/SSE pattern exactly:**

```python
@app.post("/sessions/{session_id}/finish")
async def finish_session(
    session_id: str,
    peg_diameter: float = Form(7.0), peg_length: float = Form(5.0), peg_clearance: float = Form(0.18),
    n_pegs: int = Form(4), min_wall_thickness: float = Form(1.2), alignment_key: bool = Form(False),
    dowel_shape: str = Form("round"), no_connectors: bool = Form(False),
    format: str = Form("stl"), bed_size: str = Form(DEFAULT_BED_SIZE),
):
    from .sessions import get_session
    session = get_session(session_id)
    if session is None:
        return JSONResponse({"detail": "Unknown session"}, status_code=404)

    job_id = uuid.uuid4().hex
    with _JOBS_LOCK:
        _JOBS[job_id] = Job()

    connector_kwargs = None if no_connectors else dict(
        peg_diameter=peg_diameter, peg_length=peg_length, peg_clearance=peg_clearance,
        n_pegs=n_pegs, min_wall_thickness=min_wall_thickness, alignment_key=alignment_key,
        dowel_shape=dowel_shape,
    )

    def _run():
        job = _JOBS[job_id]

        def on_update(message, fraction):
            job.message, job.fraction = message, fraction

        try:
            from .connectors import add_connectors_after_cuts
            reporter = ProgressReporter(on_update, cancel_event=job.cancel_event)
            leaf_meshes = [session.pieces[pid].mesh for pid in sorted(session.leaves)]
            if connector_kwargs is not None:
                pieces, dowels = add_connectors_after_cuts(leaf_meshes, session.applied_cuts, connector_kwargs, reporter)
            else:
                pieces, dowels = leaf_meshes, []

            # From here down: IDENTICAL to _run_job's export/preview-building
            # logic below `pieces, dowels = run_pipeline(...)` — reuse that
            # code (extract it into a shared helper `_build_job_result(pieces,
            # dowels, basename, format, bed_size)` used by both `_run_job` and
            # this function, rather than copy-pasting it).
            ...
        except JobCancelled:
            job.status, job.message = "cancelled", "Cancelled"
        except Exception as e:  # noqa: BLE001
            job.status, job.error = "error", f"Unexpected error: {e}"

    threading.Thread(target=_run, daemon=True).start()
    return JSONResponse({"job_id": job_id})
```

**Steps:**
1. Implement the endpoints above (adjust to match Phase 1's actual function
   signatures once written — this plan's sketches may drift slightly from the real
   code; the real code wins).
2. **Refactor `_run_job` first**: extract everything from `basename =
   os.path.splitext(...)` through `job.fraction = 1.0` into a shared function
   `_build_job_result(job, pieces, dowels, basename, fmt, bed_size)` that both
   `_run_job` and `/finish`'s `_run` call — do this as a small, verified-behavior-
   unchanged refactor *before* wiring `/finish` to it (run the existing test suite
   after the extraction alone, confirm nothing changed, THEN add `/finish`).
3. Add a background sweep: on FastAPI startup (`@app.on_event("startup")` or a
   simple `threading.Timer`/loop thread), call `sessions.sweep_expired_sessions()`
   every few minutes.
4. Add `tests/test_session_endpoints.py` using FastAPI's `TestClient` (check
   `pyproject.toml`/existing test deps for whether `httpx`/`starlette.testclient` is
   already available — it's a FastAPI dependency so should be, but verify): create a
   session from a small `trimesh.creation.box` STL, cut it, cut one child, undo, call
   `/finish` with `no_connectors=true`, poll (or SSE-read, matching however the
   existing job tests — if any — already do this) until done, assert the right
   piece count in the result.

**Acceptance:** All Phase 1 tests still pass. New endpoint tests pass. Existing
`tests/test_pipeline.py` suite passes unchanged (the `_run_job` refactor must not
change its behavior — this is the main regression risk in this phase).

---

## Phase 3 — Frontend: interactive mode [DONE]

**Result:** Implemented as planned: `interactiveApi.js`, `interactiveState.js`
(`useInteractiveSplit()` composable), `components/InteractiveSplit.js`, and a mode
toggle in `App.js` ("Quick split" / "Interactive split (beta)", `v-if`/`v-else` so
only one mode's three.js viewers are ever live). Reused `MeshViewer`, `ProgressBar`,
`PieceGrid`, `ResultModal`, and `api.js`'s generic `streamJob` exactly as planned —
`/finish` reuses the *exact same* SSE job-progress mechanism as `/jobs`, no new
client-side polling code needed.

One gap found while building the frontend, not anticipated in the plan: the tree/cut
endpoints only returned `extents` (size), not `bounds` (min/max corners) — but the
gizmo overlay and cut-slider range need `bounds`. Fixed by adding `bounds` to
`sessions.tree_summary` and the `/cut` endpoint's per-child response (small,
backward-compatible addition — re-verified `tests/test_sessions.py` and
`tests/test_session_endpoints.py` still pass after the change).

**Verified end-to-end in a real browser** (not just "it should work"): launched a
throwaway local test server (separate port, not the user's own running instance),
used a `DataTransfer`-based JS injection to simulate a file selection (no
file-upload capability in the available browser tool), and drove the exact user
story from the original request:
1. Started a session with a 20×20×60mm box → root piece rendered, no console errors.
2. Selected root, added a cut on Z, clicked "Cut this piece" → 2×(20×20×30mm)
   pieces appeared in the grid, active piece reset to "select a piece" as designed,
   cut history showed "root — 2 piece(s)" with an Undo button.
3. Selected one of those two pieces, switched the axis selector to X, added a cut,
   clicked "Cut this piece" → 2×(10×20×30mm) grandchildren appeared alongside the
   untouched sibling (3 total current pieces) — this is the exact "cut Z, pick a
   piece, cut it on Y/X" scenario from the original request.
4. Clicked "Finish & export" → **"3 piece(s) generated. 8 dowel(s)."** with a
   working download link, connectors placed correctly across both interfaces.

No console errors and no server errors at any point in the walkthrough. Full
backend test suite (38 tests) passes throughout.

**Not yet built (left for Phase 4 or later, per the plan's explicit scope):**
Cancellation for an in-flight per-piece plane-preview/cut, session-expiry UX,
stale-tree race UI handling beyond the raw 400, and README documentation.



**Goal:** A working "Interactive split" UI, additive to the existing one.

**New files:**
- `stlsplit/static/js/interactiveApi.js` — thin fetch wrappers for the Phase 2
  endpoints, mirroring `api.js`'s style (plain functions, no Vue).
- `stlsplit/static/js/interactiveState.js` — a `useInteractiveSplit()` composable
  (mirrors `state.js`'s `useSplitForm()` shape/conventions) owning: `sessionId`,
  `tree` (piece id → {parentId, childrenIds, isLeaf, extents, volume}), `activePieceId`,
  a single-axis plane-editing sub-state for the active piece (reuse the *shape* of
  `makeAxisController`'s `state.planes` — `{value, tiltA, tiltB, hidden, active,
  errored}` — so the existing gizmo-rendering code in `viewer.js`'s `setAxisPlanes`
  can be reused as-is for the active piece, just called with only one axis
  populated instead of three), and `finish` job state (can reuse `state.js`'s `job`
  shape/`streamJob` verbatim).
- `stlsplit/static/js/components/InteractiveSplit.js` — top-level component for
  this mode: file upload + scale (reuse `InputSection.js`), active-piece 3D view
  (reuse `MeshViewer.js`, passing `axisData` with just the one active axis), a
  single-axis cut editor (a trimmed copy of `AxisPlaneEditor.js`'s template — axis
  select control, add/remove/drag cuts, "computing…" + Stop, since per-piece
  `compute_cut_planes` search time is unchanged from today and needs the same
  cancellation affordance built for the existing preview — reuse
  `cancelPlanePreview`-equivalent wiring against the new
  `/sessions/{id}/pieces/{id}/plane_preview` endpoint), a "Cut this piece" submit
  button, a leaf-piece picker (reuse `PieceGrid.js`/`PieceCard.js` — clicking a card
  sets `activePieceId` instead of opening the modal; keep an explicit "expand" icon
  per card for the existing modal-preview behavior), an "Undo last cut on this
  piece" button (only shown for pieces with children), and a "Finish" button wired
  to `/finish` + the existing `streamJob`/`ProgressBar`/`ResultModal` components
  verbatim.
- Mode toggle in `App.js`: a simple two-button toggle at the top ("Quick split" /
  "Interactive split (beta)"), each mounting its own top-level component
  (`SplitSection`+existing flow vs. `InteractiveSplit`) — `v-if`, not `v-show`, so
  an interactive session's three.js viewer(s) aren't wastefully live while the other
  mode is shown.

**Steps (in order — each should be manually verified working before the next):**
1. Build `interactiveApi.js` against Phase 2's endpoints.
2. Build `interactiveState.js`'s session lifecycle: create session on file select,
   render root piece in a `MeshViewer`. Verify: upload a file, see it rendered.
3. Add the single-axis cut editor + debounced plane-preview (mirror
   `makeAxisController`'s `fetchPreview`/`schedulePreview`/cancel-superseded pattern
   from `state.js`, scoped to the current `activePieceId` + one axis). Verify:
   gizmo planes appear/move as expected for the root piece.
4. Wire "Cut this piece" → `POST .../cut` → on success, update `tree`, set
   `activePieceId` to `null` (force explicit re-selection) or to the first child
   (either is defensible — decide and document which), render the new leaves via
   `PieceGrid`. Verify: cutting the root on Z produces 2 pieces, both rendered.
5. Wire piece selection (click a `PieceCard` → `activePieceId` = that piece; its
   mesh becomes the one shown in the main editor viewer, ready for its own
   axis/cut choice). Verify the exact scenario from the original request: cut root
   on Z, select one resulting piece, cut it on Y, see that piece's two children.
6. Wire "Undo". Verify: undo restores the pre-cut piece and removes its children
   from the visible leaf grid.
7. Wire "Finish" → `/finish` job → reuse `streamJob`/`ProgressBar`/`ResultModal`.
   Verify full round trip: multi-level interactive cuts → Finish → connectors
   present at every interface → download works.
8. Wire the mode toggle into `App.js`.

**Acceptance:** Manually walk through the exact user story from the original
request (cut Z → see pieces → pick one → cut it on Y → see its pieces → Finish →
download) end-to-end in a browser via the `run` skill or equivalent, on at least
one non-trivial mesh. Existing "Quick split" mode still works unchanged (spot-check
manually — this phase touches `App.js`, the one shared file).

---

## Phase 3.5 — Feature parity + interaction upgrades (unplanned, added after user feedback) [DONE]

**Not in the original plan** — added after the initial Phase 3 walkthrough, in
response to specific gaps found by using the tool: full parity with Quick-split's
options, plus two genuinely new interaction features (rotation, direct 3D
dragging) that don't exist in Quick-split at all.

**Backend (`stlsplit/sessions.py`, `stlsplit/web.py`):**
- `hollow_wall` param on `POST /sessions`: hollows the mesh (via `hollow.hollow_mesh`)
  right after scaling, before any cutting — matching the single-shot pipeline's
  scale→hollow→cut order. Deliberately NOT offered per-piece post-cut: hollowing an
  already-cut piece has no well-defined meaning (which of its open cut faces should
  stay solid?).
- `rotate_piece(session, piece_id, axis, degrees)` + `POST
  .../pieces/{id}/rotate`: reorients a LEAF piece in place (mutates its mesh,
  no tree/cut-group change, so it doesn't touch `applied_cuts`) — pivots around
  the piece's own bounding-box center. Only offered for pieces with no children
  (rotating a piece that's already been cut further has no sane semantics for
  its already-fixed children). Tests: swaps extents correctly for a 90° rotation,
  preserves volume/watertightness for an arbitrary angle, rejects non-leaf pieces.

**Frontend form parity (`InteractiveSplit.js`, `interactiveState.js`):**
- Scale-target axis selector (X/Y/Z) on the initial upload form — previously
  hardcoded to Z with no way to change it.
- Hollow-wall field on the initial upload form.
- Full dowel options in Finish (dowel shape, alignment key, min wall thickness —
  previously only diameter/socket-depth/clearance/per-interface were exposed).
- 3MF bed-size preset selector, shown only when format is 3MF (matches
  `OutputSection.js`'s existing options exactly).
- Rotation buttons (±90° per axis) on the active piece, wired to the new rotate
  endpoint; rotating invalidates the cached preview and re-fetches bounds/plane
  data, since both the mesh and any placed (but not yet submitted) cut planes are
  now stale relative to the new orientation.

**New interaction features (`viewer.js`, `MeshViewer.js`):**
- **Configurable print-bed overlay**: `setBedBox(dims)` renders a wireframe box
  (mm `[x, y, z]` or null to hide), centered at the same origin the mesh is framed
  to. Deliberately NOT modeling "resting on the build plate" (the mesh's true Z=0
  isn't tracked once `frame()` centers it) — it's a direct side-by-side size
  comparison, not a physical placement simulation.
- **Drag-to-move cut planes directly in the 3D view**: grab a cut plane and drag
  it along its own axis instead of only using the slider. Implemented via
  raycasting against the plane meshes (tagged with `userData = {axisName, index,
  axisIdx, center, draggable}` at creation time) and a **closest-point-between-
  two-3D-lines** construction (mouse ray vs. the infinite line through the drag
  axis) rather than the more common "camera-facing drag plane" technique — the
  latter degenerates exactly when the drag axis lines up with the camera's view
  direction, which is guaranteed to happen whenever the user clicks the X/Y/Z
  toolbar button matching the axis being dragged (a very likely combination, not
  an edge case). Only offered for untilted planes (`tiltA === tiltB === 0`),
  since the drag math assumes the plane's normal is a plain world axis — the
  interactive editor doesn't expose tilt controls at all currently, so this
  covers every cut it can actually produce.

**Verification, not just "should work":**
- Backend: 12 new/updated tests (`test_sessions.py`, `test_session_endpoints.py`)
  covering hollow, rotation (90° extent swap, arbitrary-angle volume/watertightness
  preservation, non-leaf rejection).
- Browser walkthrough found and fixed a real methodology trap, not a product bug:
  testing the drag feature from a camera view looking straight down the SAME axis
  being dragged (edge-on to the plane, near-zero screen area) made the raycast
  miss — this is exactly the degenerate case the closest-point-between-lines
  approach was chosen to avoid for the *drag itself*, but the initial *click* to
  grab the plane still needs the plane to have visible screen area, which an
  edge-on view doesn't provide. Retested from an angled (ISO) view: confirmed the
  full mechanism end-to-end — raycast hit → closest-point computation → value
  update → Vue reactivity → slider display → clamping at both bounds (drag past
  the piece's Z-extent correctly clamped to `+30`/`-30` for a 60mm box) → "Cut
  this piece" using the dragged value produced pieces of exactly the expected
  size (13mm + 47mm = 60mm, matching a cut at Z≈-17).
- Also verified: 90° rotation swaps extents exactly as predicted (20×20×60 →
  20×60×20 after rotating +90° around X), 3MF format correctly reveals the
  bed-size selector and completes a real finish job ("2 piece(s) generated. 4
  dowel(s)."), bed overlay renders alongside a live cut plane with no errors.
- Zero console errors and zero server errors throughout. Full backend suite (44
  tests) passes.

---

## Phase 4 — Polish, edge cases, cancellation parity

**Goal:** Bring the interactive mode up to the same robustness bar as the existing
flow.

**Steps:**
1. **Cancellation for an in-flight "cut this piece" or plane-preview call**: mirror
   the existing `cancel_event`/`preview_id` pattern (`stlsplit/web.py`'s
   `_PREVIEW_CANCEL_EVENTS`) for the per-piece plane-preview endpoint; the "cut"
   endpoint itself is usually fast (one `cut_mesh` call, not a search), but if it's
   ever slow on a heavy mesh, thread a `cancel_event` into it the same way
   `compute_cut_planes` already supports.
2. **Per-piece error surfacing**: `CutPlacementError` from a specific piece's cut
   should highlight that piece / that axis's cut sliders (mirror
   `state.js`'s `highlightErrors` pattern) rather than a generic alert.
3. **Session expiry UX**: if a user's session was swept (30+ min idle) and they try
   to cut/finish, surface a clear "session expired, please re-upload" message
   instead of a raw 404.
4. **Stale-tree defense**: if the same session is open in two tabs (or a request
   races with an undo), a `/cut` on a piece that's no longer a leaf must return the
   clear 400 already implemented in Phase 2 (`KeyError` → 400) — write a test for
   this specific race, not just the happy path.
5. Tests: extend `tests/test_session_endpoints.py` with cases for each of the above.
6. Documentation: add an "Interactive split" section to `README.md`'s web UI docs
   describing the new mode and its endpoints, mirroring the existing docs' style.

**Acceptance:** All tests (Phases 1–4) pass. Manual walkthrough of each edge case
above.

---

## Summary of effort by phase

| Phase | Content | Estimate |
|---|---|---|
| 1 | Backend session/piece-tree store + unit tests | 1.5–2.5 days |
| 2 | HTTP endpoints + `_run_job` refactor + endpoint tests | 2–3 days |
| 3 | Frontend interactive mode (new composable + components + mode toggle) | 3–5 days |
| 4 | Polish, cancellation parity, error surfacing, session-expiry UX, docs | 1–2 days |
| **Total** | | **~7.5–12.5 days** |

Phase 3 is the largest and least certain estimate — it depends on how much of
`AxisPlaneEditor.js`'s interaction polish (drag behavior, tilt sliders, error
highlighting) is expected to carry over to the per-piece editor vs. a simpler v1
(e.g., skip tilt support initially, add it later) — if the interactive mode's v1 can
ship *without* tilted cuts (plain axis-aligned only), subtract roughly a day from
Phase 3.
