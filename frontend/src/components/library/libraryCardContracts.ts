import type { LibraryPrimitive } from "./LibraryPrimitiveCard.vue";

export interface LibraryCardEmits {
  "create-pipeline": [prim: LibraryPrimitive];
  "create-lifecycle-map": [prim: LibraryPrimitive];
  "view-details": [prim: LibraryPrimitive];
  "toggle-auto-update": [prim: LibraryPrimitive];
  install: [prim: LibraryPrimitive];
}
