"""Command-line entry point for stlsplit."""
from __future__ import annotations

import argparse
import os
import sys

from .connectors import SUPPORTED_SHAPES
from .export import BED_SIZE_PRESETS, DEFAULT_BED_SIZE, SUPPORTED_FORMATS, export_pieces
from .geometry import Cut, load_mesh
from .pipeline import PipelineParams, run_pipeline


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="stlsplit",
        description="Scale, split, and add connector dowels to an STL for print-bed-sized output.",
    )
    p.add_argument("input", help="Path to source STL file")

    scale_group = p.add_mutually_exclusive_group()
    scale_group.add_argument("--scale", type=float, help="Uniform scale factor")
    scale_group.add_argument("--target-dim", type=float, help="Target size (mm) on --axis")

    p.add_argument("--axis", choices=["x", "y", "z"], default="z", help="Cut axis (default: z, ignored in --bed-* auto-fit mode)")

    split_group = p.add_mutually_exclusive_group()
    split_group.add_argument("--spacing", type=float, help="Max piece size (mm) along the cut axis")
    split_group.add_argument("--pieces", type=int, help="Number of pieces to split into")
    split_group.add_argument(
        "--cut-planes",
        type=str,
        help="Comma-separated explicit cut planes (mm, on --axis), bypassing automatic placement entirely. "
        "Each cut is 'position' or 'position:tiltA:tiltB' to angle the plane by tiltA/tiltB degrees "
        "(rotating around the other two axes, in x/y/z order) instead of a plain axis-aligned cut, "
        "e.g. '10,-10:15:0'",
    )

    p.add_argument(
        "--cut-planes-x",
        type=str,
        help="Comma-separated explicit cut planes (mm) on the X axis, same 'position' or "
        "'position:tiltA:tiltB' syntax as --cut-planes; combine with --cut-planes-y/-z to split on more "
        "than one axis (mutually exclusive with --axis/--spacing/--pieces/--cut-planes)",
    )
    p.add_argument("--cut-planes-y", type=str, help="Comma-separated explicit cut planes (mm) on the Y axis; see --cut-planes-x")
    p.add_argument("--cut-planes-z", type=str, help="Comma-separated explicit cut planes (mm) on the Z axis; see --cut-planes-x")
    p.add_argument(
        "--axis-order",
        type=str,
        default="zxy",
        help="When splitting on more than one axis (--cut-planes-x/-y/-z), the order to cut them in, as a "
        "permutation of 'xyz' (default: zxy — cutting the primary/tall axis first tends to leave larger, "
        "better-connected intermediate pieces, lowering the odds of a floating-region failure on later cuts)",
    )

    p.add_argument("--bed-x", type=float, help="Print bed X dimension (mm); enables multi-axis auto-fit splitting")
    p.add_argument("--bed-y", type=float, help="Print bed Y dimension (mm); enables multi-axis auto-fit splitting")
    p.add_argument("--bed-z", type=float, help="Print bed Z dimension (mm); enables multi-axis auto-fit splitting")

    p.add_argument("--peg-diameter", type=float, default=7.0, help="Dowel diameter in mm (default: 7)")
    p.add_argument("--peg-length", type=float, default=5.0, help="Socket depth per side in mm; the dowel spans roughly 2x this across the joint (default: 5)")
    p.add_argument("--peg-clearance", type=float, default=0.18, help="Diametral clearance between dowel and socket (default: 0.18)")
    p.add_argument("--n-pegs", type=int, default=4, help="Target connector count per interface (2 or 4, default: 4)")
    p.add_argument("--min-wall-thickness", type=float, default=1.2, help="Minimum solid wall (mm) required around a socket, checked along its full depth; thinner spots are skipped (default: 1.2)")
    p.add_argument("--dowel-shape", choices=SUPPORTED_SHAPES, default="round", help="Connector cross-section: round, d (round with a flat, anti-rotation), square, or hex (default: round)")
    p.add_argument("--alignment-key", action="store_true", help="Add a D-flat to one round connector per interface to prevent rotation (square/hex are self-keying)")
    p.add_argument("--no-connectors", action="store_true", help="Skip socket/dowel generation, just cut")

    p.add_argument("--hollow-wall", type=float, help="Hollow the model to this wall thickness (mm) before splitting, instead of a solid interior")

    p.add_argument(
        "--allow-non-watertight",
        action="store_true",
        help="Proceed even if the mesh is not watertight after automatic repair is attempted; boolean cuts/connectors may fail or produce bad geometry",
    )

    p.add_argument("--format", choices=SUPPORTED_FORMATS, default="stl", help="Output format: separate STL files, or one 3MF project bundling all pieces (default: stl)")
    p.add_argument(
        "--bed-size",
        default=DEFAULT_BED_SIZE,
        help=f"3MF only: print bed preset for laying out the multi-plate grid, one of {sorted(BED_SIZE_PRESETS)}, "
        f"or an explicit 'WIDTHxDEPTH' in mm (e.g. '300x300') (default: {DEFAULT_BED_SIZE})",
    )
    p.add_argument("--out", required=True, help="Output directory")

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    bed_dims = {}
    if args.bed_x is not None:
        bed_dims["x"] = args.bed_x
    if args.bed_y is not None:
        bed_dims["y"] = args.bed_y
    if args.bed_z is not None:
        bed_dims["z"] = args.bed_z

    def parse_planes(flag: str, raw: str | None) -> list[Cut] | None:
        if not raw:
            return None
        cuts = []
        try:
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
                    raise ValueError
        except ValueError:
            print(
                f"Error: {flag} entries must be 'position' or 'position:tiltA:tiltB', got '{raw}'",
                file=sys.stderr,
            )
            raise SystemExit(1)
        return cuts

    bed_size: str | tuple[float, float] = args.bed_size
    if bed_size not in BED_SIZE_PRESETS:
        if "x" not in bed_size.lower():
            print(f"Error: --bed-size must be one of {sorted(BED_SIZE_PRESETS)} or 'WIDTHxDEPTH', got '{bed_size}'", file=sys.stderr)
            return 1
        try:
            w_str, d_str = bed_size.lower().split("x", 1)
            bed_size = (float(w_str), float(d_str))
        except ValueError:
            print(f"Error: --bed-size must be one of {sorted(BED_SIZE_PRESETS)} or 'WIDTHxDEPTH', got '{args.bed_size}'", file=sys.stderr)
            return 1

    try:
        cut_planes = parse_planes("--cut-planes", args.cut_planes)
        axis_cuts = {
            axis: planes
            for axis, planes in (
                ("x", parse_planes("--cut-planes-x", args.cut_planes_x)),
                ("y", parse_planes("--cut-planes-y", args.cut_planes_y)),
                ("z", parse_planes("--cut-planes-z", args.cut_planes_z)),
            )
            if planes is not None
        }
    except SystemExit:
        return 1

    params = PipelineParams(
        axis=args.axis,
        scale=args.scale,
        target_dim=args.target_dim,
        spacing=args.spacing if not axis_cuts else None,
        pieces=args.pieces if not axis_cuts else None,
        cut_planes=cut_planes if not axis_cuts else None,
        axis_cuts=axis_cuts or None,
        axis_order=args.axis_order,
        bed_dims=bed_dims,
        peg_diameter=args.peg_diameter,
        peg_length=args.peg_length,
        peg_clearance=args.peg_clearance,
        n_pegs=args.n_pegs,
        min_wall_thickness=args.min_wall_thickness,
        alignment_key=args.alignment_key,
        dowel_shape=args.dowel_shape,
        no_connectors=args.no_connectors,
        hollow_wall_thickness=args.hollow_wall,
    )

    try:
        mesh = load_mesh(args.input, allow_non_watertight=args.allow_non_watertight)
        pieces, dowels = run_pipeline(mesh, params)
    except (ValueError, RuntimeError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    os.makedirs(args.out, exist_ok=True)
    basename = os.path.splitext(os.path.basename(args.input))[0]
    for name, data in export_pieces(pieces, basename, args.format, dowels=dowels, bed_size=bed_size):
        out_path = os.path.join(args.out, name)
        with open(out_path, "wb") as f:
            f.write(data)
        print(f"Wrote {out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
