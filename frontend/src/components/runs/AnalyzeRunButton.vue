<template>
  <div v-if="visible" class="my-4" data-testid="run-detail-analyze">
    <!--
      The button stays focusable when disabled (aria-disabled rather than the
      native disabled attribute) so keyboard users can reach it, and the
      wrapper's group-focus-within reveals the same tooltip on focus that
      group-hover reveals on hover.
    -->
    <span class="group relative inline-flex items-center">
      <button
        type="button"
        data-testid="run-detail-analyze-button"
        class="inline-flex items-center gap-2 whitespace-nowrap rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium text-foreground transition-colors hover:bg-accent"
        :class="{ 'cursor-not-allowed opacity-60': disabledReason !== null }"
        :aria-disabled="disabledReason !== null ? 'true' : undefined"
        :aria-describedby="disabledReason !== null ? TOOLTIP_ID : undefined"
        :aria-busy="starting ? 'true' : undefined"
        @click="onAnalyze"
      >
        <LoaderIcon v-if="starting" class="h-4 w-4 animate-spin" aria-hidden="true" />
        <Stethoscope v-else class="h-4 w-4" aria-hidden="true" />
        {{ $t('components.AnalyzeRunButton.analyze') }}
      </button>
      <span
        v-if="disabledReason !== null"
        :id="TOOLTIP_ID"
        role="tooltip"
        data-testid="run-detail-analyze-tooltip"
        class="pointer-events-none absolute bottom-full left-1/2 z-50 mb-2 max-w-[260px] -translate-x-1/2 rounded-md bg-foreground px-2 py-1 text-center text-xs font-normal leading-snug text-background opacity-0 shadow-md transition-opacity duration-150 group-focus-within:opacity-100 group-hover:opacity-100"
      >{{ disabledReason }}</span>
    </span>
    <span
      role="status"
      aria-live="polite"
      class="ml-3 align-middle text-xs text-muted-foreground"
      data-testid="run-detail-analyze-status"
    >{{ statusMessage }}</span>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { LoaderIcon, Stethoscope } from '@lucide/vue'
import { api } from '@/lib/api/client'
import { formatApiError } from '@/lib/api/formatError'
import { usePlanStore } from '@/stores/planStore'
import { useAssistantStore } from '@/composables/useAssistantStore'
import { useAssistantStream } from '@/composables/useAssistantStream'
import {
  buildAnalyzeSeedMessage,
  isAnalyzableFailure,
  pipelineLabel,
  runLabel,
  type AnalyzeRunInfo,
} from './analyzeRun'

const props = defineProps<{ run: AnalyzeRunInfo }>()

const TOOLTIP_ID = 'run-detail-analyze-tooltip'

const { t } = useI18n()
const planStore = usePlanStore()
const assistantStore = useAssistantStore()

/** null = still checking whether a model backend is configured. */
const configured = ref<boolean | null>(null)
const starting = ref(false)
const statusMessage = ref('')

/**
 * "The assistant is enabled" reuses the existing signals rather than a new
 * endpoint: the `assistant` feature flag (planStore.featureEnabled) plus
 * dev mode, which is what actually gates every Assistant surface (the
 * floating panel and the /assistant route are private_preview).
 */
const assistantEnabled = computed(() => planStore.featureEnabled('assistant') && planStore.devMode)

const analyzableFailure = computed(() => isAnalyzableFailure(props.run.status))

/** Only render once we know the assistant is there — no dead buttons. */
const visible = computed(() => analyzableFailure.value && assistantEnabled.value)

const disabledReason = computed<string | null>(() => {
  if (!visible.value) return null
  if (configured.value === null) return t('components.AnalyzeRunButton.checking_configuration')
  if (configured.value === false) return t('components.AnalyzeRunButton.assistant_not_configured')
  if (starting.value || assistantStore.isStreaming) return t('components.AnalyzeRunButton.assistant_busy')
  return null
})

/**
 * Existing signal for "configured with a backend model": the same endpoint
 * the Assistant configuration page uses (`GET /api/v1/model-backends`, a
 * viewer-level permission) — configured means at least one backend holds
 * credentials, mirroring the server's own resolution rule.
 */
async function checkConfigured(): Promise<void> {
  try {
    const { data, error } = await api.GET('/api/v1/model-backends', {
      params: { query: { page_size: 100 } },
    })
    if (error) {
      configured.value = false
      return
    }
    configured.value = (data?.items ?? []).some(b => b.has_credentials === true)
  } catch {
    // Fail closed: an unknown configuration state must not launch a
    // conversation the backend cannot answer.
    configured.value = false
  }
}

// Resolve whether a backend model exists the moment the button becomes
// eligible to show. The plan itself (assistant flag + dev mode) is NOT fetched
// here — AppLayout already loads it for every authenticated route, so this
// component only reads it.
watch(
  visible,
  (isVisible) => {
    if (isVisible && configured.value === null) void checkConfigured()
  },
  { immediate: true },
)

async function onAnalyze(): Promise<void> {
  if (disabledReason.value !== null) return
  starting.value = true
  statusMessage.value = t('components.AnalyzeRunButton.status_starting')
  try {
    const session = await assistantStore.createSession({
      name: t('components.AnalyzeRunButton.session_name', { run: runLabel(props.run) }),
    })
    if (!session) {
      statusMessage.value = t('components.AnalyzeRunButton.status_failed', {
        message: assistantStore.error ?? '',
      })
      return
    }
    await assistantStore.sendMessage(buildAnalyzeSeedMessage(t, props.run))
    if (assistantStore.panelState === 'closed') {
      assistantStore.setPanelState('floating')
    }
    // Deliberately created inside the click handler: useAssistantStream only
    // registers its abort-on-unmount hook when called during component setup,
    // so starting it here keeps the analysis streaming after the user
    // navigates away from this run (the conversation lives in the panel).
    const { connectStream } = useAssistantStream()
    void connectStream(session.id)
    statusMessage.value = t('components.AnalyzeRunButton.status_started', {
      pipeline: pipelineLabel(props.run),
    })
  } catch (e: unknown) {
    statusMessage.value = t('components.AnalyzeRunButton.status_failed', {
      message: formatApiError(e),
    })
  } finally {
    starting.value = false
  }
}
</script>
