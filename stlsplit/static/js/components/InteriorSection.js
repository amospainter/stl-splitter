import CollapsibleCard from "./CollapsibleCard.js";

export default {
  name: "InteriorSection",
  components: { CollapsibleCard },
  props: { form: { type: Object, required: true } },
  template: `
    <collapsible-card title="Interior" hint="optional">
      <label class="form-label">Hollow to wall thickness (mm, blank = solid)</label>
      <input type="number" step="any" class="form-control" v-model="form.hollow_wall">
      <div class="form-text">Thin shells may not have room for connectors at every interface — sockets are skipped wherever the wall is too thin rather than punching through.</div>
    </collapsible-card>
  `,
};
