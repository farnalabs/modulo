import type { InjectionKey } from 'vue'

/**
 * Append target `AppSelect` uses for its overlay when the consumer does not
 * pass an explicit `append-to`.
 *
 * `AppSelect` defaults to `appendTo: 'self'`, which renders the overlay inside
 * the Select's own positioned wrapper. That is correct for page-level selects
 * (see FAR-851: portalling to `<body>` detached filter-bar dropdowns to the
 * viewport origin in the sidebar + main flex layout), but it is wrong inside a
 * dialog: a dialog hosts its content in `.p-dialog-content`, which is a scroll
 * container (`overflow-y: auto`). An overlay rendered inside that container is
 * clipped by it, so options that fall below the content box render outside the
 * visible area and are covered by `.p-dialog-footer` — a user clicking where
 * the option appears hits the footer instead (FAR-1908).
 *
 * A dialog host therefore provides `'body'` through this key: the overlay is
 * portalled out of the clipping container while still being positioned against
 * the trigger (PrimeVue's `absolutePosition` writes document coordinates, and
 * `<body>` is `position: static`, so the overlay's containing block is the
 * initial containing block — the coordinates land where they should).
 *
 * Provided by `FormDialog.vue`. Any dialog that hosts a Select in its scrollable
 * content must provide it too.
 */
export const SELECT_APPEND_TARGET: InjectionKey<string> = Symbol('modulo.selectAppendTarget')
