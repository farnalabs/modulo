import { computed } from 'vue'
import { useMediaQuery } from '@vueuse/core'
import { usePlanStore } from '../stores/planStore'

/**
 * Which mobile navigation chrome to render (FAR-1237).
 *
 * - `rail`    — the primary left icon rail (`mobile_sidebar_rail` ON).
 * - `drawer`  — the legacy fixed hamburger top-nav + slide-in drawer (flag OFF).
 * - `pending` — the flag has not resolved yet (no persisted cache AND the
 *   feature-flags request is still in flight). The consumer must render
 *   NEITHER chrome — a neutral placeholder — so the first paint can never be
 *   the wrong layout, whatever mode the user's org actually resolves to.
 *
 * Scope of the guarantee (FAR-1237 review findings): it covers the first paint
 * of the LAYOUT chrome, i.e. once AppLayout mounts. Before auth settles App.vue
 * renders LoginView (no nav chrome at all), and the bare routes that render
 * without AppLayout (e.g. /assistant, `meta.bare`) draw no nav chrome either —
 * so neither can flash a wrong layout, and both are intentionally out of scope.
 */
export type MobileNavMode = 'rail' | 'drawer' | 'pending'

/**
 * Stable test id for the pending placeholder header. Exported so components and
 * specs share one literal (FAR-1237 review nit: the id was duplicated as a
 * string across AppLayout.spec and the e2e spec, which could drift).
 */
export const MOBILE_NAV_PENDING_TESTID = 'mobile-nav-pending'

export function useSidebarMode() {
  const isDesktop = useMediaQuery('(min-width: 768px)')
  const planStore = usePlanStore()

  // A flag value is knowable only once something real has populated it: the
  // persisted cache (synchronous, read when the plan store is created) or a
  // server payload. An unfetched, empty map means UNKNOWN — defaulting it to
  // either layout is exactly the first-paint flash this composable exists to
  // prevent. The `features`-non-empty fallback covers state populated directly
  // without going through either source.
  const flagsKnown = computed(
    () => planStore.flagsSource !== 'none' || Object.keys(planStore.features).length > 0,
  )
  const mobileRailFlag = computed(() => planStore.featureEnabled('mobile_sidebar_rail'))

  const mobileNavMode = computed<MobileNavMode>(() => {
    if (!flagsKnown.value) return 'pending'
    return mobileRailFlag.value ? 'rail' : 'drawer'
  })

  // True whenever fixed mobile chrome occupies the top 3.5rem: the legacy
  // header AND the pending placeholder share the header slot, so <main>'s
  // pt-14 offset applies to both; only the in-flow left rail needs no offset.
  const showMobileHeader = computed(
    () => !isDesktop.value && mobileNavMode.value !== 'rail',
  )

  return { isDesktop, mobileRailFlag, mobileNavMode, showMobileHeader }
}
