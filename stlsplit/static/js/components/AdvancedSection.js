// Scale and Interior used to be their own collapsed-by-default cards
// (CollapsibleCard) so a rarely-touched field didn't clutter the main flow.
// Now that they live behind their own "Advanced" tab (see TabNav in App.js),
// that extra layer of hiding is redundant — getting here already means the
// user wants these settings, so both sections are just shown directly.
export default {
  name: "AdvancedSection",
  props: { form: { type: Object, required: true } },
  template: `
    <div>
      <div class="section-block">
        <div class="section-block-header"><i class="bi bi-arrows-angle-expand"></i> Scale</div>
        <div class="row g-2">
          <div class="col"><label class="form-label">Scale factor</label><input type="number" step="any" class="form-control" v-model="form.scale"></div>
          <div class="col"><label class="form-label">Target dimension (mm)</label><input type="number" step="any" class="form-control" v-model="form.target_dim"></div>
        </div>
        <label class="form-label">Target dimension axis</label>
        <select class="form-select" v-model="form.axis">
          <option value="x">X</option><option value="y">Y</option><option value="z">Z</option>
        </select>
        <div class="form-text">Only used when a target dimension is set above.</div>
      </div>

      <div class="section-block">
        <div class="section-block-header"><i class="bi bi-circle-half"></i> Interior</div>
        <label class="form-label">Hollow to wall thickness (mm, blank = solid)</label>
        <input type="number" step="any" class="form-control" v-model="form.hollow_wall">
        <div class="form-text">Thin shells may not have room for connectors at every interface — sockets are skipped wherever the wall is too thin rather than punching through.</div>
      </div>

      <div class="section-block">
        <div class="section-block-header"><i class="bi bi-exclamation-triangle"></i> Cut checks</div>
        <div class="form-check">
          <input type="checkbox" class="form-check-input" id="i-allow-floating-adv" v-model="form.allow_floating_regions">
          <label class="form-check-label" for="i-allow-floating-adv">Allow disconnected regions from a cut</label>
        </div>
        <div class="form-text">Normally a cut that lands where the model pinches to nothing (splitting a piece into unattached "floating" chunks) is rejected. Enable this to allow it through instead of having to reposition the cut.</div>
      </div>
    </div>
  `,
};
