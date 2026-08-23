import type { LibraryPrimitive } from "./LibraryPrimitiveCard.vue";

export type LibraryBadge = "modulo" | "community" | "preview";

// Shared prop shape reused by LibraryPrimitiveCard and LibraryPrimitiveGrid so
// the common option/state props are declared exactly once.
export interface LibraryCardSharedProps {
  badge: LibraryBadge;
  showTags?: boolean;
  showAutoUpdate?: boolean;
  toggleLoading?: Record<string, boolean>;
  adapting?: Record<string, boolean>;
}

export interface LibraryCardEmits {
  "create-pipeline": [prim: LibraryPrimitive];
  "create-lifecycle-map": [prim: LibraryPrimitive];
  "view-details": [prim: LibraryPrimitive];
  "toggle-auto-update": [prim: LibraryPrimitive];
  install: [prim: LibraryPrimitive];
}
