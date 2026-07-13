import { ref } from "vue";
import { listPresets, loadPreset, savePreset, deletePreset } from "../presets.js";

export default {
  name: "PresetBar",
  props: {
    form: { type: Object, required: true },
  },
  emits: ["apply"],
  template: `
    <div class="preset-bar mb-3">
      <div class="input-group input-group-sm mb-1" v-if="presets.length">
        <select class="form-select" v-model="selected" title="Saved settings">
          <option v-for="name in presets" :key="name" :value="name">{{ name }}</option>
        </select>
        <button type="button" class="btn btn-outline-secondary" :disabled="!selected" title="Load" @click="onLoad">
          <i class="bi bi-box-arrow-in-down"></i>
        </button>
        <button type="button" class="btn btn-outline-danger" :disabled="!selected" title="Delete" @click="onDelete">
          <i class="bi bi-trash3"></i>
        </button>
      </div>
      <div class="input-group input-group-sm">
        <input type="text" class="form-control" placeholder="Save current settings as…"
               v-model="newName" @keyup.enter="onSave" maxlength="60">
        <button type="button" class="btn btn-outline-primary" :disabled="!newName.trim()" @click="onSave">
          <i class="bi bi-save"></i>
        </button>
      </div>
      <div class="form-text" :class="{ 'text-danger': saveError }" v-if="statusText">{{ statusText }}</div>
    </div>
  `,
  setup(props, { emit }) {
    const presets = ref(listPresets());
    const selected = ref(presets.value[0] || "");
    const newName = ref("");
    const statusText = ref("");
    const saveError = ref(false);

    function refresh() {
      presets.value = listPresets();
      if (!presets.value.includes(selected.value)) selected.value = presets.value[0] || "";
    }

    function onLoad() {
      const snapshot = loadPreset(selected.value);
      if (!snapshot) return;
      emit("apply", snapshot);
      statusText.value = `Loaded "${selected.value}".`;
      saveError.value = false;
    }

    function onSave() {
      const name = newName.value.trim();
      if (!name) return;
      const overwriting = presets.value.includes(name);
      const ok = savePreset(name, props.form);
      saveError.value = !ok;
      statusText.value = ok
        ? `Saved "${name}"${overwriting ? " (overwritten)" : ""}.`
        : "Couldn't save — browser storage may be disabled or full.";
      if (ok) {
        newName.value = "";
        refresh();
        selected.value = name;
      }
    }

    function onDelete() {
      const name = selected.value;
      if (!name) return;
      deletePreset(name);
      statusText.value = `Deleted "${name}".`;
      saveError.value = false;
      refresh();
    }

    return { presets, selected, newName, statusText, saveError, onLoad, onSave, onDelete };
  },
};
