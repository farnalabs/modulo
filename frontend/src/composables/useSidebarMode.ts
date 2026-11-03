import { useMediaQuery } from '@vueuse/core'

/** The sidebar is always a left rail on mobile and desktop.
 *
 * Desktop users can expand the rail into the full sidebar. Mobile users keep
 * the touch-sized rail and can open the full sidebar as a modal panel.
 */
export type MobileNavMode = 'rail'

export function useSidebarMode() {
  const isDesktop = useMediaQuery('(min-width: 768px)')

  return { isDesktop, mobileNavMode: 'rail' as const }
}
