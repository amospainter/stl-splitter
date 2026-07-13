# STL Splitter — Project Spec

## Purpose
CLI tool to take an STL, scale it, split it into print-bed-sized pieces along one axis, and add connector pegs/sockets at each cut interface, so large prints can be split into multiple pieces and reassembled.

## Stack
- Python 3.11+
- `manifold3d` for boolean ops (cut, peg union, socket subtraction) — chosen for robustness on watertight meshes
- `trimesh` for STL I/O, mesh utilities, bounding box math
- CLI via `argparse` or `click`
- No GUI in v1. Platform-agnostic; a Flask/FastAPI web UI may wrap this later without changing the core.

## v1 Scope

### Inputs
- Source STL path
- Scale factor (uniform) or target dimension (mm) on one axis
- Cut axis (X/Y/Z)
- Cut spacing OR explicit number of pieces (even spacing across bounding box)
- Output directory

### Pipeline
1. **Load** — `trimesh.load_mesh()`, validate watertight
2. **Scale** — uniform scale factor, or compute factor from target axis dimension
3. **Determine cut planes** — single axis, evenly spaced, based on spacing or piece count (no auto bed-fit logic in v1)
4. **Cut** — boolean-intersect mesh against oversized box on each side of each plane (manifold3d) to produce N pieces
5. **Add connectors** — at each interface:
   - Compute interface face centroid + bounding box
   - Place 2–4 pegs in a grid, inset from edges
   - Peg: cylinder, ~6–8mm diameter, ~4–6mm length per side
   - Diametral clearance: 0.15–0.2mm (peg subtracted slightly undersized on socket side)
   - Union peg onto one piece, subtract socket from the mating piece
6. **Export** — write each piece as its own STL, named `<basename>_piece01.stl`, `_piece02.stl`, etc., to output dir

### CLI shape (draft)
```
stlsplit input.stl \
  --scale 2.5 \
  --axis z \
  --pieces 3 \
  --peg-diameter 7 \
  --peg-clearance 0.18 \
  --out ./output/
```

## v2 / Stretch (not in scope yet)
- Auto-cut based on target print-bed dimensions + bounding box (multi-axis if needed)
- Wall-thickness-aware peg placement (avoid thin-wall failures)
- Non-cylindrical alignment keys to prevent rotation at joint
- Web UI (Flask/FastAPI) wrapping the core pipeline
- Auto-orientation before cutting (minimize piece count / waste)

## Open Questions
- Peg pattern: fixed grid vs. adaptive to interface shape/size?
- Should clearance be configurable per-material (PLA vs PETG vs ABS shrink differently)?
- Piece labeling/assembly guide — auto-generate a diagram or numbered STL comments?

## Out of Scope (v1)
- Non-planar/curved cuts
- Multi-axis auto-splitting
- GUI of any kind
