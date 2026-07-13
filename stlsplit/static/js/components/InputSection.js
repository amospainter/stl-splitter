export default {
  name: "InputSection",
  emits: ["file-selected"],
  template: `
    <div class="card mb-3">
      <div class="card-header fw-semibold">Input</div>
      <div class="card-body">
        <label class="form-label">STL file</label>
        <input type="file" class="form-control" accept=".stl" required @change="onChange">
      </div>
    </div>
  `,
  setup(_, { emit }) {
    function onChange(e) {
      emit("file-selected", e.target.files[0] || null);
    }
    return { onChange };
  },
};
