// Named settings presets, persisted with plain localStorage (no server round
// trip — these are per-browser convenience, not project data worth a backend
// endpoint). A preset captures every user-editable setting except the file
// itself and the derived scaleFactor: split spacing/pieces/bed seeds and
// axis order, connector params, interior, and output format. It deliberately
// does NOT capture the live manual cut-plane edits (AxisPlaneEditor's
// dragged positions) — those are mm coordinates against a specific mesh's
// bounds and don't carry over meaningfully to a different file.
const STORAGE_KEY = "stlsplit.presets.v1";

export const PRESET_FIELDS = [
  "axis", "scale", "target_dim",
  "x_spacing", "x_pieces", "y_spacing", "y_pieces", "z_spacing", "z_pieces",
  "axis_order", "bed_x", "bed_y", "bed_z",
  "peg_diameter", "peg_length", "peg_clearance", "n_pegs", "min_wall_thickness",
  "dowel_shape", "alignment_key", "no_connectors",
  "hollow_wall",
  "allow_non_watertight", "allow_floating_regions", "format", "bed_size",
];

function readAll() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? JSON.parse(raw) : {};
  } catch (err) {
    return {}; // corrupted/blocked storage shouldn't take down the form
  }
}

function writeAll(presets) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(presets));
    return true;
  } catch (err) {
    return false; // e.g. storage disabled or full; caller surfaces this
  }
}

export function listPresets() {
  return Object.keys(readAll()).sort((a, b) => a.localeCompare(b));
}

export function loadPreset(name) {
  const presets = readAll();
  return presets[name] || null;
}

export function savePreset(name, form) {
  const presets = readAll();
  const snapshot = {};
  for (const key of PRESET_FIELDS) snapshot[key] = form[key];
  presets[name] = snapshot;
  return writeAll(presets);
}

export function deletePreset(name) {
  const presets = readAll();
  delete presets[name];
  return writeAll(presets);
}
