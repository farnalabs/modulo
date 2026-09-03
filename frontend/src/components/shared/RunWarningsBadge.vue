<script setup lang="ts">
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { AlertTriangle } from '@lucide/vue'

const props = defineProps<{
  runId: string
  count: number
}>()

const { t } = useI18n()
const router = useRouter()

const label = computed(() =>
  props.count === 1
    ? t('components.RunWarningsBadge.one_warning')
    : t('components.RunWarningsBadge.n_warnings', { count: props.count }),
)

function openRunWarnings() {
  // Navigate to the run detail page and scroll to the #warnings anchor.
  router.push({ path: `/runs/${props.runId}`, query: { warn: '1' } })
}
</script>

<template>
  <button
    v-if="count > 0"
    type="button"
    class="inline-flex items-center gap-1 rounded-full bg-warning/10 px-2 py-0.5 text-xs font-medium text-warning transition-colors hover:bg-warning/25"
    :data-testid="`runs-list-warnings-${runId}`"
    :aria-label="label"
    v-tooltip.top="{ value: label, showDelay: 300 }"
    @click.stop="openRunWarnings"
    @keydown.stop
  >
    <AlertTriangle aria-hidden="true" class="h-3.5 w-3.5" />
    {{ count }}
  </button>
  <span v-else class="text-muted-foreground">—</span>
</template>
