<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import { AlertTriangle } from '@lucide/vue'

export interface RunWarning {
  code?: string
  severity?: string
  message?: string
}

const props = defineProps<{
  warnings: RunWarning[]
}>()

const { t } = useI18n()

function codeLabelKey(code: string): string {
  if (code === 'missing_self_report') return 'views.RunDetailView.warning_missing_self_report'
  return 'views.RunDetailView.warning_generic'
}
</script>

<template>
  <ul v-if="warnings.length > 0" class="space-y-2">
    <li
      v-for="(warning, index) in warnings"
      :key="warning.code || index"
      class="flex items-start gap-2"
      :data-testid="`run-detail-warning-${warning.code || index}`"
    >
      <AlertTriangle aria-hidden="true" class="mt-0.5 h-4 w-4 shrink-0" />
      <span>{{ t(codeLabelKey(warning.code ?? ''), { message: warning.message || '' }) }}</span>
    </li>
  </ul>
</template>
