// Reactive store for the "Interactive split" mode: upload once, then
// repeatedly select a piece and cut IT specifically (a different axis/plane
// list per piece, not one flat list applied to the whole tree the way
// useSplitForm/state.js's "Quick split" mode works) until every leaf piece
// is print-bed-sized, then Finish to add connectors across the whole tree
// and export. See stlsplit/sessions.py for the server-side tree model this
// talks to.
import { reactive, ref, computed } from "vue";
import { streamJob } from "./api.js";
import {
  createSession, getPiecePreview, cutPiece, undoCut, deleteSession, finishSession,
  piecePlanePreview, rotatePiece,
} from "./interactiveApi.js";

export function useInteractiveSplit() {
  const form = reactive({
    file: null,
    scale: "",
    target_dim: "",
    scale_axis: "z",
    hollow_wall: "",
    allow_non_watertight: false,

    peg_diameter: 7, peg_length: 5, peg_clearance: 0.18,
    n_pegs: 4, min_wall_thickness: 1.2,
    dowel_shape: "round", alignment_key: false, no_connectors: false,

    format: "stl",
    bed_size: "256",
  });

  const session = reactive({ id: null, scaleFactor: 1 });

  // pieces: { [piece_id]: { parentId, childrenIds, isLeaf, extents, volume, bounds } }
  const tree = reactive({ pieces: {}, leaves: [] });
  const activePieceId = ref(null);

  // The single-axis cut editor for whichever piece is currently active.
  // `planes` entries: { value, tiltA, tiltB, hidden, active, errored } --
  // same shape AxisPlaneEditor.js/viewer.js's setAxisPlanes already expect,
  // so the existing gizmo-rendering code works unchanged.
  const editor = reactive({
    axis: "z",
    spacing: "",
    pieces: "",
    planes: [],
    bounds: null,
    loading: false,
    // Escape hatch for "a cut plane landed where the model pinches to
    // nothing" -- lets a cut through genuinely disconnected geometry go
    // ahead instead of forcing the user to keep nudging the plane. Reset per
    // piece (resetEditor below) rather than persisted globally, since it's a
    // property of one specific cut, not a session-wide preference.
    allowFloatingRegions: false,
  });
  let debounceTimer = null;
  let requestToken = 0;

  // Cached STL ArrayBuffers per piece id, fetched on demand (a session can
  // accumulate many pieces over a long editing session; no reason to hold
  // every one's geometry in the browser before it's actually viewed).
  const previewBuffers = reactive(new Map());

  const job = reactive({
    status: "idle", message: "", fraction: null, error: null, result: null, jobId: null,
  });

  const activePiece = computed(() => (activePieceId.value ? tree.pieces[activePieceId.value] : null));
  const leafPieces = computed(() => tree.leaves.map((id) => ({ id, ...tree.pieces[id] })));
  const hasSession = computed(() => !!session.id);

  async function ensurePreviewLoaded(pieceId) {
    if (previewBuffers.has(pieceId)) return;
    const { data_base64 } = await getPiecePreview(session.id, pieceId);
    const binary = atob(data_base64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    // Cache both the decoded ArrayBuffer (for MeshViewer) and the raw
    // base64 (so ResultModal-style "expand" can reuse it without a second
    // round trip) -- fetched once per piece, kept for the session's life.
    previewBuffers.set(pieceId, { buffer: bytes.buffer, dataBase64: data_base64 });
  }

  function resetEditor() {
    editor.axis = "z";
    editor.spacing = "";
    editor.pieces = "";
    editor.planes = [];
    editor.bounds = null;
    editor.allowFloatingRegions = false;
  }

  async function fetchPlanePreview() {
    if (!activePieceId.value) return;
    const fd = new FormData();
    fd.set("axis", editor.axis);
    fd.set("spacing", editor.spacing);
    fd.set("pieces", editor.pieces);

    const myToken = ++requestToken;
    editor.loading = true;
    let data;
    try {
      data = await piecePlanePreview(session.id, activePieceId.value, fd);
    } catch (err) {
      return; // leave whatever's on screen; a transient failure isn't worth surfacing loudly
    } finally {
      if (myToken === requestToken) editor.loading = false;
    }
    if (myToken !== requestToken) return; // superseded by a newer request

    editor.bounds = data.bounds;
    if (editor.spacing.trim() || String(editor.pieces).trim()) {
      editor.planes = data.planes.map((v) => ({
        value: v, tiltA: 0, tiltB: 0, hidden: false, active: false, errored: false,
      }));
    }
  }

  function schedulePlanePreview() {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(fetchPlanePreview, 350);
  }

  function setAxis(axis) {
    editor.axis = axis;
    editor.spacing = "";
    editor.pieces = "";
    editor.planes = [];
    fetchPlanePreview();
  }

  function addCut() {
    if (!editor.bounds) return;
    const idx = { x: 0, y: 1, z: 2 }[editor.axis];
    const lo = editor.bounds[0][idx];
    const hi = editor.bounds[1][idx];
    const values = editor.planes.map((c) => c.value).sort((a, b) => a - b);
    const marks = [lo, ...values, hi];
    let bestGap = -1, bestMid = (lo + hi) / 2;
    for (let i = 0; i < marks.length - 1; i++) {
      const gap = marks[i + 1] - marks[i];
      if (gap > bestGap) { bestGap = gap; bestMid = (marks[i] + marks[i + 1]) / 2; }
    }
    editor.planes.push({ value: bestMid, tiltA: 0, tiltB: 0, hidden: false, active: false, errored: false });
    editor.planes.sort((a, b) => a.value - b.value);
  }

  function removeCut(i) {
    editor.planes.splice(i, 1);
  }

  async function selectPiece(pieceId) {
    activePieceId.value = pieceId;
    resetEditor();
    const node = tree.pieces[pieceId];
    if (node) editor.bounds = node.bounds;
    await Promise.all([ensurePreviewLoaded(pieceId), fetchPlanePreview()]);
  }

  async function startSession(file) {
    form.file = file;
    const fd = new FormData();
    fd.set("file", file);
    fd.set("scale", form.scale);
    fd.set("target_dim", form.target_dim);
    fd.set("scale_axis", form.scale_axis);
    fd.set("hollow_wall", form.hollow_wall);
    fd.set("allow_non_watertight", form.allow_non_watertight ? "true" : "false");

    const data = await createSession(fd);
    session.id = data.session_id;
    session.scaleFactor = data.scale_factor;
    tree.pieces = {
      root: {
        parentId: null, childrenIds: [], isLeaf: true,
        extents: data.extents, volume: data.volume, bounds: data.bounds,
      },
    };
    tree.leaves = ["root"];
    await selectPiece("root");
  }

  function clearErrorHighlights() {
    editor.planes.forEach((c) => { c.errored = false; });
  }

  function highlightErrors(positions) {
    if (!positions || !positions.length) return;
    editor.planes.forEach((c) => {
      if (positions.some((p) => Math.abs(p - c.value) < 0.5)) c.errored = true;
    });
  }

  async function cutActivePiece() {
    if (!activePieceId.value || !editor.planes.length) return;
    clearErrorHighlights();
    const cutStr = editor.planes.map((c) => `${c.value}:${c.tiltA}:${c.tiltB}`).join(",");
    const fd = new FormData();
    fd.set("axis", editor.axis);
    fd.set("cut_planes", cutStr);
    fd.set("allow_floating_regions", editor.allowFloatingRegions ? "true" : "false");

    let data;
    try {
      data = await cutPiece(session.id, activePieceId.value, fd);
    } catch (err) {
      highlightErrors(err.positions);
      throw err;
    }

    const parentId = activePieceId.value;
    tree.pieces[parentId] = {
      ...tree.pieces[parentId],
      isLeaf: false,
      childrenIds: data.children.map((c) => c.piece_id),
    };
    for (const c of data.children) {
      tree.pieces[c.piece_id] = {
        parentId, childrenIds: [], isLeaf: true, extents: c.extents, volume: c.volume, bounds: c.bounds,
      };
    }
    tree.leaves = tree.leaves.filter((id) => id !== parentId).concat(data.children.map((c) => c.piece_id));
    // Fire-and-forget: pop each new leaf's preview into the grid as it loads
    // rather than blocking the cut on every child's STL round trip.
    data.children.forEach((c) => { ensurePreviewLoaded(c.piece_id); });

    // Force explicit re-selection rather than guessing which resulting piece
    // the user wants to look at next.
    activePieceId.value = null;
    resetEditor();
  }

  async function rotateActivePiece(axis, degrees) {
    if (!activePieceId.value) return;
    const pieceId = activePieceId.value;
    const fd = new FormData();
    fd.set("axis", axis);
    fd.set("degrees", degrees);
    const data = await rotatePiece(session.id, pieceId, fd);

    tree.pieces[pieceId] = {
      ...tree.pieces[pieceId],
      extents: data.extents, volume: data.volume, bounds: data.bounds,
    };
    // The cached preview/plane-editor state describe the PRE-rotation
    // geometry -- both are now stale and must be refetched, not just the
    // bounds (the cut planes the user had placed no longer mean anything
    // once the piece under them has been reoriented).
    previewBuffers.delete(pieceId);
    editor.planes = [];
    editor.spacing = "";
    editor.pieces = "";
    await Promise.all([ensurePreviewLoaded(pieceId), fetchPlanePreview()]);
  }

  // Removes every descendant of `pieceId` from the local tree mirror,
  // matching the cascading server-side undo in sessions.py's undo_cut.
  function pruneSubtree(pieceId) {
    const node = tree.pieces[pieceId];
    if (!node) return;
    for (const childId of node.childrenIds) {
      pruneSubtree(childId);
      delete tree.pieces[childId];
      tree.leaves = tree.leaves.filter((id) => id !== childId);
    }
    node.childrenIds = [];
    node.isLeaf = true;
  }

  async function undoPieceCut(pieceId) {
    await undoCut(session.id, pieceId);
    pruneSubtree(pieceId);
    if (!tree.leaves.includes(pieceId)) tree.leaves.push(pieceId);
    if (activePieceId.value && !tree.pieces[activePieceId.value]) activePieceId.value = null;
    ensurePreviewLoaded(pieceId); // was already loaded pre-cut in the common case, but cheap to confirm
  }

  async function finish() {
    job.status = "running";
    job.message = "Starting...";
    job.fraction = null;
    job.error = null;
    job.result = null;
    job.jobId = null;

    const fd = new FormData();
    fd.set("peg_diameter", form.peg_diameter);
    fd.set("peg_length", form.peg_length);
    fd.set("peg_clearance", form.peg_clearance);
    fd.set("n_pegs", form.n_pegs);
    fd.set("min_wall_thickness", form.min_wall_thickness);
    fd.set("alignment_key", form.alignment_key ? "true" : "false");
    fd.set("dowel_shape", form.dowel_shape);
    fd.set("no_connectors", form.no_connectors ? "true" : "false");
    fd.set("format", form.format);
    fd.set("bed_size", form.bed_size);

    let jobId;
    try {
      jobId = await finishSession(session.id, fd);
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
        return;
      }
      job.status = "done";
      job.result = data.result;
    });
  }

  async function reset() {
    if (session.id) await deleteSession(session.id);
    session.id = null;
    session.scaleFactor = 1;
    tree.pieces = {};
    tree.leaves = [];
    activePieceId.value = null;
    previewBuffers.clear();
    resetEditor();
    job.status = "idle";
    job.result = null;
  }

  return {
    form, session, tree, activePieceId, activePiece, leafPieces, hasSession,
    editor, previewBuffers, job,
    startSession, selectPiece, setAxis, addCut, removeCut, schedulePlanePreview,
    cutActivePiece, undoPieceCut, rotateActivePiece, finish, reset,
  };
}
