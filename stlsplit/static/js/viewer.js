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
  const bedBoxGroup = new THREE.Group();
  scene.add(bedBoxGroup);
  let currentMesh = null;
  let bedBoxGeometry = null;
  let bedBoxMaterial = null;
  let lastMaxDim = 1;
  // Mesh's own min Z after centering (see the comment in frame()) -- box
  // centering is always symmetric around its own center, so this is just
  // -size.z / 2, tracked so setBedBox() can align the bed's bottom face to
  // the model's lowest point instead of centering the bed on the origin.
  let lastMeshMinZ = 0;
  let disposed = false;

  function frame(mesh) {
    const box = new THREE.Box3().setFromObject(mesh);
    const size = box.getSize(new THREE.Vector3());
    const center = box.getCenter(new THREE.Vector3());
    mesh.position.sub(center);
    lastMeshMinZ = -size.z / 2;
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

  // Configurable print-bed-size overlay: a wireframe box (or a tiled grid of
  // them — see `gridCounts` below), `dims` mm [x, y, z] or null to hide it.
  // Centered at the scene origin, same as the loaded mesh after `frame()` —
  // this doesn't model "resting on the build plate" (the mesh's true Z=0
  // isn't tracked once centered), it's a direct side-by-side size
  // comparison: does the piece's silhouette fit inside this box (or this
  // many beds' worth of boxes), at a glance, from any angle.
  //
  // `gridCounts`, if given, is `[nx, ny, nz]` — how many bed-sized cells to
  // tile along each axis (each 1 by default, i.e. a single box, unchanged
  // from before this existed). The whole tiled block is centered in X/Y the
  // same way a single box would be, so it's a direct visual answer to "how
  // many beds would this piece need": tile counts sized to just cover the
  // piece's own extents in each dimension.
  //
  // `alignBottom` (default true): sit the bed's bottom face on the model's
  // lowest Z point instead of centering the bed on the origin -- a real bed
  // sits under the part, it doesn't clip through its middle. Off falls back
  // to the old centered-on-origin behavior.
  function setBedBox(dims, gridCounts, alignBottom = true) {
    for (const child of bedBoxGroup.children.slice()) {
      bedBoxGroup.remove(child);
    }
    if (bedBoxGeometry) {
      bedBoxGeometry.dispose();
      bedBoxGeometry = null;
    }
    if (bedBoxMaterial) {
      bedBoxMaterial.dispose();
      bedBoxMaterial = null;
    }
    if (!dims) return;
    const [dx, dy, dz] = dims;
    if (!(dx > 0) || !(dy > 0) || !(dz > 0)) return;

    const [nx, ny, nz] = (gridCounts && gridCounts.every((n) => n >= 1)) ? gridCounts : [1, 1, 1];
    const totalX = nx * dx, totalY = ny * dy, totalZ = nz * dz;
    const zBase = alignBottom ? lastMeshMinZ : -totalZ / 2;

    // One shared geometry/material for every cell (three.js supports a
    // single geometry/material referenced by many Object3D instances) --
    // cheap even for a many-cell grid, and disposed once above regardless
    // of how many instances currently reference it.
    bedBoxGeometry = new THREE.EdgesGeometry(new THREE.BoxGeometry(dx, dy, dz));
    bedBoxMaterial = new THREE.LineBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0.35 });

    for (let i = 0; i < nx; i++) {
      for (let j = 0; j < ny; j++) {
        for (let k = 0; k < nz; k++) {
          const cell = new THREE.LineSegments(bedBoxGeometry, bedBoxMaterial);
          cell.position.set(
            -totalX / 2 + dx / 2 + i * dx,
            -totalY / 2 + dy / 2 + j * dy,
            zBase + dz / 2 + k * dz,
          );
          cell.renderOrder = 500;
          bedBoxGroup.add(cell);
        }
      }
    }
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
  //
  // `onDrag(axisName, index, newValue)`, if given, is called continuously
  // while the user drags a plane by grabbing it directly in the 3D view
  // (see enablePlaneDragging below) — dragging is only offered for
  // untilted (tiltA === tiltB === 0) planes, since the drag math below
  // assumes the plane's normal is a plain world axis.
  function setAxisPlanes(axisData, onDrag) {
    for (const child of planeGroup.children.slice()) {
      planeGroup.remove(child);
      child.geometry.dispose();
      child.material.dispose();
    }
    const allCuts = Object.values(axisData || {}).flatMap((d) => (d && d.planes) || []);
    const anyActive = allCuts.some((c) => c.active);
    dragCallback = onDrag || null;

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
        // Drag metadata: which cut this plane represents, and the world
        // axis + center offset needed to convert a drag position back into
        // the mm coordinate cut.value uses (see enablePlaneDragging).
        plane.userData = {
          draggable: !cut.tiltA && !cut.tiltB,
          axisName, index: i, axisIdx, center: center[axisIdx],
        };

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

  // --- Drag-to-move cut planes directly in the 3D view ---------------------
  let dragCallback = null;
  let dragState = null; // { axisName, index, axisIdx, center, axisVec }
  const raycaster = new THREE.Raycaster();

  function pointerToNDC(event) {
    const rect = renderer.domElement.getBoundingClientRect();
    return new THREE.Vector2(
      ((event.clientX - rect.left) / rect.width) * 2 - 1,
      -((event.clientY - rect.top) / rect.height) * 2 + 1,
    );
  }

  // Closest point between the mouse ray and the infinite line through the
  // plane's current position along its own axis -- the standard
  // skew-line-closest-point construction. This is what lets dragging work
  // from any camera angle, including the toolbar's axis-aligned views
  // (a naive "camera-facing drag plane" degenerates exactly there, when the
  // drag axis lines up with the view direction).
  function closestAxisValue(ray, axisVec, pointOnAxis) {
    const o1 = ray.origin, d1 = ray.direction;
    const o2 = pointOnAxis, d2 = axisVec;
    const r = new THREE.Vector3().subVectors(o1, o2);
    const a = d1.dot(d1), b = d1.dot(d2), c = d2.dot(d2), d = d1.dot(r), e = d2.dot(r);
    const denom = a * c - b * b;
    if (Math.abs(denom) < 1e-9) return null; // ray parallel to the drag axis -- can't recover movement along it
    const t2 = (a * e - b * d) / denom;
    return new THREE.Vector3().copy(o2).addScaledVector(d2, t2);
  }

  function onPointerDown(event) {
    if (!dragCallback) return;
    const ndc = pointerToNDC(event);
    raycaster.setFromCamera(ndc, camera);
    const hits = raycaster.intersectObjects(planeGroup.children, false);
    const hit = hits.find((h) => h.object.userData && h.object.userData.draggable);
    if (!hit) return;

    const { axisName, index, axisIdx, center } = hit.object.userData;
    const axisVec = new THREE.Vector3(axisIdx === 0 ? 1 : 0, axisIdx === 1 ? 1 : 0, axisIdx === 2 ? 1 : 0);
    dragState = { axisName, index, axisIdx, center, axisVec, pointOnAxis: hit.object.position.clone() };
    controls.enabled = false;
    try { renderer.domElement.setPointerCapture(event.pointerId); } catch (err) { /* synthetic/edge-case pointer, not fatal */ }
    event.preventDefault();
  }

  function onPointerMove(event) {
    if (!dragState) return;
    const ndc = pointerToNDC(event);
    raycaster.setFromCamera(ndc, camera);
    const closest = closestAxisValue(raycaster.ray, dragState.axisVec, dragState.pointOnAxis);
    if (!closest) return;
    const localCoord = closest.getComponent(dragState.axisIdx);
    const newValue = localCoord + dragState.center;
    if (dragCallback) dragCallback(dragState.axisName, dragState.index, newValue);
  }

  function onPointerUp(event) {
    if (!dragState) return;
    dragState = null;
    controls.enabled = true;
    try { renderer.domElement.releasePointerCapture(event.pointerId); } catch (err) { /* already released */ }
  }

  renderer.domElement.addEventListener("pointerdown", onPointerDown);
  renderer.domElement.addEventListener("pointermove", onPointerMove);
  renderer.domElement.addEventListener("pointerup", onPointerUp);
  renderer.domElement.addEventListener("pointercancel", onPointerUp);

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
    if (bedBoxGeometry) bedBoxGeometry.dispose();
    if (bedBoxMaterial) bedBoxMaterial.dispose();
    for (const child of planeGroup.children) {
      child.geometry.dispose();
      child.material.dispose();
    }
    renderer.domElement.removeEventListener("pointerdown", onPointerDown);
    renderer.domElement.removeEventListener("pointermove", onPointerMove);
    renderer.domElement.removeEventListener("pointerup", onPointerUp);
    renderer.domElement.removeEventListener("pointercancel", onPointerUp);
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

  return { loadArrayBuffer, setAxisPlanes, setBedBox, setView, resize, dispose };
}
