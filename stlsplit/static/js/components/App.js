import { ref, computed } from "vue";
import { useSplitForm } from "../state.js";
import { SUPPORTED_SHAPES } from "../config.js";

import InputSection from "./InputSection.js";
import PresetBar from "./PresetBar.js";
import ScaleSection from "./ScaleSection.js";
import SplitSection from "./SplitSection.js";
import AxisPlaneEditor from "./AxisPlaneEditor.js";
import ConnectorsSection from "./ConnectorsSection.js";
import InteriorSection from "./InteriorSection.js";
import OutputSection from "./OutputSection.js";
import MeshViewer from "./MeshViewer.js";
import ProgressBar from "./ProgressBar.js";
import PieceGrid from "./PieceGrid.js";
import ResultModal from "./ResultModal.js";

export default {
  name: "App",
  components: {
    InputSection, PresetBar, ScaleSection, SplitSection, AxisPlaneEditor, ConnectorsSection,
    InteriorSection, OutputSection, MeshViewer, ProgressBar, PieceGrid, ResultModal,
  },
  template: `
    <div class="layout">
      <form class="split-form" :class="{ busy: job.status === 'running' }" @submit.prevent="onSubmit">
        <input-section @file-selected="onFileSelected"></input-section>
        <preset-bar :form="form" @apply="applyPreset"></preset-bar>
        <split-section :form="form" :axis-controllers="axisControllers"></split-section>
        <connectors-section :form="form" :shapes="shapes"></connectors-section>
        <output-section :form="form"></output-section>
        <scale-section :form="form"></scale-section>
        <interior-section :form="form"></interior-section>
        <div id="submit-bar">
          <button type="submit" class="btn btn-primary w-100 py-2" :disabled="job.status === 'running'">
            <span v-if="job.status === 'running'" class="spinner-border spinner-border-sm me-2"></span>
            {{ job.status === 'running' ? 'Splitting…' : 'Split & preview' }}
          </button>
        </div>
      </form>

      <div class="preview-pane">
        <h3 class="h5">Editor</h3>
        <div class="editor-wrap">
          <mesh-viewer :buffer="inputBuffer" :scale-factor="form.scaleFactor"
                       :axis-data="axisData" show-toolbar></mesh-viewer>
        </div>
        <div id="plane-editor" class="card bg-body-tertiary" v-show="anyAxisUsable">
          <div class="card-body">
            <axis-plane-editor v-for="a in ['x','y','z']" :key="a" :axis-name="a" :controller="axisControllers[a]"></axis-plane-editor>
            <button type="button" class="btn btn-outline-secondary btn-sm mt-1" @click="refreshAllAxisPreviews">
              <i class="bi bi-arrow-counterclockwise me-1"></i>Reset to auto placement
            </button>
          </div>
        </div>

        <h3 class="h5 mt-4">Pieces</h3>
        <div v-if="job.status === 'error'" class="alert alert-danger">
          Error: {{ job.error }}<span v-if="errorHighlighted"> (see the highlighted cut below)</span>
        </div>
        <progress-bar v-if="job.status === 'running'" :message="job.message" :fraction="job.fraction"></progress-bar>

        <template v-if="job.status === 'done' && job.result">
          <p id="result-summary" class="mb-2">
            {{ job.result.piece_count }} piece(s) generated.
            <template v-if="job.result.dowel_count">{{ job.result.dowel_count }} dowel(s).</template>
          </p>
          <a id="download-link" class="btn btn-outline-primary mb-3" :href="downloadUrl" :download="job.result.download_name">
            <i class="bi bi-download me-1"></i>Download {{ job.result.download_name }}
          </a>
          <piece-grid :pieces="job.result.previews" @expand="openModal"></piece-grid>
          <div v-if="job.result.dowel_previews && job.result.dowel_previews.length">
            <h4 class="h6 text-secondary text-uppercase mt-4">Dowels</h4>
            <piece-grid :pieces="job.result.dowel_previews" :fixed-color="0xe4e7ee" @expand="openModal"></piece-grid>
          </div>
        </template>
      </div>
    </div>

    <result-modal :piece="modalPiece" :color="modalColor" @close="closeModal"></result-modal>

    <footer class="app-footer text-center text-secondary small py-2 mt-3">
      stlsplit — scale, split into print-bed-sized pieces, and add connector pegs.
    </footer>
  `,
  setup() {
    const store = useSplitForm();
    const { form, axisControllers, job, anyAxisUsable, refreshAllAxisPreviews, applyPreset } = store;

    const inputBuffer = ref(null);
    function onFileSelected(file) {
      store.onFileSelected(file);
      if (!file) {
        inputBuffer.value = null;
        return;
      }
      file.arrayBuffer().then((buf) => { inputBuffer.value = buf; });
    }

    const axisData = computed(() => ({
      x: { planes: axisControllers.x.state.planes, bounds: axisControllers.x.state.bounds },
      y: { planes: axisControllers.y.state.planes, bounds: axisControllers.y.state.bounds },
      z: { planes: axisControllers.z.state.planes, bounds: axisControllers.z.state.bounds },
    }));

    const errorHighlighted = computed(() =>
      !!job.errorAxis && axisControllers[job.errorAxis] &&
      axisControllers[job.errorAxis].state.planes.some((c) => c.errored)
    );

    const downloadUrl = computed(() => {
      if (!job.result) return "";
      const buf = Uint8Array.from(atob(job.result.download_base64), (c) => c.charCodeAt(0));
      return URL.createObjectURL(new Blob([buf], { type: job.result.download_mime }));
    });

    async function onSubmit() {
      await store.submit();
    }

    const modalPiece = ref(null);
    const modalColor = ref(0xe4e7ee);
    function openModal(piece, color) {
      modalPiece.value = piece;
      modalColor.value = color ?? 0xe4e7ee;
    }
    function closeModal() {
      modalPiece.value = null;
    }

    return {
      form, axisControllers, job, anyAxisUsable, refreshAllAxisPreviews, applyPreset,
      inputBuffer, onFileSelected, axisData, errorHighlighted, downloadUrl, onSubmit,
      modalPiece, modalColor, openModal, closeModal,
      shapes: SUPPORTED_SHAPES,
    };
  },
};
