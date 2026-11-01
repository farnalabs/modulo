import { computed } from 'vue'
import { useI18n } from 'vue-i18n'

export function useCostPageTabs() {
  const { t } = useI18n()

  return computed(() => [
    { label: t('views.AdminCostBreakdownView.tabs_overview'), to: '/admin/costs' },
    { label: t('views.AdminCostBreakdownView.tabs_spend_limits'), to: '/admin/costs/limits' },
    { label: t('views.AdminCostBreakdownView.tabs_cost_components'), to: '/admin/costs/components' },
    { label: t('views.AdminCostBreakdownView.tabs_cost_controls'), to: '/admin/costs/controls' },
  ])
}
