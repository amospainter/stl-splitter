import { b64ToArrayBuffer } from "../viewer.js";
import MeshViewer from "./MeshViewer.js";

export default {
  name: "PieceCard",
  components: { MeshViewer },
  props: {
    name: { type: String, required: true },
    dataBase64: { type: String, required: true },
    color: { type: Number, required: true },
  },
  emits: ["expand"],
  template: `
    <div class="card piece-card h-100">
      <div class="piece-canvas" title="Click to enlarge"
           @pointerdown="onPointerDown" @click="onClick">
        <mesh-viewer :buffer="buffer" :color="color" height="160px"></mesh-viewer>
      </div>
      <button type="button" class="btn btn-dark btn-sm expand-btn" aria-label="Enlarge preview" @click.stop="$emit('expand')">
        <i class="bi bi-arrows-fullscreen"></i>
      </button>
      <div class="card-body py-2 px-2 small d-flex justify-content-between">
        <span class="text-truncate">{{ name }}</span>
      </div>
    </div>
  `,
  setup(props, { emit }) {
    const buffer = b64ToArrayBuffer(props.dataBase64);
    // OrbitControls drags fire a "click" on release; only treat it as a real
    // click (open the modal) if the pointer didn't move much between down/up.
    let downPos = null;
    function onPointerDown(e) {
      downPos = { x: e.clientX, y: e.clientY };
    }
    function onClick(e) {
      if (!downPos) return;
      const dx = e.clientX - downPos.x;
      const dy = e.clientY - downPos.y;
      if (Math.hypot(dx, dy) <= 5) emit("expand");
      downPos = null;
    }
    return { buffer, onPointerDown, onClick };
  },
};
