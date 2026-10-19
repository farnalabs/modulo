/**
 * Shared WCAG-AA e2e audit constants.
 *
 * Extracted so every axe-based e2e spec uses the SAME tag set and the SAME list
 * of tolerated library-internal violations — a second, drifted copy of a policy
 * list is how "acceptable" quietly becomes "unnoticed".
 */

/** The WCAG AA tag set the e2e audits assert against. */
export const WCAG_TAGS = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa']

/**
 * axe rule ids whose findings are library-internal and NOT a Modulo defect.
 *
 * Every entry MUST carry a written justification below, and MUST be scoped to
 * a specific third-party affordance. A rule id is never tolerated "because it
 * was failing" — that is what turned this list into a mute button (FAR-1913).
 *
 * REMOVED in FAR-1913 (each had a real finding; do not re-add without a fix):
 *   - `color-contrast` — muted with no justification. Unmuted, it found the
 *     destructive-status badge at 4.32:1 (dark) / 3.46:1 (light), both below
 *     the WCAG AA 4.5:1 floor. Fixed at the source by the `--destructive-text`
 *     token; the rule is asserted everywhere again.
 *   - `scrollable-region-focusable` — muted with no justification. Unmuted, it
 *     fired on PrimeVue's `.p-select-list-container` scroll box. Rather than
 *     tolerate it, it was fixed at the source: AppSelect now passes
 *     `pt.listContainer.tabindex = 0`, which makes every Select's scroll box
 *     keyboard-focusable (AppSelect is the only Select wrapper in the app).
 */
const ACCEPTABLE_VIOLATIONS = [
  // aria-hidden-focus: PrimeVue renders hidden tabbable focus-trap spans
  // (`p-hidden-accessible p-hidden-focusable`) inside its Select/overlay panels
  // for a11y navigation. axe flags these as aria-hidden-focus; they are a
  // library-internal navigation affordance, not a Modulo defect.
  'aria-hidden-focus',
]

/** Drop the library-internal violations Modulo does not own. */
export function filterViolations(violations: { id: string }[]) {
  return violations.filter((v) => !ACCEPTABLE_VIOLATIONS.includes(v.id))
}
