import PieceCard from "./PieceCard.js";
import { PALETTE } from "../viewer.js";

export default {
  name: "PieceGrid",
  components: { PieceCard },
  props: {
    pieces: { type: Array, required: true }, // [{name, data_base64}]
    fixedColor: { type: Number, default: null }, // dowels all share one color; pieces cycle the palette
  },
  emits: ["expand"],
  template: `
    <div class="row row-cols-2 row-cols-sm-3 row-cols-md-4 row-cols-xl-5 row-cols-xxl-6 g-3">
      <div class="col" v-for="(p, i) in pieces" :key="p.name">
        <piece-card :name="p.name" :data-base64="p.data_base64"
          :color="fixedColor ?? PALETTE[i % PALETTE.length]"
          @expand="$emit('expand', p, fixedColor ?? PALETTE[i % PALETTE.length])"></piece-card>
      </div>
    </div>
  `,
  setup() {
    return { PALETTE };
  },
};
