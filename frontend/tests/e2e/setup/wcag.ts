/**
 * Shared WCAG-AA e2e audit constants.
 *
 * Extracted so every axe-based e2e spec uses the SAME tag set and the SAME list
 * of tolerated library-internal violations — a second, drifted copy of a policy
 * list is how "acceptable" quietly becomes "unnoticed".
 */

/** The WCAG AA tag set the e2e audits assert against. */
export const WCAG_TAGS = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa']

// aria-hidden-focus: PrimeVue renders hidden tabbable focus-trap spans
// (`p-hidden-accessible p-hidden-focusable`) inside its Select/overlay panels
// for a11y navigation. axe flags these as aria-hidden-focus; they are a
// library-internal navigation affordance, not a Modulo defect.
const ACCEPTABLE_VIOLATIONS = ['color-contrast', 'scrollable-region-focusable', 'aria-hidden-focus']

/** Drop the library-internal violations Modulo does not own. */
export function filterViolations(violations: { id: string }[]) {
  return violations.filter((v) => !ACCEPTABLE_VIOLATIONS.includes(v.id))
}
