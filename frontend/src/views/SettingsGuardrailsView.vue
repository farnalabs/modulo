<template>
  <FeatureGate feature-name="guardrails" required-tier="community" show-disabled>
    <div data-theme="agent" class="page-wide">
      <header class="flex items-center justify-between">
        <PageHeader :title="$t('views.SettingsGuardrailsView.title')" :subtitle="$t('views.SettingsGuardrailsView.subtitle')" />
        <Button
          data-testid="settings-guardrails-create"
          variant="default"
          class="border-primary/30 hover:border-primary/60"
          @click="openCreateDialog"
        >
          {{ $t('views.SettingsGuardrailsView.create_guardrail') }}
        </Button>
      </header>

      <div
        v-if="killSwitchEnabled"
        data-testid="settings-guardrails-kill-switch-banner"
        role="status"
        aria-live="polite"
        class="mb-4 rounded-lg border border-amber-500/40 bg-amber-500/10 p-4"
      >
        <p class="text-sm font-medium">{{ $t('views.SettingsGuardrailsView.kill_switch_banner') }}</p>
        <p class="mt-1 text-sm text-muted-foreground">{{ $t('views.SettingsGuardrailsView.kill_switch_banner_body') }}</p>
      </div>

      <LoadingSpinner v-if="!loaded" />

      <ErrorAlert v-else-if="error" :message="error" :on-retry="loadAll" />

      <div v-else-if="items.length === 0" class="rounded-lg border bg-card p-8 text-center">
        <p class="text-lg font-medium">{{ $t('views.SettingsGuardrailsView.no_guardrails_configured') }}</p>
        <p class="mt-1 text-sm text-muted-foreground">
          {{ $t('views.SettingsGuardrailsView.no_guardrails_configured_description') }}
        </p>
      </div>

      <template v-else>
        <div class="overflow-x-auto rounded-lg border bg-card shadow-sm">
          <table class="w-full text-left text-sm">
            <thead class="bg-muted/50 text-xs font-medium uppercase text-muted-foreground">
              <tr>
                <th class="px-4 py-3">{{ $t('views.SettingsGuardrailsView.name') }}</th>
                <th class="px-4 py-3">{{ $t('views.SettingsGuardrailsView.action') }}</th>
                <th class="px-4 py-3">{{ $t('views.SettingsGuardrailsView.detection_type') }}</th>
                <th class="px-4 py-3">{{ $t('views.SettingsGuardrailsView.field') }}</th>
                <th class="px-4 py-3">{{ $t('views.SettingsGuardrailsView.pipeline') }}</th>
                <th class="px-4 py-3">{{ $t('views.SettingsGuardrailsView.status') }}</th>
              </tr>
            </thead>
            <tbody class="divide-y">
              <tr v-for="g in items" :key="g.id" class="transition-colors hover:bg-muted/30">
                <td class="px-4 py-3 font-medium">{{ g.name }}</td>
                <td class="px-4 py-3">
                  <span :class="actionBadgeClass(g)" class="badge capitalize">{{ actionLabel(g) }}</span>
                  <span
                    v-if="isObserveMode(g)"
                    data-testid="settings-guardrails-observe-badge"
                    role="status"
                    aria-live="polite"
                    class="ml-2 inline-flex items-center rounded-full bg-warning/10 px-2.5 py-0.5 text-xs font-medium text-warning"
                    :title="$t('views.SettingsGuardrailsView.observe_badge_title')"
                  >
                    {{ $t('views.SettingsGuardrailsView.observe_badge') }}
                  </span>
                </td>
                <td class="px-4 py-3">
                  <span :class="detectionBadgeClass(g)" class="badge capitalize">{{ detectionLabel(g) }}</span>
                </td>
                <td class="px-4 py-3 font-mono text-xs">{{ fieldLabel(g) }}</td>
                <td class="px-4 py-3">{{ pipelineName(g.pipeline_id) }}</td>
                <td class="px-4 py-3 capitalize">{{ failureBehaviourLabel(g) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </template>

      <FormDialog
        :open="dialogOpen"
        @update:open="dialogOpen = false"
        :title="$t('views.SettingsGuardrailsView.create_guardrail')"
        :description="$t('views.SettingsGuardrailsView.create_guardrail_description')"
        :confirmText="$t('views.SettingsGuardrailsView.create')"
        :loading="saving"
        @confirm="saveGuardrail"
      >
        <form @submit.prevent="saveGuardrail" class="space-y-4">
          <div>
            <label for="settingsguardrailsview-name" class="mb-1 block text-sm font-medium">{{ $t('views.SettingsGuardrailsView.guardrail_name') }}</label>
            <input
              id="settingsguardrailsview-name"
              v-model="form.name"
              type="text"
              class="input-base"
              :placeholder="$t('views.SettingsGuardrailsView.guardrail_name_placeholder')"
              data-testid="settings-guardrails-form-name"
            />
          </div>

          <div>
            <label for="settingsguardrailsview-pipeline" class="mb-1 block text-sm font-medium">{{ $t('views.SettingsGuardrailsView.pipeline') }}</label>
            <Select :aria-label="$t('views.SettingsGuardrailsView.pipeline')" v-model="form.pipeline_id">
              <SelectTrigger id="settingsguardrailsview-pipeline" class="input-base" :aria-label="$t('views.SettingsGuardrailsView.pipeline')" data-testid="settings-guardrails-form-pipeline">
                <SelectValue :placeholder="$t('views.SettingsGuardrailsView.select_pipeline')" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem v-for="p in pipelines" :key="p.id" :value="p.id">{{ p.name }}</SelectItem>
              </SelectContent>
            </Select>
          </div>

          <div>
            <label for="settingsguardrailsview-action" class="mb-1 block text-sm font-medium">{{ $t('views.SettingsGuardrailsView.action') }}</label>
            <Select :aria-label="$t('views.SettingsGuardrailsView.action')" v-model="form.action">
              <SelectTrigger id="settingsguardrailsview-action" class="input-base" :aria-label="$t('views.SettingsGuardrailsView.action')" data-testid="settings-guardrails-form-action">
                <SelectValue :placeholder="$t('views.SettingsGuardrailsView.select_action')" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="observe">{{ $t('views.SettingsGuardrailsView.action_observe') }}</SelectItem>
                <SelectItem value="warn">{{ $t('views.SettingsGuardrailsView.action_warn') }}</SelectItem>
                <SelectItem value="block">{{ $t('views.SettingsGuardrailsView.action_block') }}</SelectItem>
                <SelectItem value="redact">{{ $t('views.SettingsGuardrailsView.action_redact') }}</SelectItem>
              </SelectContent>
            </Select>
          </div>

          <div>
            <label for="settingsguardrailsview-detection" class="mb-1 block text-sm font-medium">{{ $t('views.SettingsGuardrailsView.detection_type') }}</label>
            <Select :aria-label="$t('views.SettingsGuardrailsView.detection_type')" v-model="form.detectionType">
              <SelectTrigger id="settingsguardrailsview-detection" class="input-base" :aria-label="$t('views.SettingsGuardrailsView.detection_type')" data-testid="settings-guardrails-form-detection">
                <SelectValue :placeholder="$t('views.SettingsGuardrailsView.select_detection')" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="regex">{{ $t('views.SettingsGuardrailsView.detection_regex') }}</SelectItem>
                <SelectItem value="json_schema">{{ $t('views.SettingsGuardrailsView.detection_json_schema') }}</SelectItem>
              </SelectContent>
            </Select>
          </div>

          <div>
            <label for="settingsguardrailsview-field" class="mb-1 block text-sm font-medium">{{ $t('views.SettingsGuardrailsView.field_path') }}</label>
            <input
              id="settingsguardrailsview-field"
              v-model="form.field"
              type="text"
              class="input-base font-mono"
              :placeholder="$t('views.SettingsGuardrailsView.field_path_placeholder')"
              data-testid="settings-guardrails-form-field"
            />
          </div>

          <div v-if="form.detectionType === 'regex'">
            <label for="settingsguardrailsview-pattern" class="mb-1 block text-sm font-medium">{{ $t('views.SettingsGuardrailsView.pattern') }}</label>
            <textarea
              id="settingsguardrailsview-pattern"
              v-model="form.pattern"
              rows="3"
              class="input-base font-mono"
              :placeholder="$t('views.SettingsGuardrailsView.pattern_placeholder')"
              data-testid="settings-guardrails-form-pattern"
            />
          </div>

          <div v-else>
            <label for="settingsguardrailsview-schema" class="mb-1 block text-sm font-medium">{{ $t('views.SettingsGuardrailsView.json_schema') }}</label>
            <textarea
              id="settingsguardrailsview-schema"
              v-model="form.schema"
              rows="6"
              class="input-base font-mono"
              :placeholder="$t('views.SettingsGuardrailsView.json_schema_placeholder')"
              data-testid="settings-guardrails-form-schema"
            />
          </div>

          <div
            data-testid="settings-guardrails-form-disclosure"
            class="rounded-lg border bg-muted/50 p-3 text-xs text-muted-foreground"
          >
            <p class="font-medium text-foreground">{{ $t('views.SettingsGuardrailsView.disclosure_title') }}</p>
            <p class="mt-1">
              {{ form.detectionType === 'regex' ? $t('views.SettingsGuardrailsView.disclosure_regex', { action: disclosureAction }) : $t('views.SettingsGuardrailsView.disclosure_json_schema', { action: disclosureAction }) }}
            </p>
          </div>

          <div
            v-if="formError"
            data-testid="settings-guardrails-form-error"
            role="alert"
            class="text-sm font-medium text-destructive"
          >
            {{ formError }}
          </div>
        </form>
      </FormDialog>
    </div>
  </FeatureGate>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { useDataFetch } from '../composables/useDataFetch'
import { useApi } from '../composables/useApi'
import { Button } from '@/components/ui/button'
import { api, getAccessToken } from '../lib/api/client'
import { decodeJwtPayload } from '../lib/jwt'
import { formatApiError } from '../lib/api/formatError'
import type { components } from '../lib/api/client'
import PageHeader from '../components/shared/PageHeader.vue'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import FormDialog from '../components/shared/FormDialog.vue'
import { usePlanStore } from '../stores/planStore'
import FeatureGate from '../components/FeatureGate.vue'
import { shortId } from '../utils/format'
import {
  Select,
  SelectTrigger,
  SelectContent,
  SelectItem,
  SelectValue,
} from '@/components/ui/select'

const planStore = usePlanStore()
const { t } = useI18n()

interface JwtPayload {
  org_role?: string
  org_id?: string
}

function readJwtPayload(): JwtPayload | null {
  return decodeJwtPayload(getAccessToken()) as JwtPayload | null
}

const orgId = computed(() => readJwtPayload()?.org_id ?? '')

type PipelineItem = components['schemas']['PipelineResponse']

interface GuardrailItem {
  id: string
  pipeline_id: string
  node_id?: string | null
  name: string
  eval_type: string
  config_json: Record<string, unknown>
  failure_behaviour?: string
}

const { error, data: guardrailsData, load: loadGuardrails, fetched: guardrailsLoaded } = useDataFetch(
  () => api.GET('/api/v1/evals', { params: { query: { page: 1, page_size: 100, eval_type: 'guardrail' } } }),
)
const { data: pipelinesData, load: loadPipelines, fetched: pipelinesLoaded } = useDataFetch(
  () => api.GET('/api/v1/pipelines', {}),
  { immediate: false }
)

const loaded = computed(() => guardrailsLoaded.value && pipelinesLoaded.value)

const items = computed<GuardrailItem[]>(() =>
  ((guardrailsData.value as { items?: GuardrailItem[] } | null)?.items ?? []),
)
const pipelines = computed<PipelineItem[]>(() =>
  ((pipelinesData.value as { items?: PipelineItem[] } | null)?.items ?? []),
)

const killSwitchEnabled = ref(false)

async function loadKillSwitch() {
  if (!orgId.value) return
  try {
    const { get } = useApi()
    const res = await get<{ enabled: boolean }>(
      `/api/v1/org/settings/guardrails/kill-switch`,
    )
    killSwitchEnabled.value = Boolean(res?.enabled)
  } catch (e: unknown) {
    console.warn(t('views.SettingsGuardrailsView.kill_switch_read_error'), e)
  }
}

const dialogOpen = ref(false)
const saving = ref(false)
const formError = ref<string | null>(null)

interface GuardrailForm {
  name: string
  pipeline_id: string
  action: string
  detectionType: string
  field: string
  pattern: string
  schema: string
}

const defaultForm: GuardrailForm = {
  name: '',
  pipeline_id: '',
  action: 'observe',
  detectionType: 'regex',
  field: '',
  pattern: '',
  schema: '',
}

const form = ref<GuardrailForm>({ ...defaultForm })

function resetForm() {
  form.value = { ...defaultForm }
  formError.value = null
}

function openCreateDialog() {
  resetForm()
  dialogOpen.value = true
}

function actionLabel(g: GuardrailItem): string {
  const action = (g.config_json as Record<string, unknown>)?.action as string | undefined
  return action || 'observe'
}

function actionBadgeClass(g: GuardrailItem): string {
  const action = actionLabel(g)
  const classes: Record<string, string> = {
    observe: 'badge badge-context-cyan',
    warn: 'badge badge-context-amber',
    block: 'badge badge-status-destructive',
    redact: 'badge badge-context-purple',
  }
  return classes[action] || 'badge badge-context-slate'
}

function detectionTypeOf(g: GuardrailItem): string {
  return ((g.config_json as Record<string, unknown>)?.type as string | undefined) || 'regex'
}

function detectionLabel(g: GuardrailItem): string {
  return detectionTypeOf(g)
}

function detectionBadgeClass(g: GuardrailItem): string {
  return detectionTypeOf(g) === 'json_schema' ? 'badge badge-context-indigo' : 'badge badge-context-slate'
}

function fieldLabel(g: GuardrailItem): string {
  const cfg = g.config_json as Record<string, unknown>
  const field = typeof cfg.field === 'string' ? cfg.field : ''
  return field || '\u2014'
}

function failureBehaviourLabel(g: GuardrailItem): string {
  return g.failure_behaviour || 'warn'
}

function isObserveMode(g: GuardrailItem): boolean {
  if (killSwitchEnabled.value) return true
  return actionLabel(g) === 'observe'
}

function pipelineName(id: string): string {
  const p = pipelines.value.find(p => p.id === id)
  return p ? p.name : shortId(id)
}

const disclosureAction = computed(() => {
  const map: Record<string, string> = {
    block: t('views.SettingsGuardrailsView.disclosure_action_block'),
    warn: t('views.SettingsGuardrailsView.disclosure_action_warn'),
    redact: t('views.SettingsGuardrailsView.disclosure_action_redact'),
    observe: t('views.SettingsGuardrailsView.disclosure_action_observe'),
  }
  return map[form.value.action] || form.value.action
})

async function saveGuardrail() {
  formError.value = null

  if (!form.value.name || !form.value.pipeline_id || !form.value.action || !form.value.detectionType || !form.value.field) {
    formError.value = t('views.SettingsGuardrailsView.required_fields')
    return
  }

  const configJson: Record<string, unknown> = {
    interception_point: 'input',
    action: form.value.action,
    redaction: [],
    required_capabilities: [],
    max_guardrails_per_node: 0,
    guardrail_timeout_seconds: 5.0,
  }

  if (form.value.detectionType === 'regex') {
    if (!form.value.pattern) {
      formError.value = t('views.SettingsGuardrailsView.regex_requires_pattern')
      return
    }
    configJson.type = 'regex'
    configJson.pattern = form.value.pattern
    configJson.field = form.value.field
  } else {
    let schema: unknown
    try {
      schema = JSON.parse(form.value.schema)
    } catch {
      formError.value = t('views.SettingsGuardrailsView.schema_requires_json')
      return
    }
    configJson.type = 'json_schema'
    configJson.schema = schema
  }

  saving.value = true
  try {
    const { error: err } = await api.POST('/api/v1/evals', {
      body: {
        pipeline_id: form.value.pipeline_id,
        node_id: null,
        name: form.value.name,
        eval_type: 'guardrail',
        config_json: configJson,
        failure_behaviour: 'warn',
      } as any,
    })
    if (err) {
      formError.value = t('views.SettingsGuardrailsView.save_failed', { detail: formatApiError(err) })
      return
    }
    dialogOpen.value = false
    await loadGuardrails()
  } catch (e: unknown) {
    formError.value = t('views.SettingsGuardrailsView.save_failed', { detail: formatApiError(e) })
  } finally {
    saving.value = false
  }
}

async function loadAll() {
  await Promise.all([loadGuardrails(), loadPipelines()])
}

onMounted(() => { planStore.fetchPlan(); loadAll(); loadKillSwitch() })

</script>
