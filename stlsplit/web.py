"""Web UI wrapping the stlsplit pipeline: upload an STL, set parameters,
preview the input and resulting pieces in-browser (three.js, via a Vue 3
SPA served from stlsplit/static/) with a live SSE progress stream, and
download the split/connected pieces as separate STLs or a single 3MF
project.

The frontend is build-free Vue 3 (loaded via ESM/CDN import, same pattern as
three.js below) rather than a Vite/npm build — see stlsplit/static/js/ and
the README's "Web UI" section for the component layout and how to extend it.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import threading
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import FastAPI, Form, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .connectors import SUPPORTED_SHAPES
from .export import BED_SIZE_PRESETS, DEFAULT_BED_SIZE, SUPPORTED_FORMATS, export_pieces
from .geometry import CutPlacementError
from .pipeline import PipelineParams, run_pipeline
from .progress import JobCancelled, ProgressReporter

_STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="stlsplit")


@app.middleware("http")
async def _no_cache_static(request, call_next):
    # StaticFiles' default response (Last-Modified/ETag, no explicit
    # Cache-Control) leaves browsers free to serve straight from their
    # heuristic cache without even a revalidation request for a little while
    # after each edit — which, empirically, produced genuinely stale JS
    # being executed seconds after a file changed on disk (confirmed via a
    # bug that only reproduced with cached code, not with a byte-identical
    # manual request). This is a locally-run tool with no real caching
    # upside, so static assets are just never cached at all.
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"
    return response


app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


@dataclass
class Job:
    status: str = "running"  # running | done | error | cancelled
    message: str = "Starting..."
    fraction: float | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    error_axis: str | None = None
    error_positions: list[float] = field(default_factory=list)
    cancel_event: threading.Event = field(default_factory=threading.Event)


_JOBS: dict[str, Job] = {}
_JOBS_LOCK = threading.Lock()

# Cancellation registry for in-flight /plane_preview computations (the
# per-axis "auto-placed cut planes" search that runs live as the user edits
# spacing/piece-count/bed-size fields). Keyed by a client-generated
# `preview_id` (one per axis's debounced request, not a stable per-axis key)
# rather than a Job-style dataclass, since a preview has no other state to
# track — just whether cancellation was requested before it finished.
_PREVIEW_CANCEL_EVENTS: dict[str, threading.Event] = {}
_PREVIEW_CANCEL_LOCK = threading.Lock()


_INDEX_HTML = """<!doctype html>
<html data-bs-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>stlsplit</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css">
<link rel="stylesheet" href="/static/css/app.css">
<script type="importmap">
{
  "imports": {
    "vue": "https://unpkg.com/vue@3/dist/vue.esm-browser.js",
    "three": "https://unpkg.com/three@0.160.0/build/three.module.js",
    "three/addons/": "https://unpkg.com/three@0.160.0/examples/jsm/"
  }
}
</script>
</head>
<body>
<div id="app" class="container-fluid py-3"></div>
<script type="module" src="/static/js/main.js"></script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return _INDEX_HTML


def _parse_float(value: str | None) -> float | None:
    if value is None or value.strip() == "":
        return None
    return float(value)


def _parse_int(value: str | None) -> int | None:
    if value is None or value.strip() == "":
        return None
    return int(value)


def _parse_cut_planes(raw: str | None):
    """Parse a comma-separated list of cuts, each 'position' or
    'position:tiltA:tiltB', into geometry.Cut objects. Returns None if
    `raw` is empty."""
    from .geometry import Cut

    if not raw or not raw.strip():
        return None
    cuts = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(":")
        if len(parts) == 1:
            cuts.append(Cut(position=float(parts[0])))
        elif len(parts) == 3:
            cuts.append(Cut(position=float(parts[0]), tilt_a=float(parts[1]), tilt_b=float(parts[2])))
        else:
            raise ValueError(f"invalid cut entry '{entry}'")
    return cuts


_MIME_TYPES = {
    "stl": "application/zip",
    "3mf": "model/3mf",
}


def _build_job_result(pieces: list, dowels: list, basename: str, fmt: str, bed_size: str) -> dict[str, Any]:
    """Build the `job.result` payload (previews + final download bytes) from
    a finished (pieces, dowels) pair. Shared between `_run_job` (the
    single-shot "Quick split" pipeline) and `finish_session` (the
    interactive-mode "Finish" step) — both end up with the same
    (pieces, dowels) shape at this point and need identical preview/export
    handling, just reached via a different route to get there."""
    # Preview data is always per-piece/per-dowel STL bytes for the three.js
    # viewer, independent of the chosen download format. Pieces and dowels
    # are exported (and previewed) separately so the piece count in each
    # preview list matches what's shown.
    piece_files = export_pieces(pieces, basename, "stl")
    previews = [
        {"name": name, "data_base64": base64.b64encode(data).decode()}
        for name, data in piece_files
    ]
    dowel_previews = []
    if dowels:
        dowel_only_files = export_pieces([], basename, "stl", dowels=dowels)
        if dowel_only_files:
            name, data = dowel_only_files[0]
            dowel_previews.append({"name": name, "data_base64": base64.b64encode(data).decode()})

    files = export_pieces(pieces, basename, fmt, dowels=dowels, bed_size=bed_size)
    if len(files) == 1:
        download_name, download_bytes = files[0]
    else:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, data in files:
                zf.writestr(name, data)
        download_name = f"{basename}_split.zip"
        download_bytes = buf.getvalue()

    return {
        "piece_count": len(pieces),
        "dowel_count": len(dowels),
        "previews": previews,
        "dowel_previews": dowel_previews,
        "download_name": download_name,
        "download_base64": base64.b64encode(download_bytes).decode(),
        "download_mime": _MIME_TYPES.get(fmt, "application/octet-stream"),
    }


def _run_job(
    job_id: str,
    raw: bytes,
    filename: str,
    params: PipelineParams,
    fmt: str,
    allow_non_watertight: bool = False,
    bed_size: str = DEFAULT_BED_SIZE,
) -> None:
    job = _JOBS[job_id]

    def on_update(message: str, fraction: float | None) -> None:
        job.message = message
        job.fraction = fraction

    try:
        mesh = load_mesh_from_bytes(raw, filename, allow_non_watertight=allow_non_watertight)
        reporter = ProgressReporter(on_update, cancel_event=job.cancel_event)
        pieces, dowels = run_pipeline(mesh, params, progress=reporter)

        basename = os.path.splitext(os.path.basename(filename))[0]
        job.result = _build_job_result(pieces, dowels, basename, fmt, bed_size)
        job.status = "done"
        job.message = "Done"
        job.fraction = 1.0
    except JobCancelled:
        job.status = "cancelled"
        job.message = "Cancelled"
    except (ValueError, RuntimeError) as e:
        job.status = "error"
        job.error = str(e)
        if isinstance(e, CutPlacementError):
            job.error_axis = e.axis
            job.error_positions = e.positions
    except Exception as e:  # noqa: BLE001 - surface unexpected errors to the client instead of hanging the poll
        job.status = "error"
        job.error = f"Unexpected error: {e}"


@app.post("/jobs")
async def create_job(
    file: UploadFile,
    axis: str = Form("z"),
    scale: str | None = Form(None),
    target_dim: str | None = Form(None),
    cut_planes_x: str | None = Form(None),
    cut_planes_y: str | None = Form(None),
    cut_planes_z: str | None = Form(None),
    axis_order: str = Form("zxy"),
    bed_x: str | None = Form(None),
    bed_y: str | None = Form(None),
    bed_z: str | None = Form(None),
    peg_diameter: float = Form(7.0),
    peg_length: float = Form(5.0),
    peg_clearance: float = Form(0.18),
    n_pegs: int = Form(4),
    min_wall_thickness: float = Form(1.2),
    alignment_key: bool = Form(False),
    dowel_shape: str = Form("round"),
    no_connectors: bool = Form(False),
    hollow_wall: str | None = Form(None),
    allow_non_watertight: bool = Form(False),
    allow_floating_regions: bool = Form(False),
    format: str = Form("stl"),
    bed_size: str = Form(DEFAULT_BED_SIZE),
):
    if format not in SUPPORTED_FORMATS:
        return JSONResponse({"detail": f"Unsupported format '{format}'"}, status_code=400)
    if dowel_shape not in SUPPORTED_SHAPES:
        return JSONResponse({"detail": f"Unsupported dowel shape '{dowel_shape}'"}, status_code=400)
    if bed_size not in BED_SIZE_PRESETS:
        return JSONResponse({"detail": f"Unsupported bed_size '{bed_size}'"}, status_code=400)

    bed_dims = {}
    bx, by, bz = _parse_float(bed_x), _parse_float(bed_y), _parse_float(bed_z)
    if bx is not None:
        bed_dims["x"] = bx
    if by is not None:
        bed_dims["y"] = by
    if bz is not None:
        bed_dims["z"] = bz

    axis_cuts: dict[str, list] = {}
    for axis_name, raw_planes in (("x", cut_planes_x), ("y", cut_planes_y), ("z", cut_planes_z)):
        try:
            parsed = _parse_cut_planes(raw_planes)
        except ValueError:
            return JSONResponse({"detail": f"Invalid cut_planes_{axis_name} value '{raw_planes}'"}, status_code=400)
        if parsed is not None:
            axis_cuts[axis_name] = parsed

    params = PipelineParams(
        axis=axis,
        scale=_parse_float(scale),
        target_dim=_parse_float(target_dim),
        # Passed as-is (never collapsed to None when empty): the web UI
        # always operates in per-axis explicit-cut mode now, including the
        # legitimate "nothing needs cutting on any axis" case (e.g. the mesh
        # already fits the bed dimensions used to seed the plane editor) —
        # see stlsplit/static/js/state.js's buildJobFormData. An empty dict
        # here is a real "explicit zero cuts" request, distinct from `None`
        # meaning "multi-axis mode wasn't used at all" (still relevant for
        # non-web callers of PipelineParams, e.g. the CLI).
        axis_cuts=axis_cuts,
        axis_order=axis_order,
        bed_dims=bed_dims,
        peg_diameter=peg_diameter,
        peg_length=peg_length,
        peg_clearance=peg_clearance,
        n_pegs=n_pegs,
        min_wall_thickness=min_wall_thickness,
        alignment_key=alignment_key,
        dowel_shape=dowel_shape,
        no_connectors=no_connectors,
        hollow_wall_thickness=_parse_float(hollow_wall),
        allow_floating_regions=allow_floating_regions,
    )

    try:
        params.validate()
    except ValueError as e:
        return JSONResponse({"detail": str(e)}, status_code=400)

    raw = await file.read()
    filename = file.filename or "input.stl"

    job_id = uuid.uuid4().hex
    with _JOBS_LOCK:
        _JOBS[job_id] = Job()

    thread = threading.Thread(
        target=_run_job,
        args=(job_id, raw, filename, params, format, allow_non_watertight, bed_size),
        daemon=True,
    )
    thread.start()

    return JSONResponse({"job_id": job_id})


@app.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str):
    """Requests cancellation of a running job. Cooperative, not immediate:
    `_run_job`'s background thread only notices at its next ProgressReporter
    checkpoint (the next cut, connector interface, or piece — see
    JobCancelled), so a very long single boolean op already in flight still
    has to finish first; the per-piece ProcessPoolExecutor branch in
    `_cut_all_axes` is the one exception, since it can actually tear down and
    stop its worker processes rather than just unwinding a call stack.
    Returns 404 if the job doesn't exist, but always 200 (not an error) if
    the job already finished by the time this arrives — cancelling something
    that's already done/failed is a no-op, not a client mistake."""
    job = _JOBS.get(job_id)
    if job is None:
        return JSONResponse({"detail": "Unknown job id"}, status_code=404)
    if job.status == "running":
        job.cancel_event.set()
    return JSONResponse({"status": job.status})


@app.post("/plane_preview")
async def plane_preview(
    file: UploadFile,
    axis: str = Form("z"),
    scale_axis: str | None = Form(None),
    scale: str | None = Form(None),
    target_dim: str | None = Form(None),
    spacing: str | None = Form(None),
    pieces: str | None = Form(None),
    preview_id: str | None = Form(None),
):
    """Compute (smart, auto-placed) cut plane positions for the interactive
    editor, without running the full split/connector pipeline. Used so the
    UI can render plane gizmos over the input mesh and let the user drag
    them before actually submitting the job. `axis` is the axis being split
    on; `scale_axis` (defaults to `axis`) is the axis --target-dim scales
    against, so calling this once per split axis with a fixed `scale_axis`
    yields a consistent scale factor across all of them.

    The actual mesh work below is synchronous, CPU-bound trimesh/numpy code
    with no `await` points of its own — run inline in this coroutine, it
    would block uvicorn's single event loop for however long
    compute_cut_planes takes (which, for a complex mesh with a pinch-avoidance
    search, can be tens of seconds). Since the frontend fires one of these
    requests per axis, back-to-back, that meant one slow axis blocked every
    other axis's (otherwise fast) request, and even the SSE job-progress
    stream, until it finished. Offloading to a thread via `asyncio.to_thread`
    lets them actually run concurrently instead of queuing behind each other.

    `preview_id`, if given, registers a cancellable slot for this specific
    computation (see `_PREVIEW_CANCEL_EVENTS` and `cancel_plane_preview`
    below) — the frontend generates one per debounced request so it can stop
    a long-running search directly (via the "Stop" control next to the
    "computing…" spinner) instead of only ever discarding the eventual,
    still-running-in-the-background result."""
    from .geometry import axis_index, compute_cut_planes, scale_mesh

    scale_ref_axis = scale_axis or axis
    scale_val, target_dim_val = _parse_float(scale), _parse_float(target_dim)
    spacing_val, pieces_val = _parse_float(spacing), _parse_int(pieces)
    if spacing_val is not None and pieces_val is not None:
        return JSONResponse({"detail": "specify only one of spacing or pieces"}, status_code=400)

    raw = await file.read()
    filename = file.filename or "input.stl"

    cancel_event = None
    if preview_id is not None:
        cancel_event = threading.Event()
        with _PREVIEW_CANCEL_LOCK:
            _PREVIEW_CANCEL_EVENTS[preview_id] = cancel_event

    def _compute():
        mesh = load_mesh_from_bytes(raw, filename, allow_non_watertight=True)
        pre_extent = float(mesh.extents[axis_index(scale_ref_axis)])
        mesh = scale_mesh(mesh, scale=scale_val, target_dim=target_dim_val, axis=scale_ref_axis)
        # With neither spacing nor pieces given, just return bounds/scale so
        # the UI can size a manual editor; no auto-computed planes to offer.
        planes = (
            compute_cut_planes(mesh, axis, spacing=spacing_val, pieces=pieces_val, cancel_event=cancel_event)
            if spacing_val is not None or pieces_val is not None
            else []
        )
        return mesh, pre_extent, planes

    try:
        mesh, pre_extent, planes = await asyncio.to_thread(_compute)
    except JobCancelled:
        return {"cancelled": True}
    except ValueError as e:
        return JSONResponse({"detail": str(e)}, status_code=400)
    except Exception as e:  # noqa: BLE001 - surface unexpected errors instead of a bare 500
        return JSONResponse({"detail": f"Unexpected error: {e}"}, status_code=400)
    finally:
        if preview_id is not None:
            with _PREVIEW_CANCEL_LOCK:
                _PREVIEW_CANCEL_EVENTS.pop(preview_id, None)

    if scale_val is not None:
        scale_factor = scale_val
    elif target_dim_val is not None and pre_extent > 0:
        scale_factor = target_dim_val / pre_extent
    else:
        scale_factor = 1.0

    return {
        "axis": axis,
        "planes": planes,
        "auto": spacing_val is not None or pieces_val is not None,
        "bounds": mesh.bounds.tolist(),
        "scale_factor": scale_factor,
    }


@app.post("/plane_preview/{preview_id}/cancel")
async def cancel_plane_preview(preview_id: str):
    """Requests cancellation of one in-flight /plane_preview computation.
    Cooperative, like job cancellation (see `cancel_job` below) — flips the
    matching event and returns immediately; the preview's own response
    (`{"cancelled": true}`) is what tells the caller it actually stopped.
    A no-op (still 200) if `preview_id` is unknown, e.g. the computation
    already finished by the time this arrives."""
    with _PREVIEW_CANCEL_LOCK:
        event = _PREVIEW_CANCEL_EVENTS.get(preview_id)
    if event is not None:
        event.set()
    return {"status": "ok"}


# --- Interactive split mode: per-piece cut-tree sessions -------------------
#
# Unlike /jobs (upload once, configure everything, submit once, get final
# pieces back), this mode holds a whole cut TREE server-side across several
# requests: cut one piece, look at its children, pick one, cut IT
# differently, etc. See stlsplit/sessions.py for the tree/undo bookkeeping
# itself; everything here is thin HTTP plumbing over that.


def _sweep_sessions_periodically() -> None:
    from .sessions import sweep_expired_sessions

    sweep_expired_sessions()
    timer = threading.Timer(300.0, _sweep_sessions_periodically)
    timer.daemon = True  # never blocks process exit (e.g. during tests importing this module)
    timer.start()


_sweep_sessions_periodically()


@app.post("/sessions")
async def create_session_endpoint(
    file: UploadFile,
    scale: str | None = Form(None),
    target_dim: str | None = Form(None),
    scale_axis: str = Form("z"),
    hollow_wall: str | None = Form(None),
    allow_non_watertight: bool = Form(False),
):
    """Upload + scale (+ optionally hollow) a mesh, creating a new
    interactive session whose root piece is the result. Returns piece
    summary + scale_factor; the mesh preview itself is fetched separately
    via GET .../pieces/{piece_id}/preview (kept out of this response since a
    client that already knows it doesn't need it yet shouldn't have to pay
    for it).

    `hollow_wall`, if given, hollows the mesh (see hollow.hollow_mesh) right
    after scaling and before any cutting happens — matching the single-shot
    pipeline's order (scale, then hollow, then cut) and the fact that
    hollowing an already-cut piece is a much stranger operation to reason
    about (which of its now-open cut faces should stay solid?) than hollowing
    the one, still-whole starting mesh."""
    from .geometry import axis_index, scale_mesh
    from .hollow import hollow_mesh
    from .sessions import create_session

    raw = await file.read()
    filename = file.filename or "input.stl"
    scale_val, target_dim_val = _parse_float(scale), _parse_float(target_dim)
    hollow_val = _parse_float(hollow_wall)

    def _prepare():
        mesh = load_mesh_from_bytes(raw, filename, allow_non_watertight=allow_non_watertight)
        pre_extent = float(mesh.extents[axis_index(scale_axis)])
        mesh = scale_mesh(mesh, scale=scale_val, target_dim=target_dim_val, axis=scale_axis)
        if hollow_val is not None:
            mesh = hollow_mesh(mesh, hollow_val)
        return mesh, pre_extent

    try:
        mesh, pre_extent = await asyncio.to_thread(_prepare)
    except (ValueError, RuntimeError) as e:
        return JSONResponse({"detail": str(e)}, status_code=400)

    if scale_val is not None:
        scale_factor = scale_val
    elif target_dim_val is not None and pre_extent > 0:
        scale_factor = target_dim_val / pre_extent
    else:
        scale_factor = 1.0

    create_params = {
        "scale": scale_val,
        "target_dim": target_dim_val,
        "scale_axis": scale_axis,
        "hollow_wall": hollow_val,
        "allow_non_watertight": allow_non_watertight,
    }
    session = create_session(mesh, filename, scale_factor, create_params=create_params)
    root = session.pieces["root"]
    return {
        "session_id": session.id,
        "piece_id": "root",
        "extents": root.mesh.extents.tolist(),
        "volume": float(root.mesh.volume),
        "bounds": root.mesh.bounds.tolist(),
        "scale_factor": scale_factor,
    }


@app.get("/sessions/{session_id}")
async def session_tree(session_id: str):
    from .sessions import get_session, tree_summary

    session = get_session(session_id)
    if session is None:
        return JSONResponse({"detail": "Unknown or expired session"}, status_code=404)
    return tree_summary(session)


@app.get("/sessions/{session_id}/export")
async def export_session_endpoint(session_id: str):
    """Lightweight, downloadable JSON snapshot of everything needed to
    reconstruct this session later via POST /sessions/resume: the
    scale/hollow parameters used to build the root piece, plus the full
    cut/rotate/align/undo history (see sessions.replay_history). Deliberately
    excludes all mesh data -- resuming re-derives every piece's geometry by
    replaying against a re-uploaded copy of the ORIGINAL file, which the
    caller must keep track of separately (this alone can't resume anything;
    see the frontend's Export/Resume flow, which pairs it with a reminder to
    keep the source STL)."""
    from .sessions import get_session

    session = get_session(session_id)
    if session is None:
        return JSONResponse({"detail": "Unknown or expired session"}, status_code=404)
    with session.lock:
        payload = {
            "format": "stlsplit-session-v1",
            "original_filename": session.filename,
            "create_params": session.create_params,
            "history": session.history,
        }
    basename = os.path.splitext(os.path.basename(session.filename))[0]
    body = json.dumps(payload, indent=2).encode("utf-8")
    return StreamingResponse(
        io.BytesIO(body),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{basename}_session.json"'},
    )


@app.post("/sessions/resume")
async def resume_session_endpoint(file: UploadFile, session_json: UploadFile):
    """Recreates a session from an original STL plus a JSON snapshot
    downloaded from GET /sessions/{id}/export: re-runs the exact same
    scale/hollow steps create_session_endpoint used originally (from
    `create_params`), then replays every recorded cut/rotate/align/undo in
    order (sessions.replay_history) to rebuild the identical piece tree.
    `file` must be the SAME original STL the session was created from --
    replay reproduces geometry deterministically from it, not from anything
    persisted about the pieces themselves, so a different file produces a
    different (likely broken) tree."""
    from .geometry import axis_index, scale_mesh
    from .hollow import hollow_mesh
    from .sessions import create_session, replay_history, tree_summary

    raw = await file.read()
    filename = file.filename or "input.stl"
    try:
        payload = json.loads(await session_json.read())
    except json.JSONDecodeError:
        return JSONResponse({"detail": "session_json is not valid JSON"}, status_code=400)
    if payload.get("format") != "stlsplit-session-v1":
        return JSONResponse({"detail": "Unrecognized session file format"}, status_code=400)

    params = payload.get("create_params", {})
    history = payload.get("history", [])

    def _prepare():
        mesh = load_mesh_from_bytes(raw, filename, allow_non_watertight=params.get("allow_non_watertight", False))
        pre_extent = float(mesh.extents[axis_index(params.get("scale_axis", "z"))])
        mesh = scale_mesh(
            mesh, scale=params.get("scale"), target_dim=params.get("target_dim"), axis=params.get("scale_axis", "z")
        )
        if params.get("hollow_wall") is not None:
            mesh = hollow_mesh(mesh, params["hollow_wall"])
        return mesh, pre_extent

    try:
        mesh, pre_extent = await asyncio.to_thread(_prepare)
    except (ValueError, RuntimeError) as e:
        return JSONResponse({"detail": f"Could not reproduce the original starting mesh: {e}"}, status_code=400)

    if params.get("scale") is not None:
        scale_factor = params["scale"]
    elif params.get("target_dim") is not None and pre_extent > 0:
        scale_factor = params["target_dim"] / pre_extent
    else:
        scale_factor = 1.0

    session = create_session(mesh, filename, scale_factor, create_params=params)
    try:
        cut_order = await asyncio.to_thread(replay_history, session, history)
    except (KeyError, ValueError) as e:
        from .sessions import close_session

        close_session(session.id)
        return JSONResponse({"detail": f"session_json doesn't match this STL (replay failed: {e})"}, status_code=400)

    summary = tree_summary(session)
    summary["scale_factor"] = scale_factor
    summary["cut_order"] = cut_order
    return summary


@app.get("/sessions/{session_id}/pieces/{piece_id}/preview")
async def piece_preview(session_id: str, piece_id: str):
    """STL bytes (base64) for one piece — fetched on demand rather than
    every session mutation pushing full mesh bytes for every piece it
    touches."""
    from .sessions import get_session

    session = get_session(session_id)
    if session is None:
        return JSONResponse({"detail": "Unknown or expired session"}, status_code=404)
    node = session.pieces.get(piece_id)
    if node is None:
        return JSONResponse({"detail": "Unknown piece"}, status_code=404)
    data = export_pieces([node.mesh], "piece", "stl")[0][1]
    return {"data_base64": base64.b64encode(data).decode()}


@app.post("/sessions/{session_id}/pieces/{piece_id}/plane_preview")
async def piece_plane_preview(
    session_id: str,
    piece_id: str,
    axis: str = Form("z"),
    spacing: str | None = Form(None),
    pieces: str | None = Form(None),
):
    """Auto-placed cut plane positions for one specific (already-in-session)
    piece — the per-piece analogue of the top-level /plane_preview, but
    scoped to a piece that already lives in the session instead of
    re-uploading/re-scaling the whole file each time."""
    from .geometry import compute_cut_planes
    from .sessions import get_session

    session = get_session(session_id)
    if session is None:
        return JSONResponse({"detail": "Unknown or expired session"}, status_code=404)
    node = session.pieces.get(piece_id)
    if node is None or piece_id not in session.leaves:
        return JSONResponse({"detail": "Piece is not a current leaf"}, status_code=400)

    spacing_val, pieces_val = _parse_float(spacing), _parse_int(pieces)
    if spacing_val is not None and pieces_val is not None:
        return JSONResponse({"detail": "specify only one of spacing or pieces"}, status_code=400)

    def _compute():
        return compute_cut_planes(node.mesh, axis, spacing=spacing_val, pieces=pieces_val)

    try:
        planes = (
            await asyncio.to_thread(_compute)
            if (spacing_val is not None or pieces_val is not None)
            else []
        )
    except Exception as e:  # noqa: BLE001 - surface unexpected errors instead of a bare 500
        return JSONResponse({"detail": f"Unexpected error: {e}"}, status_code=400)

    return {"planes": planes, "bounds": node.mesh.bounds.tolist()}


@app.post("/sessions/{session_id}/pieces/{piece_id}/cut")
async def cut_piece_endpoint(
    session_id: str,
    piece_id: str,
    axis: str = Form("z"),
    cut_planes: str = Form(""),
    allow_floating_regions: bool = Form(False),
):
    from .sessions import cut_piece, get_session

    session = get_session(session_id)
    if session is None:
        return JSONResponse({"detail": "Unknown or expired session"}, status_code=404)

    try:
        cuts = _parse_cut_planes(cut_planes)
    except ValueError:
        return JSONResponse({"detail": f"Invalid cut_planes value '{cut_planes}'"}, status_code=400)
    if not cuts:
        return JSONResponse({"detail": "No cut planes given"}, status_code=400)

    def _do_cut():
        with session.lock:
            return cut_piece(session, piece_id, axis, cuts, allow_floating_regions=allow_floating_regions)

    try:
        children = await asyncio.to_thread(_do_cut)
    except KeyError as e:
        return JSONResponse({"detail": str(e).strip("'\"")}, status_code=400)
    except CutPlacementError as e:
        return JSONResponse({"detail": str(e), "axis": e.axis, "positions": e.positions}, status_code=400)
    except Exception as e:  # noqa: BLE001 - surface unexpected errors instead of a bare 500
        return JSONResponse({"detail": f"Unexpected error: {e}"}, status_code=400)

    return {
        "children": [
            {
                "piece_id": c.id,
                "extents": c.mesh.extents.tolist(),
                "volume": float(c.mesh.volume),
                "bounds": c.mesh.bounds.tolist(),
            }
            for c in children
        ]
    }


@app.post("/sessions/{session_id}/pieces/{piece_id}/undo")
async def undo_cut_endpoint(session_id: str, piece_id: str):
    from .sessions import get_session, undo_cut

    session = get_session(session_id)
    if session is None:
        return JSONResponse({"detail": "Unknown or expired session"}, status_code=404)
    try:
        with session.lock:
            undo_cut(session, piece_id)
    except KeyError as e:
        return JSONResponse({"detail": str(e).strip("'\"")}, status_code=400)
    return {"status": "ok"}


@app.post("/sessions/{session_id}/pieces/{piece_id}/rotate")
async def rotate_piece_endpoint(session_id: str, piece_id: str, axis: str = Form("z"), degrees: float = Form(90.0)):
    """Reorients a leaf piece in place (no cut, no tree change) -- lets the
    user reorient a piece for easier cutting or printing without it counting
    as a cut interface. See sessions.rotate_piece."""
    from .sessions import get_session, rotate_piece

    session = get_session(session_id)
    if session is None:
        return JSONResponse({"detail": "Unknown or expired session"}, status_code=404)
    try:
        with session.lock:
            node = rotate_piece(session, piece_id, axis, degrees)
    except KeyError as e:
        return JSONResponse({"detail": str(e).strip("'\"")}, status_code=400)
    except Exception as e:  # noqa: BLE001 - surface unexpected errors instead of a bare 500
        return JSONResponse({"detail": f"Unexpected error: {e}"}, status_code=400)

    return {
        "piece_id": node.id,
        "extents": node.mesh.extents.tolist(),
        "volume": float(node.mesh.volume),
        "bounds": node.mesh.bounds.tolist(),
    }


@app.post("/sessions/{session_id}/pieces/{piece_id}/align")
async def align_piece_endpoint(session_id: str, piece_id: str):
    """Rotates a leaf piece so the cut face that produced it snaps to its
    nearest world axis -- meant for pieces from a tilted cut, whose AABB
    otherwise reports an inflated footprint. See sessions.align_piece_to_cut."""
    from .sessions import align_piece_to_cut, get_session

    session = get_session(session_id)
    if session is None:
        return JSONResponse({"detail": "Unknown or expired session"}, status_code=404)
    try:
        with session.lock:
            node = align_piece_to_cut(session, piece_id)
    except KeyError as e:
        return JSONResponse({"detail": str(e).strip("'\"")}, status_code=400)
    except Exception as e:  # noqa: BLE001 - surface unexpected errors instead of a bare 500
        return JSONResponse({"detail": f"Unexpected error: {e}"}, status_code=400)

    return {
        "piece_id": node.id,
        "extents": node.mesh.extents.tolist(),
        "volume": float(node.mesh.volume),
        "bounds": node.mesh.bounds.tolist(),
    }


@app.delete("/sessions/{session_id}")
async def delete_session_endpoint(session_id: str):
    from .sessions import close_session

    close_session(session_id)
    return {"status": "ok"}


@app.post("/sessions/{session_id}/finish")
async def finish_session(
    session_id: str,
    peg_diameter: float = Form(7.0),
    peg_length: float = Form(5.0),
    peg_clearance: float = Form(0.18),
    n_pegs: int = Form(4),
    min_wall_thickness: float = Form(1.2),
    alignment_key: bool = Form(False),
    dowel_shape: str = Form("round"),
    no_connectors: bool = Form(False),
    format: str = Form("stl"),
    bed_size: str = Form(DEFAULT_BED_SIZE),
):
    """Places connectors across the WHOLE cut tree in one pass (see
    `connectors.add_connectors_after_cuts`, which already handles an
    arbitrary multi-level tree given every ResolvedCut applied anywhere in
    it — the same mechanism that fixed a real bug where eager per-axis
    connector placement could get sliced through by a later cut) and
    exports, reusing the exact Job/SSE progress mechanism /jobs uses so the
    frontend's existing streamJob/ProgressBar/ResultModal work unchanged."""
    from .connectors import add_connectors_after_cuts
    from .sessions import get_session

    if format not in SUPPORTED_FORMATS:
        return JSONResponse({"detail": f"Unsupported format '{format}'"}, status_code=400)
    if dowel_shape not in SUPPORTED_SHAPES:
        return JSONResponse({"detail": f"Unsupported dowel shape '{dowel_shape}'"}, status_code=400)
    if bed_size not in BED_SIZE_PRESETS:
        return JSONResponse({"detail": f"Unsupported bed_size '{bed_size}'"}, status_code=400)

    session = get_session(session_id)
    if session is None:
        return JSONResponse({"detail": "Unknown or expired session"}, status_code=404)

    connector_kwargs = None if no_connectors else dict(
        peg_diameter=peg_diameter, peg_length=peg_length, peg_clearance=peg_clearance,
        n_pegs=n_pegs, min_wall_thickness=min_wall_thickness, alignment_key=alignment_key,
        dowel_shape=dowel_shape,
    )

    job_id = uuid.uuid4().hex
    with _JOBS_LOCK:
        _JOBS[job_id] = Job()

    def _run():
        job = _JOBS[job_id]

        def on_update(message: str, fraction: float | None) -> None:
            job.message, job.fraction = message, fraction

        try:
            reporter = ProgressReporter(on_update, cancel_event=job.cancel_event)
            with session.lock:
                leaf_ids = sorted(session.leaves)
                leaf_nodes = [session.pieces[pid] for pid in leaf_ids]
                # `applied_cuts` records each cut plane in the frame it was
                # cut in -- a leaf later reoriented via rotate/align (see
                # PieceNode.display_rotation) no longer has its cut faces
                # sitting on that recorded plane, so connector placement
                # must run against each leaf's UN-rotated geometry (undoing
                # display_rotation) rather than its current, possibly
                # reoriented `mesh`. Pieces never reoriented have an
                # identity display_rotation, so this is a no-op for them.
                canonical_meshes = []
                for node in leaf_nodes:
                    if np.allclose(node.display_rotation, np.eye(4)):
                        canonical_meshes.append(node.mesh)
                    else:
                        canonical = node.mesh.copy()
                        canonical.apply_transform(np.linalg.inv(node.display_rotation))
                        canonical_meshes.append(canonical)
                applied_cuts = list(session.applied_cuts)
            if connector_kwargs is not None:
                pieces, dowels = add_connectors_after_cuts(canonical_meshes, applied_cuts, connector_kwargs, reporter)
            else:
                pieces, dowels = canonical_meshes, []

            # Re-apply each leaf's display rotation now that sockets are
            # carved in the canonical frame -- brings every piece back to
            # the orientation the user last saw/set it to in the editor.
            reoriented = []
            for node, piece in zip(leaf_nodes, pieces):
                if np.allclose(node.display_rotation, np.eye(4)):
                    reoriented.append(piece)
                else:
                    piece = piece.copy()
                    piece.apply_transform(node.display_rotation)
                    reoriented.append(piece)
            pieces = reoriented

            basename = os.path.splitext(os.path.basename(session.filename))[0]
            job.result = _build_job_result(pieces, dowels, basename, format, bed_size)
            job.status = "done"
            job.message = "Done"
            job.fraction = 1.0
        except JobCancelled:
            job.status = "cancelled"
            job.message = "Cancelled"
        except Exception as e:  # noqa: BLE001 - surface unexpected errors to the client instead of hanging the poll
            job.status = "error"
            job.error = f"Unexpected error: {e}"

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return JSONResponse({"job_id": job_id})


def _job_payload(job: Job) -> dict[str, Any]:
    if job.status == "running":
        return {"status": "running", "message": job.message, "fraction": job.fraction}
    if job.status == "error":
        return {
            "status": "error",
            "error": job.error,
            "error_axis": job.error_axis,
            "error_positions": job.error_positions,
        }
    if job.status == "cancelled":
        return {"status": "cancelled"}
    return {"status": "done", "result": job.result}


@app.get("/jobs/{job_id}/stream")
async def stream_job(job_id: str):
    """Server-Sent Events progress stream for a job — replaces the old
    client-side setTimeout poll loop against a plain GET endpoint. The
    server still only has an in-memory Job dict updated by a background
    thread (see _run_job), so this is internally a sleep-loop watching that
    dict for changes; SSE just turns that into a single push-style
    connection from the browser's perspective (via EventSource) instead of
    a new HTTP request every 400ms. Closes itself after the first
    done/error message."""

    async def event_stream():
        job = _JOBS.get(job_id)
        if job is None:
            yield f"data: {json.dumps({'status': 'error', 'error': 'Unknown job id'})}\n\n"
            return
        last_sent: tuple[str, str | None, float | None] | None = None
        while True:
            payload = _job_payload(job)
            key = (payload["status"], payload.get("message"), payload.get("fraction"))
            if key != last_sent:
                yield f"data: {json.dumps(payload)}\n\n"
                last_sent = key
            if payload["status"] != "running":
                return
            await asyncio.sleep(0.25)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


def load_mesh_from_bytes(raw: bytes, filename: str, allow_non_watertight: bool = False):
    import trimesh

    from .geometry import repair_watertight

    ext = os.path.splitext(filename)[1].lstrip(".") or "stl"
    mesh = trimesh.load_mesh(io.BytesIO(raw), file_type=ext)
    if isinstance(mesh, trimesh.Scene):
        mesh = trimesh.util.concatenate([g for g in mesh.geometry.values()])
    if not mesh.is_watertight:
        repair_watertight(mesh)
    if not mesh.is_watertight and not allow_non_watertight:
        raise ValueError(
            f"Uploaded mesh '{filename}' is not watertight; cannot safely perform boolean ops. "
            "Enable 'Allow non-watertight mesh' to proceed anyway (boolean cuts/connectors may fail or produce bad geometry)."
        )
    return mesh


def run() -> None:
    import uvicorn

    uvicorn.run("stlsplit.web:app", host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":
    run()
