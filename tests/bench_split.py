"""Benchmark + behavior-lock harness for the splitting pipeline.

Not a pytest file -- run directly:
    .venv\\Scripts\\python.exe tests\\bench_split.py

Prints timing and per-piece geometry stats for a couple of representative
jobs, so later optimizations can be checked against
`tests/bench_baseline.txt` for both speed (should improve) and behavior
(piece/dowel counts and volumes must stay effectively identical -- see
OPTIMIZATION_PLAN.md).

Model choice: `standing_figure.stl` (the repo's real test model) has
splayed limbs that make most axis-aligned cuts land on a disconnected
region no matter the piece count (see `bench_standing_figure_watertight`
below, which just checks it loads/is watertight rather than cutting it).
For actual cut-performance benchmarking we use a synthetic capsule mesh
instead: convex-ish along its long axis, so any number of even cuts stay
single-component, and its face count is deliberately pushed well above
`geometry._PARALLEL_FACE_THRESHOLD` (20_000) so Phase 1+ timings reflect
the heavy-mesh path real jobs hit.
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import trimesh

from stlsplit.geometry import load_mesh
from stlsplit.pipeline import PipelineParams, run_pipeline

MODEL_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "standing_figure.stl")


def _capsule_mesh() -> trimesh.Trimesh:
    # radius=20, height=150 -> long axis is Z; count=[160,160] gives ~50k
    # faces, comfortably above _PARALLEL_FACE_THRESHOLD.
    return trimesh.creation.capsule(radius=20, height=150, count=[160, 160])


def _report(label: str, mesh: trimesh.Trimesh, params: PipelineParams) -> None:
    mesh = mesh.copy()
    start = time.perf_counter()
    pieces, dowels = run_pipeline(mesh, params)
    elapsed = time.perf_counter() - start

    print(f"=== {label} ===")
    print(f"elapsed: {elapsed:.4f}s")
    print(f"pieces: {len(pieces)}  dowels: {len(dowels)}")
    for i, p in enumerate(pieces):
        print(f"  piece {i}: volume={p.volume:.3f} faces={len(p.faces)} watertight={p.is_watertight}")
    print()


def bench_standing_figure_watertight() -> None:
    mesh = load_mesh(MODEL_PATH)
    print("=== standing_figure.stl load check ===")
    print(f"faces={len(mesh.faces)} watertight={mesh.is_watertight} extents={mesh.extents}")
    print()


def main() -> None:
    bench_standing_figure_watertight()

    capsule = _capsule_mesh()
    z_extent = capsule.extents[2]

    _report(
        "capsule pieces=4 (axis=z)",
        capsule,
        PipelineParams(axis="z", pieces=4, peg_diameter=7, peg_length=5, peg_clearance=0.18),
    )
    _report(
        "capsule spacing (axis=z, ~1/3 z-extent)",
        capsule,
        PipelineParams(axis="z", spacing=z_extent / 3.0, peg_diameter=7, peg_length=5, peg_clearance=0.18),
    )
    _report(
        "capsule pieces=8 (axis=z, no connectors)",
        capsule,
        PipelineParams(axis="z", pieces=8, no_connectors=True),
    )


if __name__ == "__main__":
    main()
