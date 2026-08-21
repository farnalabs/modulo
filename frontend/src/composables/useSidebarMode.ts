import { computed } from 'vue'
import { useMediaQuery } from '@vueuse/core'
import { usePlanStore } from '../stores/planStore'

export function useSidebarMode() {
  const isDesktop = useMediaQuery('(min-width: 768px)')
  const planStore = usePlanStore()
  const mobileRailFlag = computed(() => planStore.featureEnabled('mobile_sidebar_rail'))
  const showMobileHeader = computed(() => !isDesktop.value && !mobileRailFlag.value)

  return { isDesktop, mobileRailFlag, showMobileHeader }
}
