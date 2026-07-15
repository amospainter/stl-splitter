// Single reactive store for the whole split form + job lifecycle. Centralizing
// this here (instead of scattering module-level `let`s the way the old inline
// script did) is what lets every component just read/write plain reactive
// properties and have the DOM update itself, instead of manually re-rendering
// on every change.
import { reactive, computed, watch } from "vue";
import { startJob, streamJob, cancelJob, planePreview, cancelPlanePreview } from "./api.js";
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
  // The server-side preview_id of whichever /plane_preview request is
  // currently in flight for this axis, if any — lets a newer request (the
  // user kept typing) or an explicit Stop click actually cancel the
  // still-running backend computation, rather than only ever discarding
  // its eventual, wasted result once it finishes on its own.
  let inFlightPreviewId = null;

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

    // A still-running previous request for this axis is about to be
    // superseded — actually stop its backend computation instead of just
    // letting it burn CPU in the background until it finishes on its own
    // (its result would be discarded anyway via the requestToken check
    // below). Best-effort/fire-and-forget: nothing here needs to wait for it.
    if (inFlightPreviewId) cancelPlanePreview(inFlightPreviewId);
    const previewId = `${axisName}-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    inFlightPreviewId = previewId;

    const fd = new FormData();
    fd.set("file", form.file);
    fd.set("axis", axisName);
    fd.set("scale_axis", form.axis);
    fd.set("scale", form.scale);
    fd.set("target_dim", form.target_dim);
    fd.set("spacing", spacingVal);
    fd.set("pieces", piecesVal);
    fd.set("preview_id", previewId);

    // A pinch-avoiding safety search on a large/complex mesh can take
    // genuinely long (tens of seconds) — surfaced in the UI (see the
    // "computing…" hint and Stop button in AxisPlaneEditor) so a slow
    // response reads as "still working, and stoppable" rather than "broken".
    const myToken = ++requestToken;
    state.loading = true;
    let data;
    try {
      data = await planePreview(fd);
    } catch (err) {
      return; // leave whatever was on screen; a transient preview failure isn't worth surfacing loudly
    } finally {
      if (myToken === requestToken) state.loading = false;
      if (inFlightPreviewId === previewId) inFlightPreviewId = null;
    }

    // A newer request has since been issued (e.g. the user kept typing) —
    // drop this now-stale result instead of letting it overwrite whatever
    // the latest in-flight/completed request already produced. Same for a
    // result that only came back because it was cancelled (either by a
    // newer request above, or by the user's own Stop click): there's
    // nothing new to apply, just leave the screen as it was.
    if (myToken !== requestToken || data.cancelled) return;

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

  // Lets the user explicitly stop a long-running auto-placement
  // computation from the UI (the Stop button next to the "computing…"
  // spinner in AxisPlaneEditor), rather than only being able to wait it out
  // or supersede it by changing another field.
  function stopPreview() {
    if (inFlightPreviewId) cancelPlanePreview(inFlightPreviewId);
  }

  function schedulePreview() {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(fetchPreview, 350);
  }

  return { name: axisName, state, fit, serialize, addCut, removeCut, clearErrors, highlightErrors, fetchPreview, schedulePreview, stopPreview };
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
    allow_floating_regions: false,
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
    status: "idle", // idle | running | done | error | cancelled
    message: "",
    fraction: null,
    error: null,
    errorAxis: null,
    errorPositions: [],
    result: null,
    submittedSignature: null,
    jobId: null,
  });

  // Everything that actually affects the split output (cut planes, scale,
  // connector params, etc. — everything buildJobFormData sends except the
  // file itself), condensed to a comparable string. The plane editor stays
  // open and editable after a split finishes (dragging a cut, changing
  // connector settings, etc. all still work), but without this there was no
  // way to tell whether the pieces on screen still reflect what's currently
  // configured, or a "Split & preview" from before the last edit — resultStale
  // below is what drives the "Regenerate" prompt in App.js.
  function fieldsSignature() {
    return JSON.stringify({
      axis: form.axis, scale: form.scale, target_dim: form.target_dim, axis_order: form.axis_order,
      cuts: AXES.map((a) => axisControllers[a].serialize()),
      peg_diameter: form.peg_diameter, peg_length: form.peg_length, peg_clearance: form.peg_clearance,
      n_pegs: form.n_pegs, min_wall_thickness: form.min_wall_thickness, alignment_key: form.alignment_key,
      dowel_shape: form.dowel_shape, no_connectors: form.no_connectors, hollow_wall: form.hollow_wall,
      allow_non_watertight: form.allow_non_watertight, allow_floating_regions: form.allow_floating_regions,
      format: form.format, bed_size: form.bed_size,
    });
  }

  const resultStale = computed(() =>
    job.status === "done" && job.submittedSignature !== null && fieldsSignature() !== job.submittedSignature
  );

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
    fd.set("allow_floating_regions", form.allow_floating_regions ? "true" : "false");
    fd.set("format", form.format);
    fd.set("bed_size", form.bed_size);
    return fd;
  }

  async function submit() {
    const signature = fieldsSignature();
    job.status = "running";
    job.message = "Starting...";
    job.fraction = null;
    job.error = null;
    job.errorAxis = null;
    job.errorPositions = [];
    job.result = null;
    job.jobId = null;
    clearErrorHighlights();

    let jobId;
    try {
      jobId = await startJob(buildJobFormData());
      job.submittedSignature = signature;
      job.jobId = jobId;
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
      if (data.status === "cancelled") {
        job.status = "cancelled";
        job.message = "Cancelled";
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

  // Cooperative — see progress.py's JobCancelled. The server notices at its
  // next checkpoint (next cut/interface/piece), not instantly, so `job`
  // stays "running" with its existing message/progress bar until the
  // "cancelled" SSE message actually arrives; this just requests it.
  async function cancel() {
    if (job.status !== "running" || !job.jobId) return;
    try {
      await cancelJob(job.jobId);
    } catch (err) {
      // Best-effort: if the request itself fails (e.g. connection drop),
      // there's nothing more useful to do than leave the job running.
    }
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
    form, axisControllers, job, anyAxisUsable, resultStale,
    refreshAllAxisPreviews,
    onFileSelected, submit, cancel, applyPreset,
  };
}
