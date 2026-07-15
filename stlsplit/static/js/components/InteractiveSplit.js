import { computed, reactive, ref } from "vue";
import { useInteractiveSplit } from "../interactiveState.js";
import { paletteHex } from "../viewer.js";
import MeshViewer from "./MeshViewer.js";
import ProgressBar from "./ProgressBar.js";
import PieceGrid from "./PieceGrid.js";
import ResultModal from "./ResultModal.js";
import { TILT_MAX, TILT_TICKS } from "./AxisPlaneEditor.js";

const AXES = ["x", "y", "z"];
const TILT_LABELS = { x: ["Tilt Y", "Tilt Z"], y: ["Tilt X", "Tilt Z"], z: ["Tilt X", "Tilt Y"] };

export default {
  name: "InteractiveSplit",
  components: { MeshViewer, ProgressBar, PieceGrid, ResultModal },
  template: `
    <div class="interactive-split">
      <template v-if="!store.hasSession.value">
        <div class="card mb-3 compact-form-card">
          <div class="card-header fw-semibold">Input</div>
          <div class="card-body">
            <label class="form-label">STL file</label>
            <input type="file" class="form-control mb-3" accept=".stl" @change="onFileChange">

            <div class="row g-2">
              <div class="col-4">
                <label class="form-label small">Scale factor</label>
                <input type="number" class="form-control form-control-sm" v-model="store.form.scale" step="any">
              </div>
              <div class="col-4">
                <label class="form-label small">Target dimension (mm)</label>
                <input type="number" class="form-control form-control-sm" v-model="store.form.target_dim" step="any">
              </div>
              <div class="col-4">
                <label class="form-label small">on axis</label>
                <select class="form-select form-select-sm" v-model="store.form.scale_axis">
                  <option value="x">X</option><option value="y">Y</option><option value="z">Z</option>
                </select>
              </div>
            </div>
            <div class="form-text small">"On axis" applies to the target dimension above -- e.g. target dimension 200mm on Z scales the whole model so its Z size becomes 200mm.</div>

            <label class="form-label small mt-2">Hollow to wall thickness (mm, blank = solid)</label>
            <input type="number" class="form-control form-control-sm" v-model="store.form.hollow_wall" step="any">
            <div class="form-text small">Applied once, before any cutting -- hollowing an already-cut piece has no well-defined meaning (which of its open cut faces would stay solid?).</div>

            <div class="form-check mt-2">
              <input type="checkbox" class="form-check-input" id="i-allow-nw" v-model="store.form.allow_non_watertight">
              <label class="form-check-label small" for="i-allow-nw">Allow non-watertight mesh</label>
            </div>

            <button type="button" class="btn btn-primary mt-3" :disabled="!pendingFile || starting" @click="onStart">
              <span v-if="starting" class="spinner-border spinner-border-sm me-2"></span>
              Start interactive session
            </button>
            <div v-if="startError" class="alert alert-danger mt-2 py-2 mb-0 small">{{ startError }}</div>
          </div>
        </div>
      </template>

      <template v-else>
        <div class="d-flex align-items-center justify-content-between mb-2">
          <span class="small text-secondary">Session active — {{ store.leafPieces.value.length }} current piece(s)</span>
          <button type="button" class="btn btn-outline-danger btn-sm" @click="onResetSession">
            <i class="bi bi-x-lg me-1"></i>Discard session
          </button>
        </div>

        <div class="card mb-3">
          <div class="card-header fw-semibold">
            Active piece
            <span class="text-secondary small ms-2" v-if="!store.activePieceId.value">— select a piece below to cut it</span>
          </div>
          <div class="card-body">
            <div class="row g-3">
              <div class="col-lg-5 order-lg-1">
                <template v-if="store.activePieceId.value">
                  <div class="d-flex align-items-center gap-2 mb-2 flex-wrap">
                    <span class="small text-secondary">Rotate:</span>
                    <div class="btn-group btn-group-sm" role="group" v-for="a in axes" :key="'rot-' + a">
                      <button type="button" class="btn btn-outline-secondary" :disabled="rotating"
                              @click="onRotate(a, -90)" :title="'Rotate -90&deg; around ' + a.toUpperCase()">
                        <i class="bi bi-arrow-counterclockwise"></i>{{ a.toUpperCase() }}
                      </button>
                      <button type="button" class="btn btn-outline-secondary" :disabled="rotating"
                              @click="onRotate(a, 90)" :title="'Rotate +90&deg; around ' + a.toUpperCase()">
                        {{ a.toUpperCase() }}<i class="bi bi-arrow-clockwise"></i>
                      </button>
                    </div>
                    <span class="spinner-border spinner-border-sm text-secondary" v-if="rotating"></span>
                  </div>
                  <div v-if="rotateError" class="alert alert-danger py-2 mb-2 small">{{ rotateError }}</div>

                  <div class="btn-group mb-2" role="group">
                    <button type="button" class="btn btn-sm" v-for="a in axes" :key="a"
                            :class="store.editor.axis === a ? 'btn-primary' : 'btn-outline-primary'"
                            @click="store.setAxis(a)">{{ a.toUpperCase() }}</button>
                  </div>

                  <div class="row g-2 mb-2">
                    <div class="col-6">
                      <label class="form-label small">Spacing (mm)</label>
                      <input type="number" class="form-control form-control-sm" v-model="store.editor.spacing"
                             @input="store.schedulePlanePreview()" step="any">
                    </div>
                    <div class="col-6">
                      <label class="form-label small">Pieces</label>
                      <input type="number" class="form-control form-control-sm" v-model="store.editor.pieces"
                             @input="store.schedulePlanePreview()" min="2" step="1">
                    </div>
                  </div>

                  <div class="d-flex align-items-center gap-2 mb-1">
                    <span class="spinner-border spinner-border-sm text-secondary" v-if="store.editor.loading"></span>
                    <span class="text-secondary small flex-grow-1">
                      {{ store.editor.loading ? "computing safe cut positions…" : (store.editor.planes.length ? "drag to fine-tune, or remove" : "no cuts yet") }}
                    </span>
                    <button type="button" class="btn btn-outline-primary btn-sm" @click="store.addCut()">
                      <i class="bi bi-plus-lg me-1"></i>Add cut
                    </button>
                  </div>

                  <div class="plane-list d-flex flex-column gap-2 mb-3" v-if="store.editor.bounds">
                    <div class="plane-item" v-for="(cut, i) in store.editor.planes" :key="i"
                         :class="{ 'cut-error': cut.errored }">
                      <div class="plane-row d-flex align-items-center gap-2">
                        <span class="plane-swatch" :style="{ background: paletteHex(i) }"></span>
                        <input type="range" class="form-range flex-grow-1" :min="lo" :max="hi" :step="step"
                               v-model.number="cut.value">
                        <span class="plane-value small text-nowrap">{{ cut.value.toFixed(1) }} mm</span>
                        <button type="button" class="btn btn-sm btn-link text-danger text-decoration-none p-1"
                                @click="store.removeCut(i)"><i class="bi bi-x-lg"></i></button>
                      </div>
                      <div class="tilt-row d-flex align-items-center gap-2 ps-4" v-for="field in ['tiltA', 'tiltB']" :key="field">
                        <span class="tilt-label small text-secondary" style="flex: 0 0 46px">{{ tiltLabel(field) }}</span>
                        <input type="range" class="form-range flex-grow-1" :min="-tiltMax" :max="tiltMax" step="1" list="i-tilt-ticks"
                               v-model.number="cut[field]"
                               :style="{ accentColor: paletteHex(i) }">
                        <span class="tilt-value small text-secondary" style="flex: 0 0 36px">{{ cut[field] }}&deg;</span>
                      </div>
                    </div>
                  </div>
                  <datalist id="i-tilt-ticks">
                    <option v-for="t in tiltTicks" :key="t" :value="t"></option>
                  </datalist>

                  <div class="form-check mb-2">
                    <input type="checkbox" class="form-check-input" id="i-allow-floating" v-model="store.editor.allowFloatingRegions">
                    <label class="form-check-label small" for="i-allow-floating">
                      Allow disconnected regions (skip the "pinches to nothing" check)
                    </label>
                  </div>

                  <button type="button" class="btn btn-primary" :disabled="!store.editor.planes.length || cutting"
                          @click="onCut">
                    <span v-if="cutting" class="spinner-border spinner-border-sm me-2"></span>
                    Cut this piece
                  </button>
                  <div v-if="cutError" class="alert alert-danger mt-2 py-2 mb-0 small">
                    {{ cutError }}
                    <template v-if="!store.editor.allowFloatingRegions && /pinches to nothing/.test(cutError)">
                      <br>
                      <button type="button" class="btn btn-link btn-sm p-0 mt-1" @click="store.editor.allowFloatingRegions = true">
                        Allow it and cut anyway
                      </button>
                    </template>
                  </div>
                </template>
              </div>

              <div class="col-lg-7 order-lg-2">
                <div class="d-flex align-items-center gap-2 mb-2 flex-wrap small" v-if="store.activePieceId.value">
                  <div class="form-check mb-0">
                    <input type="checkbox" class="form-check-input" id="i-show-bed" v-model="bedOverlay.show">
                    <label class="form-check-label" for="i-show-bed">Show print bed</label>
                  </div>
                  <template v-if="bedOverlay.show">
                    <input type="number" class="form-control form-control-sm" style="width: 70px" v-model.number="bedOverlay.x" title="Bed X (mm)">
                    <span class="text-secondary">&times;</span>
                    <input type="number" class="form-control form-control-sm" style="width: 70px" v-model.number="bedOverlay.y" title="Bed Y (mm)">
                    <span class="text-secondary">&times;</span>
                    <input type="number" class="form-control form-control-sm" style="width: 70px" v-model.number="bedOverlay.z" title="Bed Z / height (mm)">
                    <span class="text-secondary">mm</span>
                    <div class="form-check mb-0 ms-2">
                      <input type="checkbox" class="form-check-input" id="i-bed-grid" v-model="bedOverlay.grid">
                      <label class="form-check-label" for="i-bed-grid">Tile as grid</label>
                    </div>
                    <div class="form-check mb-0 ms-2">
                      <input type="checkbox" class="form-check-input" id="i-bed-align" v-model="bedOverlay.alignBottom">
                      <label class="form-check-label" for="i-bed-align" title="Sit the bed on the model's lowest point instead of centering it">Align to bottom</label>
                    </div>
                    <span class="text-secondary" v-if="bedOverlay.grid && bedGridCounts">
                      {{ bedGridCounts[0] }} &times; {{ bedGridCounts[1] }} &times; {{ bedGridCounts[2] }}
                      = {{ bedGridCounts[0] * bedGridCounts[1] * bedGridCounts[2] }} bed(s)
                    </span>
                  </template>
                </div>

                <div class="editor-wrap" v-if="store.activePieceId.value">
                  <!-- No :scale-factor here (unlike the Quick-split viewer): every
                       piece preview STL fetched via getPiecePreview is exported
                       from mesh geometry that already went through scale_mesh()
                       server-side at session creation, so it's already at final
                       scale -- multiplying again here would double-scale the
                       rendered mesh relative to the gizmo plane, which is sized
                       directly from that same already-scaled bounds. -->
                  <mesh-viewer :buffer="activeBuffer" :axis-data="axisData" :bed-dims="bedDims" :bed-grid-counts="bedGridCounts"
                               :bed-align-bottom="bedOverlay.alignBottom"
                               show-toolbar @plane-drag="onPlaneDrag"></mesh-viewer>
                  <div class="form-text small mt-1">Drag a plane directly in the view to reposition it, or use the slider below.</div>
                </div>
              </div>
            </div>

            <h3 class="h5 mt-4">Current pieces</h3>
            <div class="row row-cols-2 row-cols-sm-3 row-cols-md-4 g-3">
              <div class="col" v-for="p in store.leafPieces.value" :key="p.id">
                <div class="card piece-card h-100" :class="{ 'border-primary': p.id === store.activePieceId.value }">
                  <div class="piece-canvas" style="cursor: pointer" @click="store.selectPiece(p.id)">
                    <mesh-viewer v-if="bufferFor(p.id)" :buffer="bufferFor(p.id)" :color="0x9ec5fe" height="140px"></mesh-viewer>
                    <div v-else class="d-flex align-items-center justify-content-center text-secondary small" style="height:140px">
                      loading…
                    </div>
                  </div>
                  <div class="card-body py-2 px-2 small">
                    <div class="text-truncate">{{ p.id.slice(0, 8) }}</div>
                    <div class="text-secondary">{{ p.extents.map(e => e.toFixed(0)).join(' × ') }} mm</div>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>

        <div v-if="cutHistory.length" class="mb-3">
          <h3 class="h6 text-secondary text-uppercase mt-4">Cut history</h3>
          <ul class="list-group list-group-flush small">
            <li class="list-group-item d-flex justify-content-between align-items-center" v-for="p in cutHistory" :key="p.id">
              <span>{{ p.id.slice(0, 8) }} — {{ p.childrenIds.length }} piece(s)</span>
              <button type="button" class="btn btn-outline-secondary btn-sm" @click="store.undoPieceCut(p.id)">
                <i class="bi bi-arrow-counterclockwise me-1"></i>Undo
              </button>
            </li>
          </ul>
        </div>

        <div class="card compact-form-card">
          <div class="card-header fw-semibold">Finish</div>
          <div class="card-body">
            <div class="form-check mb-2">
              <input type="checkbox" class="form-check-input" id="i-no-conn" v-model="store.form.no_connectors">
              <label class="form-check-label" for="i-no-conn">No connectors (cut only)</label>
            </div>
            <template v-if="!store.form.no_connectors">
              <div class="row g-2">
                <div class="col-3"><label class="form-label small">Dowel shape</label>
                  <select class="form-select form-select-sm" v-model="store.form.dowel_shape">
                    <option value="round">Round</option>
                    <option value="d">D-shaped (anti-rotation)</option>
                    <option value="square">Square</option>
                    <option value="hex">Hexagon</option>
                  </select>
                </div>
                <div class="col-3"><label class="form-label small">Diameter</label>
                  <input type="number" class="form-control form-control-sm" v-model="store.form.peg_diameter" step="any"></div>
                <div class="col-3"><label class="form-label small">Socket depth</label>
                  <input type="number" class="form-control form-control-sm" v-model="store.form.peg_length" step="any"></div>
                <div class="col-3"><label class="form-label small">Clearance</label>
                  <input type="number" class="form-control form-control-sm" v-model="store.form.peg_clearance" step="any"></div>
              </div>
              <div class="row g-2 mt-1">
                <div class="col-4"><label class="form-label small">Per interface</label>
                  <input type="number" class="form-control form-control-sm" v-model="store.form.n_pegs" step="1"></div>
                <div class="col-4"><label class="form-label small">Min wall thickness</label>
                  <input type="number" class="form-control form-control-sm" v-model="store.form.min_wall_thickness" step="any"></div>
              </div>
              <div class="form-check mt-2">
                <input type="checkbox" class="form-check-input" id="i-align-key" v-model="store.form.alignment_key">
                <label class="form-check-label small" for="i-align-key">Add alignment key (D-flat) to round dowels</label>
              </div>
            </template>

            <div class="row g-2 mt-2">
              <div class="col-6">
                <label class="form-label small">Format</label>
                <select class="form-select form-select-sm" v-model="store.form.format">
                  <option value="stl">Separate STL files (zip)</option>
                  <option value="3mf">Single 3MF project</option>
                </select>
              </div>
              <div class="col-6" v-if="store.form.format === '3mf'">
                <label class="form-label small">Print bed (3MF plate layout)</label>
                <select class="form-select form-select-sm" v-model="store.form.bed_size">
                  <option value="a1_mini">A1 mini (180 &times; 180mm)</option>
                  <option value="256">X1C / P1P / P1S / A1 (256 &times; 256mm)</option>
                  <option value="h2">H2D / H2S (350 &times; 320mm)</option>
                </select>
              </div>
            </div>

            <button type="button" class="btn btn-success mt-3" :disabled="store.job.status === 'running'" @click="store.finish()">
              <span v-if="store.job.status === 'running'" class="spinner-border spinner-border-sm me-2"></span>
              Finish &amp; export
            </button>

            <progress-bar v-if="store.job.status === 'running'" :message="store.job.message" :fraction="store.job.fraction"></progress-bar>
            <div v-if="store.job.status === 'error'" class="alert alert-danger mt-2 py-2 small">{{ store.job.error }}</div>
            <div v-if="store.job.status === 'cancelled'" class="alert alert-secondary mt-2 py-2 small">Cancelled.</div>

            <template v-if="store.job.status === 'done' && store.job.result">
              <p class="mt-3 mb-2">
                {{ store.job.result.piece_count }} piece(s) generated.
                <template v-if="store.job.result.dowel_count">{{ store.job.result.dowel_count }} dowel(s).</template>
              </p>
              <a class="btn btn-outline-primary mb-3" :href="downloadUrl" :download="store.job.result.download_name">
                <i class="bi bi-download me-1"></i>Download {{ store.job.result.download_name }}
              </a>
              <piece-grid :pieces="store.job.result.previews" @expand="openModal"></piece-grid>
            </template>
          </div>
        </div>
      </template>
    </div>

    <result-modal :piece="modalPiece" :color="modalColor" @close="modalPiece = null"></result-modal>
  `,
  setup() {
    const store = useInteractiveSplit();
    const pendingFile = ref(null);
    const starting = ref(false);
    const startError = ref("");
    const cutting = ref(false);
    const cutError = ref("");
    const rotating = ref(false);
    const rotateError = ref("");
    const modalPiece = ref(null);
    const modalColor = ref(0xe4e7ee);

    const bedOverlay = reactive({ show: false, grid: false, alignBottom: true, x: 255, y: 255, z: 255 });
    const bedDims = computed(() => (bedOverlay.show ? [bedOverlay.x, bedOverlay.y, bedOverlay.z] : null));
    // How many bed-sized cells the ACTIVE piece would need along each axis --
    // just ceil(piece extent / bed dimension), floored at 1 so a piece
    // smaller than the bed still shows a single cell rather than zero.
    const bedGridCounts = computed(() => {
      if (!bedOverlay.show || !bedOverlay.grid) return null;
      const piece = store.activePiece.value;
      if (!piece || !piece.extents) return null;
      const dims = [bedOverlay.x, bedOverlay.y, bedOverlay.z];
      return dims.map((d, i) => (d > 0 ? Math.max(1, Math.ceil(piece.extents[i] / d)) : 1));
    });

    function onPlaneDrag(axisName, index, newValue) {
      if (axisName !== store.editor.axis) return; // dragging a plane from a different axis than the one currently shown shouldn't happen, but guard anyway
      const cut = store.editor.planes[index];
      if (!cut || !store.editor.bounds) return;
      const axisIdx = { x: 0, y: 1, z: 2 }[axisName];
      const lo = store.editor.bounds[0][axisIdx];
      const hi = store.editor.bounds[1][axisIdx];
      cut.value = Math.min(hi, Math.max(lo, newValue));
    }

    function onFileChange(e) {
      pendingFile.value = e.target.files[0] || null;
    }

    async function onStart() {
      if (!pendingFile.value) return;
      starting.value = true;
      startError.value = "";
      try {
        await store.startSession(pendingFile.value);
      } catch (err) {
        startError.value = err.message;
      } finally {
        starting.value = false;
      }
    }

    async function onResetSession() {
      await store.reset();
      pendingFile.value = null;
    }

    async function onCut() {
      cutting.value = true;
      cutError.value = "";
      try {
        await store.cutActivePiece();
      } catch (err) {
        cutError.value = err.message;
      } finally {
        cutting.value = false;
      }
    }

    async function onRotate(axis, degrees) {
      rotating.value = true;
      rotateError.value = "";
      try {
        await store.rotateActivePiece(axis, degrees);
      } catch (err) {
        rotateError.value = err.message;
      } finally {
        rotating.value = false;
      }
    }

    function bufferFor(pieceId) {
      const cached = store.previewBuffers.get(pieceId);
      return cached ? cached.buffer : null;
    }

    const activeBuffer = computed(() => bufferFor(store.activePieceId.value));

    const axisData = computed(() => ({
      [store.editor.axis]: { planes: store.editor.planes, bounds: store.editor.bounds },
    }));

    const lo = computed(() => {
      const idx = { x: 0, y: 1, z: 2 }[store.editor.axis];
      return store.editor.bounds ? store.editor.bounds[0][idx] : 0;
    });
    const hi = computed(() => {
      const idx = { x: 0, y: 1, z: 2 }[store.editor.axis];
      return store.editor.bounds ? store.editor.bounds[1][idx] : 1;
    });
    const step = computed(() => Math.max((hi.value - lo.value) / 500, 0.001));

    function tiltLabel(field) {
      return TILT_LABELS[store.editor.axis][field === "tiltA" ? 0 : 1];
    }

    const cutHistory = computed(() =>
      Object.entries(store.tree.pieces)
        .filter(([, p]) => !p.isLeaf)
        .map(([id, p]) => ({ id, ...p }))
    );

    const downloadUrl = computed(() => {
      if (!store.job.result) return "";
      const buf = Uint8Array.from(atob(store.job.result.download_base64), (c) => c.charCodeAt(0));
      return URL.createObjectURL(new Blob([buf], { type: store.job.result.download_mime }));
    });

    function openModal(piece, color) {
      modalPiece.value = piece;
      modalColor.value = color ?? 0xe4e7ee;
    }

    return {
      store, axes: AXES, paletteHex,
      pendingFile, starting, startError, cutting, cutError, rotating, rotateError,
      onFileChange, onStart, onResetSession, onCut, onRotate, onPlaneDrag,
      bufferFor, activeBuffer, axisData, bedOverlay, bedDims, bedGridCounts, lo, hi, step, tiltLabel, cutHistory, downloadUrl,
      modalPiece, modalColor, openModal, tiltMax: TILT_MAX, tiltTicks: TILT_TICKS,
    };
  },
};
