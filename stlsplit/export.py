"""Export split pieces (and their standalone connector dowels) to disk-ready
bytes, either as separate STL files or as a single Bambu Studio / OrcaSlicer
multi-plate 3MF project.

Pieces retain their position along the cut axis from the original mesh, so
an upper piece from a vertical (Z-axis) split ends up floating high above
the origin rather than resting on the print bed — not usable as-is in a
slicer. Every exported part is therefore rested flat on Z=0, and for 3MF
(where every piece and dowel shares one project) parts are also arranged
onto separate plates so the whole assembly can be sliced without manual
rearranging.

Each dowel is first rotated so its long axis stands vertically ("on end")
— always the better print orientation for a simple cylindrical/prismatic
connector. Pieces are *not* auto-rotated: for an arbitrary (often organic,
asymmetric) shape there's no reliable way to infer a "correct" print
orientation from the geometry alone, and guessing (e.g. via PCA on the
vertex cloud) tends to produce an essentially arbitrary tilt instead of
respecting whatever orientation the source model already had.

How this is built, verified against a real 2-plate 3MF exported directly
from Bambu Studio (not just against OrcaSlicer's source, which is what
earlier attempts relied on and which did not actually resolve the issue in
Bambu Studio itself):

1. 3D/3dmodel.model's root <model> element must carry, at minimum, a
   <metadata name="Application"> tag whose value starts with
   "BambuStudio-" — the reference file used "BambuStudio-02.07.01.62"
   verbatim. Earlier attempts used an "OrcaSlicer-" tag, which is accepted
   by OrcaSlicer's own parser but is not confirmed to satisfy Bambu Studio's
   (closed-source) equivalent gate, and this is the most likely reason
   those attempts silently fell back to a single default plate even when
   opened via "File -> Open Project". A <metadata
   name="BambuStudio:3mfVersion"> tag is also present in the reference file
   and is included here for the same reason.
2. Metadata/project_settings.config (a JSON print/filament profile) must be
   present and parse to a non-empty config, specifically a non-empty
   "filament_colour" list.
3. Metadata/slice_info.config, a small XML file carrying
   X-BBL-Client-Type/X-BBL-Client-Version header fields, is present in the
   reference file and was not part of any previous attempt; it's included
   here since it may be an independent "is this a genuine Bambu-family
   file" check ahead of (or instead of) the Application-tag check.
4. Metadata/model_settings.config's <object id=...> values match the
   *build-item* object ids in 3D/3dmodel.model directly (confirmed against
   the reference file) — not some separate internal mesh id. Each piece
   gets its own <plate> block; all dowels share one "Connectors" plate.
   Object→plate assignment in this file is parsed by Bambu Studio but the
   objects are then also physically positioned over their plate's build
   volume (see _plate_cell_center) since (per OrcaSlicer's source)
   assignment is ultimately re-derived from geometry position, not solely
   from this config.
"""
from __future__ import annotations

import io
import math
import zipfile

import numpy as np
import trimesh

SUPPORTED_FORMATS = ("stl", "3mf")
_PLATE_MARGIN = 5.0  # mm gap between arranged parts

# Bed size presets (mm, width x depth) for the plate grid math below.
# Named after the common Bambu Lab / OrcaSlicer printer families.
BED_SIZE_PRESETS: dict[str, tuple[float, float]] = {
    "a1_mini": (180.0, 180.0),
    "256": (256.0, 256.0),  # X1 Carbon / P1P / P1S / A1
    "h2": (350.0, 320.0),  # H2D / H2S
}
DEFAULT_BED_SIZE = "256"
# OrcaSlicer's LOGICAL_PART_PLATE_GAP (1/5): plate pitch = bed * (1 + 1/5).
# Confirmed against the reference file: a 256mm-bed second plate's item sat
# at X=435.200043 = 256 * 1.2 + 128 (half-bed), matching this exactly.
_PLATE_GAP_FRAC = 1.0 / 5.0

_BAMBU_APPLICATION_TAG = "BambuStudio-02.07.01.62"


def resolve_bed_size(bed_size: "str | tuple[float, float] | None") -> tuple[float, float]:
    """Resolve a preset name (see BED_SIZE_PRESETS), an explicit (width,
    depth) tuple, or None (falls back to DEFAULT_BED_SIZE) into (width,
    depth) mm."""
    if bed_size is None:
        bed_size = DEFAULT_BED_SIZE
    if isinstance(bed_size, str):
        try:
            return BED_SIZE_PRESETS[bed_size]
        except KeyError:
            raise ValueError(
                f"Unknown bed_size '{bed_size}'; choose one of {sorted(BED_SIZE_PRESETS)} or pass an explicit (width, depth)"
            ) from None
    width, depth = bed_size
    return float(width), float(depth)


def _plate_columns(count: int) -> int:
    """Number of grid columns OrcaSlicer lays `count` plates out in
    (replicates their compute_colum_count: round-up-ish of sqrt)."""
    value = math.sqrt(count)
    rounded = round(value)
    return int(rounded + 1 if value > rounded else rounded)


def _plate_cell_center(plate_index: int, cols: int, bed: tuple[float, float]) -> np.ndarray:
    """World XY of the center of plate `plate_index`'s bed cell, matching
    OrcaSlicer's grid: origin (col*stride_x, -row*stride_y) with a
    corner-origin [0,bed] bed, so the cell center is that origin plus half
    the bed size."""
    bed_w, bed_d = bed
    stride_x = bed_w * (1.0 + _PLATE_GAP_FRAC)
    stride_y = bed_d * (1.0 + _PLATE_GAP_FRAC)
    row, col = divmod(plate_index, cols)
    return np.array([col * stride_x + bed_w / 2.0, -row * stride_y + bed_d / 2.0])


def _rest_on_plate(mesh: trimesh.Trimesh) -> trimesh.Trimesh:
    """Copy of `mesh` translated so its lowest point sits at Z=0, XY
    position unchanged."""
    m = mesh.copy()
    offset = np.zeros(3)
    offset[2] = -m.bounds[0][2]
    m.apply_translation(offset)
    return m


def _stand_on_end(mesh: trimesh.Trimesh) -> trimesh.Trimesh:
    """Copy of `mesh` rotated so its longest dimension (found via PCA on its
    vertices, so this works regardless of the mesh's current orientation —
    e.g. a dowel built along a tilted cut normal) points along world Z."""
    centered = mesh.vertices - mesh.vertices.mean(axis=0)
    cov = centered.T @ centered
    eigvals, eigvecs = np.linalg.eigh(cov)
    axis = eigvecs[:, int(np.argmax(eigvals))]
    axis = axis / (np.linalg.norm(axis) or 1.0)

    m = mesh.copy()
    if not np.allclose(np.abs(axis), [0.0, 0.0, 1.0], atol=1e-6):
        m.apply_transform(trimesh.geometry.align_vectors(axis, [0.0, 0.0, 1.0]))
    return m


def _arrange_grid(meshes: list[trimesh.Trimesh]) -> list[trimesh.Trimesh]:
    """Return copies of `meshes` translated into a non-overlapping grid on
    the XY plane, each resting flat on Z=0, centered on the origin as a
    group. Used to pack multiple dowels together within one plate."""
    if not meshes:
        return []

    cell = max(float(m.extents[:2].max()) for m in meshes) + _PLATE_MARGIN
    n_cols = max(1, math.ceil(math.sqrt(len(meshes))))
    n_rows = math.ceil(len(meshes) / n_cols)

    arranged = []
    for i, mesh in enumerate(meshes):
        row, col = divmod(i, n_cols)
        m = _rest_on_plate(mesh)
        # center the whole grid on the origin so it can be dropped onto a
        # plate cell by a single translation afterwards
        target_center_xy = np.array([
            (col - (n_cols - 1) / 2.0) * cell,
            (row - (n_rows - 1) / 2.0) * cell,
        ])
        offset = np.zeros(3)
        offset[:2] = target_center_xy - m.bounds[:, :2].mean(axis=0)
        m.apply_translation(offset)
        arranged.append(m)
    return arranged


def _move_center_xy(mesh: trimesh.Trimesh, center_xy: np.ndarray) -> trimesh.Trimesh:
    """Translate `mesh` (in place) so its XY bounding-box center sits at
    `center_xy`, leaving Z untouched."""
    cur = mesh.bounds[:, :2].mean(axis=0)
    offset = np.zeros(3)
    offset[:2] = center_xy - cur
    mesh.apply_translation(offset)
    return mesh


def _model_settings_config(piece_ids: list[int], dowel_ids: list[int]) -> bytes:
    """Hand-written Metadata/model_settings.config giving each piece its own
    plate and gathering every dowel onto one shared plate. Object ids must
    match the <object id=...>/<item objectid=...> values trimesh already
    assigned in 3D/3dmodel.model (confirmed against a real Bambu Studio
    export: its <object id> in this file matches the build-item objectid
    directly, not some separate internal mesh id)."""
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<config>"]

    def add_object(oid: int, name: str) -> None:
        lines.append(f'  <object id="{oid}">')
        lines.append(f'    <metadata key="name" value="{name}"/>')
        lines.append('    <part id="1" subtype="normal_part">')
        lines.append(f'      <metadata key="name" value="{name}"/>')
        lines.append('      <metadata key="matrix" value="1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"/>')
        lines.append("    </part>")
        lines.append("  </object>")

    for oid in piece_ids:
        add_object(oid, f"piece{oid:02d}")
    for oid in dowel_ids:
        add_object(oid, "connectors")

    def add_plate(plate_num: int, name: str, object_ids: list[int]) -> None:
        lines.append("  <plate>")
        lines.append(f'    <metadata key="plater_id" value="{plate_num}"/>')
        lines.append(f'    <metadata key="plater_name" value="{name}"/>')
        lines.append('    <metadata key="locked" value="false"/>')
        for oid in object_ids:
            lines.append("    <model_instance>")
            lines.append(f'      <metadata key="object_id" value="{oid}"/>')
            lines.append('      <metadata key="instance_id" value="0"/>')
            lines.append(f'      <metadata key="identify_id" value="{oid}"/>')
            lines.append("    </model_instance>")
        lines.append("  </plate>")

    plate_num = 1
    for oid in piece_ids:
        add_plate(plate_num, "", [oid])
        plate_num += 1
    if dowel_ids:
        add_plate(plate_num, "Connectors", dowel_ids)

    lines.append("  <assemble>")
    lines.append("  </assemble>")
    lines.append("</config>")
    return ("\n".join(lines) + "\n").encode()


def _mark_as_bambu_project(model_xml: bytes) -> bytes:
    """Splice in the metadata tags a real Bambu Studio export carries on
    3D/3dmodel.model's root <model> element, ahead of <resources>: an
    Application tag using the "BambuStudio-" prefix (verified byte-for-byte
    against a reference export — earlier attempts used an "OrcaSlicer-"
    tag, which is not confirmed to satisfy Bambu Studio's own parser even
    though OrcaSlicer's is more lenient), plus the BambuStudio:3mfVersion
    tag that accompanies it in every real export. trimesh's exporter never
    writes either, so they're inserted here."""
    text = model_xml.decode("utf-8")
    marker = "<resources>"
    idx = text.find(marker)
    if idx == -1:
        return model_xml  # unexpected shape; leave untouched rather than guess
    tags = (
        f'<metadata name="Application">{_BAMBU_APPLICATION_TAG}</metadata>'
        '<metadata name="BambuStudio:3mfVersion">1</metadata>'
    )
    return (text[:idx] + tags + text[idx:]).encode("utf-8")


_PRINTER_PROFILE_BY_PRESET = {
    "a1_mini": ("Bambu Lab A1 mini 0.4 nozzle", "Bambu Lab A1 mini"),
    "256": ("Bambu Lab X1 Carbon 0.4 nozzle", "Bambu Lab X1 Carbon"),
    "h2": ("Bambu Lab H2D 0.4 nozzle", "Bambu Lab H2D"),
}


def _project_settings_config(bed_size_name: str) -> bytes:
    """Minimal Metadata/project_settings.config (a JSON print/filament
    profile). Its mere presence and non-emptiness is what's believed to keep
    plate creation from model_settings.config active on import — a real
    Bambu Studio project_settings.config is a full ~50KB profile, but only
    a non-empty "filament_colour" list has been confirmed to matter.

    Deliberately omits `filament_settings_id`/`print_settings_id`: naming a
    real system preset (e.g. "Bambu PLA Basic") without the rest of that
    preset's ~50KB of fields makes Bambu Studio treat it as a *modified*
    version of that preset on import, triggering a "Customized Preset -
    please confirm the G-codes are safe" dialog. Leaving those keys out
    avoids referencing any named preset at all, so there's nothing to flag
    as customized."""
    import json

    printer_settings_id, printer_model = _PRINTER_PROFILE_BY_PRESET.get(
        bed_size_name, _PRINTER_PROFILE_BY_PRESET["256"]
    )
    profile = {
        "printer_settings_id": printer_settings_id,
        "printer_model": printer_model,
        "filament_colour": ["#FFFFFF"],
        "nozzle_diameter": ["0.4"],
    }
    return json.dumps(profile, indent=2).encode("utf-8")


def _slice_info_config() -> bytes:
    """Metadata/slice_info.config: present in every real Bambu Studio
    export but not covered by any previous fix attempt. Its header
    identifies the file as coming from a genuine Bambu-family slicer
    client, which may gate multi-plate parsing independently of (or ahead
    of) the Application-tag check on 3D/3dmodel.model."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<config>\n"
        "  <header>\n"
        '    <header_item key="X-BBL-Client-Type" value="slicer"/>\n'
        '    <header_item key="X-BBL-Client-Version" value="02.07.01.62"/>\n'
        "  </header>\n"
        "</config>\n"
    ).encode()


def _inject_files(zip_bytes: bytes, replacements: dict[str, bytes], additions: dict[str, bytes]) -> bytes:
    """Return a copy of the (in-memory) `zip_bytes` archive with the given
    existing entries' contents replaced and the given new entries added."""
    src = zipfile.ZipFile(io.BytesIO(zip_bytes))
    out_buf = io.BytesIO()
    with zipfile.ZipFile(out_buf, "w", zipfile.ZIP_DEFLATED) as out:
        for item in src.infolist():
            content = replacements.get(item.filename, src.read(item.filename))
            out.writestr(item, content)
        for arcname, data in additions.items():
            out.writestr(arcname, data)
    return out_buf.getvalue()


def export_pieces(
    pieces: list[trimesh.Trimesh],
    basename: str,
    fmt: str,
    dowels: list[trimesh.Trimesh] | None = None,
    bed_size: "str | tuple[float, float] | None" = None,
) -> list[tuple[str, bytes]]:
    """Returns a list of (filename, bytes). For "stl" this is one entry per
    piece (each rested flat on Z=0) plus one combined, grid-arranged entry
    for all dowels (if any). For "3mf" it's a single entry: a multi-plate
    project with each piece on its own plate (in its original orientation)
    and all dowels standing on end together on one shared "Connectors" plate.
    `bed_size` (3MF only) is a preset name from BED_SIZE_PRESETS (e.g.
    "a1_mini", "256", "h2") or an explicit (width, depth) mm tuple; it sets
    the plate grid spacing so plates don't overlap on whichever bed the
    target printer actually has."""
    fmt = fmt.lower()
    if fmt not in SUPPORTED_FORMATS:
        raise ValueError(f"Unsupported output format '{fmt}'; choose one of {SUPPORTED_FORMATS}")

    dowels = dowels or []

    if fmt == "stl":
        files = []
        for i, piece in enumerate(pieces, start=1):
            data = _rest_on_plate(piece).export(file_type="stl")
            if isinstance(data, str):
                data = data.encode()
            files.append((f"{basename}_piece{i:02d}.stl", data))
        if dowels:
            combined = trimesh.util.concatenate(_arrange_grid(dowels))
            data = combined.export(file_type="stl")
            if isinstance(data, str):
                data = data.encode()
            files.append((f"{basename}_dowels.stl", data))
        return files

    # Plate layout: each piece gets its own plate (cells 0..N-1); all dowels
    # share one final "Connectors" plate (cell N). Objects are *physically*
    # positioned over their plate's cell center in addition to the config
    # mapping below. Only dowels get rotated "on end" — a cylinder's long
    # axis is unambiguous and always better printed vertically. Pieces keep
    # whatever orientation they already had (see module docstring).
    bed = resolve_bed_size(bed_size)
    bed_size_name = bed_size if isinstance(bed_size, str) and bed_size in BED_SIZE_PRESETS else DEFAULT_BED_SIZE
    n_plates = len(pieces) + (1 if dowels else 0)
    cols = _plate_columns(n_plates)

    scene = trimesh.Scene()
    piece_ids = []
    for i, piece in enumerate(pieces, start=1):
        m = _rest_on_plate(piece)
        _move_center_xy(m, _plate_cell_center(i - 1, cols, bed))
        scene.add_geometry(m, node_name=f"{basename}_piece{i:02d}")
        piece_ids.append(i)

    dowel_ids = []
    if dowels:
        connectors_center = _plate_cell_center(len(pieces), cols, bed)
        packed = _arrange_grid([_stand_on_end(d) for d in dowels])  # cluster centered on origin
        # shift the whole cluster (one rigid offset) onto the connectors cell,
        # preserving the dowels' relative spacing from _arrange_grid
        cluster_offset = np.zeros(3)
        cluster_offset[:2] = connectors_center
        for j, dowel in enumerate(packed, start=1):
            dowel.apply_translation(cluster_offset)
            scene.add_geometry(dowel, node_name=f"{basename}_dowel{j:02d}")
            dowel_ids.append(len(pieces) + j)

    data = scene.export(file_type="3mf")
    if isinstance(data, str):
        data = data.encode()

    src = zipfile.ZipFile(io.BytesIO(data))
    model_xml = _mark_as_bambu_project(src.read("3D/3dmodel.model"))
    data = _inject_files(
        data,
        replacements={"3D/3dmodel.model": model_xml},
        additions={
            "Metadata/model_settings.config": _model_settings_config(piece_ids, dowel_ids),
            "Metadata/project_settings.config": _project_settings_config(bed_size_name),
            "Metadata/slice_info.config": _slice_info_config(),
        },
    )
    return [(f"{basename}.3mf", data)]
