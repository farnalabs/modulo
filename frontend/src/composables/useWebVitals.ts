import { onMounted, onUnmounted } from 'vue'
import { usePlanStore } from '../stores/planStore'
import type { MetricType } from 'web-vitals'

interface WebVitalEvent {
  metric_name: string
  metric_value: number
  metric_rating: string | null
  route_path: string
  page_url: string
  navigation_type: string | null
}

const VITALS_API = '/api/v1/metrics/web-vitals'
const _buffer: WebVitalEvent[] = []
let _flushTimer: ReturnType<typeof setTimeout> | null = null
let _enabled = false

function flush() {
  if (_buffer.length === 0) return
  const batch = _buffer.splice(0)
  fetch(VITALS_API, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ events: batch }),
    credentials: 'include',
  }).catch(() => {
    // Silently fail — analytics, not critical
  })
}

function scheduleFlush() {
  if (_flushTimer) clearTimeout(_flushTimer)
  _flushTimer = setTimeout(flush, 5000)
}

function sendToAnalytics(metric: MetricType) {
  if (!_enabled) return

  const routePath = window.location.pathname
  _buffer.push({
    metric_name: metric.name,
    metric_value: metric.value,
    metric_rating: metric.rating,
    route_path: routePath,
    page_url: window.location.href,
    navigation_type: metric.navigationType ?? null,
  })

  if (_buffer.length >= 10) {
    flush()
  } else {
    scheduleFlush()
  }
}

/**
 * Composable that initializes Web Vitals tracking.
 * Only activates if the web_vitals_analytics feature flag is enabled.
 * Call once from App.vue.
 */
export function useWebVitals() {
  const planStore = usePlanStore()

  onMounted(async () => {
    if (!planStore.currentTier) {
      try {
        await planStore.fetchPlan()
      } catch {
        return
      }
    }

    _enabled = planStore.featureEnabled('web_vitals_analytics')

    if (!_enabled) return

    try {
      const { onLCP, onFCP, onCLS, onINP, onTTFB } = await import('web-vitals')
      onLCP(sendToAnalytics)
      onFCP(sendToAnalytics)
      onCLS(sendToAnalytics)
      onINP(sendToAnalytics)
      onTTFB(sendToAnalytics)
    } catch {
      // web-vitals not available
    }
  })

  onUnmounted(() => {
    flush()
    if (_flushTimer) {
      clearTimeout(_flushTimer)
      _flushTimer = null
    }
  })
}
