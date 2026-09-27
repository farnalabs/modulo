/**
 * Shared visual tokens for VueFlow canvases (pipeline editor, composite editor,
 * library wizard preview, lifecycle maps) — FAR-1249.
 *
 * Edges follow the theme instead of a hard-coded grey: the muted-foreground
 * token at reduced opacity reads as a quiet connector on both the light and
 * dark themes. VueFlow applies edge `style` as an inline CSS style on the SVG
 * path, so CSS custom properties resolve at render time and the stroke tracks
 * theme switches without re-rendering.
 *
 * Node "kind" labels (manual / agent / router / gate ...) above a node title
 * use `font-brand-mono text-[11px] font-medium lowercase tracking-wide` plus
 * the kind's colour class — the lowercase monospace flavour from the product
 * intro video. Lowercasing is CSS-only so the i18n strings stay untouched.
 *
 * The value deliberately contains no spaces: when used as a marker colour,
 * VueFlow derives the SVG <marker> element id from it and references it via
 * `url('#<id>')`, and a space in that fragment would break the lookup.
 */
export const CANVAS_EDGE_STROKE = 'hsl(var(--muted-foreground)/0.6)'
