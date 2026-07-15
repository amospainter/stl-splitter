import { computed } from "vue";
import { paletteHex } from "../viewer.js";

const TILT_LABELS = { x: ["Tilt Y", "Tilt Z"], y: ["Tilt X", "Tilt Z"], z: ["Tilt X", "Tilt Y"] };
// Kept short of a true +/-90 (which would make the plane's normal parallel
// to the cut axis -- a degenerate, zero-thickness slice) but well past the
// old +/-75 cap some users were bumping into on genuinely steep cuts.
export const TILT_MAX = 85;
// Backs the <input list="..."> tick marks on the tilt sliders below --
// round 15-degree steps plus both endpoints.
export const TILT_TICKS = [-85, -75, -60, -45, -30, -15, 0, 15, 30, 45, 60, 75, 85];

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
        <button type="button" class="btn btn-outline-secondary btn-sm" v-if="controller.state.loading"
                @click="controller.stopPreview()">
          <i class="bi bi-stop-fill me-1"></i>Stop
        </button>
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
            <input type="range" class="form-range flex-grow-1" :min="-tiltMax" :max="tiltMax" step="1" :list="tiltTicksId"
                   v-model.number="cut[field]"
                   :style="{ accentColor: paletteHex(i) }"
                   @pointerdown="cut.active = true" @change="cut.active = false">
            <span class="tilt-value small text-secondary" style="flex: 0 0 36px">{{ cut[field] }}&deg;</span>
          </div>
        </div>
      </div>
      <datalist :id="tiltTicksId">
        <option v-for="t in tiltTicks" :key="t" :value="t"></option>
      </datalist>
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
    return {
      lo, hi, step, hint, paletteHex, tiltLabel,
      tiltMax: TILT_MAX, tiltTicks: TILT_TICKS, tiltTicksId: `tilt-ticks-${props.axisName}`,
    };
  },
};
