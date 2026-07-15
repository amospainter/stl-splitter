import numpy as np
import pytest
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


def test_auto_fit_split_rotates_to_avoid_unnecessary_cut():
    # A long thin piece lying the "wrong" way (its long axis on a bed axis
    # too small for it) should be reoriented (90-degree multiples only) to
    # fit as a single piece, rather than cut, whenever some rotation makes
    # it fit -- here rotating the 300mm axis onto Z (which has headroom)
    # avoids the cut entirely.
    m = trimesh.creation.box(extents=[300, 50, 50])
    bed_dims = {"x": 220, "y": 220, "z": 320}
    pieces, dowels = auto_fit_split(m, bed_dims, connector_kwargs=None)
    assert len(pieces) == 1
    assert dowels == []
    piece = pieces[0]
    assert piece.is_watertight
    assert abs(piece.volume - m.volume) < 1e-6
    assert sorted(piece.extents.round(3)) == sorted(m.extents.round(3))
    for axis, limit in (("x", bed_dims["x"]), ("y", bed_dims["y"]), ("z", bed_dims["z"])):
        idx = {"x": 0, "y": 1, "z": 2}[axis]
        assert piece.extents[idx] <= limit + 1e-6


def test_auto_fit_split_allow_rotation_false_still_cuts():
    # Regression: allow_rotation=False must restore the original
    # cut-only-in-place behavior exactly -- same piece, same bed, but
    # explicitly opted out of reorientation.
    m = trimesh.creation.box(extents=[300, 50, 50])
    bed_dims = {"x": 220, "y": 220, "z": 320}
    pieces, dowels = auto_fit_split(m, bed_dims, connector_kwargs=None, allow_rotation=False)
    assert len(pieces) > 1
    assert all(p.is_watertight for p in pieces)
    assert abs(sum(p.volume for p in pieces) - m.volume) < 1e-3


def test_auto_fit_split_rotation_falls_back_to_cutting_when_no_fit():
    # When no 90-degree reorientation lets a piece fit the bed either, the
    # rotation check must be a no-op and normal cutting must still proceed
    # (not get stuck or skip cutting incorrectly).
    m = trimesh.creation.box(extents=[500, 500, 500])
    bed_dims = {"x": 220, "y": 220, "z": 220}
    pieces, dowels = auto_fit_split(m, bed_dims, connector_kwargs=None)
    assert len(pieces) > 1
    assert all(p.is_watertight for p in pieces)
    for p in pieces:
        assert p.extents[0] <= bed_dims["x"] + 1e-6
        assert p.extents[1] <= bed_dims["y"] + 1e-6
        assert p.extents[2] <= bed_dims["z"] + 1e-6
    assert abs(sum(p.volume for p in pieces) - m.volume) < 1e-2


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


def _min_pairwise_distance(dowels):
    import numpy as np
    centers = np.array([d.centroid for d in dowels])
    best = None
    for i in range(len(centers)):
        for j in range(i + 1, len(centers)):
            d = float(np.linalg.norm(centers[i] - centers[j]))
            if best is None or d < best:
                best = d
    return best


def test_add_connectors_never_places_physically_overlapping_sockets():
    # Regression: farthest-point selection used to run over the full
    # candidate pool BEFORE filtering for true 3D safety, so once the
    # well-spread early candidates got rejected (e.g. sitting over a thin
    # wall a few mm deeper than the cut plane), the *accepted* subset was
    # only ever spread relative to points that got dropped -- not relative
    # to each other -- and could end up huddled well inside overlap range.
    # A 7mm peg + 0.18mm clearance needs >= 8.18mm center-to-center spacing
    # to avoid two sockets physically intersecting.
    m = _l_shaped_prism()
    planes = compute_cut_planes(m, "z", pieces=3)
    resolved = resolve_cuts(m, "z", planes)
    pieces = cut_mesh(m, resolved)
    pieces, dowels = add_connectors(
        pieces, resolved, peg_diameter=7, peg_length=5, peg_clearance=0.18, n_pegs=4
    )
    assert dowels
    # Check globally rather than per-interface: every dowel anywhere in this
    # mesh must respect the same minimum spacing regardless of which
    # interface it came from.
    min_dist = _min_pairwise_distance(dowels)
    if min_dist is not None:
        assert min_dist >= 7 + 0.18 - 1e-6, f"two dowels only {min_dist:.2f}mm apart, sockets would overlap"


def test_add_connectors_spreads_pegs_across_available_area_not_just_first_safe_spot():
    # Regression, same root cause as above: verify pegs actually spread out
    # (not just "don't overlap") when there's clearly enough room to do so.
    m = trimesh.creation.box(extents=[60, 60, 20])
    resolved = resolve_cuts(m, "z", [0.0])
    pieces = cut_mesh(m, resolved)
    pieces, dowels = add_connectors(
        pieces, resolved, peg_diameter=7, peg_length=5, peg_clearance=0.18, n_pegs=4, inset=8.0
    )
    assert len(dowels) == 4
    min_dist = _min_pairwise_distance(dowels)
    # A 60x60 face with an 8mm inset leaves a 44x44 safe area -- 4 pegs
    # spread toward the corners of that area should end up tens of mm apart,
    # not just barely clearing the overlap threshold.
    assert min_dist > 20.0


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


def test_multi_axis_connectors_placed_after_all_cuts_not_between_axes():
    # Regression: connectors used to be added right after each axis's cuts,
    # before the *next* axis was cut -- so a perpendicular later cut could
    # slice straight through a socket the earlier axis had just carved. A
    # peg placed near a not-yet-existing cut plane would end up with no
    # margin at all once that plane was actually cut (in the worst observed
    # case, a peg landed exactly ON the corner where both planes met).
    # Connectors must now be placed once, over the FINAL pieces from every
    # axis, so every peg keeps its full `inset` clearance from every cut
    # plane, not just the one its own interface belongs to.
    from stlsplit.connectors import dedupe_cuts
    from stlsplit.pipeline import PipelineParams, _cut_all_axes, run_pipeline

    m = trimesh.creation.box(extents=[60, 60, 60])
    params = PipelineParams(
        axis_cuts={"z": [0.0], "x": [0.0]},
        axis_order="zxy",
        peg_diameter=7, peg_length=5, peg_clearance=0.18, n_pegs=4,
    )
    pieces, dowels = run_pipeline(m, params)
    assert len(pieces) == 4
    assert all(p.is_watertight for p in pieces)
    assert dowels

    # Recompute the same cut planes independently (via the cut-only helper)
    # to check every dowel's clearance from both interfaces, not just its own.
    _, applied_cuts = _cut_all_axes(m, params.axis_cuts, params.axis_order, progress=None)
    planes = dedupe_cuts(applied_cuts)
    assert len(planes) == 2  # the z-plane and the x-plane, deduplicated

    inset = 8.0  # add_connectors' default
    tol = 1.0  # mm slack for candidate-grid discretization
    for dowel in dowels:
        centroid = dowel.centroid
        dists = sorted(abs(float(np.dot(c.normal, centroid - c.origin))) for c in planes)
        own_dist, other_dist = dists[0], dists[1]
        assert own_dist < 1.0, "dowel should sit essentially on its own interface plane"
        assert other_dist >= inset - tol, (
            f"dowel only {other_dist:.2f}mm from the OTHER cut plane (need >= {inset - tol}mm) -- "
            "a socket was placed without knowing about a perpendicular cut"
        )


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


def test_hollow_mesh_rejects_wall_thickness_too_thin_for_mesh_size():
    # Regression: a wall_thickness the voxel-erosion grid can't actually
    # resolve on a mesh this large used to be silently accepted and produce
    # badly corrupted output -- on a real ~500mm model hollowed to a 2mm
    # wall, this ate nearly the entire model (0.02 mm^3 left out of ~7.7
    # million mm^3) while still reporting `is_watertight`. Must now raise a
    # clear, actionable error instead of returning a corrupted mesh.
    m = trimesh.creation.box(extents=[500, 300, 200])
    with pytest.raises(ValueError, match="too thin to hollow reliably"):
        hollow_mesh(m, wall_thickness=2.0)


def test_hollow_mesh_accepts_proportionally_thick_wall_on_same_large_mesh():
    # Same large mesh as above, but with a wall_thickness the tool itself
    # reports as safe -- must succeed cleanly with a plausible shell volume,
    # proving the rejection above is about the specific wall/size ratio, not
    # the mesh being large per se. A correctly hollowed shell is naturally
    # TWO disjoint watertight surfaces (outer skin + inner cavity boundary,
    # which share no vertices/edges) -- that's normal shell topology, not a
    # defect, so this deliberately does NOT assert a single component.
    m = trimesh.creation.box(extents=[500, 300, 200])
    hollow = hollow_mesh(m, wall_thickness=10.0)
    assert hollow.is_watertight
    expected_shell_volume = 500 * 300 * 200 - 480 * 280 * 180
    assert hollow.volume < m.volume
    assert hollow.volume > m.volume * 0.001
    assert abs(hollow.volume - expected_shell_volume) < expected_shell_volume * 0.3


def test_hollow_mesh_achieves_requested_thickness_not_just_watertight():
    # Regression: `mesh.voxelized(pitch).fill()` fills the grid exactly to
    # the mesh's own bounding box with no empty margin, so cells right at the
    # true surface had no neighboring "outside" voxel for the distance
    # transform to measure against -- `distance_transform_edt` returned
    # wildly-too-large distances there (measured directly: up to 619mm on a
    # mesh whose true max distance-to-surface was ~100mm), which incorrectly
    # passed the erosion threshold and ate the wall almost everywhere. The
    # corrupted output still reported `is_watertight`, so this checks actual
    # wall thickness via ray-casting, not just topology.
    m = trimesh.creation.icosphere(subdivisions=3, radius=100)
    wall = 5.0
    hollow = hollow_mesh(m, wall_thickness=wall)
    assert hollow.is_watertight

    sample_pts, face_idx = trimesh.sample.sample_surface(m, 200)
    normals = m.face_normals[face_idx]
    origins = sample_pts - normals * 0.01
    locations, index_ray, _ = hollow.ray.intersects_location(origins, -normals)
    missing = len(origins) - len(set(index_ray.tolist()))
    assert missing == 0, f"{missing} surface samples found no wall behind them (a hole)"

    thicknesses = []
    for i in range(len(origins)):
        hits = np.nonzero(index_ray == i)[0]
        dists = np.linalg.norm(locations[hits] - origins[i], axis=1)
        thicknesses.append(dists.min())
    # A sphere is the easiest case for voxel erosion (no thin/concave
    # features) -- thickness should land close to what was requested, not
    # collapse toward zero the way the pre-fix bug did.
    assert min(thicknesses) > wall * 0.5
    assert abs(np.mean(thicknesses) - wall) < wall * 0.5
