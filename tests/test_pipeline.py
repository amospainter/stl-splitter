import trimesh
from shapely.geometry import Polygon

from stlsplit.autofit import auto_fit_split
from stlsplit.connectors import add_connectors
from stlsplit.cutting import cut_mesh
from stlsplit.geometry import CutPlacementError, compute_cut_planes, resolve_cuts, scale_mesh
from stlsplit.hollow import hollow_mesh


def _box():
    return trimesh.creation.box(extents=[20, 20, 60])


def test_scale_mesh_by_factor():
    m = scale_mesh(_box(), scale=2.0)
    expected = trimesh.creation.box(extents=[40, 40, 120]).extents
    assert (abs(m.extents - expected) < 1e-6).all()


def test_scale_mesh_by_target_dim():
    m = scale_mesh(_box(), target_dim=120, axis="z")
    assert abs(m.extents[2] - 120) < 1e-6


def test_compute_cut_planes_by_pieces():
    planes = compute_cut_planes(_box(), "z", pieces=3)
    assert len(planes) == 2
    assert planes == sorted(planes)


def test_cut_mesh_produces_watertight_pieces():
    m = _box()
    planes = compute_cut_planes(m, "z", pieces=3)
    resolved = resolve_cuts(m, "z", planes)
    pieces = cut_mesh(m, resolved)
    assert len(pieces) == 3
    assert all(p.is_watertight for p in pieces)
    assert abs(sum(p.volume for p in pieces) - m.volume) < 1e-3


def test_add_connectors_keeps_pieces_watertight_and_produces_dowels():
    m = _box()
    planes = compute_cut_planes(m, "z", pieces=3)
    resolved = resolve_cuts(m, "z", planes)
    pieces = cut_mesh(m, resolved)
    pieces, dowels = add_connectors(pieces, resolved, peg_diameter=7, peg_length=5, peg_clearance=0.18)
    assert len(pieces) == 3
    assert all(p.is_watertight for p in pieces)
    assert len(dowels) > 0
    assert all(d.is_watertight for d in dowels)
    # sockets subtracted from both sides of each interface -> every piece
    # loses volume, none of them gain any (no more embedded pegs)
    original_volume = _box().volume / 3
    for p in pieces:
        assert p.volume < original_volume


def test_add_connectors_with_alignment_key_stays_watertight():
    m = _box()
    planes = compute_cut_planes(m, "z", pieces=2)
    resolved = resolve_cuts(m, "z", planes)
    pieces = cut_mesh(m, resolved)
    pieces, dowels = add_connectors(
        pieces, resolved, peg_diameter=7, peg_length=5, peg_clearance=0.18, alignment_key=True
    )
    assert len(pieces) == 2
    assert all(p.is_watertight for p in pieces)
    assert all(d.is_watertight for d in dowels)


def test_add_connectors_shapes():
    for shape in ("round", "d", "square", "hex"):
        m = _box()
        planes = compute_cut_planes(m, "z", pieces=2)
        resolved = resolve_cuts(m, "z", planes)
        pieces = cut_mesh(m, resolved)
        pieces, dowels = add_connectors(
            pieces, resolved, peg_diameter=7, peg_length=5, peg_clearance=0.18, dowel_shape=shape
        )
        assert all(p.is_watertight for p in pieces), shape
        assert dowels, shape
        assert all(d.is_watertight for d in dowels), shape


def test_auto_fit_split_multi_axis():
    m = trimesh.creation.box(extents=[300, 300, 60])
    bed_dims = {"x": 220, "y": 220, "z": 250}
    pieces, dowels = auto_fit_split(m, bed_dims, connector_kwargs=None)
    assert len(pieces) == 4  # 300/220 needs 2 cuts along both x and y
    assert dowels == []
    for p in pieces:
        assert p.is_watertight
        assert p.extents[0] <= bed_dims["x"] + 1e-6
        assert p.extents[1] <= bed_dims["y"] + 1e-6
        assert p.extents[2] <= bed_dims["z"] + 1e-6
    assert abs(sum(p.volume for p in pieces) - m.volume) < 1e-3


def test_auto_fit_split_with_connectors():
    m = trimesh.creation.box(extents=[300, 60, 60])
    bed_dims = {"x": 220, "y": 220, "z": 220}
    pieces, dowels = auto_fit_split(
        m, bed_dims, connector_kwargs=dict(peg_diameter=7, peg_length=5, peg_clearance=0.18)
    )
    assert len(pieces) == 2
    assert all(p.is_watertight for p in pieces)
    assert len(dowels) > 0


def _l_shaped_prism(height=60.0):
    # An L-shaped cross-section extruded along Z: non-convex, and its
    # bounding-box corners (e.g. near (40, 40)) fall in the L's missing
    # quadrant, outside the solid entirely.
    poly = Polygon([(0, 0), (40, 0), (40, 15), (15, 15), (15, 40), (0, 40)])
    return trimesh.creation.extrude_polygon(poly, height=height)


def test_add_connectors_on_nonconvex_mesh_actually_carves_sockets():
    # Regression test: bbox-corner peg placement used to land pegs outside
    # the actual solid on non-convex cross-sections (e.g. an L-shaped part),
    # silently producing floating pegs and no-op socket subtractions (zero
    # volume change). Every interior interface here must show a real volume
    # delta, proving sockets actually intersected the solid.
    m = _l_shaped_prism()
    assert m.is_watertight

    planes = compute_cut_planes(m, "z", pieces=3)
    resolved = resolve_cuts(m, "z", planes)
    pieces = cut_mesh(m, resolved)
    before = [p.volume for p in pieces]

    pieces, dowels = add_connectors(pieces, resolved, peg_diameter=7, peg_length=5, peg_clearance=0.18)
    assert all(p.is_watertight for p in pieces)
    assert dowels

    for p, v0 in zip(pieces, before):
        assert p.volume < v0 - 1.0


def _dumbbell():
    # Two boxes with a genuine empty gap between them: individually
    # watertight, but a single disconnected non-manifold whole -- the
    # canonical "unavoidable pinch" shape for exercising the
    # floating-region safety checks below.
    a = trimesh.creation.box(extents=[20, 20, 20])
    a.apply_translation([-40, 0, 0])
    b = trimesh.creation.box(extents=[20, 20, 20])
    b.apply_translation([40, 0, 0])
    return trimesh.util.concatenate([a, b])


def test_cut_mesh_raises_clear_error_on_floating_region():
    # Regression: a cut plane perpendicular to the dumbbell's *separation*
    # axis (Y here, not X -- an X cut at the gap would just cleanly divide
    # the two lobes) leaves both resulting pieces straddling both
    # disconnected lobes, which must raise a clear, specific
    # CutPlacementError -- not silently produce a multi-body "piece" that
    # looks fine until it's in a slicer.
    m = _dumbbell()
    resolved = resolve_cuts(m, "y", [0.0])
    try:
        cut_mesh(m, resolved)
        assert False, "expected CutPlacementError"
    except CutPlacementError as e:
        assert "disconnected" in e.message


def test_compute_cut_planes_spacing_does_not_over_fragment_unsafe_mesh():
    # Regression: compute_cut_planes's spacing-mode retry loop used to
    # escalate piece count on every unsafe attempt (up to +5 extra) and
    # return the *most* fragmented one even when none were actually safe.
    # Cutting the dumbbell along Y (perpendicular to its X-axis separation)
    # is unsafe at *every* piece count -- both lobes span the same Y range,
    # so no Y cut ever isolates them from each other -- which used to mean
    # this always burned through all 5 extra attempts. More pieces never
    # fixes that, so it should fall back to the smallest (first) attempt
    # instead of ballooning to 6+ cuts.
    m = _dumbbell()
    planes = compute_cut_planes(m, "y", spacing=8.0)
    assert len(planes) <= 2  # was ballooning to 7 before the fix


def test_compute_cut_planes_parallel_candidate_search_matches_serial():
    # Regression: the ProcessPoolExecutor-based candidate-safety-check path
    # (used on heavy meshes -- see geometry._PARALLEL_FACE_THRESHOLD) must
    # produce the exact same result as the serial path, just faster. Forces
    # it on for this small/fast dumbbell mesh by temporarily lowering the
    # threshold, rather than needing a genuinely heavy fixture in the suite.
    import stlsplit.geometry as geo

    m = _dumbbell()
    serial = compute_cut_planes(m, "y", spacing=8.0)

    original_threshold = geo._PARALLEL_FACE_THRESHOLD
    geo._PARALLEL_FACE_THRESHOLD = 0
    try:
        parallel = compute_cut_planes(m, "y", spacing=8.0)
    finally:
        geo._PARALLEL_FACE_THRESHOLD = original_threshold

    assert parallel == serial


def test_multi_axis_split_uses_parallel_path_and_conserves_watertightness():
    # Coverage/regression for _cut_all_axes' per-piece ProcessPoolExecutor
    # path (kicks in once an axis has 2+ pieces from the previous axis).
    from stlsplit.pipeline import PipelineParams, run_pipeline

    m = trimesh.creation.box(extents=[100, 40, 40])
    params = PipelineParams(
        axis_cuts={"x": [-30, -10, 10, 30], "y": [0]},
        axis_order="xyz",
        peg_diameter=6, peg_length=4, peg_clearance=0.18, n_pegs=2,
    )
    pieces, dowels = run_pipeline(m, params)
    assert len(pieces) == 10  # 5 x-slices * 2 y-halves
    assert all(p.is_watertight for p in pieces)
    assert dowels
    # sockets remove some volume at each interface, but shouldn't come
    # close to the whole piece disappearing
    assert abs(sum(p.volume for p in pieces) - m.volume) < m.volume * 0.05


def test_progress_reporter_raises_job_cancelled_when_event_set():
    # Regression for the web UI's "Cancel" button: ProgressReporter.step()
    # must raise JobCancelled the moment its cancel_event is set, and must
    # NOT raise (or otherwise misbehave) when no cancel_event was given, so
    # every existing non-web caller (CLI, other tests) is unaffected.
    import threading

    from stlsplit.progress import JobCancelled, ProgressReporter

    reporter_no_event = ProgressReporter()
    reporter_no_event.step("fine")  # must not raise

    event = threading.Event()
    reporter = ProgressReporter(cancel_event=event)
    reporter.step("still fine")
    event.set()
    try:
        reporter.step("should raise")
        assert False, "expected JobCancelled"
    except JobCancelled:
        pass


def test_run_pipeline_stops_early_when_cancelled():
    # End-to-end: a cancel_event set partway through a multi-axis split
    # must stop run_pipeline via JobCancelled rather than completing, and
    # must not leave the process pool used for the second axis's parallel
    # branch hanging around.
    import threading

    from stlsplit.pipeline import PipelineParams, run_pipeline
    from stlsplit.progress import JobCancelled, ProgressReporter

    m = trimesh.creation.box(extents=[100, 40, 40])
    params = PipelineParams(
        axis_cuts={"x": [-30, -10, 10, 30], "y": [0]},
        axis_order="xyz",
        peg_diameter=6, peg_length=4, peg_clearance=0.18, n_pegs=2,
    )
    event = threading.Event()
    steps_seen = []

    def on_update(message, fraction):
        steps_seen.append(message)
        if len(steps_seen) == 2:  # cancel partway through, not immediately
            event.set()

    reporter = ProgressReporter(on_update, cancel_event=event)
    try:
        run_pipeline(m, params, progress=reporter)
        assert False, "expected JobCancelled"
    except JobCancelled:
        pass
    assert len(steps_seen) < 20  # didn't run to completion (would be more steps)


def test_hollow_mesh_reduces_volume_and_stays_watertight():
    m = trimesh.creation.icosphere(subdivisions=2, radius=20)
    hollow = hollow_mesh(m, wall_thickness=3.0)
    assert hollow.is_watertight
    assert hollow.volume < m.volume * 0.7
