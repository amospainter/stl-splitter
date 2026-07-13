import { ref } from "vue";

// Tiny reusable disclosure for tucking away esoteric settings (fine-tuning
// most users leave at defaults) behind a toggle, so the primary flow isn't
// buried in rarely-touched fields. Plain v-show, no bootstrap.js dependency.
export default {
  name: "CollapsePanel",
  props: {
    label: { type: String, required: true },
    startOpen: { type: Boolean, default: false },
  },
  template: `
    <div class="collapse-panel mt-2">
      <button type="button" class="btn btn-link btn-sm p-0 text-decoration-none text-secondary" @click="open = !open">
        <i class="bi" :class="open ? 'bi-chevron-down' : 'bi-chevron-right'"></i> {{ label }}
      </button>
      <div v-show="open" class="mt-2">
        <slot></slot>
      </div>
    </div>
  `,
  setup(props) {
    const open = ref(props.startOpen);
    return { open };
  },
};
