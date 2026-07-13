"""Benchmark + behavior-lock harness for the splitting pipeline.

Not a pytest file -- run directly:
    .venv\\Scripts\\python.exe tests\\bench_split.py

Prints timing and per-piece geometry stats for a couple of representative
jobs against the real test model `standing_figure.stl`, so later
optimizations can be checked against `tests/bench_baseline.txt` for both
speed (should improve) and behavior (piece/dowel counts and volumes must
stay effectively identical -- see OPTIMIZATION_PLAN.md).
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stlsplit.geometry import load_mesh
from stlsplit.pipeline import PipelineParams, run_pipeline

MODEL_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "standing_figure.stl")


def _report(label: str, params: PipelineParams) -> None:
    mesh = load_mesh(MODEL_PATH)
    start = time.perf_counter()
    pieces, dowels = run_pipeline(mesh, params)
    elapsed = time.perf_counter() - start

    print(f"=== {label} ===")
    print(f"elapsed: {elapsed:.4f}s")
    print(f"pieces: {len(pieces)}  dowels: {len(dowels)}")
    for i, p in enumerate(pieces):
        print(f"  piece {i}: volume={p.volume:.3f} faces={len(p.faces)} watertight={p.is_watertight}")
    print()


def main() -> None:
    mesh = load_mesh(MODEL_PATH)
    z_extent = mesh.extents[2]

    _report(
        "pieces=4 (axis=z)",
        PipelineParams(axis="z", pieces=4, peg_diameter=7, peg_length=5, peg_clearance=0.18),
    )
    _report(
        "spacing (axis=z, ~1/3 z-extent)",
        PipelineParams(axis="z", spacing=z_extent / 3.0, peg_diameter=7, peg_length=5, peg_clearance=0.18),
    )


if __name__ == "__main__":
    main()
