"""Phase 6 (OPTIMIZATION_PLAN.md) comparison harness: not a pytest file --
run directly:

    .venv\\Scripts\\python.exe tests\\verify_graph_connectivity.py

Wraps `geometry._is_single_component` to log every `(axis_idx, lo, hi)` slab
it's actually asked to check during real `compute_cut_planes` calls (across
several models, axes, and piece counts), then re-checks each logged slab with
`geometry.is_single_component_graph` and reports any disagreement.

Per the plan: only swap the graph check in as the primary implementation if
disagreement is zero on these models; otherwise it can still be used as a
fast pre-filter (trust "connected", confirm "disconnected" with the real
check).
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import trimesh

import stlsplit.geometry as geo

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _standing_figure():
    return trimesh.load_mesh(os.path.join(REPO_ROOT, "standing_figure.stl"))


def _synthetic_box():
    # stlsplit/static/box.stl doesn't exist in this checkout; a plain box
    # covers the "well-behaved, always-safe" side of the comparison, mirroring
    # what the plan asked box.stl for.
    return trimesh.creation.box(extents=[40, 60, 100])


def _dumbbell():
    a = trimesh.creation.box(extents=[20, 20, 20]).apply_translation([-40, 0, 0])
    b = trimesh.creation.box(extents=[20, 20, 20]).apply_translation([40, 0, 0])
    return trimesh.util.concatenate([a, b])


def _l_shaped_prism(height=60.0):
    from shapely.geometry import Polygon

    poly = Polygon([(0, 0), (40, 0), (40, 15), (15, 15), (15, 40), (0, 40)])
    return trimesh.creation.extrude_polygon(poly, height=height)


class _Recorder:
    def __init__(self):
        self.queries: dict[int, set[tuple]] = {}

    def wrap(self, orig):
        def wrapped(mesh, axis_idx, lo, hi):
            self.queries.setdefault(axis_idx, set()).add((lo, hi))
            return orig(mesh, axis_idx, lo, hi)

        return wrapped


def _collect_queries(mesh, axis, pieces_list, spacings):
    """Run compute_cut_planes with real `_is_single_component`, logging every
    (axis_idx, lo, hi) triple it's asked about."""
    recorder = _Recorder()
    orig = geo._is_single_component
    geo._is_single_component = recorder.wrap(orig)
    try:
        for pieces in pieces_list:
            try:
                geo.compute_cut_planes(mesh, axis, pieces=pieces)
            except Exception:
                pass  # planning-time failures are fine; we only care about queries made along the way
        for spacing in spacings:
            try:
                geo.compute_cut_planes(mesh, axis, spacing=spacing)
            except Exception:
                pass
    finally:
        geo._is_single_component = orig
    return recorder.queries


def _compare(label, mesh):
    span_by_axis = {i: mesh.extents[i] for i in range(3)}
    disagreements = []
    total = 0
    for axis, axis_idx in geo.AXES.items():
        span = span_by_axis[axis_idx]
        pieces_list = [2, 3, 4, 5]
        spacings = [span / 2.2, span / 3.3, span / 5.5]
        queries = _collect_queries(mesh, axis, pieces_list, spacings)
        for a_idx, pairs in queries.items():
            index = geo.build_graph_connectivity_index(mesh, a_idx)
            for lo, hi in pairs:
                total += 1
                old = geo._is_single_component(mesh, a_idx, lo, hi)
                new = geo.is_single_component_graph(index, lo, hi)
                if old != new:
                    disagreements.append((label, a_idx, lo, hi, old, new))
    return total, disagreements


def main():
    models = [
        ("box", _synthetic_box()),
        ("dumbbell", _dumbbell()),
        ("l_shaped_prism", _l_shaped_prism()),
        ("standing_figure.stl", _standing_figure()),
    ]

    grand_total = 0
    all_disagreements = []
    for label, mesh in models:
        total, disagreements = _compare(label, mesh)
        grand_total += total
        all_disagreements.extend(disagreements)
        print(f"{label}: {total} slabs checked, {len(disagreements)} disagreements")

    print()
    print(f"TOTAL: {grand_total} slabs checked, {len(all_disagreements)} disagreements")
    if all_disagreements:
        print("\nDisagreements (label, axis_idx, lo, hi, slice_based, graph_based):")
        for d in all_disagreements[:50]:
            print(" ", d)
        if len(all_disagreements) > 50:
            print(f"  ... and {len(all_disagreements) - 50} more")


if __name__ == "__main__":
    main()
