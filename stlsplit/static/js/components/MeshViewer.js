// Thin Vue wrapper around viewer.js's three.js scene: owns exactly one scene
// per mounted instance, tied to this component's lifecycle so switching
// pieces/closing the modal properly disposes GL resources instead of piling
// up hidden canvases.
import { ref, onMounted, onBeforeUnmount, watch } from "vue";
import { makeViewer } from "../viewer.js";

export default {
  name: "MeshViewer",
  props: {
    buffer: { type: ArrayBuffer, default: null },
    color: { type: Number, default: 0xe4e7ee },
    scaleFactor: { type: Number, default: 1 },
    axisData: { type: Object, default: null }, // {x:{planes,bounds}, y:{...}, z:{...}} plane gizmo overlay, main editor only
    showToolbar: { type: Boolean, default: false },
    // Empty (the default) leaves height up to CSS — e.g. the main editor
    // viewer's height is a clamp() rule in app.css that scales with viewport
    // height on large screens; an inline style here would always win over
    // that rule regardless of specificity, so only set it when a context
    // (piece cards, the modal) genuinely needs a fixed/explicit value.
    height: { type: String, default: "" },
  },
  emits: ["ready"],
  template: `
    <div class="mesh-viewer" :style="height ? { height } : {}">
      <div class="mesh-viewer-canvas" ref="container"></div>
      <div class="view-toolbar btn-group" v-if="showToolbar">
        <button type="button" class="btn btn-dark btn-sm" v-for="v in ['x','y','z','iso']" :key="v"
                @click="viewer && viewer.setView(v)">{{ v.toUpperCase() }}</button>
      </div>
    </div>
  `,
  setup(props, { emit }) {
    const container = ref(null);
    let viewer = null;
    let resizeObserver = null;

    function loadIfReady() {
      if (viewer && props.buffer) viewer.loadArrayBuffer(props.buffer, props.scaleFactor);
      if (viewer && props.axisData) viewer.setAxisPlanes(props.axisData);
    }

    onMounted(() => {
      viewer = makeViewer(container.value, props.color);
      resizeObserver = new ResizeObserver(() => viewer && viewer.resize());
      resizeObserver.observe(container.value);
      loadIfReady();
      emit("ready", viewer);
    });

    onBeforeUnmount(() => {
      if (resizeObserver) resizeObserver.disconnect();
      if (viewer) viewer.dispose();
    });

    watch(() => props.buffer, () => loadIfReady());
    // Scale changes (e.g. typing a target dimension after the file's
    // already loaded) update the axis bounds/gizmo-plane positions
    // reactively, but without this the mesh itself kept rendering at
    // whatever scale it had when `buffer` last changed — planes would
    // reposition to the new scaled mm coordinates while the mesh stayed
    // its old (visually much smaller) size, drifting the two apart.
    watch(() => props.scaleFactor, () => loadIfReady());
    watch(() => props.axisData, () => { if (viewer && props.axisData) viewer.setAxisPlanes(props.axisData); }, { deep: true });

    return { container, get viewer() { return viewer; } };
  },
};
