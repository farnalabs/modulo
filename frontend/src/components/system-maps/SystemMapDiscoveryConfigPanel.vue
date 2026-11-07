<template>
  <div class="fixed inset-0 z-50" data-testid="system-map-discovery-config">
    <button type="button" class="absolute inset-0 bg-black/50" :aria-label="$t('common.close')" :disabled="submitting" @click="$emit('close')" data-testid="system-map-discovery-config-backdrop" />
    <dialog open class="absolute right-0 top-0 h-full w-full max-w-xl m-0 bg-card shadow-2xl overflow-hidden flex flex-col" aria-labelledby="discovery-config-title">
      <header class="border-b px-4 py-3 flex items-center justify-between">
        <h2 id="discovery-config-title" class="font-semibold">{{ $t('views.SystemMapDetailView.discovery_title') }}</h2>
        <button type="button" class="rounded p-1" :aria-label="$t('common.close')" :disabled="submitting" @click="$emit('close')" data-testid="system-map-discovery-config-close">×</button>
      </header>
      <form class="flex-1 overflow-y-auto p-4 space-y-5" @submit.prevent="submit">
        <fieldset :disabled="submitting">
          <legend class="text-sm font-medium">{{ $t('views.SystemMapDetailView.discovery_mode_label') }}</legend>
          <div class="mt-2 grid gap-2">
            <label class="rounded border p-3 cursor-pointer" :class="mode === 'deterministic' ? 'border-primary bg-primary/5' : ''">
              <input v-model="mode" type="radio" value="deterministic" class="mr-2" data-testid="system-map-discovery-mode-deterministic" />
              <span class="font-medium">{{ $t('views.SystemMapDetailView.mode_deterministic') }}</span>
              <span class="block text-xs text-muted-foreground ml-6">{{ $t('views.SystemMapDetailView.deterministic_hint') }}</span>
            </label>
            <label v-if="canUseAi" class="rounded border p-3 cursor-pointer" :class="mode === 'ai_assisted' ? 'border-primary bg-primary/5' : ''">
              <input v-model="mode" type="radio" value="ai_assisted" class="mr-2" data-testid="system-map-discovery-mode-ai" />
              <span class="font-medium">{{ $t('views.SystemMapDetailView.mode_ai_assisted') }}</span>
              <span class="ml-2 rounded bg-muted px-1.5 py-0.5 text-xs">{{ $t('views.SystemMapDetailView.beta_private_preview') }}</span>
              <span class="block text-xs text-muted-foreground ml-6">{{ $t('views.SystemMapDetailView.ai_assisted_hint') }}</span>
            </label>
          </div>
        </fieldset>

        <div v-if="mode === 'ai_assisted'" class="space-y-4" data-testid="system-map-ai-controls">
          <p class="text-sm text-muted-foreground">{{ $t('views.SystemMapDetailView.ai_gate_explanation') }}</p>
          <div>
            <label for="discovery-backend" class="block text-sm font-medium">{{ $t('views.SystemMapDetailView.model_backend_label') }}</label>
            <select id="discovery-backend" v-model="backendId" class="mt-1 w-full rounded border bg-background px-3 py-2" :disabled="loading || failed || submitting" data-testid="system-map-discovery-backend">
              <option value="">{{ $t('views.SystemMapDetailView.select_model_backend') }}</option>
              <option v-for="backend in backends" :key="backend.id" :value="backend.id">{{ backend.display_name }} ({{ backend.provider }})</option>
            </select>
          </div>
          <div>
            <label for="discovery-eval" class="block text-sm font-medium">{{ $t('views.SystemMapDetailView.eval_judge_label') }}</label>
            <select id="discovery-eval" v-model="evalId" class="mt-1 w-full rounded border bg-background px-3 py-2" :disabled="loading || failed || submitting" data-testid="system-map-discovery-eval">
              <option value="">{{ $t('views.SystemMapDetailView.select_eval_judge') }}</option>
              <option v-for="evaluation in evals" :key="evaluation.id" :value="evaluation.id">{{ evaluation.name }} (v{{ evaluation.version }})</option>
            </select>
          </div>
          <p v-if="loading" class="text-sm text-muted-foreground" aria-live="polite">{{ $t('views.SystemMapDetailView.loading_ai_options') }}</p>
          <p v-if="failed" class="rounded border border-destructive bg-destructive/10 p-3 text-sm text-destructive" aria-live="assertive">{{ $t('views.SystemMapDetailView.ai_options_unavailable') }}</p>
          <p v-else-if="!accessProbePositive" class="rounded border border-destructive bg-destructive/10 p-3 text-sm text-destructive" aria-live="assertive">{{ $t('views.SystemMapDetailView.internal_probe_required') }}</p>
        </div>
        <p v-if="submitting" class="rounded border bg-muted/40 p-3 text-sm" aria-live="polite" data-testid="system-map-discovery-request-loading">{{ $t('views.SystemMapDetailView.discovery_request_loading') }}</p>
        <p v-if="submitError" class="rounded border border-destructive bg-destructive/10 p-3 text-sm text-destructive" aria-live="assertive" data-testid="system-map-discovery-request-error">{{ submitError }}</p>
      </form>
      <footer class="border-t px-4 py-3 flex justify-end gap-2">
        <button type="button" class="rounded border px-3 py-1.5 text-sm" :disabled="submitting" @click="$emit('close')" data-testid="system-map-discovery-config-cancel">{{ $t('common.cancel') }}</button>
        <button type="button" class="rounded bg-primary px-3 py-1.5 text-sm text-primary-foreground disabled:opacity-50" :disabled="!canSubmit || submitting" @click="submit" data-testid="system-map-discovery-submit">{{ submitting ? $t('views.SystemMapDetailView.discovery_request_loading') : $t('views.SystemMapDetailView.start_discovery') }}</button>
      </footer>
    </dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { api } from '../../lib/api/client'
import type { components } from '../../lib/api/schema'
import type { DiscoveryMode } from '../../stores/systemMaps'

type Backend = components['schemas']['ModelBackendResponse']
type Evaluation = components['schemas']['EvalDefinitionResponse']

const props = withDefaults(defineProps<{ canUseAi?: boolean; submitting?: boolean; submitError?: string | null }>(), {
  canUseAi: false,
  submitting: false,
  submitError: null,
})
const emit = defineEmits<{ (event: 'close'): void; (event: 'submit', options?: { modelBackendId?: string; evalDefinitionId?: string; modelBackendName?: string; evalName?: string; evalVersion?: number }): void }>()
const mode = ref<DiscoveryMode>('deterministic')
const backendId = ref('')
const evalId = ref('')
const backends = ref<Backend[]>([])
const evals = ref<Evaluation[]>([])
const loading = ref(false)
const failed = ref(false)
const accessProbePositive = ref(false)

const canSubmit = computed(() => mode.value === 'deterministic' || (props.canUseAi && !loading.value && !failed.value && accessProbePositive.value && Boolean(backendId.value) && Boolean(evalId.value)))

async function loadAiOptions(): Promise<void> {
  if (!props.canUseAi) return
  loading.value = true
  failed.value = false
  accessProbePositive.value = false
  try {
    const [probe, backendResponse, evalResponse] = await Promise.all([
      api.GET('/api/v1/admin/feature-flags', { params: { query: { _fresh: true } } }),
      api.GET('/api/v1/model-backends', { params: { query: { page: 1, page_size: 100 } } }),
      api.GET('/api/v1/evals', { params: { query: { page: 1, page_size: 100, eval_type: 'model_judge' } } }),
    ])
    if (probe.error || backendResponse.error || evalResponse.error || !backendResponse.data || !evalResponse.data) throw new Error('AI discovery options unavailable')
    const flags = (probe.data as { flags?: Array<{ name: string; currently_active: boolean }> }).flags
    accessProbePositive.value = Array.isArray(flags) && flags.some((flag) => flag.name === 'system_maps' && flag.currently_active)
    if (!accessProbePositive.value) throw new Error('Internal access probe failed')
    backends.value = backendResponse.data.items.filter((backend) => backend.visibility === 'org' && backend.tier !== 'in_dev')
    evals.value = evalResponse.data.items.filter((evaluation) => evaluation.eval_type === 'model_judge')
  } catch {
    failed.value = true
    backends.value = []
    evals.value = []
  } finally {
    loading.value = false
  }
}

watch(() => [mode.value, props.canUseAi] as const, ([value, allowed]) => { if (value === 'ai_assisted' && allowed) void loadAiOptions() })
onMounted(() => { if (mode.value === 'ai_assisted') void loadAiOptions() })

function submit(): void {
  if (!canSubmit.value) return
  if (mode.value === 'deterministic') emit('submit')
  else {
    const backend = backends.value.find((item) => item.id === backendId.value)
    const evaluation = evals.value.find((item) => item.id === evalId.value)
    emit('submit', {
      modelBackendId: backendId.value,
      evalDefinitionId: evalId.value,
      modelBackendName: backend?.display_name,
      evalName: evaluation?.name,
      evalVersion: evaluation?.version,
    })
  }
}
</script>
