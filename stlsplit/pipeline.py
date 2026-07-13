"""Shared pipeline logic used by both the CLI and the web UI."""
from __future__ import annotations

import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field

import trimesh

from .autofit import auto_fit_split
from .connectors import SUPPORTED_SHAPES, add_connectors
from .cutting import cut_mesh
from .geometry import Cut, CutPlacementError, axis_index, compute_cut_planes, resolve_cuts, scale_mesh
from .hollow import hollow_mesh
from .progress import ProgressReporter

# Below this piece count, spinning up a process pool (importing trimesh/
# manifold3d/shapely fresh in each worker process, roughly 0.5-1s per worker)
# costs more than it saves — parallelism only pays off once there are enough
# independent pieces to actually spread across cores.
_MIN_PIECES_FOR_PARALLEL = 2


def _cut_mesh_or_raise(mesh: trimesh.Trimesh, resolved, axis: str, progress: ProgressReporter | None):
    """cut_mesh, but on failure fills in which axis and cut position(s) are
    responsible before re-raising, so the caller can point the user at the
    specific cut instead of just a piece number."""
    try:
        return cut_mesh(mesh, resolved, progress=progress)
    except CutPlacementError as e:
        _annotate_cut_error(e, resolved, axis)
        raise


def _annotate_cut_error(e: CutPlacementError, resolved, axis: str) -> None:
    e.axis = axis
    e.positions = [resolved[i].position for i in (e.piece_index - 1, e.piece_index) if 0 <= i < len(resolved)]
    pos_str = " or ".join(f"{p:.2f}mm" for p in e.positions)
    location = f" (axis {axis}, cut position {pos_str})" if pos_str else f" (axis {axis})"
    e.message = f"{e.message}{location} Try moving that cut, changing its tilt, or picking a different axis."


def _process_one_piece(
    vertices,
    faces,
    axis: str,
    planes: list,
    connector_kwargs: dict | None,
):
    """Worker body for splitting a single top-level piece on one axis (cut +
    its own connectors), run in a separate process. Takes/returns plain
    vertex/face arrays rather than `trimesh.Trimesh` objects: a `Trimesh`
    can carry cached, unpicklable state (e.g. a ray-query engine) once
    something downstream has touched it, and the plain-array round trip
    sidesteps that entirely rather than depending on what happens to be
    picklable today. Runs the *entire* per-piece workload (cut, then
    connectors on the resulting sub-pieces) in one process, since piece i's
    cut/connector work never touches piece j's — the same independence the
    sequential loop already relied on, just executed off the main process."""
    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
    idx = axis_index(axis)
    lo, hi = mesh.bounds[0][idx], mesh.bounds[1][idx]
    relevant = [p for p in planes if lo < Cut.coerce(p).position < hi]
    resolved = resolve_cuts(mesh, axis, relevant)
    try:
        sub_pieces = cut_mesh(mesh, resolved)
    except CutPlacementError as e:
        _annotate_cut_error(e, resolved, axis)
        raise
    dowels: list[trimesh.Trimesh] = []
    if resolved and connector_kwargs is not None:
        sub_pieces, dowels = add_connectors(sub_pieces, resolved, **connector_kwargs)
    return (
        [(p.vertices, p.faces) for p in sub_pieces],
        [(d.vertices, d.faces) for d in dowels],
    )


@dataclass
class PipelineParams:
    axis: str = "z"  # reference axis for --scale/--target-dim; unused when axis_cuts is set
    scale: float | None = None
    target_dim: float | None = None
    spacing: float | None = None
    pieces: int | None = None
    cut_planes: list["Cut | float"] | None = None
    axis_cuts: dict[str, list["Cut | float"]] | None = None  # e.g. {"x": [...], "z": [...]} for splitting on multiple axes
    axis_order: str = "zxy"  # order to apply axis_cuts in when multiple axes are set; a permutation of "xyz"
    bed_dims: dict[str, float] = field(default_factory=dict)
    peg_diameter: float = 7.0
    peg_length: float = 5.0
    peg_clearance: float = 0.18
    n_pegs: int = 4
    min_wall_thickness: float = 1.2
    alignment_key: bool = False
    dowel_shape: str = "round"
    no_connectors: bool = False
    hollow_wall_thickness: float | None = None

    def validate(self) -> None:
        has_single_axis = self.spacing is not None or self.pieces is not None or self.cut_planes is not None
        # `is not None` (not a truthiness check): an explicitly empty dict
        # means "multi-axis mode was engaged with zero cuts requested" (a
        # valid no-op — e.g. the mesh already fits every axis being edited),
        # distinct from `None` meaning multi-axis mode wasn't used at all.
        has_multi_axis = self.axis_cuts is not None
        if not self.bed_dims and not has_single_axis and not has_multi_axis:
            raise ValueError("one of spacing, pieces, cut_planes, axis_cuts, or bed dimensions is required")
        if sum([bool(self.bed_dims), has_single_axis, has_multi_axis]) > 1:
            raise ValueError("bed-dims, spacing/pieces/cut_planes, and axis_cuts are mutually exclusive")
        if self.cut_planes is not None and (self.spacing is not None or self.pieces is not None):
            raise ValueError("cut_planes can't be combined with spacing/pieces")
        if self.axis_cuts is not None:
            for axis, planes in self.axis_cuts.items():
                axis_index(axis)  # raises ValueError on an invalid axis name
                if not planes:
                    raise ValueError(f"axis_cuts['{axis}'] must be a non-empty list of cut positions")
        if sorted(self.axis_order.lower()) != ["x", "y", "z"]:
            raise ValueError(f"axis_order must be a permutation of 'xyz', got '{self.axis_order}'")
        if self.scale is not None and self.target_dim is not None:
            raise ValueError("specify only one of scale or target_dim")
        if self.dowel_shape not in SUPPORTED_SHAPES:
            raise ValueError(f"dowel_shape must be one of {SUPPORTED_SHAPES}")
        if self.hollow_wall_thickness is not None and self.hollow_wall_thickness <= 0:
            raise ValueError("hollow_wall_thickness must be > 0")

    def connector_kwargs(self) -> dict:
        return dict(
            peg_diameter=self.peg_diameter,
            peg_length=self.peg_length,
            peg_clearance=self.peg_clearance,
            n_pegs=self.n_pegs,
            min_wall_thickness=self.min_wall_thickness,
            alignment_key=self.alignment_key,
            dowel_shape=self.dowel_shape,
        )


def _cut_all_axes(
    mesh: trimesh.Trimesh,
    axis_cuts: dict[str, list[float]],
    axis_order: str,
    connector_kwargs: dict | None,
    progress: ProgressReporter | None,
) -> tuple[list[trimesh.Trimesh], list[trimesh.Trimesh]]:
    """Split on each axis present in `axis_cuts`, in `axis_order` (default
    Z, X, Y — cutting the tall/primary axis first tends to leave larger,
    better-connected intermediate pieces than cutting a thin secondary axis
    first, which lowers the odds of a floating-region failure), cutting
    every piece produced so far again on the next axis. Plane positions are
    absolute coordinates (as picked by the user against the whole, unsplit
    mesh), so for each piece only the planes that actually fall inside that
    piece's current bounds on that axis are applied."""
    pieces = [mesh]
    dowels: list[trimesh.Trimesh] = []
    ordered_axes = [a for a in axis_order.lower() if a in axis_cuts]
    for axis in ordered_axes:
        planes = axis_cuts[axis]
        idx = axis_index(axis)

        if len(pieces) >= _MIN_PIECES_FOR_PARALLEL:
            # Each top-level piece's cut + its own connectors are fully
            # independent of every other piece's (they only ever touch their
            # own geometry), so this loop iteration is the natural boundary
            # to parallelize across cores instead of running one piece at a
            # time on a single core.
            next_pieces = []
            max_workers = min(len(pieces), os.cpu_count() or 1)
            with ProcessPoolExecutor(max_workers=max_workers) as pool:
                futures = [
                    pool.submit(_process_one_piece, piece.vertices, piece.faces, axis, planes, connector_kwargs)
                    for piece in pieces
                ]
                for future in futures:
                    piece_arrays, dowel_arrays = future.result()
                    next_pieces.extend(
                        trimesh.Trimesh(vertices=v, faces=f, process=False) for v, f in piece_arrays
                    )
                    dowels.extend(trimesh.Trimesh(vertices=v, faces=f, process=False) for v, f in dowel_arrays)
                    if progress:
                        progress.step(f"Split a piece on {axis} axis")
        else:
            next_pieces = []
            for piece in pieces:
                lo, hi = piece.bounds[0][idx], piece.bounds[1][idx]
                relevant = [p for p in planes if lo < Cut.coerce(p).position < hi]
                resolved = resolve_cuts(piece, axis, relevant)
                sub_pieces = _cut_mesh_or_raise(piece, resolved, axis, progress)
                if resolved and connector_kwargs is not None:
                    sub_pieces, sub_dowels = add_connectors(sub_pieces, resolved, progress=progress, **connector_kwargs)
                    dowels.extend(sub_dowels)
                next_pieces.extend(sub_pieces)

        pieces = next_pieces
        if progress:
            progress.step(f"Split on {axis} axis into {len(pieces)} piece(s)")
    return pieces, dowels


def run_pipeline(
    mesh: trimesh.Trimesh,
    params: PipelineParams,
    progress: ProgressReporter | None = None,
) -> tuple[list[trimesh.Trimesh], list[trimesh.Trimesh]]:
    """Returns (pieces, dowels)."""
    params.validate()

    if progress:
        progress.step("Loaded mesh")

    mesh = scale_mesh(mesh, scale=params.scale, target_dim=params.target_dim, axis=params.axis)
    if progress:
        progress.step("Scaled mesh")

    if params.hollow_wall_thickness is not None:
        mesh = hollow_mesh(mesh, params.hollow_wall_thickness)
        if progress:
            progress.step(f"Hollowed to {params.hollow_wall_thickness}mm wall")

    if params.bed_dims:
        # total cut count isn't known upfront for auto-fit; leave indeterminate
        return auto_fit_split(
            mesh,
            params.bed_dims,
            connector_kwargs=None if params.no_connectors else params.connector_kwargs(),
            progress=progress,
        )

    if params.axis_cuts is not None:
        # `is not None`, not truthiness: an explicitly empty dict is a valid
        # "zero cuts requested" no-op (see validate()) — _cut_all_axes
        # already handles that correctly, returning the mesh untouched.
        #
        # This is also the path the *web UI* always submits through now
        # (single-axis splits included — see stlsplit/static/js/state.js's
        # buildJobFormData), so leaving this indeterminate meant the web
        # progress bar never showed a real percentage for any split,
        # regressing what the single-axis branch below already did. The
        # per-piece/per-axis recursion means an exact step count isn't
        # knowable upfront without re-deriving the whole cut tree, but a
        # rough estimate is enough to keep the bar visibly moving — actual
        # completion still forces fraction to 1.0 regardless (see
        # `_run_job` in web.py), so slight over/under-estimation here only
        # affects how the bar moves mid-job, never whether it reaches 100%.
        connector_kwargs = None if params.no_connectors else params.connector_kwargs()
        if progress:
            ordered_axes = [a for a in params.axis_order.lower() if a in params.axis_cuts]
            total_cuts = sum(len(params.axis_cuts[a]) for a in ordered_axes)
            per_cut_units = 2 if connector_kwargs is not None else 1  # cut + its connector interface
            already_done = 3 if params.hollow_wall_thickness is not None else 2  # loaded + scaled (+ hollowed)
            progress.set_total(already_done + total_cuts * per_cut_units + len(ordered_axes))
        return _cut_all_axes(
            mesh,
            params.axis_cuts,
            params.axis_order,
            connector_kwargs=connector_kwargs,
            progress=progress,
        )

    if params.cut_planes is not None:
        planes = params.cut_planes
    else:
        planes = compute_cut_planes(mesh, params.axis, spacing=params.spacing, pieces=params.pieces)
    resolved = resolve_cuts(mesh, params.axis, planes)

    if progress:
        n_pieces = len(resolved) + 1
        n_interfaces = len(resolved) if (resolved and not params.no_connectors) else 0
        base_steps = 3 if params.hollow_wall_thickness is not None else 2  # loaded + scaled (+ hollowed)
        progress.set_total(base_steps + 1 + n_pieces + n_interfaces)  # + planes + cuts + connectors
        progress.step(f"Computed {len(resolved)} cut plane(s)")

    pieces = _cut_mesh_or_raise(mesh, resolved, params.axis, progress)
    dowels: list[trimesh.Trimesh] = []
    if resolved and not params.no_connectors:
        pieces, dowels = add_connectors(pieces, resolved, progress=progress, **params.connector_kwargs())
    return pieces, dowels
