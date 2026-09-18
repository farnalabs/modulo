import { ref } from 'vue'
import { useApi } from './useApi'

interface OrgSettingsResponse {
  currency?: string | null
}

const DEFAULT_CURRENCY = 'USD'

const currencyCode = ref<string | null>(null)
let inflight: Promise<string> | null = null

async function fetchCurrencyCode(): Promise<string> {
  if (currencyCode.value) return currencyCode.value
  if (!inflight) {
    const { get } = useApi()
    inflight = get<OrgSettingsResponse>('/api/v1/org/settings')
      .then((data) => {
        const code = data?.currency
        currencyCode.value =
          code && typeof code === 'string' && code.trim() ? code.trim().toUpperCase() : DEFAULT_CURRENCY
        return currencyCode.value
      })
      .catch(() => {
        currencyCode.value = DEFAULT_CURRENCY
        return DEFAULT_CURRENCY
      })
      .finally(() => {
        inflight = null
      })
  }
  return inflight
}

export function useOrgCurrency() {
  return {
    currencyCode,
    loadCurrency: fetchCurrencyCode,
  }
}
