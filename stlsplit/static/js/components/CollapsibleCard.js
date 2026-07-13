import { ref } from "vue";

// A whole card that starts collapsed — for settings most users never touch
// (Scale, Interior hollowing) so they don't cost vertical space/scroll on
// the common path, but stay one click away rather than buried in a
// sub-menu. Distinct from CollapsePanel (a plain toggle within a section)
// mainly in that the whole card header is the click target and it looks
// like a normal card when closed, matching the always-open sections around it.
export default {
  name: "CollapsibleCard",
  props: {
    title: { type: String, required: true },
    hint: { type: String, default: "" },
    startOpen: { type: Boolean, default: false },
  },
  template: `
    <div class="card mb-3">
      <div class="card-header fw-semibold d-flex align-items-center" style="cursor: pointer" @click="open = !open">
        <span class="flex-grow-1">{{ title }} <span v-if="hint" class="text-secondary fw-normal small">{{ hint }}</span></span>
        <i class="bi" :class="open ? 'bi-chevron-up' : 'bi-chevron-down'"></i>
      </div>
      <div class="card-body" v-show="open">
        <slot></slot>
      </div>
    </div>
  `,
  setup(props) {
    const open = ref(props.startOpen);
    return { open };
  },
};
