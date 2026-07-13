// Framework-agnostic three.js scene helper. Kept independent of Vue so it can
// be unit-tested/reused on its own; MeshViewer.js is the thin Vue component
// wrapper that owns one of these per mounted <mesh-viewer>.
import * as THREE from "three";
import { STLLoader } from "three/addons/loaders/STLLoader.js";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

const loader = new STLLoader();
export const AXIS_NAMES = ["x", "y", "z"];
export const AXIS_INDEX = { x: 0, y: 1, z: 2 };

export const PALETTE = [0x9ec5fe, 0xfeaad0, 0xa8f0c6, 0xffd08a, 0xd3aefc, 0xa6ecec];
export function paletteHex(i) {
  return "#" + PALETTE[i % PALETTE.length].toString(16).padStart(6, "0");
}

export function b64ToArrayBuffer(b64) {
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes.buffer;
}

export function makeViewer(container, color) {
  const width = container.clientWidth || 300;
  const height = container.clientHeight || 200;
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x16181d);
  const camera = new THREE.PerspectiveCamera(45, width / height, 0.1, 10000);
  camera.up.set(0, 0, 1); // STL/print-bed convention is Z-up, not three.js's default Y-up
  const renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setSize(width, height);
  container.appendChild(renderer.domElement);

  scene.add(new THREE.AmbientLight(0xffffff, 0.6));
  const dir = new THREE.DirectionalLight(0xffffff, 0.8);
  dir.position.set(1, 1, 1);
  scene.add(dir);

  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;

  const planeGroup = new THREE.Group();
  scene.add(planeGroup);
  let currentMesh = null;
  let lastMaxDim = 1;
  let disposed = false;

  function frame(mesh) {
    const box = new THREE.Box3().setFromObject(mesh);
    const size = box.getSize(new THREE.Vector3());
    const center = box.getCenter(new THREE.Vector3());
    mesh.position.sub(center);
    lastMaxDim = Math.max(size.x, size.y, size.z) || 1;
    camera.position.set(lastMaxDim * 1.2, lastMaxDim * 1.2, lastMaxDim * 1.2);
    camera.lookAt(0, 0, 0);
    controls.target.set(0, 0, 0);
    controls.update();
  }

  // Snap the camera to look straight down a world axis (or back to the
  // default isometric-ish corner view), keeping the current framing
  // distance. Scene "up" stays fixed at +Z the whole time (STL/print-bed
  // convention, set once above) — OrbitControls caches an internal
  // alignment quaternion from camera.up at construction time, so changing
  // it per-view here would desync orbiting after the fact. Looking exactly
  // down the Z axis is the one inherent gimbal-lock case that comes with
  // keeping a fixed up vector, same as any orbit-style viewer's top view.
  function setView(view) {
    const d = lastMaxDim * 1.8;
    const positions = {
      x: [d, 0, 0],
      y: [0, d, 0],
      z: [0, 0, d],
      iso: [lastMaxDim * 1.2, lastMaxDim * 1.2, lastMaxDim * 1.2],
    };
    const [px, py, pz] = positions[view] || positions.iso;
    camera.position.set(px, py, pz);
    camera.lookAt(0, 0, 0);
    controls.target.set(0, 0, 0);
    controls.update();
  }

  function loadArrayBuffer(buf, scaleFactor) {
    if (currentMesh) {
      scene.remove(currentMesh);
      currentMesh.geometry.dispose();
      currentMesh.material.dispose();
    }
    const geometry = loader.parse(buf);
    geometry.computeVertexNormals();
    const material = new THREE.MeshStandardMaterial({ color, metalness: 0.1, roughness: 0.7 });
    const mesh = new THREE.Mesh(geometry, material);
    if (scaleFactor) mesh.scale.setScalar(scaleFactor);
    scene.add(mesh);
    currentMesh = mesh;
    frame(mesh);
  }

  // Render translucent gizmo planes for every active axis. `axisData` is
  // { x: {planes, bounds}, y: {...}, z: {...} }; `planes` entries are
  // { value, tiltA, tiltB, hidden, active, errored } (tilt in degrees). Each
  // axis's planes are colored in order from PALETTE so a cut's gizmo color
  // matches its slider's swatch. Tilt is applied as additional world-axis
  // rotations around the same two world axes (in axis order) that the
  // backend rotates the plane's normal around, so the gizmo always matches
  // the actual cut geometry. `hidden` cuts are skipped entirely; if any cut
  // anywhere is `active` (currently being dragged), every other cut is
  // dimmed way down so it doesn't visually block the one being edited.
  function setAxisPlanes(axisData) {
    for (const child of planeGroup.children.slice()) {
      planeGroup.remove(child);
      child.geometry.dispose();
      child.material.dispose();
    }
    const allCuts = Object.values(axisData || {}).flatMap((d) => (d && d.planes) || []);
    const anyActive = allCuts.some((c) => c.active);

    Object.entries(axisData || {}).forEach(([axisName, data]) => {
      const axisIdx = AXIS_INDEX[axisName];
      const planes = data && data.planes;
      const bounds = data && data.bounds;
      if (!bounds || !planes || !planes.length) return;
      const [min, max] = bounds;
      const size = [max[0] - min[0], max[1] - min[1], max[2] - min[2]];
      const center = [(min[0] + max[0]) / 2, (min[1] + max[1]) / 2, (min[2] + max[2]) / 2];
      const other = [0, 1, 2].filter((i) => i !== axisIdx);
      const extra = Math.max(size[other[0]], size[other[1]]) * 1.25 || 1;
      const worldDirA = new THREE.Vector3(other[0] === 0 ? 1 : 0, other[0] === 1 ? 1 : 0, other[0] === 2 ? 1 : 0);
      const worldDirB = new THREE.Vector3(other[1] === 0 ? 1 : 0, other[1] === 1 ? 1 : 0, other[1] === 2 ? 1 : 0);
      planes.forEach((cut, i) => {
        if (cut.hidden) return;
        const color = cut.errored ? 0xff3b3b : PALETTE[i % PALETTE.length];
        let opacity = 0.22;
        if (anyActive) opacity = cut.active ? 0.55 : 0.05;
        if (cut.errored) opacity = 0.55; // always stand out, even while another cut is being dragged

        const geo = new THREE.PlaneGeometry(extra, extra);
        const mat = new THREE.MeshBasicMaterial({
          color, transparent: true, opacity, side: THREE.DoubleSide,
          depthWrite: false, depthTest: false,
        });
        const plane = new THREE.Mesh(geo, mat);
        plane.renderOrder = cut.errored ? 1001 : 999;

        const edges = new THREE.LineSegments(
          new THREE.EdgesGeometry(geo),
          new THREE.LineBasicMaterial({
            color, depthTest: false, transparent: true,
            opacity: cut.errored ? 1 : (anyActive && !cut.active ? 0.25 : 0.95),
          }),
        );
        edges.renderOrder = 1002;
        plane.add(edges);

        if (axisIdx === 0) plane.rotation.y = Math.PI / 2;
        else if (axisIdx === 1) plane.rotation.x = Math.PI / 2;
        plane.position[AXIS_NAMES[axisIdx]] = cut.value - center[axisIdx];
        planeGroup.add(plane);

        if (cut.tiltA) plane.rotateOnWorldAxis(worldDirA, THREE.MathUtils.degToRad(cut.tiltA));
        if (cut.tiltB) plane.rotateOnWorldAxis(worldDirB, THREE.MathUtils.degToRad(cut.tiltB));
      });
    });
  }

  function resize() {
    const w = container.clientWidth || 300;
    const h = container.clientHeight || 200;
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    renderer.setSize(w, h);
  }

  function dispose() {
    disposed = true;
    if (currentMesh) {
      currentMesh.geometry.dispose();
      currentMesh.material.dispose();
    }
    for (const child of planeGroup.children) {
      child.geometry.dispose();
      child.material.dispose();
    }
    controls.dispose();
    renderer.dispose();
    if (renderer.domElement.parentNode) renderer.domElement.parentNode.removeChild(renderer.domElement);
  }

  (function animate() {
    if (disposed) return;
    requestAnimationFrame(animate);
    controls.update();
    renderer.render(scene, camera);
  })();

  return { loadArrayBuffer, setAxisPlanes, setView, resize, dispose };
}
