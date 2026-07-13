const SHAPE_LABELS = { round: "Round", d: "D-shaped (anti-rotation)", square: "Square", hex: "Hexagon" };

export default {
  name: "ConnectorsSection",
  props: {
    form: { type: Object, required: true },
    shapes: { type: Array, required: true },
  },
  template: `
    <div>
      <div class="section-block">
        <div class="section-block-header"><i class="bi bi-plug"></i> Connectors</div>
        <div class="form-check mb-2">
          <input type="checkbox" class="form-check-input" id="no-connectors" v-model="form.no_connectors">
          <label class="form-check-label" for="no-connectors">No connectors (cut only)</label>
        </div>

        <div v-show="!form.no_connectors">
          <div class="row g-2">
            <div class="col"><label class="form-label">Dowel shape</label>
              <select class="form-select" v-model="form.dowel_shape">
                <option v-for="s in shapes" :key="s" :value="s">{{ shapeLabels[s] }}</option>
              </select>
            </div>
            <div class="col"><label class="form-label">Diameter (mm)</label><input type="number" step="any" class="form-control" v-model="form.peg_diameter"></div>
          </div>
          <div class="form-check">
            <input type="checkbox" class="form-check-input" id="alignment-key" v-model="form.alignment_key">
            <label class="form-check-label" for="alignment-key">Add alignment key (D-flat) to round dowels</label>
          </div>
        </div>
      </div>

      <div class="section-block" v-show="!form.no_connectors">
        <div class="section-block-header"><i class="bi bi-sliders"></i> Socket &amp; fit</div>
        <div class="form-text mb-2">Sockets are carved into both mating pieces; matching dowels are generated as separate parts to print and glue/press in at assembly.</div>
        <div class="row g-2">
          <div class="col"><label class="form-label">Socket depth per side (mm)</label><input type="number" step="any" class="form-control" v-model="form.peg_length"></div>
          <div class="col"><label class="form-label">Clearance (mm)</label><input type="number" step="any" class="form-control" v-model="form.peg_clearance"></div>
        </div>
        <div class="row g-2">
          <div class="col"><label class="form-label">Connectors per interface</label><input type="number" step="1" class="form-control" v-model="form.n_pegs"></div>
          <div class="col"><label class="form-label">Min wall thickness (mm)</label><input type="number" step="any" class="form-control" v-model="form.min_wall_thickness"></div>
        </div>
      </div>
    </div>
  `,
  setup() {
    return { shapeLabels: SHAPE_LABELS };
  },
};
