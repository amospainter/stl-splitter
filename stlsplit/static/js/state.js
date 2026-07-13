// Single reactive store for the whole split form + job lifecycle. Centralizing
// this here (instead of scattering module-level `let`s the way the old inline
// script did) is what lets every component just read/write plain reactive
// properties and have the DOM update itself, instead of manually re-rendering
// on every change.
import { reactive, computed, watch } from "vue";
import { startJob, streamJob, planePreview } from "./api.js";
import { AXIS_INDEX } from "./viewer.js";
import { PRESET_FIELDS } from "./presets.js";

const AXES = ["x", "y", "z"];
const BED_FIELD = { x: "bed_x", y: "bed_y", z: "bed_z" };

// Vue 3's v-model auto-casts <input type="number"> to an actual JS number
// once a user types into it (it starts out as the empty string "" until
// then) — so every numeric form field is a string-or-number union in
// practice, never reliably one or the other. Blank-checking with `.trim()`
// directly on these values throws the moment a real number lands there;
// this normalizes either shape to a trimmed string first.
function strVal(v) {
  return v === null || v === undefined ? "" : String(v).trim();
}

function makeAxisController(axisName, form) {
  const idx = AXIS_INDEX[axisName];
  // `planes` entries: { value, tiltA, tiltB, hidden, active, errored }
  const state = reactive({ planes: [], bounds: null, loading: false });
  let debounceTimer = null;
  // Concurrent /plane_preview requests for the same axis can resolve
  // out of order (a slower request started earlier can finish after a
  // faster one started later, especially now that the backend runs them
  // truly concurrently via asyncio.to_thread) — without this guard, the
  // stale response would overwrite the fresher result, making the plane
  // editor look like it's flickering/updating over time instead of
  // settling once. Each fetch stamps a token; only the fetch that's still
  // the *latest* one issued is allowed to apply its result.
  let requestToken = 0;

  // Bed X/Y/Z used to switch this axis into a separate, non-editable
  // recursive auto-fit mode entirely (see autofit.py — it re-checks each
  // resulting piece and can cut it differently than its siblings, which a
  // flat per-axis plane list can't represent). Instead, a bed dimension is
  // now just a convenience seed for this axis's spacing: it's used as the
  // spacing value (same math as typing it into the "Spacing (mm)" field)
  // whenever that field itself is blank, so the result lands in the same
  // editable plane list either way. The actual split is always submitted as
  // explicit cut planes (buildJobFormData below), never as bed_dims.
  function effectiveSpacing() {
    const explicit = strVal(form[`${axisName}_spacing`]);
    if (explicit) return explicit;
    return strVal(form[BED_FIELD[axisName]]);
  }

  // Conservative fit check against this axis's bed dimension (if any): the
  // true piece is always a subset of the slab between two consecutive cut
  // planes, so comparing slab width against the bed size can only ever
  // *undersell* how well something fits, never falsely claim a fit that
  // isn't real. Purely client-side — no extra request, recomputed from data
  // already on hand (bounds + planes) whenever either changes.
  const fit = computed(() => {
    const bed = parseFloat(form[BED_FIELD[axisName]]);
    if (!state.bounds || !bed || !(bed > 0)) return null;
    const lo = state.bounds[0][idx];
    const hi = state.bounds[1][idx];
    const marks = [lo, ...state.planes.filter((c) => !c.hidden).map((c) => c.value).sort((a, b) => a - b), hi];
    let worst = 0;
    for (let i = 0; i < marks.length - 1; i++) worst = Math.max(worst, marks[i + 1] - marks[i]);
    return { ok: worst <= bed + 1e-6, worst, bed };
  });

  function serialize() {
    return state.planes.map((c) => `${c.value}:${c.tiltA}:${c.tiltB}`).join(",");
  }

  function addCut() {
    if (!state.bounds) return;
    const lo = state.bounds[0][idx];
    const hi = state.bounds[1][idx];
    const values = state.planes.map((c) => c.value).sort((a, b) => a - b);
    const marks = [lo, ...values, hi];
    // Insert at the midpoint of whichever gap between existing cuts (or the
    // mesh bounds) is currently largest, so a new cut starts somewhere useful.
    let bestGap = -1, bestMid = (lo + hi) / 2;
    for (let i = 0; i < marks.length - 1; i++) {
      const gap = marks[i + 1] - marks[i];
      if (gap > bestGap) {
        bestGap = gap;
        bestMid = (marks[i] + marks[i + 1]) / 2;
      }
    }
    state.planes.push({ value: bestMid, tiltA: 0, tiltB: 0, hidden: false, active: false, errored: false });
    state.planes.sort((a, b) => a.value - b.value);
  }

  function removeCut(i) {
    state.planes.splice(i, 1);
  }

  function clearErrors() {
    let changed = false;
    state.planes.forEach((c) => {
      if (c.errored) changed = true;
      c.errored = false;
    });
    return changed;
  }

  function highlightErrors(positions) {
    let found = false;
    state.planes.forEach((c) => {
      if (positions.some((p) => Math.abs(p - c.value) < 0.5)) {
        c.errored = true;
        found = true;
      }
    });
    return found;
  }

  async function fetchPreview() {
    const spacingVal = effectiveSpacing();
    const piecesVal = strVal(form[`${axisName}_pieces`]);

    if (!form.file) {
      state.planes = [];
      state.bounds = null;
      return;
    }

    const fd = new FormData();
    fd.set("file", form.file);
    fd.set("axis", axisName);
    fd.set("scale_axis", form.axis);
    fd.set("scale", form.scale);
    fd.set("target_dim", form.target_dim);
    fd.set("spacing", spacingVal);
    fd.set("pieces", piecesVal);

    // A pinch-avoiding safety search on a large/complex mesh can take
    // genuinely long (tens of seconds) — surfaced in the UI (see the
    // "computing…" hint in AxisPlaneEditor) so a slow response reads as
    // "still working" rather than "broken".
    const myToken = ++requestToken;
    state.loading = true;
    let data;
    try {
      data = await planePreview(fd);
    } catch (err) {
      return; // leave whatever was on screen; a transient preview failure isn't worth surfacing loudly
    } finally {
      if (myToken === requestToken) state.loading = false;
    }

    // A newer request has since been issued (e.g. the user kept typing) —
    // drop this now-stale result instead of letting it overwrite whatever
    // the latest in-flight/completed request already produced.
    if (myToken !== requestToken) return;

    // Only let the server's auto-computed planes replace what's on screen
    // when spacing/pieces actually asked for a count; otherwise (manual
    // mode) keep whatever the user has added/removed/dragged so far and
    // just refresh bounds/scale (e.g. after a scale change). Auto planes
    // are always plain (untilted) positions.
    if (data.auto) {
      state.planes = data.planes.map((v) => ({ value: v, tiltA: 0, tiltB: 0, hidden: false, active: false, errored: false }));
    }
    state.bounds = data.bounds;
    form.scaleFactor = data.scale_factor;
  }

  function schedulePreview() {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(fetchPreview, 350);
  }

  return { name: axisName, state, fit, serialize, addCut, removeCut, clearErrors, highlightErrors, fetchPreview, schedulePreview };
}

export function useSplitForm() {
  const form = reactive({
    file: null,
    axis: "z",
    scale: "",
    target_dim: "",
    scaleFactor: 1,

    x_spacing: "", x_pieces: "",
    y_spacing: "", y_pieces: "",
    z_spacing: "", z_pieces: "",
    axis_order: "zxy",
    bed_x: "", bed_y: "", bed_z: "",

    peg_diameter: 7, peg_length: 5, peg_clearance: 0.18,
    n_pegs: 4, min_wall_thickness: 1.2,
    dowel_shape: "round", alignment_key: false, no_connectors: false,

    hollow_wall: "",

    allow_non_watertight: false,
    format: "stl",
    bed_size: "256",
  });

  const axisControllers = {
    x: makeAxisController("x", form),
    y: makeAxisController("y", form),
    z: makeAxisController("z", form),
  };

  const anyAxisUsable = computed(() =>
    AXES.some((a) => axisControllers[a].state.bounds || axisControllers[a].state.loading)
  );

  function refreshAllAxisPreviews() {
    AXES.forEach((a) => axisControllers[a].fetchPreview());
  }

  // Anything that changes every axis's scale/bounds debounce-refreshes all
  // three previews (each axis's own spacing/piece-count/bed-size field only
  // needs to debounce-refresh *that* axis, mirrored below). Debouncing here
  // too matters as much as the per-axis fields: typing a multi-digit target
  // dimension fires one change per keystroke, and without a debounce each
  // keystroke kicks off a fresh round of (possibly tens-of-seconds-long)
  // /plane_preview requests for all three axes at once, piling up in flight.
  let globalDebounceTimer = null;
  function scheduleRefreshAll() {
    clearTimeout(globalDebounceTimer);
    globalDebounceTimer = setTimeout(refreshAllAxisPreviews, 350);
  }
  [() => form.axis, () => form.scale, () => form.target_dim]
    .forEach((src) => watch(src, scheduleRefreshAll));
  AXES.forEach((a) => {
    watch(() => form[`${a}_spacing`], () => axisControllers[a].schedulePreview());
    watch(() => form[`${a}_pieces`], () => axisControllers[a].schedulePreview());
    watch(() => form[BED_FIELD[a]], () => axisControllers[a].schedulePreview());
  });

  const job = reactive({
    status: "idle", // idle | running | done | error
    message: "",
    fraction: null,
    error: null,
    errorAxis: null,
    errorPositions: [],
    result: null,
  });

  function clearErrorHighlights() {
    AXES.forEach((a) => {
      if (axisControllers[a].clearErrors()) {
        // no-op: reactive arrays already trigger re-render
      }
    });
  }

  function buildJobFormData() {
    // Bed X/Y/Z are only ever used client-side to seed each axis's spacing
    // (see effectiveSpacing() above) so the resulting cuts land in the same
    // editable plane list as manual spacing/pieces — the actual job is
    // always submitted as explicit cut_planes_x/y/z, never as bed_x/y/z, so
    // the backend's recursive auto-fit path (which can't be edited this way)
    // is never engaged from the web UI.
    const fd = new FormData();
    fd.set("file", form.file);
    fd.set("axis", form.axis);
    fd.set("scale", form.scale);
    fd.set("target_dim", form.target_dim);
    fd.set("axis_order", form.axis_order);
    AXES.forEach((a) => {
      fd.set(`cut_planes_${a}`, axisControllers[a].serialize());
    });
    fd.set("peg_diameter", form.peg_diameter);
    fd.set("peg_length", form.peg_length);
    fd.set("peg_clearance", form.peg_clearance);
    fd.set("n_pegs", form.n_pegs);
    fd.set("min_wall_thickness", form.min_wall_thickness);
    fd.set("alignment_key", form.alignment_key ? "true" : "false");
    fd.set("dowel_shape", form.dowel_shape);
    fd.set("no_connectors", form.no_connectors ? "true" : "false");
    fd.set("hollow_wall", form.hollow_wall);
    fd.set("allow_non_watertight", form.allow_non_watertight ? "true" : "false");
    fd.set("format", form.format);
    fd.set("bed_size", form.bed_size);
    return fd;
  }

  async function submit() {
    job.status = "running";
    job.message = "Starting...";
    job.fraction = null;
    job.error = null;
    job.errorAxis = null;
    job.errorPositions = [];
    job.result = null;
    clearErrorHighlights();

    let jobId;
    try {
      jobId = await startJob(buildJobFormData());
    } catch (err) {
      job.status = "error";
      job.error = err.message;
      return;
    }

    streamJob(jobId, (data) => {
      if (data.status === "running") {
        job.message = data.message;
        job.fraction = data.fraction;
        return;
      }
      if (data.status === "error") {
        job.status = "error";
        job.error = data.error;
        job.errorAxis = data.error_axis;
        job.errorPositions = data.error_positions || [];
        if (job.errorAxis && axisControllers[job.errorAxis]) {
          axisControllers[job.errorAxis].highlightErrors(job.errorPositions);
        }
        return;
      }
      job.status = "done";
      job.result = data.result;
    });
  }

  function onFileSelected(file) {
    form.file = file;
    job.status = "idle";
    job.result = null;
    if (file) refreshAllAxisPreviews();
  }

  // Applies a saved preset snapshot (see presets.js) onto the live form.
  // Assigning through the reactive `form` object (rather than replacing it)
  // is what makes every existing watcher — the debounced per-axis preview
  // refetches, the format/bed-size UI toggles — fire naturally, exactly as
  // if the user had retyped each field by hand.
  function applyPreset(snapshot) {
    if (!snapshot) return;
    for (const key of PRESET_FIELDS) {
      if (key in snapshot) form[key] = snapshot[key];
    }
  }

  return {
    form, axisControllers, job, anyAxisUsable,
    refreshAllAxisPreviews,
    onFileSelected, submit, applyPreset,
  };
}
