// Shared focus-trap primitives used by modal/sheet components so the
// Tab-cycle algorithm and its selector live in exactly one place.
//
// `trapTabInElement` keeps keyboard focus (Tab / Shift+Tab) inside `root`.
// When focus is already on the first/last focusable, on the `root` container
// itself (a panel that took focus on open), or has escaped `root` entirely, it
// wraps to the opposite end. `FOCUSABLE_SELECTOR` is the single source of truth
// for "what counts as focusable" and must stay in sync with the querySelector
// calls used to find the initial focus target.

export const FOCUSABLE_SELECTOR =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'

export function trapTabInElement(e: KeyboardEvent, root: HTMLElement | null): void {
  if (!root) return
  const focusable = Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR))
  if (focusable.length === 0) {
    e.preventDefault()
    return
  }
  const first = focusable[0]
  const last = focusable.at(-1)!
  const active = document.activeElement
  const focusInside = active instanceof HTMLElement && root.contains(active)
  // A panel with `tabindex="-1"` takes focus when it opens, so
  // `document.activeElement` can be `root` itself. Its DOM position is before
  // every focusable descendant, so for a backwards Tab it is the start edge —
  // without this, Shift+Tab from a freshly-opened panel escapes behind it.
  const atStart = active === first || active === root

  if (e.shiftKey) {
    if (atStart || !focusInside) {
      e.preventDefault()
      last.focus()
    }
  } else if (active === last || !focusInside) {
    e.preventDefault()
    first.focus()
  }
}
