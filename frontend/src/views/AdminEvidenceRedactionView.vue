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
        :disabled="loading || loadError !== null || saving"
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

    <div v-else-if="initialLoadError" aria-live="assertive" data-testid="admin-evidence-redaction-load-error">
      <ErrorAlert :message="initialLoadError" :on-retry="loadRules" />
    </div>

    <template v-else>
      <!-- A failed REFETCH (after a successful first load) must not replace the
           page: the last-good table and the success message stay rendered, and
           the failure surfaces here as a non-fatal, retry-able banner. -->
      <div
        v-if="refetchLoadError"
        aria-live="assertive"
        class="mb-4"
        data-testid="admin-evidence-redaction-refetch-error"
      >
        <ErrorAlert :message="refetchLoadError" :on-retry="loadRules" />
      </div>

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
            <AppSelect
              id="evidence-redaction-role"
              v-model="form.role"
              :options="formRoleOptions.map((roleOption) => ({ value: roleOption, label: roleLabel(roleOption) }))"
              option-label="label"
              option-value="value"
              label="Redaction role"
              :disabled="editingRule !== null"
              class="w-full"
              data-testid="admin-evidence-redaction-role"
            />
            <p
              v-if="!roleIsKnown"
              class="mt-1 text-xs text-destructive-text"
              data-testid="admin-evidence-redaction-role-unrecognised"
            >
              {{ $t('views.AdminEvidenceRedaction.role_unrecognised', { role: form.role }) }}
            </p>
          </div>

          <fieldset>
            <legend class="mb-2 block text-sm font-medium">
              {{ $t('views.AdminEvidenceRedaction.redacted_fields') }}
            </legend>
            <div class="space-y-2">
              <label
                v-for="redactionClass in classOptions"
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
            <Button
              type="submit"
              :disabled="saving || !roleIsKnown"
              data-testid="admin-evidence-redaction-save"
            >
              {{ saving ? $t('views.AdminEvidenceRedaction.saving') : $t('views.AdminEvidenceRedaction.save') }}
            </Button>
            <button
              type="button"
              class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-60"
              :disabled="saving"
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
              <td class="px-4 py-3 text-muted-foreground">{{ formatDateShort(rule.updated_at) }}</td>
              <td class="px-4 py-3 text-right">
                <button
                  type="button"
                  class="rounded-lg border border-input bg-background px-3 py-1.5 text-xs font-medium hover:bg-accent disabled:opacity-60"
                  :disabled="saving"
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
import AppSelect from '../components/shared/AppSelect.vue'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import EmptyState from '../components/shared/EmptyState.vue'
import { api } from '../lib/api/client'
import type { components } from '../lib/api/schema'
import { useDataFetch } from '../composables/useDataFetch'
import { formatApiError } from '../lib/api/formatError'
import { formatDateShort } from '../lib/formatDate'

/**
 * The redaction-rule wire shapes come from the generated OpenAPI schema, never
 * a hand-rolled mirror: ``RedactionRuleResponse`` is what GET returns and
 * ``RedactionRuleUpsert`` is the PUT body (snake_case, unaliased on the API).
 */
type RedactionRule = components['schemas']['RedactionRuleResponse']
type RedactionRuleUpsert = components['schemas']['RedactionRuleUpsert']

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
const REDACTION_CLASSES: ReadonlySet<string> = new Set(['sensitive_output', 'internal_metadata'])

/** The seeded catch-all pair (migration 0320 / org creation). */
const CATCH_ALL_PATTERN = '*'
const CATCH_ALL_ROLE = 'viewer'

const { t, te } = useI18n()

const {
  data: rulesData,
  loading,
  error: loadError,
  fetched,
  load: fetchRules,
} = useDataFetch(
  async () => {
    const res = await api.GET('/api/v1/evidence/redaction-rules')
    return { data: res.data, error: res.error }
  },
  // silentRefetch: only the FIRST load flips `loading`. A post-save refetch
  // must not unmount the table (and the success message), so its failure is
  // split out below into a non-fatal inline banner.
  { initialValue: [] as RedactionRule[], silentRefetch: true },
)

// useDataFetch was initialised with `[]`, so the data ref is never undefined.
const rules = computed(() => rulesData.value)

/**
 * Split the fetch failure by WHEN it happened. Before the first successful
 * load there is nothing to preserve, so it stays page-fatal; after one
 * (`fetched` only ever flips true) the last-good table stays rendered and the
 * failure surfaces as a retry-able inline banner instead.
 */
const initialLoadError = computed(() => (fetched.value ? null : loadError.value))
// Only rendered once the first load has succeeded (the `v-else` branch below),
// so an error reaching here is always a refetch failure.
const refetchLoadError = computed(() => loadError.value)

async function loadRules(): Promise<void> {
  // useDataFetch reports a failed request through its `error` state; `refetch`
  // resolves rather than rejecting, so there is nothing to catch here.
  await fetchRules()
}

const formOpen = ref(false)
const editingRule = ref<RedactionRule | null>(null)
const saving = ref(false)
const formError = ref<string | null>(null)
const statusMessage = ref('')

const form = ref<{ key_pattern: string; role: string; redacted_fields: string[] }>({
  key_pattern: '',
  role: 'viewer',
  redacted_fields: [],
})

/**
 * A stored role this page does not recognise (e.g. a future role) must stay
 * visible and block Save — coercing it to `viewer` would silently rewrite the
 * rule's identity on the next upsert.
 */
const roleIsKnown = computed(() => (ROLES as readonly string[]).includes(form.value.role))

const formRoleOptions = computed(() => {
  const options: string[] = [...ROLES]
  if (form.value.role && !options.includes(form.value.role)) options.push(form.value.role)
  return options
})

/**
 * Backend classes plus any stored class this build does not know about. An
 * unknown class stays visible (and removable) instead of being a hidden value
 * that makes the rule unsaveable.
 */
const classOptions = computed(() => {
  const extras = form.value.redacted_fields.filter(item => !REDACTION_CLASSES.has(item))
  return [...REDACTION_CLASSES, ...extras]
})

/** The existing rule that a CREATE would silently overwrite via the upsert. */
const conflictingRule = computed(() => {
  const keyPattern = form.value.key_pattern.trim()
  return (
    rules.value.find(rule => rule.key_pattern === keyPattern && rule.role === form.value.role) ??
    null
  )
})

const hasCatchAll = computed(() =>
  rules.value.some(rule => isCatchAll(rule)),
)

function isCatchAll(rule: RedactionRule): boolean {
  return rule.key_pattern === CATCH_ALL_PATTERN && rule.role === CATCH_ALL_ROLE
}

function roleLabel(role: string): string {
  const key = `views.AdminEvidenceRedaction.role_${role}`
  return te(key) ? t(key) : role
}

function classLabel(redactionClass: string): string {
  const key = `views.AdminEvidenceRedaction.class_${redactionClass}`
  return te(key) ? t(key) : redactionClass
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
    role: rule.role,
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
  if (saving.value) return
  formError.value = null

  const keyPattern = form.value.key_pattern.trim()
  if (!keyPattern) {
    formError.value = t('views.AdminEvidenceRedaction.key_pattern_required')
    return
  }
  if (keyPattern.length > 255) {
    formError.value = t('views.AdminEvidenceRedaction.key_pattern_too_long')
    return
  }
  if (!roleIsKnown.value) {
    formError.value = t('views.AdminEvidenceRedaction.role_unrecognised', { role: form.value.role })
    return
  }
  // An empty class set resolves to "redact nothing" on the server, which would
  // silently turn the boundary OFF for the matched keys (and empty the seeded
  // '*' / viewer catch-all). Never allow it.
  if (form.value.redacted_fields.length === 0) {
    formError.value = t('views.AdminEvidenceRedaction.select_at_least_one_class')
    return
  }
  // The PUT is an upsert keyed on (key_pattern, role), so a create that
  // collides with an existing pair would silently overwrite it.
  if (editingRule.value === null && conflictingRule.value) {
    formError.value = t('views.AdminEvidenceRedaction.rule_exists', {
      pattern: keyPattern,
      role: roleLabel(form.value.role),
    })
    return
  }

  const role = form.value.role as Role
  const payload: RedactionRuleUpsert = {
    key_pattern: keyPattern,
    role,
    redacted_fields: [...form.value.redacted_fields],
  }

  saving.value = true
  statusMessage.value = ''
  try {
    const { error: err } = await api.PUT('/api/v1/evidence/redaction-rules', { body: payload })
    if (err) {
      formError.value = formatApiError(err)
      return
    }
    // Set AFTER the write and before the refetch: a failed refetch must not
    // erase the confirmation of the save that actually succeeded.
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
