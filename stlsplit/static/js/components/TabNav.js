// Small top-level tab strip for the settings sidebar (Split / Connectors /
// Output / Advanced) — plain v-show driven, no bootstrap.js dependency
// (same constraint as CollapsePanel/CollapsibleCard: Vue owns the DOM here,
// a second library toggling classes on the same elements is a recipe for
// the two fighting each other).
export default {
  name: "TabNav",
  props: {
    tabs: { type: Array, required: true }, // [{ id, label }]
    modelValue: { type: String, required: true },
  },
  emits: ["update:modelValue"],
  template: `
    <ul class="nav nav-tabs section-tabs mb-2">
      <li class="nav-item" v-for="t in tabs" :key="t.id">
        <button type="button" class="nav-link" :class="{ active: modelValue === t.id }"
                @click="$emit('update:modelValue', t.id)">{{ t.label }}</button>
      </li>
    </ul>
  `,
};
