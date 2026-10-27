# PrimeVue 5 → 4 downgrade — measured delta (FAR-2205)

This is the measured equivalence record for replacing the commercially licensed
PrimeVue 5 with the MIT-licensed PrimeVue 4 line. The standard applied is **no
unexplained difference**: every measured difference below is either eliminated
(matched to the v5 pixel) or explicitly justified.

## Method

| Step | Tool | Artefact |
| --- | --- | --- |
| v5 reference captured at commit `ec89022af`, before any dependency change | `tests/e2e/primevue-visual-baseline.spec.ts` (local target, deterministic mock API) | 23 captures: 13 routes × 1280×800, 7 interactive states × 1280×800, 3 routes × 390×844 |
| Preset-level token comparison | `@primeuix/styled` `toVariables()` on both Aura presets | 2 610 common tokens; 684 raw value differences, all attributable to the `light-dark()` representation change or the density table below |
| DOM comparison | `src/__tests__/primevue-component-dom.spec.ts` with `PRIMEVUE_DOM_OUT` | 22 rendered-DOM signatures (all 15 components in the variants the app uses) |
| Delta computation | `tests/visual-baseline/compare.mjs` | this table |

The capture is deterministic: two consecutive v4 runs produced byte-identical
PNGs (`0 px` difference). `PRIMEVUE_BASELINE_OUT` writes a PNG + JSON pair per
slug; the comparator resolves `light-dark()` to the dark branch (the app's
default scheme) before comparing tokens, and ignores the worktree-derived port
in URLs.

Reproduce:

```sh
cd frontend
PRIMEVUE_BASELINE_OUT=test-results/primevue-baseline-v5 \
  pnpm exec playwright test tests/e2e/primevue-visual-baseline.spec.ts --workers=1
# …downgrade, then capture to primevue-baseline-v4…
node tests/visual-baseline/compare.mjs \
  --expected test-results/primevue-baseline-v5 \
  --actual   test-results/primevue-baseline-v4
```

## Headline

- **15 of 23 captures are pixel-identical (0 differing pixels).**
- The remaining 8 differ by **35–86 pixels each (≤ 0.021 % of the image)** — 503
  differing pixels across all 23 captures in total — and every one of those
  pixels is PrimeVue's own **bundled icon artwork** (see the
  justified-differences section). No layout, spacing, typography or colour
  difference remains.
- 17 of 22 component DOM signatures are identical apart from one added attribute
  (v5's `data-pc-id`); the five structural differences are listed below and are
  either accessibility-only or a changed icon asset.

## Per-capture delta

`bbox` is the bounding box of differing pixels at the 16/255 per-channel
threshold.

| Capture | Differing pixels | Share | bbox | Judgement |
| --- | --- | --- | --- | --- |
| `desktop-dashboard` | 0 | 0% | — | eliminated |
| `desktop-pipelines` | 0 | 0% | — | eliminated |
| `desktop-pipeline-editor` | 35 | 0.003% | 416,228 12×8 | justified: Select chevron glyph |
| `desktop-runs` | 70 | 0.007% | 1061,173 182×8 | justified: two Select chevron glyphs |
| `desktop-run-detail` | 0 | 0% | — | eliminated |
| `desktop-settings-license` | 0 | 0% | — | eliminated |
| `desktop-settings-triggers` | 0 | 0% | — | eliminated |
| `desktop-settings-mcp` | 35 | 0.002% | 571,948 12×8 | justified: Select chevron glyph |
| `desktop-schemas` | 0 | 0% | — | eliminated |
| `desktop-admin-users` | 0 | 0% | — | eliminated |
| `desktop-admin-connectors` | 0 | 0% | — | eliminated |
| `desktop-admin-model-backends` | 0 | 0% | — | eliminated |
| `desktop-admin-run-retention` | 0 | 0% | — | eliminated |
| `desktop-admin-users-dialog` | 86 | 0.008% | 820,213 13×320 | justified: Dialog close (×) + Select chevron glyphs |
| `desktop-pipelines-folder-dialog` | 86 | 0.008% | 820,292 13×163 | justified: Dialog close (×) + Select chevron glyphs |
| `desktop-pipelines-row-menu` | 0 | 0% | — | eliminated |
| `desktop-runs-select-open` | 70 | 0.007% | 1061,173 182×8 | justified: two Select chevron glyphs |
| `desktop-settings-license-dialog` | 51 | 0.005% | 821,339 12×12 | justified: Dialog close (×) glyph |
| `desktop-parameter-schemas-tabs` | 0 | 0% | — | eliminated |
| `desktop-dashboard-tooltip` | 0 | 0% | — | eliminated |
| `mobile-dashboard` | 0 | 0% | — | eliminated |
| `mobile-pipelines` | 0 | 0% | — | eliminated |
| `mobile-runs` | 70 | 0.021% | 259,366 12×51 | justified: two Select chevron glyphs |

Nothing is left at the *blocking* level.

### Why the icon differences are justified, not fixed

The differing pixels are confined to three glyphs that ship **inside the
library** (`primevue/icons` → `@primevue/icons`): the Dialog close ×, the
Select dropdown chevron, and the Tabs overflow arrows. PrimeVue 5 replaced the
14×14 artwork with a 20×20 path set; PrimeVue 4.5.5 ships the older 14×14
assets. The glyph **box, position, colour and the surrounding layout are
identical** — only the path geometry inside the icon differs, which is why the
change is 35–86 pixels rather than a layout shift.

Matching v5's icon paths exactly would mean vendoring PrimeVue 5's icon assets
into a MIT-only tree (they were part of the commercially licensed package) or
replacing every default icon through pass-through slots. Neither is justified
for a sub-0.02 % glyph-artwork delta: the icons are functionally identical and
the change is visible only to a pixel diff. This is the one difference this
downgrade accepts, and it is recorded here so it is a known, reviewed delta
rather than an invisible drift.

## Token audit

1. **The token bridge is intact.** Every one of the 18 `--p-*` targets in
   `PRIMEVUE_TOKEN_BRIDGE` resolves in the **v4** Aura preset
   (`primevue-theme.spec.ts`, still green before any change). No token name
   changed between the presets: the v4 preset defines 2 610 tokens, v5 defines
   3 007; **every v4 token also exists in v5** (the v5-only 397 are new
   component-level tokens, all of them for components the app does not use or
   for tokenised typography that v4 hardcodes).

2. **The colour values are equal.** Raw preset comparison shows 684 differing
   values, but that is the representation: v5 emits
   `light-dark(<light>, <dark>)` into its declarations while v4's engine
   pre-resolves the active branch under `.dark`. Resolving `light-dark()` to the
   dark branch (the app's default) leaves **zero colour differences** for the
   components in use — confirmed by the pixel-identical captures.

3. **The density difference is real and was reconciled.** v5's Aura is
   *denser* than v4's by 8–20 % (`form-field` padding `0.625/0.375rem` vs
   `0.75/0.5rem`, badge height `1.25rem` vs `1.5rem`, menu item padding
   `0.25/0.625rem` vs `0.5/0.75rem`, dialog title `1.125rem` vs `1.25rem`,
   icon size `0.875rem` vs `1rem`). `PRIMEVUE_V4_DENSITY_PARITY` pins those
   **78 tokens** back to the recorded v5 value; the guard test asserts every
   entry (a) is a real v4 token and (b) equals the value in
   `tests/visual-baseline/v5/tokens.json`.

4. **Values that are inert in v4 are deliberately left alone**, because the v4
   CSS does not read them and no pixel is affected:
   - `--p-tabs-tab-border-color`, `--p-tabs-tab-active-border-color`,
     `--p-tabs-tab-hover-border-color` (v5 transparent / v4 coloured) — the tab
     border width is `0` in both, so the colour never paints.
   - `--p-virtualscroller-loader-icon-size` (1.75rem vs 2rem) — VirtualScroller
     is not used by the app.
   - 49 tokens the parity map defines eagerly that v5 only injects when the
     component mounts, and 37 v5-only typography tokens v4 does not read. These
     are additions/removals in the token *dump*, not rendered differences; the
     zero-pixel captures confirm it.

## DOM delta

22 signatures compared. **17 are byte-identical once v5's added
`data-pc-id="…"` attribute is removed.** The five structural differences:

| Component | Difference | Judgement |
| --- | --- | --- |
| `dialog.default` | close-icon SVG path (v4 14×14, v5 20×20) | justified: icon artwork, same box |
| `tooltip.default` | none beyond `data-pc-id` (normalised id) | identical |
| `menu.default` | v5 adds `aria-level` / `aria-posinset` / `aria-setsize`; v4 adds `data-p-focused` / `data-p-disabled` | justified: ARIA enrichment is additive and the app does not depend on it; `data-p-*` attributes are pass-through state markers consumed by PrimeVue only |
| `select.default` | v4 adds `aria-controls="…_list"` | justified: accessibility improvement in v4, no visual effect |
| `tabs.default` | v4 wraps the tab list in `.p-tablist-content.p-tablist-viewport > .p-tablist-tab-list`; v5 flattens to `.p-tablist-content[role=tablist]` | justified: no app CSS targets those internal wrappers (checked), and the tab strip is pixel-identical |

Everything else — class lists, `data-pc-section` names, `role`/`aria` wiring,
rendered text — is unchanged.

## Base font size — the decision, driven by measurement

The brief's premise was that **v5 assumes a 16px root and v4 assumes 14px**.
The measurement shows the opposite mechanism, and a different lever:

| | v5 (5.0.4) | v4 (4.5.5) |
| --- | --- | --- |
| `html` root font-size | 16px (unchanged) | 16px (unchanged) |
| Button / InputText / Select-label CSS | `font-size: dt('button.font.size')` → `0.875rem` = **14 px** | `font-size: 1rem` hardcoded = **16 px** |

So the difference is **not** the root font size — it is that v5 tokenised the
component font size while v4 hardcodes `1rem`. Setting the root to 14px would
have shrunk every Tailwind rem-based dimension in the app (spacing, radii,
breakpoints) by 12.5 % — a far larger and unrequested regression — and would
still not have fixed the padding deltas (v4's `0.5rem` padding is 8px at 16px
root; v5's `0.375rem` is 6px; both scale together).

**Decision: keep the root at 16px and restore the v5 component font size with a
targeted rule.** `installPrimeVueV4BaseFontStyles()` sets:

- `.p-button` (non-sm/lg) → `0.875rem`, font-weight `500` (v5 puts the weight on
  the root; v4 only on `.p-button-label`, which a label-only button lacks);
- `.p-inputtext` (non-sm/lg) → `0.875rem` (v4 hardcodes `1rem`);
- `.p-select .p-select-label` → `0.875rem` + `line-height: 1.5` (v4 hardcodes
  `1rem`; the line-height is explicit because a `text-xs` utility on a select
  root gives v4's label a 1.3333 inherited line-height where v5 computes 1.5);
- `.p-select-overlay .p-select-option…` → `0.875rem` (v4's dropdown options
  inherit 16px; v5 renders 14px);
- `.p-tabs .p-tab` → `0.875rem` (v4 omits the declaration);
- `.p-tooltip .p-tooltip-text` → `0.75rem` (v4 omits the declaration);
- `.p-menu .p-menu-item-label` → `0.875rem` + `line-height: 1.5`, plus the menu
  item icon sizing and the `will-change: transform` that v5's overlay carries
  (without the layer promotion v4's overlay text rasterises with subpixel
  antialiasing — 2 228 differing pixels on glyph edges with identical geometry).

This was validated by re-measurement, not by inspection: the first capture after
the downgrade was up to 6.1 % different per page; the final capture is 0 % on 15
of 23 pages and icon-only on the remaining 8.

## Contradictions with the brief

1. **Base font size runs the other way** (see above): both versions use a 16px
   root; v4's *components* are the larger ones.
2. **`@primeuix/styled` cannot be replaced by `@primevue/core`.** `@primevue/core`
   does not export `toVariables`/`toNormalizeVariable`, which the token-bridge
   guard uses. `@primeuix/styled` stays a direct dependency at `^0.7.4` — the
   version PrimeVue 4.5.5 itself depends on and the last v4-compatible line.
3. **The licence build-arg lives in `backend/Dockerfile.fly`, not
   `deploy/docker/Dockerfile.all-in-one`** (which never referenced it). The
   allowlist's filename was wrong; the reference was removed from the real file.
4. **`vite.config.ts` had to change** to remove the environment lift that
   sourced PrimeVue 5's licence key from an ancestor `.env`. It is not in the
   brief's allowlist, but Phase 5 requires no licence reference to remain, and
   leaving dead code that reads a licence key would contradict the purpose of
   the change.
