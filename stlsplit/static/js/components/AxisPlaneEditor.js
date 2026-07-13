import { computed } from "vue";
import { paletteHex } from "../viewer.js";

const TILT_LABELS = { x: ["Tilt Y", "Tilt Z"], y: ["Tilt X", "Tilt Z"], z: ["Tilt X", "Tilt Y"] };

export default {
  name: "AxisPlaneEditor",
  props: {
    axisName: { type: String, required: true },
    controller: { type: Object, required: true }, // one entry from useSplitForm().axisControllers
  },
  template: `
    <div class="axis-plane-group mb-3" v-show="controller.state.bounds || controller.state.loading">
      <div class="axis-plane-title d-flex align-items-center gap-2 mb-1">
        <span class="fw-semibold small text-uppercase">{{ axisName.toUpperCase() }}</span>
        <span class="spinner-border spinner-border-sm text-secondary" v-if="controller.state.loading"></span>
        <span class="text-secondary small flex-grow-1">{{ hint }}</span>
        <button type="button" class="btn btn-outline-primary btn-sm" @click="controller.addCut()">
          <i class="bi bi-plus-lg me-1"></i>Add cut
        </button>
      </div>
      <div class="plane-list d-flex flex-column gap-2">
        <div class="plane-item" v-for="(cut, i) in controller.state.planes" :key="i"
             :class="{ 'cut-hidden': cut.hidden, 'cut-error': cut.errored }">
          <div class="plane-row d-flex align-items-center gap-2">
            <span class="plane-swatch" :style="{ background: paletteHex(i) }"></span>
            <span class="plane-label small text-secondary" style="flex: 0 0 55px">Cut {{ i + 1 }}</span>
            <input type="range" class="form-range flex-grow-1" :min="lo" :max="hi" :step="step"
                   v-model.number="cut.value"
                   :style="{ accentColor: paletteHex(i) }"
                   @pointerdown="cut.active = true" @change="cut.active = false">
            <span class="plane-value small text-nowrap">{{ cut.value.toFixed(1) }} mm</span>
            <button type="button" class="btn btn-sm btn-link text-decoration-none p-1"
                    :title="cut.hidden ? 'Show this cut plane' : 'Hide this cut plane'"
                    @click="cut.hidden = !cut.hidden">
              <i class="bi" :class="cut.hidden ? 'bi-eye-slash' : 'bi-eye'"></i>
            </button>
            <button type="button" class="btn btn-sm btn-link text-danger text-decoration-none p-1"
                    title="Remove this cut" @click="controller.removeCut(i)">
              <i class="bi bi-x-lg"></i>
            </button>
          </div>
          <div class="tilt-row d-flex align-items-center gap-2 ps-4" v-for="field in ['tiltA', 'tiltB']" :key="field">
            <span class="tilt-label small text-secondary" style="flex: 0 0 46px">{{ tiltLabel(field) }}</span>
            <input type="range" class="form-range flex-grow-1" min="-75" max="75" step="1"
                   v-model.number="cut[field]"
                   :style="{ accentColor: paletteHex(i) }"
                   @pointerdown="cut.active = true" @change="cut.active = false">
            <span class="tilt-value small text-secondary" style="flex: 0 0 36px">{{ cut[field] }}&deg;</span>
          </div>
        </div>
      </div>
    </div>
  `,
  setup(props) {
    const idx = { x: 0, y: 1, z: 2 }[props.axisName];
    const lo = computed(() => props.controller.state.bounds ? props.controller.state.bounds[0][idx] : 0);
    const hi = computed(() => props.controller.state.bounds ? props.controller.state.bounds[1][idx] : 1);
    const step = computed(() => Math.max((hi.value - lo.value) / 500, 0.001));
    const hint = computed(() => {
      if (props.controller.state.loading) return "— computing safe cut positions… (can take a while on a large/complex mesh)";
      return props.controller.state.planes.length ? "— drag to fine-tune, or remove" : "— no cuts yet";
    });
    function tiltLabel(field) {
      return TILT_LABELS[props.axisName][field === "tiltA" ? 0 : 1];
    }
    return { lo, hi, step, hint, paletteHex, tiltLabel };
  },
};
