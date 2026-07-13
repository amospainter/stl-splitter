import CollapsePanel from "./CollapsePanel.js";

export default {
  name: "SplitSection",
  components: { CollapsePanel },
  props: {
    form: { type: Object, required: true },
    axisControllers: { type: Object, required: true },
  },
  template: `
    <div class="card mb-3">
      <div class="card-header fw-semibold">Split</div>
      <div class="card-body">
        <div class="form-text mb-2">Spacing, piece count, or bed size per axis — set any one to split on that axis, leave blank to skip it.</div>

        <table class="table table-sm align-middle mb-1">
          <thead>
            <tr class="text-secondary" style="font-size: 0.72rem">
              <th></th><th>Spacing (mm)</th><th>Pieces</th>
              <th>
                Bed (mm)
              </th>
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

        <collapse-panel label="Advanced: cut order">
          <label class="form-label">Cut order (when splitting on more than one axis)</label>
          <select class="form-select" v-model="form.axis_order">
            <option value="zxy">Z, then X, then Y</option>
            <option value="zyx">Z, then Y, then X</option>
            <option value="xyz">X, then Y, then Z</option>
            <option value="xzy">X, then Z, then Y</option>
            <option value="yxz">Y, then X, then Z</option>
            <option value="yzx">Y, then Z, then X</option>
          </select>
          <div class="form-text">Cutting the primary/tall axis first tends to leave larger, better-connected pieces, lowering the odds of a floating-region failure on later cuts. Bed size auto-fills that axis's spacing (still editable in the plane editor below, and fit-checked as you drag) rather than using a separate auto-fit mode.</div>
        </collapse-panel>
      </div>
    </div>
  `,
};
