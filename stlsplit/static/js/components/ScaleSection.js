import CollapsibleCard from "./CollapsibleCard.js";

export default {
  name: "ScaleSection",
  components: { CollapsibleCard },
  props: { form: { type: Object, required: true } },
  template: `
    <collapsible-card title="Scale" hint="optional">
      <div class="row g-2">
        <div class="col"><label class="form-label">Scale factor</label><input type="number" step="any" class="form-control" v-model="form.scale"></div>
        <div class="col"><label class="form-label">Target dimension (mm)</label><input type="number" step="any" class="form-control" v-model="form.target_dim"></div>
      </div>
      <label class="form-label">Target dimension axis</label>
      <select class="form-select" v-model="form.axis">
        <option value="x">X</option><option value="y">Y</option><option value="z">Z</option>
      </select>
      <div class="form-text">Only used when a target dimension is set above.</div>
    </collapsible-card>
  `,
};
