<template>
  <FeatureGate feature-name="webhook_trigger" required-tier="community" show-disabled>

    <div data-theme="agent" class="page-wide">
    <header class="flex items-center justify-between">
      <PageHeader :title="$t('views.SettingsTriggersView.title')" :subtitle="$t('views.SettingsTriggersView.subtitle')" />
      <Button data-testid="settings-triggers-create" class="border-primary/30 hover:border-primary/60" @click="openCreateDialog">
        {{ $t('views.SettingsTriggersView.create_trigger') }}
      </Button>
    </header>

    <div
      v-if="orgTriggersPaused"
      data-testid="settings-triggers-paused-banner"
      role="status"
      aria-live="polite"
      :aria-label="$t('views.SettingsTriggersView.paused_banner_title')"
      class="mb-4 block rounded-lg border border-amber-500/40 bg-amber-500/10 p-4"
    >
      <p class="text-sm font-medium">{{ $t('views.SettingsTriggersView.paused_banner_title') }}</p>
      <p class="mt-1 text-sm text-muted-foreground">{{ $t('views.SettingsTriggersView.paused_banner_body') }}</p>
      <p v-if="orgPausedAt" class="mt-1 text-xs text-muted-foreground">
        {{ $t('views.SettingsTriggersView.paused_at_label', { at: formatTimestamp(orgPausedAt) }) }}
      </p>
    </div>

    <div v-if="isOrgAdmin" class="mb-4 rounded-lg border bg-card p-4">
      <div class="flex items-center justify-between">
        <div class="pr-4">
          <p class="text-sm font-medium">{{ $t('views.SettingsTriggersView.pause_all_triggers') }}</p>
          <p class="mt-0.5 text-xs text-muted-foreground">{{ $t('views.SettingsTriggersView.pause_all_triggers_description') }}</p>
        </div>
        <Button data-testid="settings-triggers-pause-all" :severity="orgTriggersPaused ? 'secondary' : 'primary'" :outlined="orgTriggersPaused" :disabled="pauseToggling" :aria-pressed="orgTriggersPaused" @click="togglePauseAll">
          {{ orgTriggersPaused ? $t('views.SettingsTriggersView.resume_triggers') : $t('views.SettingsTriggersView.pause_all_triggers_action') }}
        </Button>
      </div>
      <p
        v-if="pauseError"
        data-testid="settings-triggers-pause-error"
        role="alert"
        class="mt-2 text-sm font-medium text-destructive"
      >
        {{ pauseError }}
      </p>
    </div>

    <LoadingSpinner v-if="!loaded" />

    <ErrorAlert v-else-if="error" :message="error" :on-retry="loadAll" />

    <div v-else-if="items.length === 0" class="rounded-lg border bg-card p-8 text-center">
      <p class="text-lg font-medium">{{ $t('views.SettingsTriggersView.no_triggers_configured') }}</p>
      <p class="mt-1 text-sm text-muted-foreground">
        {{ $t('views.SettingsTriggersView.no_triggers_configured_description') }}
      </p>
    </div>

    <template v-else>
      <div class="overflow-x-auto rounded-lg border bg-card shadow-sm">
        <table class="w-full text-left text-sm">
          <thead class="bg-muted/50 text-xs font-medium uppercase text-muted-foreground">
            <tr>
              <th class="px-4 py-3">{{ $t('views.SettingsTriggersView.pipeline') }}</th>
              <th class="px-4 py-3">{{ $t('views.SettingsTriggersView.type') }}</th>
              <th class="px-4 py-3 capitalize">{{ $t('views.SettingsTriggersView.status') }}</th>
              <th class="px-4 py-3">{{ $t('views.SettingsTriggersView.last_fired') }}</th>
              <th class="px-4 py-3">{{ $t('views.SettingsTriggersView.next_fire') }}</th>
              <th class="px-4 py-3 text-right">{{ $t('views.SettingsTriggersView.actions') }}</th>
            </tr>
          </thead>
          <tbody class="divide-y">
            <template v-for="t in items" :key="t.id">
              <tr class="transition-colors hover:bg-muted/30">
                <td class="px-4 py-3 font-medium">
                  {{ pipelineName(t.pipeline_id) }}
                </td>
              <td class="px-4 py-3">
                <span :class="typeBadgeClass(t.trigger_type)" class="badge">
                  {{ typeLabel(t.trigger_type) }}
                </span>
              </td>
              <td class="px-4 py-3">
                <div class="flex flex-col items-start gap-1">
                  <span
                    v-if="t.trigger_type === 'ongoing' && t.streak_status?.enabled"
                    data-testid="settings-triggers-streak"
                    :class="streakBadgeClass(t)"
                    class="inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-medium"
                  >
                    {{ $t('views.SettingsTriggersView.streak_display', { streak: t.streak_status.streak ?? 0, threshold: t.streak_status.threshold ?? 0 }) }}
                  </span>
                  <div
                    v-if="isDeactivatedOngoing(t)"
                    data-testid="settings-triggers-deactivated-badge"
                    role="status"
                    aria-live="polite"
                    :aria-label="$t('views.SettingsTriggersView.deactivated_badge', { reason: deactivatedReasonLabel(t) })"
                    class="inline-flex items-center gap-1 rounded-full bg-destructive/10 px-2.5 py-0.5 text-xs font-medium text-destructive"
                  >
                    {{ $t('views.SettingsTriggersView.deactivated_badge', { reason: deactivatedReasonLabel(t) }) }}
                  </div>
                  <div class="flex items-center gap-2">
                    <button
                      type="button"
                      class="inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium transition-colors disabled:opacity-50"
                      :class="t.active ? 'bg-success/10 text-success hover:bg-success/20' : 'bg-muted text-muted-foreground hover:bg-muted/80'"
                      :disabled="triggerToggling[t.id]"
                      data-testid="settings-triggers-toggle"
                      @click="toggleActive(t)"
                    >
                      <span
                        class="h-1.5 w-1.5 rounded-full"
                        :class="t.active ? 'bg-success' : 'bg-muted-foreground'"
                      />
                      {{ triggerToggling[t.id] ? '...' : (t.active ? $t('views.SettingsTriggersView.active') : $t('views.SettingsTriggersView.inactive')) }}
                    </button>
                    <Button v-if="isDeactivatedOngoing(t) && isOrgOperator" data-testid="settings-triggers-reenable" size="small" severity="secondary" outlined :disabled="triggerToggling[t.id]" @click="toggleActive(t)">
                      {{ $t('views.SettingsTriggersView.re_enable') }}
                    </Button>
                  </div>
                  <p
                    v-if="actionErrors[t.id]"
                    data-testid="settings-triggers-toggle-error"
                    role="alert"
                    class="text-xs font-medium text-destructive"
                  >
                    {{ actionErrors[t.id] }}
                  </p>
                </div>
              </td>
              <td class="px-4 py-3 text-muted-foreground">
                {{ formatTimestamp(t.last_fired_at ?? null) }}
              </td>
              <td class="px-4 py-3 text-muted-foreground">
                {{ formatTimestamp(t.next_fire_at ?? null) }}
              </td>
              <td class="px-4 py-3 text-right">
                <div class="flex items-center justify-end gap-2">
                  <TableActions :actions="triggerActions(t)" />
                  <button
                    type="button"
                    v-if="hasOutcomes(t)"
                    data-testid="settings-triggers-outcomes-toggle"
                    class="text-xs text-muted-foreground hover:text-foreground"
                    :aria-expanded="expandedOutcomes.has(t.id)"
                    :aria-controls="`outcomes-${t.id}`"
                    @click="toggleOutcomes(t.id)"
                  >
                    {{ expandedOutcomes.has(t.id) ? $t('views.SettingsTriggersView.hide_outcomes') : $t('views.SettingsTriggersView.show_outcomes') }}
                  </button>
                </div>
              </td>
              </tr>
              <tr v-if="expandedOutcomes.has(t.id) && hasOutcomes(t)" :id="`outcomes-${t.id}`" class="bg-muted/20">
                <td :colspan="6" class="px-4 py-3">
                  <p class="text-xs font-medium uppercase text-muted-foreground">
                    {{ $t('views.SettingsTriggersView.recent_outcomes') }}
                  </p>
                  <ul class="mt-2 space-y-1">
                    <li
                      v-for="(o, i) in t.streak_status?.last_outcomes ?? []"
                      :key="o.run_id || i"
                      class="flex items-center gap-2 text-xs"
                    >
                      <span :class="outcomeBadgeClass(o.classification)" class="rounded-full px-2 py-0.5 font-medium">
                        {{ outcomeLabel(o.classification) }}
                      </span>
                      <span class="text-muted-foreground">{{ o.reason || '—' }}</span>
                      <span class="ml-auto text-muted-foreground">{{ formatTimestamp(o.completed_at ?? null) }}</span>
                    </li>
                  </ul>
                </td>
              </tr>
            </template>
          </tbody>
        </table>
      </div>
    </template>

    <FormDialog
      :open="dialogOpen"
      @update:open="dialogOpen = false"
      :title="editingId ? $t('views.SettingsTriggersView.edit_trigger') : $t('views.SettingsTriggersView.create_trigger')"
      :description="editingId ? $t('views.SettingsTriggersView.update_trigger_description') : $t('views.SettingsTriggersView.create_trigger_description')"
      :confirmText="editingId ? $t('views.SettingsTriggersView.update') : $t('views.SettingsTriggersView.create')"
      :loading="saving"
      @confirm="saveTrigger"
    >
      <form @submit.prevent="saveTrigger" class="space-y-4">
        <div>
          <label for="settingstriggersview-pipeline" class="mb-1 block text-sm font-medium">{{ $t('views.SettingsTriggersView.pipeline') }}</label>
          <Select
  :aria-label="$t('views.SettingsTriggersView.pipeline')"
  v-model="form.pipeline_id"
  :placeholder="$t('views.SettingsTriggersView.select_pipeline')"
  data-testid="settings-triggers-form-pipeline"
  id="settingstriggersview-pipeline"
  class="input-base"
  :options="pipelines.map(p => ({ value: p.id, label: p.name }))"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
        </div>

        <div v-if="!editingId">
          <label for="settingstriggersview-trigger-type" class="mb-1 block text-sm font-medium">{{ $t('views.SettingsTriggersView.trigger_type_label') }}</label>
          <Select
  :aria-label="$t('views.SettingsTriggersView.trigger_type_label')"
  v-model="form.trigger_type"
  :placeholder="$t('views.SettingsTriggersView.select_type')"
  data-testid="settings-triggers-form-type"
  id="settingstriggersview-trigger-type"
  class="input-base"
  :options="[{ value: 'webhook', label: $t('views.SettingsTriggersView.webhook') }, { value: 'cron', label: $t('views.SettingsTriggersView.cron') }, { value: 'polling', label: $t('views.SettingsTriggersView.polling') }, { value: 'agent_signal', label: $t('views.SettingsTriggersView.agent_signal') }, { value: 'ongoing', label: $t('views.SettingsTriggersView.ongoing') }]"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
        </div>
        <div v-else class="text-sm text-muted-foreground">
          {{ $t('views.SettingsTriggersView.type_label') }}: <span class="font-medium">{{ typeLabel(editingType) }}</span>
        </div>

        <!-- Webhook config -->
        <template v-if="form.trigger_type === 'webhook'">
          <div>
            <label for="settingstriggersview-field-13" class="mb-1 block text-sm font-medium">{{ $t('views.SettingsTriggersView.url') }}</label>
            <input id="settingstriggersview-field-13"
              v-model="form.webhook_url"
              type="url"
              class="input-base"
              :placeholder="$t('views.SettingsTriggersView.url_placeholder')"
              data-testid="settings-triggers-form-webhook-url"
            />
          </div>
          <div>
            <label for="settingstriggersview-http-method" class="mb-1 block text-sm font-medium">{{ $t('views.SettingsTriggersView.http_method') }}</label>
            <Select
  :aria-label="$t('views.SettingsTriggersView.http_method')"
  v-model="form.webhook_method"
  :placeholder="$t('views.SettingsTriggersView.select_method')"
  data-testid="settings-triggers-form-webhook-method"
  id="settingstriggersview-http-method"
  class="input-base"
  :options="[{ value: 'POST', label: 'POST' }, { value: 'GET', label: 'GET' }, { value: 'PUT', label: 'PUT' }]"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
          </div>
          <div>
            <label for="settingstriggersview-field-11" class="mb-1 block text-sm font-medium">{{ $t('views.SettingsTriggersView.headers_json') }}</label>
            <textarea id="settingstriggersview-field-11"
              v-model="form.webhook_headers"
              rows="3"
              class="input-base font-mono"
              :placeholder="$t('views.SettingsTriggersView.headers_placeholder')"
              data-testid="settings-triggers-form-webhook-headers"
            />
          </div>
        </template>

        <!-- Cron config -->
        <template v-if="form.trigger_type === 'cron'">
          <div>
            <label for="settingstriggersview-field-10" class="mb-1 block text-sm font-medium">{{ $t('views.SettingsTriggersView.cron_expression') }}</label>
            <input id="settingstriggersview-field-10"
              v-model="form.cron_expression"
              type="text"
              class="input-base font-mono"
              :placeholder="$t('views.SettingsTriggersView.cron_expression_placeholder')"
              data-testid="settings-triggers-form-cron-expr"
            />
          </div>
          <div>
            <label for="settingstriggersview-field-9" class="mb-1 block text-sm font-medium">{{ $t('views.SettingsTriggersView.timezone') }}</label>
            <input id="settingstriggersview-field-9"
              v-model="form.cron_timezone"
              type="text"
              class="input-base"
              :placeholder="$t('views.SettingsTriggersView.timezone_placeholder')"
              data-testid="settings-triggers-form-cron-tz"
            />
          </div>
          <div>
            <label for="settingstriggersview-field-8" class="mb-1 block text-sm font-medium">{{ $t('views.SettingsTriggersView.input_template_json') }}</label>
            <textarea id="settingstriggersview-field-8"
              v-model="form.input_template"
              rows="3"
              class="input-base font-mono"
              :placeholder="$t('views.SettingsTriggersView.input_template_placeholder')"
              data-testid="settings-triggers-form-cron-input"
            />
          </div>
        </template>

        <!-- Polling config -->
        <template v-if="form.trigger_type === 'polling'">
          <div>
            <label for="settingstriggersview-field-7" class="mb-1 block text-sm font-medium">{{ $t('views.SettingsTriggersView.connector_instance_id') }}</label>
            <input id="settingstriggersview-field-7"
              v-model="form.connector_instance_id"
              type="text"
              class="input-base font-mono"
              :placeholder="$t('views.SettingsTriggersView.connector_instance_id_placeholder')"
              data-testid="settings-triggers-form-polling-connector"
            />
          </div>
          <div>
            <label for="settingstriggersview-field-6" class="mb-1 block text-sm font-medium">{{ $t('views.SettingsTriggersView.query') }}</label>
            <textarea id="settingstriggersview-field-6"
              v-model="form.poll_query"
              rows="3"
              class="input-base font-mono"
              :placeholder="$t('views.SettingsTriggersView.query_placeholder')"
              data-testid="settings-triggers-form-polling-query"
            />
          </div>
          <div>
            <label for="settingstriggersview-field-5" class="mb-1 block text-sm font-medium">{{ $t('views.SettingsTriggersView.poll_interval_seconds') }}</label>
            <input id="settingstriggersview-field-5"
              v-model="form.poll_interval"
              type="number"
              min="60"
              class="input-base"
              :placeholder="$t('views.SettingsTriggersView.poll_interval_placeholder')"
              data-testid="settings-triggers-form-polling-interval"
            />
            <p class="mt-1 text-xs text-muted-foreground">{{ $t('views.SettingsTriggersView.poll_interval_seconds_hint') }}</p>
          </div>
          <div>
            <label for="settingstriggersview-field-4" class="mb-1 block text-sm font-medium">{{ $t('views.SettingsTriggersView.condition_expression') }}</label>
            <input id="settingstriggersview-field-4"
              v-model="form.condition_expression"
              type="text"
              class="input-base font-mono"
              :placeholder="$t('views.SettingsTriggersView.condition_expression_placeholder')"
              data-testid="settings-triggers-form-polling-condition"
            />
          </div>
        </template>

        <!-- Agent Signal config -->
        <template v-if="form.trigger_type === 'agent_signal'">
          <div>
            <label for="settingstriggersview-source-pipeline" class="mb-1 block text-sm font-medium">{{ $t('views.SettingsTriggersView.source_pipeline') }}</label>
            <Select
  :aria-label="$t('views.SettingsTriggersView.source_pipeline')"
  v-model="form.signal_source_pipeline"
  :placeholder="$t('views.SettingsTriggersView.select_source_pipeline')"
  data-testid="settings-triggers-form-signal-pipeline"
  id="settingstriggersview-source-pipeline"
  class="input-base"
  :options="pipelines.map(p => ({ value: p.id, label: p.name }))"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
          </div>
          <div>
            <label for="settingstriggersview-field-2" class="mb-1 block text-sm font-medium">{{ $t('views.SettingsTriggersView.source_node_id') }}</label>
            <input id="settingstriggersview-field-2"
              v-model="form.signal_source_node"
              type="text"
              class="input-base font-mono"
              :placeholder="$t('views.SettingsTriggersView.source_node_id_placeholder')"
              data-testid="settings-triggers-form-signal-node"
            />
          </div>
        </template>

        <!-- Ongoing config -->
        <template v-if="form.trigger_type === 'ongoing'">
          <p class="text-sm text-muted-foreground">{{ $t('views.SettingsTriggersView.ongoing_description') }}</p>
          <div>
            <label for="settingstriggersview-ongoing-target" class="mb-1 block text-sm font-medium">{{ $t('views.SettingsTriggersView.target_runs') }}</label>
            <input id="settingstriggersview-ongoing-target"
              v-model.number="form.max_concurrent_runs"
              type="number"
              min="1"
              max="20"
              class="input-base"
              :placeholder="$t('views.SettingsTriggersView.target_runs_placeholder')"
              data-testid="settings-triggers-form-ongoing-target"
            />
          </div>
          <div>
            <label for="settingstriggersview-ongoing-interval" class="mb-1 block text-sm font-medium">{{ $t('views.SettingsTriggersView.scan_interval') }}</label>
            <input id="settingstriggersview-ongoing-interval"
              v-model.number="form.ongoing_scan_interval"
              type="number"
              min="60"
              class="input-base"
              :placeholder="$t('views.SettingsTriggersView.scan_interval_placeholder')"
              data-testid="settings-triggers-form-ongoing-interval"
            />
          </div>
          <div>
            <label for="settingstriggersview-ongoing-template" class="mb-1 block text-sm font-medium">{{ $t('views.SettingsTriggersView.input_template_json') }}</label>
            <textarea id="settingstriggersview-ongoing-template"
              v-model="form.input_template"
              rows="3"
              class="input-base font-mono"
              :placeholder="$t('views.SettingsTriggersView.input_template_placeholder')"
              data-testid="settings-triggers-form-ongoing-template"
            />
          </div>
          <div>
            <label for="settingstriggersview-ongoing-spend" class="mb-1 block text-sm font-medium">{{ $t('views.SettingsTriggersView.daily_spend_limit') }} <span class="text-destructive">*</span></label>
            <input id="settingstriggersview-ongoing-spend"
              v-model="form.daily_spend_limit"
              type="number"
              min="0.01"
              step="0.01"
              class="input-base"
              :placeholder="$t('views.SettingsTriggersView.daily_spend_limit_placeholder')"
              data-testid="settings-triggers-form-ongoing-spend"
            />
            <p class="mt-1 text-xs text-muted-foreground">{{ $t('views.SettingsTriggersView.daily_spend_limit_required') }}</p>
          </div>
          <div>
            <label for="settingstriggersview-ongoing-snapshot" class="mb-1 block text-sm font-medium">{{ $t('views.SettingsTriggersView.snapshot_id') }}</label>
            <input id="settingstriggersview-ongoing-snapshot"
              v-model="form.snapshot_id"
              type="text"
              class="input-base font-mono"
              :placeholder="$t('views.SettingsTriggersView.snapshot_id_placeholder')"
              data-testid="settings-triggers-form-ongoing-snapshot"
            />
          </div>
        </template>

        <div class="flex items-center gap-2">
          <label for="settingstriggersview-field-1" class="flex items-center gap-2 text-sm">
            <input id="settingstriggersview-field-1"
              v-model="form.active"
              type="checkbox"
              class="rounded border-input"
              data-testid="settings-triggers-form-active"
            />
            {{ $t('views.SettingsTriggersView.active') }}
          </label>
        </div>

        <div v-if="formError" class="text-sm text-destructive">{{ formError }}</div>
      </form>
    </FormDialog>

    <FormDialog
      :open="deleteDialogOpen"
      @update:open="deleteDialogOpen = false"
      :title="$t('views.SettingsTriggersView.delete_trigger')"
      :description="$t('views.SettingsTriggersView.delete_trigger_description')"
      :confirmText="$t('views.SettingsTriggersView.delete')"
      :loading="deleting"
      @confirm="deleteTrigger"
    >
      <p
        v-if="deleteError"
        data-testid="settings-triggers-delete-error"
        role="alert"
        class="text-sm font-medium text-destructive"
      >
        {{ deleteError }}
      </p>
    </FormDialog>
  </div>
  </FeatureGate>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { useDataFetch } from '../composables/useDataFetch'
import { useApi } from '../composables/useApi'
import Button from 'primevue/button'
import { api, getAccessToken } from '../lib/api/client'
import { decodeJwtPayload } from '../lib/jwt'
import { formatApiError } from '../lib/api/formatError'
import type { components } from '../lib/api/client'
import PageHeader from '../components/shared/PageHeader.vue'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import FormDialog from '../components/shared/FormDialog.vue'
import TableActions from '../components/shared/TableActions.vue'
import { usePlanStore } from '../stores/planStore'
import FeatureGate from '../components/FeatureGate.vue'
import { shortId } from '../utils/format'
import Select from 'primevue/select'

const planStore = usePlanStore()
const { t } = useI18n()

interface JwtPayload {
  org_role?: string
  org_id?: string
}

function readJwtPayload(): JwtPayload | null {
  // FIX 6: the shared decoder (handles padded + unpadded base64url).
  return decodeJwtPayload(getAccessToken()) as JwtPayload | null
}

const isOrgAdmin = computed(() => readJwtPayload()?.org_role === 'admin')
// FAR-191: the re-enable action is operator-or-above (backend trigger.update
// resolves to operator). Admins are operators too (viewer < runner < operator
// < admin), so both are granted.
const isOrgOperator = computed(() => {
  const role = readJwtPayload()?.org_role
  return role === 'operator' || role === 'admin'
})
const orgId = computed(() => readJwtPayload()?.org_id ?? '')

// Org-wide "pause all triggers" kill-switch (admin-managed). The org's paused
// state is read from the triggers-list GET top-level fields; the toggle PUTs to
// the admin endpoint and updates local state from the response.
const orgTriggersPaused = computed<boolean>(() => Boolean((triggersData.value as { triggers_paused?: boolean } | null)?.triggers_paused ?? false))
const orgPausedAt = computed<string | null>(() => (triggersData.value as { paused_at?: string | null } | null)?.paused_at ?? null)

const pauseToggling = ref(false)
const pauseError = ref<string | null>(null)

async function togglePauseAll() {
  if (pauseToggling.value || !orgId.value) return
  pauseToggling.value = true
  pauseError.value = null
  try {
    const { put } = useApi()
    const res = await put<{ paused: boolean; paused_at: string | null }>(
      `/api/v1/admin/orgs/${orgId.value}/triggers/pause`,
      { paused: !orgTriggersPaused.value },
    )
    if (res && typeof res.paused === 'boolean') {
      triggersData.value = {
        ...triggersData.value,
        triggers_paused: res.paused,
        paused_at: res.paused_at ?? null,
      } as typeof triggersData.value
    } else {
      await loadTriggers()
    }
  } catch (e: unknown) {
    pauseError.value = t('views.SettingsTriggersView.failed_to_update_pause', { detail: formatApiError(e) })
  } finally {
    pauseToggling.value = false
  }
}

interface StreakOutcome {
  run_id?: string | null
  classification?: string | null
  reason?: string | null
  completed_at?: string | null
}

interface StreakStatus {
  enabled: boolean
  // FIX 5: the backend now ALWAYS emits the uniform 6-key shape (streak/
  // threshold default to 0 for non-ongoing), but they stay optional here so a
  // cached/stale shape can never crash a consumer with an undefined hazard.
  streak?: number
  threshold?: number
  state: 'ok' | 'deactivated' | 'unconfigured'
  deactivated_reason?: 'no_delivery_streak' | 'config_failure' | null
  last_outcomes?: StreakOutcome[]
}

interface TriggerItem {
  id: string
  pipeline_id: string
  trigger_type: string
  active: boolean
  max_concurrent_runs?: number
  daily_spend_limit?: number | null
  config_json: Record<string, unknown>
  cron_expression?: string | null
  cron_timezone?: string | null
  last_fired_at?: string | null
  next_fire_at?: string | null
  streak_status?: StreakStatus
}
type PipelineItem = components['schemas']['PipelineResponse']

interface TriggerForm {
  pipeline_id: string
  trigger_type: string
  active: boolean
  webhook_url: string
  webhook_method: string
  webhook_headers: string
  cron_expression: string
  cron_timezone: string
  input_template: string
  connector_instance_id: string
  poll_query: string
  poll_interval: number
  condition_expression: string
  signal_source_pipeline: string
  signal_source_node: string
  max_concurrent_runs: number
  daily_spend_limit: string
  ongoing_scan_interval: number
  snapshot_id: string
}

const { error, data: triggersData, load: loadTriggers, fetched: triggersLoaded } = useDataFetch(
  () => api.GET('/api/v1/triggers', { params: { query: { page: 1, page_size: 100 } } }),
)
const { data: pipelinesData, load: loadPipelines, fetched: pipelinesLoaded } = useDataFetch(
  () => api.GET('/api/v1/pipelines', {}),
  { immediate: false }
)

const loaded = computed(() => triggersLoaded.value && pipelinesLoaded.value)
const items = computed<TriggerItem[]>(() =>
  ((triggersData.value as { items?: TriggerItem[] } | null)?.items ?? []),
)
const pipelines = computed<PipelineItem[]>(() =>
  ((pipelinesData.value as { items?: PipelineItem[] } | null)?.items ?? []),
)

const dialogOpen = ref(false)
const deleteDialogOpen = ref(false)
const editingId = ref<string | null>(null)
const editingType = ref('')
const saving = ref(false)
const deleting = ref(false)
const formError = ref<string | null>(null)
const deleteTarget = ref<TriggerItem | null>(null)
const expandedOutcomes = ref<Set<string>>(new Set())
// FIX 4: per-row action error state — a failed toggle/re-enable/delete must
// surface INLINE near the row, never clobber the whole list via the page-level
// `error` ref (which is reserved for page-load failures).
const actionErrors = ref<Record<string, string>>({})
const deleteError = ref<string | null>(null)

const defaultForm: TriggerForm = {
  pipeline_id: '',
  trigger_type: '',
  active: true,
  webhook_url: '',
  webhook_method: 'POST',
  webhook_headers: '',
  cron_expression: '',
  cron_timezone: 'UTC',
  input_template: '',
  connector_instance_id: '',
  poll_query: '',
  poll_interval: 60,
  condition_expression: '',
  signal_source_pipeline: '',
  signal_source_node: '',
  max_concurrent_runs: 1,
  daily_spend_limit: '',
  ongoing_scan_interval: 60,
  snapshot_id: '',
}

const form = ref<TriggerForm>({ ...defaultForm })

function typeLabel(type: string): string {
  const labels: Record<string, string> = {
    manual: t('views.SettingsTriggersView.manual'),
    webhook: t('views.SettingsTriggersView.webhook'),
    cron: t('views.SettingsTriggersView.cron'),
    polling: t('views.SettingsTriggersView.polling'),
    agent_signal: t('views.SettingsTriggersView.agent_signal'),
    ongoing: t('views.SettingsTriggersView.ongoing'),
  }
  return labels[type] || type
}

function typeBadgeClass(type: string): string {
  const classes: Record<string, string> = {
    manual: 'badge badge-context-blue',
    webhook: 'badge badge-context-purple',
    cron: 'badge badge-context-amber',
    polling: 'badge badge-context-cyan',
    agent_signal: 'badge badge-context-indigo',
    ongoing: 'badge badge-context-emerald',
  }
  return classes[type] || 'badge badge-context-slate'
}

function formatTimestamp(ts: string | null): string {
  if (!ts) return '\u2014'
  const d = new Date(ts)
  return d.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function pipelineName(id: string): string {
  const p = pipelines.value.find(p => p.id === id)
  return p ? p.name : shortId(id)
}

function isDeactivatedOngoing(trigger: TriggerItem): boolean {
  return trigger.trigger_type === 'ongoing' && trigger.streak_status?.state === 'deactivated'
}

function streakBadgeClass(trigger: TriggerItem): string {
  const s = trigger.streak_status
  if (!s) return 'bg-muted text-muted-foreground'
  const streak = s.streak ?? 0
  const threshold = s.threshold ?? 0
  // Approaching deactivation. Guard with streak > 0 so a threshold=1 fresh
  // trigger (0/1) is never permanently amber; a streak AT/OVER the threshold
  // gets the red tier (a deactivation is eligible/imminent).
  if (s.enabled && threshold > 0 && streak > 0 && streak >= threshold - 1) {
    if (streak >= threshold) return 'bg-destructive/10 text-destructive'
    return 'bg-amber-500/10 text-amber-600'
  }
  return 'bg-muted text-muted-foreground'
}

function deactivatedReasonLabel(trigger: TriggerItem): string {
  const reason = trigger.streak_status?.deactivated_reason
  if (reason === 'config_failure') return t('views.SettingsTriggersView.deactivated_reason_config_failure')
  return t('views.SettingsTriggersView.deactivated_reason_no_delivery')
}

function hasOutcomes(trigger: TriggerItem): boolean {
  return Boolean(trigger.streak_status?.last_outcomes?.length)
}

function toggleOutcomes(id: string): void {
  const next = new Set(expandedOutcomes.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  expandedOutcomes.value = next
}

function outcomeBadgeClass(classification: string | null | undefined): string {
  if (classification === 'delivered') return 'bg-success/10 text-success'
  if (classification === 'no_delivery') return 'bg-amber-500/10 text-amber-600'
  return 'bg-muted text-muted-foreground'
}

function outcomeLabel(classification: string | null | undefined): string {
  const labels: Record<string, string> = {
    delivered: t('views.SettingsTriggersView.outcome_delivered'),
    no_delivery: t('views.SettingsTriggersView.outcome_no_delivery'),
    excluded: t('views.SettingsTriggersView.outcome_excluded'),
    unclassified: t('views.SettingsTriggersView.outcome_unclassified'),
  }
  return classification ? labels[classification] || classification : '\u2014'
}

function resetForm() {
  form.value = { ...defaultForm }
  formError.value = null
}

function openCreateDialog() {
  editingId.value = null
  editingType.value = ''
  resetForm()
  dialogOpen.value = true
}

function openEditDialog(trigger: TriggerItem) {
  editingId.value = trigger.id
  editingType.value = trigger.trigger_type

  const cfg = trigger.config_json || {}

  form.value = {
    pipeline_id: trigger.pipeline_id,
    trigger_type: trigger.trigger_type,
    active: trigger.active,
    webhook_url: (cfg as any).url || '',
    webhook_method: (cfg as any).method || 'POST',
    webhook_headers: (cfg as any).headers ? JSON.stringify((cfg as any).headers, null, 2) : '',
    cron_expression: trigger.cron_expression || '',
    cron_timezone: trigger.cron_timezone || 'UTC',
    input_template: (cfg as any).input_template ? JSON.stringify((cfg as any).input_template, null, 2) : '',
    connector_instance_id: (cfg as any).connector_instance_id || '',
    poll_query: (cfg as any).poll_query || '',
    poll_interval: (cfg as any).poll_interval_seconds || 60,
    condition_expression: (cfg as any).condition_expression || '',
    signal_source_pipeline: (cfg as any).source_pipeline_id || '',
    signal_source_node: (cfg as any).source_node_id || '',
    max_concurrent_runs: trigger.max_concurrent_runs ?? 1,
    daily_spend_limit: trigger.daily_spend_limit != null ? String(trigger.daily_spend_limit) : '',
    ongoing_scan_interval: (cfg as any).scan_interval_seconds || 60,
    snapshot_id: (cfg as any).snapshot_id ? String((cfg as any).snapshot_id) : '',
  }
  dialogOpen.value = true
}

function confirmDelete(trigger: TriggerItem) {
  deleteTarget.value = trigger
  deleteDialogOpen.value = true
}

async function saveTrigger() {
  formError.value = null

  if (!form.value.pipeline_id) {
    formError.value = t('views.SettingsTriggersView.please_select_a_pipeline')
    return
  }

  if (!editingId.value && !form.value.trigger_type) {
    formError.value = t('views.SettingsTriggersView.please_select_a_trigger_type')
    return
  }

  const triggerType = editingId.value ? editingType.value : form.value.trigger_type

  if (triggerType === 'ongoing') {
    // FAR-158: the ongoing spend limit is REQUIRED (backend rejects None).
    if (!form.value.daily_spend_limit || Number(form.value.daily_spend_limit) <= 0) {
      formError.value = t('views.SettingsTriggersView.daily_spend_limit_required')
      return
    }
    if (!form.value.max_concurrent_runs || form.value.max_concurrent_runs < 1 || form.value.max_concurrent_runs > 20) {
      formError.value = t('views.SettingsTriggersView.target_runs_invalid')
      return
    }
  }

  try {
    const configJson: Record<string, unknown> = {}

    if (triggerType === 'webhook') {
      if (form.value.webhook_url) configJson.url = form.value.webhook_url
      if (form.value.webhook_method) configJson.method = form.value.webhook_method
      if (form.value.webhook_headers) {
        try {
          configJson.headers = JSON.parse(form.value.webhook_headers)
        } catch {
          formError.value = t('views.SettingsTriggersView.headers_must_be_valid_json')
          return
        }
      }
    }

    if (triggerType === 'polling') {
      if (!form.value.poll_interval || form.value.poll_interval < 60) {
        formError.value = t('views.SettingsTriggersView.poll_interval_seconds_too_low')
        return
      }
      if (form.value.connector_instance_id) configJson.connector_instance_id = form.value.connector_instance_id
      if (form.value.poll_query) configJson.poll_query = form.value.poll_query
      configJson.poll_interval_seconds = form.value.poll_interval
      if (form.value.condition_expression) configJson.condition_expression = form.value.condition_expression
    }

    if (triggerType === 'cron' && form.value.input_template) {
      try {
        configJson.input_template = JSON.parse(form.value.input_template)
      } catch {
        formError.value = t('views.SettingsTriggersView.input_template_must_be_valid_json')
        return
      }
    }

    if (triggerType === 'agent_signal') {
      if (form.value.signal_source_pipeline) configJson.source_pipeline_id = form.value.signal_source_pipeline
      if (form.value.signal_source_node) configJson.source_node_id = form.value.signal_source_node
    }

    if (triggerType === 'ongoing') {
      configJson.scan_interval_seconds = form.value.ongoing_scan_interval || 60
      if (form.value.input_template) {
        try {
          configJson.input_template = JSON.parse(form.value.input_template)
        } catch {
          formError.value = t('views.SettingsTriggersView.input_template_must_be_valid_json')
          return
        }
      }
      if (form.value.snapshot_id) configJson.snapshot_id = form.value.snapshot_id
    }

    saving.value = true

    if (editingId.value) {
      const body: Record<string, unknown> = {
        active: form.value.active,
        config_json: Object.keys(configJson).length > 0 ? configJson : undefined,
        // max_concurrent_runs was previously never sent on PUT — send it now
        // so an edited target persists (FAR-158).
        max_concurrent_runs: form.value.max_concurrent_runs,
      }
      if (triggerType === 'ongoing') {
        body.daily_spend_limit = form.value.daily_spend_limit
      }
      if (triggerType === 'cron') {
        if (form.value.cron_expression) body.cron_expression = form.value.cron_expression
        body.cron_timezone = form.value.cron_timezone || 'UTC'
      }
      const { error: err } = await api.PUT('/api/v1/triggers/{trigger_id}', {
        params: { path: { trigger_id: editingId.value } },
        body: body as any,
      })
      if (err) {
        formError.value = t('views.SettingsTriggersView.failed_to_update_trigger', { detail: formatApiError(err) })
        return
      }
    } else {
      const body: Record<string, unknown> = {
        trigger_type: triggerType,
        active: form.value.active,
        config_json: configJson,
      }
      if (triggerType === 'ongoing') {
        body.max_concurrent_runs = form.value.max_concurrent_runs
        body.daily_spend_limit = form.value.daily_spend_limit
      }
      if (triggerType === 'cron') {
        if (form.value.cron_expression) body.cron_expression = form.value.cron_expression
        body.cron_timezone = form.value.cron_timezone || 'UTC'
      }
      const { error: err } = await api.POST('/api/v1/pipelines/{pipeline_id}/triggers', {
        params: { path: { pipeline_id: form.value.pipeline_id } },
        body: body as any,
      })
      if (err) {
        formError.value = t('views.SettingsTriggersView.failed_to_create_trigger', { detail: formatApiError(err) })
        return
      }
    }

    dialogOpen.value = false
    await loadTriggers()
  } catch (e: unknown) {
    formError.value = t('views.SettingsTriggersView.error_saving_trigger', { detail: formatApiError(e) })
  } finally {
    saving.value = false
  }
}

async function deleteTrigger() {
  if (!deleteTarget.value) return
  deleting.value = true
  deleteError.value = null
  try {
    const { error: err } = await api.DELETE('/api/v1/triggers/{trigger_id}', {
      params: { path: { trigger_id: deleteTarget.value.id } },
    })
    if (err) {
      deleteError.value = t('views.SettingsTriggersView.failed_to_delete_trigger', { detail: formatApiError(err) })
      return
    }
    deleteDialogOpen.value = false
    deleteTarget.value = null
    await loadTriggers()
  } catch (e: unknown) {
    deleteError.value = t('views.SettingsTriggersView.error_deleting_trigger', { detail: formatApiError(e) })
  } finally {
    deleting.value = false
  }
}

const triggerToggling = ref<Record<string, boolean>>({})

async function toggleActive(trigger: TriggerItem) {
  // Re-entry guard: a double-click must not fire two toggles.
  if (triggerToggling.value[trigger.id]) return
  triggerToggling.value[trigger.id] = true
  delete actionErrors.value[trigger.id]
  try {
    const { error: err } = await api.POST('/api/v1/triggers/{trigger_id}/toggle', {
      params: { path: { trigger_id: trigger.id } },
    })
    if (err) {
      actionErrors.value[trigger.id] = t('views.SettingsTriggersView.failed_to_toggle_trigger', { detail: formatApiError(err) })
      return
    }
    await loadTriggers()
  } catch (e: unknown) {
    actionErrors.value[trigger.id] = t('views.SettingsTriggersView.error_toggling_trigger', { detail: formatApiError(e) })
  } finally {
    triggerToggling.value[trigger.id] = false
  }
}

async function loadAll() {
  await Promise.all([loadTriggers(), loadPipelines()])
}

function triggerActions(trigger: TriggerItem) {
  return [
    {
      key: 'edit',
      label: t('views.SettingsTriggersView.edit'),
      onClick: () => openEditDialog(trigger),
    },
    {
      key: 'delete',
      label: t('views.SettingsTriggersView.delete'),
      onClick: () => confirmDelete(trigger),
      danger: true,
    },
  ]
}

onMounted(() => { planStore.fetchPlan(); loadAll() })

</script>
