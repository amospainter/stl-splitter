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

        # Preview data is always per-piece/per-dowel STL bytes for the
        # three.js viewer, independent of the chosen download format. Pieces
        # and dowels are exported (and previewed) separately so the piece
        # count in each preview list matches what's shown.
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

        job.result = {
            "piece_count": len(pieces),
            "dowel_count": len(dowels),
            "previews": previews,
            "dowel_previews": dowel_previews,
            "download_name": download_name,
            "download_base64": base64.b64encode(download_bytes).decode(),
            "download_mime": _MIME_TYPES.get(fmt, "application/octet-stream"),
        }
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
    lets them actually run concurrently instead of queuing behind each other."""
    from .geometry import axis_index, compute_cut_planes, scale_mesh

    scale_ref_axis = scale_axis or axis
    scale_val, target_dim_val = _parse_float(scale), _parse_float(target_dim)
    spacing_val, pieces_val = _parse_float(spacing), _parse_int(pieces)
    if spacing_val is not None and pieces_val is not None:
        return JSONResponse({"detail": "specify only one of spacing or pieces"}, status_code=400)

    raw = await file.read()
    filename = file.filename or "input.stl"

    def _compute():
        mesh = load_mesh_from_bytes(raw, filename, allow_non_watertight=True)
        pre_extent = float(mesh.extents[axis_index(scale_ref_axis)])
        mesh = scale_mesh(mesh, scale=scale_val, target_dim=target_dim_val, axis=scale_ref_axis)
        # With neither spacing nor pieces given, just return bounds/scale so
        # the UI can size a manual editor; no auto-computed planes to offer.
        planes = (
            compute_cut_planes(mesh, axis, spacing=spacing_val, pieces=pieces_val)
            if spacing_val is not None or pieces_val is not None
            else []
        )
        return mesh, pre_extent, planes

    try:
        mesh, pre_extent, planes = await asyncio.to_thread(_compute)
    except ValueError as e:
        return JSONResponse({"detail": str(e)}, status_code=400)
    except Exception as e:  # noqa: BLE001 - surface unexpected errors instead of a bare 500
        return JSONResponse({"detail": f"Unexpected error: {e}"}, status_code=400)

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
