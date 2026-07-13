export default {
  name: "OutputSection",
  props: { form: { type: Object, required: true } },
  template: `
    <div class="card mb-3">
      <div class="card-header fw-semibold">Output</div>
      <div class="card-body">
        <label class="form-label">Format</label>
        <select class="form-select" v-model="form.format">
          <option value="stl">Separate STL files (zip)</option>
          <option value="3mf">Single 3MF project (multi-plate)</option>
        </select>
        <div v-show="form.format === '3mf'" class="mt-3">
          <label class="form-label">Print bed (for 3MF plate layout)</label>
          <select class="form-select" v-model="form.bed_size">
            <option value="a1_mini">A1 mini (180 &times; 180mm)</option>
            <option value="256">X1C / P1P / P1S / A1 (256 &times; 256mm)</option>
            <option value="h2">H2D / H2S (350 &times; 320mm)</option>
          </select>
        </div>
      </div>
    </div>
  `,
};
