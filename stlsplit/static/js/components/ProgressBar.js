export default {
  name: "ProgressBar",
  props: {
    message: { type: String, default: "" },
    fraction: { type: Number, default: null }, // null = indeterminate
  },
  template: `
    <div class="progress-block mb-3">
      <div class="text-secondary small mb-1">{{ message }}</div>
      <div class="progress" style="height: 8px">
        <div class="progress-bar" :class="{ 'progress-bar-striped progress-bar-animated': fraction === null }"
             :style="{ width: (fraction === null ? 40 : Math.round(fraction * 100)) + '%' }"></div>
      </div>
    </div>
  `,
};
