import { onMounted, onBeforeUnmount } from "vue";
import { b64ToArrayBuffer } from "../viewer.js";
import MeshViewer from "./MeshViewer.js";

export default {
  name: "ResultModal",
  components: { MeshViewer },
  props: {
    piece: { type: Object, default: null }, // {name, data_base64} or null when closed
    color: { type: Number, default: 0xe4e7ee },
  },
  emits: ["close"],
  template: `
    <div class="modal fade" :class="{ show: !!piece }" tabindex="-1"
         :style="{ display: piece ? 'block' : 'none' }" @click.self="$emit('close')">
      <div class="modal-dialog modal-lg modal-dialog-centered" style="max-width: 900px" v-if="piece">
        <div class="modal-content" style="height: min(720px, 90vh)">
          <div class="modal-header py-2">
            <span class="small">{{ piece.name }}</span>
            <button type="button" class="btn-close" aria-label="Close" @click="$emit('close')"></button>
          </div>
          <div class="modal-body p-0 flex-grow-1">
            <mesh-viewer :buffer="buffer" :color="color" show-toolbar height="100%"></mesh-viewer>
          </div>
        </div>
      </div>
    </div>
    <div class="modal-backdrop fade show" v-if="piece"></div>
  `,
  setup(props, { emit }) {
    function onKeydown(e) {
      if (e.key === "Escape" && props.piece) emit("close");
    }
    onMounted(() => document.addEventListener("keydown", onKeydown));
    onBeforeUnmount(() => document.removeEventListener("keydown", onKeydown));

    return {
      get buffer() { return props.piece ? b64ToArrayBuffer(props.piece.data_base64) : null; },
    };
  },
};
