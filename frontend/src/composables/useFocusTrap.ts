// Shared focus-trap primitives used by modal/sheet components so the
// Tab-cycle algorithm and its selector live in exactly one place.
//
// `trapTabInElement` keeps keyboard focus (Tab / Shift+Tab) inside `root`.
// When focus is already on the first/last focusable, or has escaped `root`
// entirely, it wraps to the opposite end. `FOCUSABLE_SELECTOR` is the single
// source of truth for "what counts as focusable" and must stay in sync with
// the querySelectoral calls used to find the initial focus target.

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
  const last = focusable[focusable.length - 1]
  const active = document.activeElement
  const focusInside = active instanceof HTMLElement && root.contains(active)

  if (e.shiftKey) {
    if (active === first || !focusInside) {
      e.preventDefault()
      last.focus()
    }
  } else if (active === last || !focusInside) {
    e.preventDefault()
    first.focus()
  }
}
