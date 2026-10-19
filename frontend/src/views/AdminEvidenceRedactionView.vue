<template>
  <div class="page-wide">
    <header class="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
      <PageHeader
        :title="$t('views.AdminEvidenceRedaction.title')"
        :subtitle="$t('views.AdminEvidenceRedaction.subtitle')"
      />
      <Button
        class="border-primary/30 hover:border-primary/60"
        data-testid="admin-evidence-redaction-add"
        @click="openCreate"
      >
        {{ $t('views.AdminEvidenceRedaction.add_rule') }}
      </Button>
    </header>

    <div
      class="mb-4 rounded-lg border bg-muted/40 p-3 text-sm text-muted-foreground"
      data-testid="admin-evidence-redaction-server-note"
    >
      {{ $t('views.AdminEvidenceRedaction.server_boundary_note') }}
    </div>
    <div
      class="mb-4 rounded-lg border border-warning/40 bg-warning/10 p-3 text-sm"
      data-testid="admin-evidence-redaction-fail-closed-note"
    >
      {{ $t('views.AdminEvidenceRedaction.fail_closed_note') }}
    </div>

    <div
      v-if="loading"
      aria-live="polite"
      class="flex items-center gap-3 text-sm text-muted-foreground"
      data-testid="admin-evidence-redaction-loading"
    >
      <LoadingSpinner />
      <span>{{ $t('views.AdminEvidenceRedaction.loading') }}</span>
    </div>

    <div v-else-if="loadError" aria-live="assertive" data-testid="admin-evidence-redaction-load-error">
      <ErrorAlert :message="loadError" :on-retry="loadRules" />
    </div>

    <template v-else>
      <div v-if="formOpen" class="card mb-4 p-6">
        <h2 class="mb-4 text-base font-semibold" data-testid="admin-evidence-redaction-form-title">
          {{ editingRule ? $t('views.AdminEvidenceRedaction.edit_rule') : $t('views.AdminEvidenceRedaction.new_rule') }}
        </h2>

        <form class="space-y-4" @submit.prevent="saveRule">
          <div>
            <label for="evidence-redaction-key-pattern" class="mb-1 block text-sm font-medium">
              {{ $t('views.AdminEvidenceRedaction.key_pattern') }}
            </label>
            <input
              id="evidence-redaction-key-pattern"
              v-model="form.key_pattern"
              type="text"
              maxlength="255"
              required
              :disabled="editingRule !== null"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 font-mono text-sm focus:outline-none focus:ring-2 focus:ring-primary/50 disabled:opacity-60"
              data-testid="admin-evidence-redaction-key-pattern"
            />
            <p class="mt-1 text-xs text-muted-foreground">
              {{ $t('views.AdminEvidenceRedaction.key_pattern_hint') }}
            </p>
          </div>

          <div>
            <label for="evidence-redaction-role" class="mb-1 block text-sm font-medium">
              {{ $t('views.AdminEvidenceRedaction.role') }}
            </label>
            <select
              id="evidence-redaction-role"
              v-model="form.role"
              :disabled="editingRule !== null"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary/50 disabled:opacity-60"
              data-testid="admin-evidence-redaction-role"
            >
              <option v-for="roleOption in ROLES" :key="roleOption" :value="roleOption">
                {{ roleLabel(roleOption) }}
              </option>
            </select>
          </div>

          <fieldset>
            <legend class="mb-2 block text-sm font-medium">
              {{ $t('views.AdminEvidenceRedaction.redacted_fields') }}
            </legend>
            <div class="space-y-2">
              <label
                v-for="redactionClass in REDACTION_CLASSES"
                :key="redactionClass"
                class="flex items-start gap-2 text-sm"
              >
                <input
                  v-model="form.redacted_fields"
                  type="checkbox"
                  :value="redactionClass"
                  class="mt-0.5"
                  data-testid="admin-evidence-redaction-field-checkbox"
                />
                <span>
                  <span class="font-mono text-xs">{{ redactionClass }}</span>
                  <span class="ml-2 text-muted-foreground">{{ classLabel(redactionClass) }}</span>
                </span>
              </label>
            </div>
            <p class="mt-2 text-xs text-muted-foreground">
              {{ $t('views.AdminEvidenceRedaction.redacted_fields_hint') }}
            </p>
          </fieldset>

          <p v-if="editingRule" class="text-xs text-muted-foreground">
            {{ $t('views.AdminEvidenceRedaction.identity_fixed_note') }}
          </p>

          <p
            v-if="formError"
            aria-live="assertive"
            class="text-sm text-destructive-text"
            data-testid="admin-evidence-redaction-form-error"
          >
            {{ formError }}
          </p>

          <div class="flex items-center gap-2">
            <Button type="submit" :disabled="saving" data-testid="admin-evidence-redaction-save">
              {{ saving ? $t('views.AdminEvidenceRedaction.saving') : $t('views.AdminEvidenceRedaction.save') }}
            </Button>
            <button
              type="button"
              class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
              data-testid="admin-evidence-redaction-cancel"
              @click="closeForm"
            >
              {{ $t('views.AdminEvidenceRedaction.cancel') }}
            </button>
          </div>
        </form>
      </div>

      <p
        v-if="statusMessage"
        role="status"
        aria-live="polite"
        class="mb-4 rounded-lg border border-success/50 bg-success/10 p-3 text-sm text-success"
        data-testid="admin-evidence-redaction-status"
      >
        {{ statusMessage }}
      </p>

      <p
        v-if="!hasCatchAll"
        aria-live="assertive"
        class="mb-4 rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive-text"
        data-testid="admin-evidence-redaction-catch-all-missing"
      >
        {{ $t('views.AdminEvidenceRedaction.catch_all_missing') }}
      </p>

      <EmptyState
        v-if="rules.length === 0 && !formOpen"
        :title="$t('views.AdminEvidenceRedaction.no_rules')"
        :description="$t('views.AdminEvidenceRedaction.no_rules_description')"
      />

      <div
        v-else-if="rules.length > 0"
        class="overflow-x-auto rounded-lg border"
        data-testid="admin-evidence-redaction-table"
      >
        <table class="w-full text-left text-sm">
          <thead class="bg-muted/50">
            <tr>
              <th scope="col" class="px-4 py-3 font-medium">{{ $t('views.AdminEvidenceRedaction.col_key_pattern') }}</th>
              <th scope="col" class="px-4 py-3 font-medium">{{ $t('views.AdminEvidenceRedaction.col_role') }}</th>
              <th scope="col" class="px-4 py-3 font-medium">{{ $t('views.AdminEvidenceRedaction.col_redacted_fields') }}</th>
              <th scope="col" class="px-4 py-3 font-medium">{{ $t('views.AdminEvidenceRedaction.col_updated') }}</th>
              <th scope="col" class="px-4 py-3 text-right font-medium">{{ $t('views.AdminEvidenceRedaction.col_actions') }}</th>
            </tr>
          </thead>
          <tbody class="divide-y">
            <tr v-for="rule in rules" :key="rule.id" class="hover:bg-muted/30 transition-colors">
              <td class="px-4 py-3">
                <span class="inline-flex flex-wrap items-center gap-2">
                  <code class="font-mono text-xs">{{ rule.key_pattern }}</code>
                  <span
                    v-if="isCatchAll(rule)"
                    class="rounded-full bg-primary/10 px-2 py-0.5 text-xs font-medium text-primary"
                    data-testid="admin-evidence-redaction-catch-all"
                  >
                    {{ $t('views.AdminEvidenceRedaction.mandatory') }}
                  </span>
                </span>
              </td>
              <td class="px-4 py-3">{{ roleLabel(rule.role) }}</td>
              <td class="px-4 py-3">
                <span v-if="rule.redacted_fields.length === 0" class="text-muted-foreground">—</span>
                <span v-else class="inline-flex flex-wrap gap-1.5">
                  <span
                    v-for="redactionClass in rule.redacted_fields"
                    :key="redactionClass"
                    class="rounded bg-muted px-2 py-0.5 font-mono text-xs"
                  >
                    {{ redactionClass }}
                  </span>
                </span>
              </td>
              <td class="px-4 py-3 text-muted-foreground">{{ formatDate(rule.updated_at) }}</td>
              <td class="px-4 py-3 text-right">
                <button
                  type="button"
                  class="rounded-lg border border-input bg-background px-3 py-1.5 text-xs font-medium hover:bg-accent"
                  data-testid="admin-evidence-redaction-edit"
                  @click="openEdit(rule)"
                >
                  {{ $t('views.AdminEvidenceRedaction.edit') }}
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <p
        v-if="hasCatchAll"
        class="mt-3 text-xs text-muted-foreground"
        data-testid="admin-evidence-redaction-catch-all-note"
      >
        {{ $t('views.AdminEvidenceRedaction.catch_all_note') }}
      </p>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import Button from 'primevue/button'
import PageHeader from '../components/shared/PageHeader.vue'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import EmptyState from '../components/shared/EmptyState.vue'
import { api } from '../lib/api/client'
import { useDataFetch } from '../composables/useDataFetch'
import { formatApiError } from '../lib/api/formatError'
import { formatDateShort } from '../lib/formatDate'

/**
 * The redaction rule wire shape, mirroring the backend Pydantic models
 * (``RedactionRuleUpsert`` / ``RedactionRuleResponse`` in
 * ``backend/src/modulo/api/routes/evidence.py``). Snake_case is deliberate —
 * the API does not alias these fields.
 */
interface RedactionRule {
  id: string
  key_pattern: string
  role: string
  redacted_fields: string[]
  created_at: string
  updated_at: string
}

type Role = 'admin' | 'operator' | 'viewer'

/** The roles the backend CHECK constraint and the upsert model accept. */
const ROLES: readonly Role[] = ['admin', 'operator', 'viewer']

/**
 * Mirrors ``REDACTION_CLASS_FIELDS`` in
 * ``backend/src/modulo/core/evidence_redaction.py``. The frontend cannot import
 * the Python export, so this list is a mirror: the backend rejects an unknown
 * class with a 422 (never a silent no-op), and a class added there must be
 * added here in the same change.
 */
const REDACTION_CLASSES: readonly string[] = ['sensitive_output', 'internal_metadata']

/** The seeded catch-all pair (migration 0320 / org creation). */
const CATCH_ALL_PATTERN = '*'
const CATCH_ALL_ROLE = 'viewer'

const { t } = useI18n()

const { data: rulesData, loading, error: loadError, load: loadRules } = useDataFetch(
  async () => {
    const res = await api.GET('/api/v1/evidence/redaction-rules')
    return { data: res.data, error: res.error }
  },
  { initialValue: [] as RedactionRule[] },
)

const rules = computed(() => rulesData.value ?? [])

const formOpen = ref(false)
const editingRule = ref<RedactionRule | null>(null)
const saving = ref(false)
const formError = ref<string | null>(null)
const statusMessage = ref('')

const form = ref<{ key_pattern: string; role: Role; redacted_fields: string[] }>({
  key_pattern: '',
  role: 'viewer',
  redacted_fields: [],
})

const hasCatchAll = computed(() =>
  rules.value.some(rule => isCatchAll(rule)),
)

function isCatchAll(rule: RedactionRule): boolean {
  return rule.key_pattern === CATCH_ALL_PATTERN && rule.role === CATCH_ALL_ROLE
}

function roleLabel(role: string): string {
  const key = `views.AdminEvidenceRedaction.role_${role}`
  const label = t(key)
  return label === key ? role : label
}

function classLabel(redactionClass: string): string {
  const key = `views.AdminEvidenceRedaction.class_${redactionClass}`
  const label = t(key)
  return label === key ? redactionClass : label
}

function formatDate(value: string): string {
  try {
    return formatDateShort(new Date(value))
  } catch {
    return value
  }
}

function resetForm(): void {
  form.value = { key_pattern: '', role: 'viewer', redacted_fields: [] }
  formError.value = null
}

function openCreate(): void {
  editingRule.value = null
  resetForm()
  formOpen.value = true
  statusMessage.value = ''
}

function openEdit(rule: RedactionRule): void {
  editingRule.value = rule
  form.value = {
    key_pattern: rule.key_pattern,
    role: (ROLES as readonly string[]).includes(rule.role) ? (rule.role as Role) : 'viewer',
    redacted_fields: [...rule.redacted_fields],
  }
  formError.value = null
  formOpen.value = true
  statusMessage.value = ''
}

function closeForm(): void {
  formOpen.value = false
  editingRule.value = null
  formError.value = null
}

async function saveRule(): Promise<void> {
  formError.value = null
  statusMessage.value = ''

  const keyPattern = form.value.key_pattern.trim()
  if (!keyPattern) {
    formError.value = t('views.AdminEvidenceRedaction.key_pattern_required')
    return
  }
  if (keyPattern.length > 255) {
    formError.value = t('views.AdminEvidenceRedaction.key_pattern_too_long')
    return
  }

  saving.value = true
  try {
    const role = form.value.role
    const { error: err } = await api.PUT('/api/v1/evidence/redaction-rules', {
      body: {
        key_pattern: keyPattern,
        role,
        redacted_fields: [...form.value.redacted_fields],
      },
    })
    if (err) {
      formError.value = formatApiError(err)
      return
    }
    statusMessage.value = t('views.AdminEvidenceRedaction.saved', {
      pattern: keyPattern,
      role: roleLabel(role),
    })
    closeForm()
    await loadRules()
  } catch (e: unknown) {
    formError.value = formatApiError(e)
  } finally {
    saving.value = false
  }
}
</script>
