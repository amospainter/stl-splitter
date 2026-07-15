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
    // Print-bed-size overlay, mm [x, y, z] or null to hide it -- a plain
    // wireframe box for visually judging whether the current mesh fits.
    bedDims: { type: Array, default: null },
    // How many bed-sized cells to tile along each axis, [nx, ny, nz] --
    // defaults to a single box ([1,1,1]) when not given.
    bedGridCounts: { type: Array, default: null },
    // Sit the bed box's bottom face on the model's lowest Z point (true, the
    // default -- matches how a part actually sits on a build plate) instead
    // of centering the bed on the origin the way the mesh itself is centered.
    bedAlignBottom: { type: Boolean, default: true },
    showToolbar: { type: Boolean, default: false },
    // Empty (the default) leaves height up to CSS — e.g. the main editor
    // viewer's height is a clamp() rule in app.css that scales with viewport
    // height on large screens; an inline style here would always win over
    // that rule regardless of specificity, so only set it when a context
    // (piece cards, the modal) genuinely needs a fixed/explicit value.
    height: { type: String, default: "" },
  },
  emits: ["ready", "plane-drag"],
  template: `
    <div class="mesh-viewer" ref="root" :style="height ? { height } : {}">
      <div class="mesh-viewer-canvas" ref="container"></div>
      <div class="view-toolbar btn-group" v-if="showToolbar">
        <button type="button" class="btn btn-dark btn-sm" v-for="v in ['x','y','z','iso']" :key="v"
                @click="viewer && viewer.setView(v)">{{ v.toUpperCase() }}</button>
        <button type="button" class="btn btn-dark btn-sm" @click="toggleFullscreen"
                :title="isFullscreen ? 'Exit full screen' : 'Full screen'">
          <i :class="isFullscreen ? 'bi bi-fullscreen-exit' : 'bi bi-arrows-fullscreen'"></i>
        </button>
      </div>
    </div>
  `,
  setup(props, { emit }) {
    const container = ref(null);
    const root = ref(null);
    const isFullscreen = ref(false);
    let viewer = null;
    let resizeObserver = null;

    function onPlaneDrag(axisName, index, newValue) {
      emit("plane-drag", axisName, index, newValue);
    }

    function loadIfReady() {
      if (viewer && props.buffer) viewer.loadArrayBuffer(props.buffer, props.scaleFactor);
      if (viewer && props.axisData) viewer.setAxisPlanes(props.axisData, onPlaneDrag);
      if (viewer) viewer.setBedBox(props.bedDims, props.bedGridCounts, props.bedAlignBottom);
    }

    function toggleFullscreen() {
      if (!root.value) return;
      if (document.fullscreenElement === root.value) {
        document.exitFullscreen();
      } else {
        root.value.requestFullscreen();
      }
    }

    function onFullscreenChange() {
      isFullscreen.value = document.fullscreenElement === root.value;
      // Fullscreen swaps the element's box to fill the screen instantly, but
      // the ResizeObserver callback lands a frame later -- resize here too
      // so the three.js canvas doesn't render a stretched frame in between.
      if (viewer) viewer.resize();
    }

    onMounted(() => {
      viewer = makeViewer(container.value, props.color);
      resizeObserver = new ResizeObserver(() => viewer && viewer.resize());
      resizeObserver.observe(container.value);
      document.addEventListener("fullscreenchange", onFullscreenChange);
      loadIfReady();
      emit("ready", viewer);
    });

    onBeforeUnmount(() => {
      if (resizeObserver) resizeObserver.disconnect();
      document.removeEventListener("fullscreenchange", onFullscreenChange);
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
    watch(() => props.axisData, () => { if (viewer && props.axisData) viewer.setAxisPlanes(props.axisData, onPlaneDrag); }, { deep: true });
    watch(
      [() => props.bedDims, () => props.bedGridCounts, () => props.bedAlignBottom],
      () => { if (viewer) viewer.setBedBox(props.bedDims, props.bedGridCounts, props.bedAlignBottom); },
      { deep: true },
    );

    return { container, root, isFullscreen, toggleFullscreen, get viewer() { return viewer; } };
  },
};
