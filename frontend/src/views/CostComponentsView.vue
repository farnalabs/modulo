<template>
  <PageTabs :tabs="[
    { label: 'Overview', to: '/admin/costs' },
    { label: 'Spend Limits', to: '/admin/costs/limits' },
    { label: 'Cost Components', to: '/admin/costs/components' },
    { label: 'Cost Controls', to: '/admin/costs/controls' },
  ]" />
  <div data-theme="agent" class="page-wide">
    <div class="flex items-center justify-between">
      <PageHeader :title="$t('views.CostComponentsView.cost_components')" :subtitle="$t('views.CostComponentsView.configure_the_named_cost_components_that_make_up_a_runs_cost')" />
      <Button
        variant="default"
        class="border-primary/30 hover:border-primary/60"
        data-testid="cost-components-add"
        @click="openCreate"
      >
        {{ $t('views.CostComponentsView.add_component') }}
      </Button>
    </div>

    <FeatureGate feature-name="admin_cost_breakdown" required-tier="team" show-disabled>

      <LoadingSpinner v-if="loading" />

      <ErrorAlert v-else-if="error" :message="error" :on-retry="loadComponents" />

      <template v-else>
        <EmptyState
          v-if="components.length === 0"
          :title="$t('views.CostComponentsView.no_cost_components_configured')"
          :description="$t('views.CostComponentsView.add_a_component_to_break_down_run_costs')"
        />

        <Card v-else>
          <CardContent class="p-0">
            <DataTable
              :columns="[
                { key: 'display_name', label: $t('views.CostComponentsView.display_name') },
                { key: 'kind', label: $t('views.CostComponentsView.kind') },
                { key: 'rate', label: $t('views.CostComponentsView.rate_usd'), numeric: true },
                { key: 'formula', label: $t('views.CostComponentsView.formula') },
                { key: 'report_key', label: $t('views.CostComponentsView.report_key') },
                { key: 'enabled', label: $t('views.CostComponentsView.enabled') },
                { key: 'actions', label: '' },
              ]"
              :rows="tableRows"
              :row-clickable="false"
            >
              <template #cell-display_name="{ row }">
                <div class="flex items-center gap-2">
                  <span class="font-medium">{{ (row as any).display_name }}</span>
                  <span class="font-mono text-xs text-muted-foreground">{{ (row as any).name }}</span>
                </div>
              </template>
              <template #cell-kind="{ row }">
                <span
                  class="inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium capitalize"
                  :class="(row as any).kind === 'self_reported' ? 'bg-primary/10 text-primary' : 'bg-muted text-muted-foreground'"
                >
                  {{ $t(`views.CostComponentsView.${(row as any).kind}`) }}
                </span>
              </template>
              <template #cell-rate="{ row }">
                <span class="tabular-nums">{{ (row as any).rateDisplay }}</span>
                <span v-if="(row as any).rate_fallback" class="ml-1 font-mono text-xs text-muted-foreground">({{ (row as any).rate_fallback }})</span>
              </template>
              <template #cell-formula="{ row }">
                <code class="max-w-64 truncate rounded bg-muted px-1.5 py-0.5 font-mono text-xs">{{ (row as any).formula || '—' }}</code>
              </template>
              <template #cell-report_key="{ row }">
                <code class="rounded bg-muted px-1.5 py-0.5 font-mono text-xs">{{ (row as any).report_key || '—' }}</code>
              </template>
              <template #cell-enabled="{ row }">
                <label class="relative inline-flex cursor-pointer items-center" :data-testid="'cost-components-toggle-' + (row as any).id">
                  <input
                    type="checkbox"
                    class="peer sr-only"
                    :aria-label="$t('views.CostComponentsView.enabled') + ': ' + (row as any).display_name"
                    :checked="(row as any).enabled"
                    @change="toggleEnabled(row as any)"
                  />
                  <div class="peer h-6 w-11 rounded-full bg-muted-foreground/30 after:absolute after:start-[2px] after:top-[2px] after:h-5 after:w-5 after:rounded-full after:border after:border-muted after:bg-background after:transition-all peer-checked:bg-primary peer-checked:after:translate-x-full" />
                </label>
              </template>
              <template #cell-actions="{ row }">
                <div class="text-right">
                  <TableActions :actions="rowActions(row as any)" />
                </div>
              </template>
            </DataTable>
          </CardContent>
        </Card>

        <p v-if="formError" class="mt-3 text-sm text-destructive" data-testid="cost-components-form-error">{{ formError }}</p>
        <p v-if="formSuccess" class="mt-3 text-sm text-success" data-testid="cost-components-form-success">{{ formSuccess }}</p>

        <FormDialog
          v-model:open="dialogOpen"
          :title="editing ? $t('views.CostComponentsView.edit_component') : $t('views.CostComponentsView.create_component')"
          :confirm-text="editing ? $t('views.CostComponentsView.save') : $t('views.CostComponentsView.create')"
          :loading="saving"
          :confirm-disabled="saving"
          @confirm="saveComponent"
        >
          <form class="space-y-4" @submit.prevent="saveComponent">
            <div class="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <div>
                <label for="costcomponents-name" class="mb-1.5 block text-xs font-medium text-muted-foreground">{{ $t('views.CostComponentsView.name') }}</label>
                <Input id="costcomponents-name" v-model="form.name" data-testid="cost-components-name" class="font-mono" placeholder="e.g. sandbox_infra" :disabled="editing" />
              </div>
              <div>
                <label for="costcomponents-display-name" class="mb-1.5 block text-xs font-medium text-muted-foreground">{{ $t('views.CostComponentsView.display_name') }}</label>
                <Input id="costcomponents-display-name" v-model="form.display_name" data-testid="cost-components-display-name" placeholder="Sandbox Infra" />
              </div>
            </div>

            <div>
              <span class="mb-1.5 block text-xs font-medium text-muted-foreground">{{ $t('views.CostComponentsView.kind') }}</span>
              <div class="flex gap-4">
                <label class="flex items-center gap-2 text-sm" data-testid="cost-components-kind-calculated">
                  <input type="radio" value="calculated" v-model="form.kind" class="h-4 w-4 rounded-full border-muted-foreground" />
                  {{ $t('views.CostComponentsView.calculated') }}
                </label>
                <label class="flex items-center gap-2 text-sm" data-testid="cost-components-kind-self-reported">
                  <input type="radio" value="self_reported" v-model="form.kind" class="h-4 w-4 rounded-full border-muted-foreground" />
                  {{ $t('views.CostComponentsView.self_reported') }}
                </label>
              </div>
              <p class="mt-1 text-xs text-muted-foreground">{{ kindHint }}</p>
            </div>

            <div class="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <div>
                <label for="costcomponents-rate" class="mb-1.5 block text-xs font-medium text-muted-foreground">{{ $t('views.CostComponentsView.rate_usd') }}</label>
                <Input
                  id="costcomponents-rate"
                  v-model="form.rate_usd"
                  data-testid="cost-components-rate"
                  type="number"
                  min="0"
                  step="0.000001"
                  :placeholder="form.kind === 'calculated' ? $t('views.CostComponentsView.none_uses_env_fallback') : '0.00'"
                />
              </div>
              <div v-if="showRateFallback">
                <label for="costcomponents-fallback" class="mb-1.5 block text-xs font-medium text-muted-foreground">{{ $t('views.CostComponentsView.env_fallback') }}</label>
                <Select :aria-label="$t('views.CostComponentsView.env_fallback')" :model-value="form.rate_fallback || undefined" @update:model-value="onFallbackChange">
                  <SelectTrigger id="costcomponents-fallback" data-testid="cost-components-fallback" class="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm">
                    <SelectValue :placeholder="$t('views.CostComponentsView.select_fallback')" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem v-for="fb in REGISTERED_FALLBACKS" :key="fb" :value="fb">{{ fb }}</SelectItem>
                  </SelectContent>
                </Select>
                <p class="mt-1 text-xs text-muted-foreground">{{ $t('views.CostComponentsView.fallback_resolves_rate_from_settings') }}</p>
              </div>
            </div>

            <div v-if="form.kind === 'calculated'">
              <label for="costcomponents-formula" class="mb-1.5 block text-xs font-medium text-muted-foreground">{{ $t('views.CostComponentsView.formula') }}</label>
              <Input id="costcomponents-formula" v-model="form.formula" data-testid="cost-components-formula" class="font-mono" :placeholder="'wall_clock_hours * rate'" />
              <details class="mt-2 rounded-lg border bg-muted/30 p-3">
                <summary class="cursor-pointer text-xs font-medium text-muted-foreground">{{ $t('views.CostComponentsView.available_params') }}</summary>
                <table class="mt-2 w-full text-xs">
                  <thead>
                    <tr class="border-b text-left text-muted-foreground">
                      <th class="py-1 pr-3 font-medium">{{ $t('views.CostComponentsView.param') }}</th>
                      <th class="py-1 pr-3 font-medium">{{ $t('views.CostComponentsView.type') }}</th>
                      <th class="py-1 font-medium">{{ $t('views.CostComponentsView.meaning') }}</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="p in availableParams" :key="p.name" class="border-b last:border-b-0">
                      <td class="py-1 pr-3 font-mono text-primary">{{ p.name }}</td>
                      <td class="py-1 pr-3 text-muted-foreground">{{ p.type }}</td>
                      <td class="py-1 text-muted-foreground">{{ p.meaning }}</td>
                    </tr>
                  </tbody>
                </table>
              </details>
            </div>

            <div v-if="form.kind === 'self_reported'">
              <label for="costcomponents-report-key" class="mb-1.5 block text-xs font-medium text-muted-foreground">{{ $t('views.CostComponentsView.report_key') }}</label>
              <Input id="costcomponents-report-key" v-model="form.report_key" data-testid="cost-components-report-key" class="font-mono" placeholder="model_cost_usd" />
              <p class="mt-1 text-xs text-muted-foreground">{{ $t('views.CostComponentsView.a_dead_report_key_wastes_a_cap_slot') }}</p>
            </div>

            <label class="flex items-center gap-2 text-sm" data-testid="cost-components-enabled">
              <input type="checkbox" v-model="form.enabled" class="h-4 w-4 rounded border-muted-foreground" />
              {{ $t('views.CostComponentsView.enabled') }}
            </label>
          </form>
        </FormDialog>

        <div v-if="deleteTarget" class="rounded-lg border p-4" :class="deleteTarget.kind === 'self_reported' ? 'border-warning/50 bg-warning/5' : 'border-destructive/50 bg-destructive/10'">
          <p class="text-sm font-medium" :class="deleteTarget.kind === 'self_reported' ? 'text-warning' : 'text-destructive'">
            {{ $t('views.CostComponentsView.delete_component', { name: deleteTarget.display_name }) }}
          </p>
          <p class="mt-1 text-sm" :class="deleteTarget.kind === 'self_reported' ? 'text-warning/80' : 'text-destructive/80'">
            {{ deleteTarget.kind === 'self_reported' ? $t('views.CostComponentsView.delete_self_reported_warning') : $t('views.CostComponentsView.this_action_cannot_be_undone') }}
          </p>
          <div class="mt-3 flex items-center gap-2">
            <Button :disabled="deleting" variant="destructive" data-testid="cost-components-delete-confirm" @click="confirmDelete">
              {{ deleting ? $t('views.CostComponentsView.deleting') : $t('views.CostComponentsView.delete') }}
            </Button>
            <Button variant="outline" data-testid="cost-components-delete-cancel" @click="deleteTarget = null">
              {{ $t('views.CostComponentsView.cancel') }}
            </Button>
          </div>
        </div>
      </template>
    </FeatureGate>
  </div>
</template>

<script setup lang="ts">
import PageHeader from '../components/shared/PageHeader.vue'
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { api } from '../lib/api/client'
import { useDataFetch } from '../composables/useDataFetch'
import { formatApiError } from '../lib/api/formatError'
import { usePlanStore } from '../stores/planStore'
import FeatureGate from '../components/FeatureGate.vue'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import EmptyState from '../components/shared/EmptyState.vue'
import FormDialog from '../components/shared/FormDialog.vue'
import TableActions from '../components/shared/TableActions.vue'
import { Button } from '../components/ui/button'
import { Card, CardContent } from '../components/ui/card'
import { Input } from '../components/ui/input'
import { DataTable } from '../components/ui/data-table'
import { Select, SelectTrigger, SelectContent, SelectItem, SelectValue } from '../components/ui/select'
import PageTabs from '../components/PageTabs.vue'
import { formatMoney } from '../lib/money'
import { useOrgCurrency } from '../composables/useOrgCurrency'

const planStore = usePlanStore()

const { currencyCode, loadCurrency } = useOrgCurrency()

// REGISTERED_RATE_FALLBACKS (backend modulo.core.cost_controller.breakdown.params)
const REGISTERED_FALLBACKS = ['e2b_rate']

const AVAILABLE_PARAMS = [
  { name: 'rate', type: 'Decimal', meaningKey: 'param_rate' },
  { name: 'e2b_rate', type: 'Decimal', meaningKey: 'param_e2b_rate' },
  { name: 'input_token_rate', type: 'Decimal', meaningKey: 'param_input_token_rate' },
  { name: 'output_token_rate', type: 'Decimal', meaningKey: 'param_output_token_rate' },
  { name: 'wall_clock_hours', type: 'Decimal', meaningKey: 'param_wall_clock_hours' },
  { name: 'tokens_input', type: 'int', meaningKey: 'param_tokens_input' },
  { name: 'tokens_output', type: 'int', meaningKey: 'param_tokens_output' },
  { name: 'tokens_estimated', type: 'int', meaningKey: 'param_tokens_estimated' },
  { name: 'node_count', type: 'int', meaningKey: 'param_node_count' },
  { name: 'nodes_estimated', type: 'int', meaningKey: 'param_nodes_estimated' },
]

const { t } = useI18n()

interface CostComponent {
  id: string
  name: string
  display_name: string
  kind: 'calculated' | 'self_reported'
  rate_usd: string | null
  rate_fallback: string | null
  formula: string | null
  report_key: string | null
  enabled: boolean
  sort_order: number
  deleted_at: string | null
}

interface ComponentForm {
  name: string
  display_name: string
  kind: 'calculated' | 'self_reported'
  rate_usd: string
  rate_fallback: string | null
  formula: string
  report_key: string
  enabled: boolean
}

const { data, loading, error, load: loadComponents } = useDataFetch(
  () => (api as any).GET('/api/v1/admin/costs/components'),
  { initialValue: [] as CostComponent[] },
)

const components = computed(() => {
  const d = data.value
  return Array.isArray(d) ? (d as CostComponent[]) : []
})

const tableRows = computed(() =>
  components.value.map((c) => ({
    ...c,
    rateDisplay: c.rate_usd != null ? formatMoney(Number(c.rate_usd), currencyCode.value, 6) : '—',
  })),
)

const dialogOpen = ref(false)
const editing = ref(false)
const editingId = ref<string | null>(null)
const saving = ref(false)
const deleting = ref(false)
const deleteTarget = ref<CostComponent | null>(null)
const formError = ref<string | null>(null)
const formSuccess = ref<string | null>(null)

const form = ref<ComponentForm>(emptyForm())

function emptyForm(): ComponentForm {
  return {
    name: '',
    display_name: '',
    kind: 'calculated',
    rate_usd: '',
    rate_fallback: null,
    formula: '',
    report_key: '',
    enabled: true,
  }
}

function openCreate() {
  editing.value = false
  editingId.value = null
  form.value = emptyForm()
  formError.value = null
  formSuccess.value = null
  dialogOpen.value = true
}

function openEdit(component: CostComponent) {
  editing.value = true
  editingId.value = component.id
  form.value = {
    name: component.name,
    display_name: component.display_name,
    kind: component.kind,
    rate_usd: component.rate_usd != null ? String(component.rate_usd) : '',
    rate_fallback: component.rate_fallback,
    formula: component.formula ?? '',
    report_key: component.report_key ?? '',
    enabled: component.enabled,
  }
  formError.value = null
  formSuccess.value = null
  dialogOpen.value = true
}

function onFallbackChange(value: unknown) {
  form.value.rate_fallback = value ? String(value) : null
}

const showRateFallback = computed(() => {
  if (form.value.kind !== 'calculated') return false
  const formula = form.value.formula
  const referencesRate = /(^|[^a-z0-9_])rate([^a-z0-9_]|$)/.test(formula)
  const rateEmpty = form.value.rate_usd == null || form.value.rate_usd === ''
  return referencesRate && rateEmpty
})

const kindHint = computed(() =>
  t(`views.CostComponentsView.kind_hint_${form.value.kind}`),
)

const availableParams = computed(() =>
  AVAILABLE_PARAMS.map((p) => ({ ...p, meaning: t(`views.CostComponentsView.${p.meaningKey}`) })),
)

async function saveComponent() {
  saving.value = true
  formError.value = null
  formSuccess.value = null
  const payload: Record<string, unknown> = {
    name: form.value.name,
    display_name: form.value.display_name,
    kind: form.value.kind,
    rate_usd: form.value.rate_usd === '' ? null : Number(form.value.rate_usd),
    rate_fallback: form.value.rate_fallback,
    formula: form.value.kind === 'calculated' ? form.value.formula : null,
    report_key: form.value.kind === 'self_reported' ? form.value.report_key : null,
    enabled: form.value.enabled,
  }
  try {
    let err: unknown
    if (editing.value && editingId.value) {
      const resp = await (api as any).PUT(`/api/v1/admin/costs/components/${editingId.value}`, { body: payload })
      err = resp.error
    } else {
      const resp = await (api as any).POST('/api/v1/admin/costs/components', { body: payload })
      err = resp.error
    }
    if (err) {
      formError.value = `Failed to save: ${formatApiError(err)}`
    } else {
      formSuccess.value = editing.value
        ? t('views.CostComponentsView.component_updated')
        : t('views.CostComponentsView.component_created')
      dialogOpen.value = false
      await loadComponents()
    }
  } catch (e: unknown) {
    formError.value = `Failed to save: ${formatApiError(e)}`
  } finally {
    saving.value = false
  }
}

async function toggleEnabled(component: CostComponent) {
  try {
    const { error: err } = await (api as any).PUT(`/api/v1/admin/costs/components/${component.id}`, {
      body: { enabled: !component.enabled },
    })
    if (err) {
      formError.value = `Failed to toggle: ${formatApiError(err)}`
    }
    await loadComponents()
  } catch (e: unknown) {
    formError.value = `Failed to toggle: ${formatApiError(e)}`
  }
}

function confirmDeleteRequest(component: CostComponent) {
  deleteTarget.value = component
  formError.value = null
}

async function confirmDelete() {
  if (!deleteTarget.value) return
  deleting.value = true
  formError.value = null
  try {
    const { error: err, response } = await (api as any).DELETE(`/api/v1/admin/costs/components/${deleteTarget.value.id}`)
    if (err) {
      formError.value = `Failed to delete: ${formatApiError(err)}`
    } else if (response.status === 204 || response.ok) {
      formSuccess.value = t('views.CostComponentsView.component_deleted')
      deleteTarget.value = null
      await loadComponents()
    }
  } catch (e: unknown) {
    formError.value = `Failed to delete: ${formatApiError(e)}`
  } finally {
    deleting.value = false
  }
}

function rowActions(component: CostComponent) {
  return [
    { key: 'edit', label: t('views.CostComponentsView.edit'), onClick: () => openEdit(component) },
    {
      key: 'delete',
      label: t('views.CostComponentsView.delete'),
      onClick: () => confirmDeleteRequest(component),
      danger: true,
    },
  ]
}

onMounted(() => {
  planStore.fetchPlan()
  loadComponents()
  loadCurrency()
})
</script>
