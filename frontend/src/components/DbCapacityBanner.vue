<template>
  <div
    v-if="visible"
    class="flex items-start gap-3 px-6 py-3 border-l-4 bg-card text-foreground"
    :class="[bannerClass.border, bannerClass.background]"
    :role="levelMeta.role"
    :aria-live="levelMeta.ariaLive"
    data-testid="db-capacity-banner"
    :data-alert-level="alertLevel"
  >
    <svg
      class="h-5 w-5 shrink-0"
      :class="bannerClass.text"
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="2"
      stroke-linecap="round"
      stroke-linejoin="round"
      aria-hidden="true"
    >
      <template v-if="!isFull">
        <path d="m21.73 18-8-14a2 2 0 0 0-3.46 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z" />
        <path d="M12 9v4M12 17h.01" />
      </template>
      <template v-else>
        <rect x="4" y="4" width="16" height="16" rx="3" />
        <path d="M9 9l6 6M15 9l-6 6" />
      </template>
    </svg>

    <div class="min-w-0 flex-1 space-y-1">
      <p class="text-sm font-semibold">
        <template v-if="isFull">{{ $t('components.DbCapacityBanner.full_message', { percent: roundedPercent }) }}</template>
        <template v-else-if="isCritical">{{ $t('components.DbCapacityBanner.critical_message', { percent: roundedPercent }) }}</template>
        <template v-else>{{ $t('components.DbCapacityBanner.warn_message', { percent: roundedPercent }) }}</template>
      </p>
      <p class="text-xs text-muted-foreground">
        <span v-if="usedBytesLabel !== null" data-testid="db-capacity-usage">
          {{ $t('components.DbCapacityBanner.storage_usage', { used: usedBytesLabel, capacity: capacityBytesLabel }) }}
        </span>
        <span v-if="modeNote" class="block">
          {{ $t('components.DbCapacityBanner.mode_note', { mode: modeNote }) }}
        </span>
        <span v-if="isFull" class="block">
          {{ $t('components.DbCapacityBanner.operator_bypass_note') }}
        </span>
      </p>
      <div class="flex flex-wrap items-center gap-x-4 gap-y-1 pt-1">
        <router-link
          to="/admin/run-retention"
          class="text-xs font-medium underline underline-offset-2"
          :class="bannerClass.text"
          data-testid="db-capacity-run-retention-link"
        >{{ $t('components.DbCapacityBanner.run_retention_link') }}</router-link>
        <router-link
          to="/admin/housekeeping"
          class="text-xs font-medium underline underline-offset-2"
          :class="bannerClass.text"
          data-testid="db-capacity-housekeeping-link"
        >{{ $t('components.DbCapacityBanner.housekeeping_link') }}</router-link>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { fetchDbCapacity, type DbCapacityInfo } from '../lib/api/dbCapacity'

const POLL_INTERVAL_MS = 60_000

const capacity = ref<DbCapacityInfo | null>(null)
let pollTimer: ReturnType<typeof setInterval> | null = null

async function refresh() {
  capacity.value = await fetchDbCapacity()
}

onMounted(() => {
  void refresh()
  pollTimer = setInterval(() => { void refresh() }, POLL_INTERVAL_MS)
})

onBeforeUnmount(() => {
  if (pollTimer !== null) {
    clearInterval(pollTimer)
    pollTimer = null
  }
})

const percent = computed<number | null>(() => {
  const p = capacity.value?.capacity_percent
  return typeof p === 'number' && Number.isFinite(p) ? p : null
})

const roundedPercent = computed<number>(() => Math.round(percent.value ?? 0))

const alertLevel = computed<string>(() => capacity.value?.alert_level ?? 'ok')

const mode = computed<string>(() => capacity.value?.mode ?? 'fixed')

const modeNote = computed<string | null>(() => (mode.value ? mode.value : null))

// No banner for ok, elastic, or disabled — those states need no operator
// action, and a null/invalid percent is treated as unknown (hide).
const visible = computed<boolean>(() => {
  const c = capacity.value
  if (!c) return false
  if (c.mode === 'elastic' || c.mode === 'disabled') return false
  if (c.alert_level === 'ok') return false
  return percent.value !== null
})

const isFull = computed<boolean>(() => alertLevel.value === 'full')

const isCritical = computed<boolean>(() => alertLevel.value === 'critical')

interface BannerClass {
  border: string
  background: string
  text: string
}

const bannerClass = computed<BannerClass>(() => {
  if (isCritical.value || isFull.value) {
    return {
      border: 'border-l-[hsl(var(--destructive))]',
      background: 'bg-[hsl(var(--destructive)/0.08)]',
      text: 'text-[hsl(var(--destructive))]',
    }
  }
  return {
    border: 'border-l-[hsl(var(--warning))]',
    background: 'bg-[hsl(var(--warning)/0.08)]',
    text: 'text-[hsl(var(--warning))]',
  }
})

const levelMeta = computed<{ role: string; ariaLive: 'polite' | 'assertive' }>(() => {
  if (isCritical.value || isFull.value) {
    return { role: 'alert', ariaLive: 'assertive' }
  }
  return { role: 'status', ariaLive: 'polite' }
})

function formatBytes(bytes: number | null | undefined): string {
  if (bytes == null || typeof bytes !== 'number' || !Number.isFinite(bytes) || bytes < 0) {
    return '—'
  }
  const units = ['B', 'KB', 'MB', 'GB', 'TB'] as const
  let value = bytes
  let idx = 0
  while (value >= 1024 && idx < units.length - 1) {
    value /= 1024
    idx += 1
  }
  const decimals = Number.isInteger(value) ? 0 : 1
  return `${value.toFixed(decimals)} ${units[idx]}`
}

const usedBytesLabel = computed<string | null>(() => {
  const used = capacity.value?.used_bytes
  return typeof used === 'number' && Number.isFinite(used) ? formatBytes(used) : null
})

const capacityBytesLabel = computed<string | null>(() => {
  const cap = capacity.value?.capacity_bytes
  return typeof cap === 'number' && Number.isFinite(cap) ? formatBytes(cap) : null
})
</script>
