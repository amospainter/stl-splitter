export default {
  name: "SplitSection",
  props: {
    form: { type: Object, required: true },
    axisControllers: { type: Object, required: true },
  },
  template: `
    <div>
      <div class="section-block">
        <div class="section-block-header"><i class="bi bi-grid-3x3-gap"></i> Per-axis split</div>
        <div class="form-text mb-2">Spacing, piece count, or bed size per axis — set any one to split on that axis, leave blank to skip it.</div>

        <table class="table table-sm align-middle mb-0">
          <thead>
            <tr class="text-secondary" style="font-size: 0.68rem">
              <th></th><th>Spacing (mm)</th><th>Pieces</th>
              <th>Bed (mm)</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="a in ['x','y','z']" :key="a">
              <td class="axis-row-label">{{ a.toUpperCase() }}</td>
              <td><input type="number" step="any" class="form-control" v-model="form[a + '_spacing']"></td>
              <td><input type="number" step="1" class="form-control" v-model="form[a + '_pieces']"></td>
              <td>
                <div class="d-flex align-items-center gap-1">
                  <input type="number" step="any" class="form-control" v-model="form['bed_' + a]">
                  <span v-if="axisControllers[a].fit.value" class="badge rounded-pill"
                        :class="axisControllers[a].fit.value.ok ? 'text-bg-success' : 'text-bg-warning'"
                        :title="axisControllers[a].fit.value.ok ? 'Every piece fits' : 'Largest piece is ' + axisControllers[a].fit.value.worst.toFixed(1) + 'mm, over the ' + axisControllers[a].fit.value.bed + 'mm bed'">
                    <i class="bi" :class="axisControllers[a].fit.value.ok ? 'bi-check-lg' : 'bi-exclamation-triangle-fill'"></i>
                  </span>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="section-block">
        <div class="section-block-header"><i class="bi bi-arrow-down-up"></i> Cut order</div>
        <label class="form-label">Order to apply axes in (when splitting on more than one)</label>
        <select class="form-select" v-model="form.axis_order">
          <option value="zxy">Z, then X, then Y</option>
          <option value="zyx">Z, then Y, then X</option>
          <option value="xyz">X, then Y, then Z</option>
          <option value="xzy">X, then Z, then Y</option>
          <option value="yxz">Y, then X, then Z</option>
          <option value="yzx">Y, then Z, then X</option>
        </select>
        <div class="form-text">Cutting the primary/tall axis first tends to leave larger, better-connected pieces, lowering the odds of a floating-region failure on later cuts. If one axis keeps needing far more cuts than its bed size should require, try reordering (or dropping) that axis — it may have geometry (an appendage, a coiled tail) that only reconnects to the rest of the model outside the range a cut on that axis can avoid.</div>
      </div>
    </div>
  `,
};
