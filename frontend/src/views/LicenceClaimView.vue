<template>
  <!--
    FAR-2114: the customer-facing surface for an EARLY renewal.

    This page is PUBLIC (``meta.public``) and renders without a session: the
    endpoint is admitted by the two signed keys, not by a browser session, and
    the customer may have no account on this deployment at all. Do not add an
    authenticated shell assumption here.
  -->
  <div class="min-h-screen bg-background px-4 py-10">
    <div class="mx-auto w-full max-w-2xl">
      <header>
        <h1 class="text-2xl font-semibold" data-testid="licence-claim-title">
          {{ $t('views.LicenceClaimView.title') }}
        </h1>
        <p class="mt-2 text-sm text-muted-foreground">
          {{ $t('views.LicenceClaimView.subtitle') }}
        </p>
      </header>

      <!--
        One form, two outcomes (FAR-2114): a ``stacked`` response means the
        previous term was carried over; ``unstacked`` is a SUCCESS that simply
        added nothing (the purchase key already grants a full term), so it is
        rendered as information, never as an error.
      -->
      <form
        v-if="result === null"
        class="mt-6 space-y-5 rounded-xl border border-border bg-card p-6 shadow-sm"
        novalidate
        @submit.prevent="submit"
      >
        <div>
          <label for="licence-claim-purchase-key" class="block text-sm font-medium">
            {{ $t('views.LicenceClaimView.purchase_key_label') }}
          </label>
          <p id="licence-claim-purchase-key-hint" class="mt-1 text-xs text-muted-foreground">
            {{ $t('views.LicenceClaimView.purchase_key_hint') }}
          </p>
          <textarea
            id="licence-claim-purchase-key"
            v-model="purchaseKey"
            rows="3"
            required
            autocomplete="off"
            spellcheck="false"
            aria-describedby="licence-claim-purchase-key-hint"
            data-testid="licence-claim-purchase-key"
            class="mt-2 w-full rounded-lg border border-input bg-background px-3 py-2 font-mono text-sm placeholder:text-muted-foreground/50 focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 outline-none transition-all"
            :placeholder="$t('views.LicenceClaimView.key_placeholder')"
          />
        </div>

        <div>
          <label for="licence-claim-previous-key" class="block text-sm font-medium">
            {{ $t('views.LicenceClaimView.previous_key_label') }}
          </label>
          <p id="licence-claim-previous-key-hint" class="mt-1 text-xs text-muted-foreground">
            {{ $t('views.LicenceClaimView.previous_key_hint') }}
          </p>
          <textarea
            id="licence-claim-previous-key"
            v-model="previousKey"
            rows="3"
            autocomplete="off"
            spellcheck="false"
            aria-describedby="licence-claim-previous-key-hint"
            data-testid="licence-claim-previous-key"
            class="mt-2 w-full rounded-lg border border-input bg-background px-3 py-2 font-mono text-sm placeholder:text-muted-foreground/50 focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 outline-none transition-all"
            :placeholder="$t('views.LicenceClaimView.key_placeholder')"
          />
        </div>

        <!-- Client validation and every rejection: one actionable message, never a raw code. -->
        <ErrorAlert
          v-if="errorMessage"
          :message="errorMessage"
          :retryable="false"
          role="alert"
          data-testid="licence-claim-error"
        />
        <p v-if="errorDetail" class="text-xs text-muted-foreground" data-testid="licence-claim-error-detail">
          {{ errorDetail }}
        </p>

        <button
          type="submit"
          :disabled="submitting"
          data-testid="licence-claim-submit"
          class="w-full rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90 disabled:opacity-50"
        >
          {{ submitting ? $t('views.LicenceClaimView.submitting') : $t('views.LicenceClaimView.submit') }}
        </button>
      </form>

      <!-- Stacked: the previous term was applied. -->
      <section
        v-else-if="result.status === 'stacked'"
        class="mt-6 space-y-4 rounded-xl border border-success/50 bg-success/10 p-6"
        aria-live="polite"
        data-testid="licence-claim-stacked"
      >
        <h2 class="text-lg font-semibold text-success-text" data-testid="licence-claim-stacked-title">
          {{ result.repeated ? $t('views.LicenceClaimView.stacked_repeated_title') : $t('views.LicenceClaimView.stacked_title') }}
        </h2>

        <div>
          <p class="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            {{ $t('views.LicenceClaimView.stacked_key_label') }}
          </p>
          <code
            class="mt-1 block break-all rounded-lg border border-border bg-background p-3 font-mono text-xs"
            data-testid="licence-claim-key-value"
          >{{ result.license_key }}</code>
          <div class="mt-2 flex flex-wrap items-center gap-3">
            <button
              type="button"
              data-testid="licence-claim-copy"
              class="rounded-lg border border-border px-3 py-1.5 text-sm font-medium transition-colors hover:bg-muted"
              @click="copyKey"
            >
              {{ copied ? $t('views.LicenceClaimView.copied') : $t('views.LicenceClaimView.copy_key') }}
            </button>
            <span v-if="copyFailed" class="text-xs text-destructive-text" role="alert" data-testid="licence-claim-copy-error">
              {{ $t('views.LicenceClaimView.copy_failed') }}
            </span>
          </div>
        </div>

        <dl class="grid gap-2 text-sm sm:grid-cols-2">
          <div>
            <dt class="text-xs text-muted-foreground">{{ $t('views.LicenceClaimView.expiry_label') }}</dt>
            <dd class="font-medium" data-testid="licence-claim-expiry">{{ formatDate(result.expires_at) }}</dd>
          </div>
          <div v-if="result.previous_expires_at">
            <dt class="text-xs text-muted-foreground">{{ $t('views.LicenceClaimView.previous_expiry_label') }}</dt>
            <dd class="font-medium" data-testid="licence-claim-previous-expiry">
              {{ formatDate(result.previous_expires_at) }}
            </dd>
          </div>
        </dl>

        <p v-if="daysKept !== null" class="text-sm" data-testid="licence-claim-days-kept">
          {{ daysKept > 0
            ? $t('views.LicenceClaimView.days_kept', { count: daysKept }, daysKept)
            : $t('views.LicenceClaimView.days_kept_zero') }}
        </p>

        <!-- The server's own explanation is the authoritative account of what happened. -->
        <p class="text-sm text-muted-foreground" data-testid="licence-claim-detail">{{ result.detail }}</p>
        <p class="text-sm text-muted-foreground">{{ $t('views.LicenceClaimView.install_hint') }}</p>

        <button
          type="button"
          data-testid="licence-claim-again"
          class="text-sm font-medium text-primary-text underline underline-offset-2"
          @click="reset"
        >
          {{ $t('views.LicenceClaimView.claim_another') }}
        </button>
      </section>

      <!--
        Unstacked: a legitimate outcome, NOT a failure. Nothing was added, the
        purchase key is returned unchanged, and the customer can re-submit with
        their previous licence.
      -->
      <section
        v-else
        class="mt-6 space-y-4 rounded-xl border border-border bg-card p-6 shadow-sm"
        aria-live="polite"
        data-testid="licence-claim-unstacked"
      >
        <h2 class="text-lg font-semibold" data-testid="licence-claim-unstacked-title">
          {{ $t('views.LicenceClaimView.unstacked_title') }}
        </h2>
        <p class="text-sm text-muted-foreground">
          {{ $t('views.LicenceClaimView.unstacked_body', { date: formatDate(result.expires_at) }) }}
        </p>

        <div>
          <p class="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            {{ $t('views.LicenceClaimView.unstacked_key_label') }}
          </p>
          <code
            class="mt-1 block break-all rounded-lg border border-border bg-background p-3 font-mono text-xs"
            data-testid="licence-claim-key-value"
          >{{ result.license_key }}</code>
          <button
            type="button"
            data-testid="licence-claim-copy"
            class="mt-2 rounded-lg border border-border px-3 py-1.5 text-sm font-medium transition-colors hover:bg-muted"
            @click="copyKey"
          >
            {{ copied ? $t('views.LicenceClaimView.copied') : $t('views.LicenceClaimView.copy_key') }}
          </button>
          <span v-if="copyFailed" class="ml-2 text-xs text-destructive-text" role="alert" data-testid="licence-claim-copy-error">
            {{ $t('views.LicenceClaimView.copy_failed') }}
          </span>
        </div>

        <p class="text-sm text-muted-foreground" data-testid="licence-claim-detail">{{ result.detail }}</p>

        <button
          type="button"
          data-testid="licence-claim-again"
          class="text-sm font-medium text-primary-text underline underline-offset-2"
          @click="retryWithPrevious"
        >
          {{ $t('views.LicenceClaimView.add_previous_key') }}
        </button>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { format, parseISO } from 'date-fns'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import { api } from '../lib/api/client'
import { isProblemDetail } from '../lib/api/formatError'
import type { components } from '../lib/api/schema'

/**
 * The wire shape is the generated OpenAPI type, never a hand-maintained copy:
 * a renamed backend field fails `vue-tsc` here instead of silently rendering
 * `undefined` (FAR-2114).
 */
type RenewalClaimResponse = components['schemas']['RenewalClaimResponse']

const { t } = useI18n()

const CLAIM_PATH = '/api/v1/webhooks/stripe/renewal-claim'

//: Rejection reasons the endpoint can return (FAR-2109) plus the server-fault
//: code. Every one maps to a locale key carrying an ACTION, never the raw code.
const REJECTION_KEYS: Record<string, string> = {
  malformed: 'error_malformed',
  signature_mismatch: 'error_signature_mismatch',
  anchor_mismatch: 'error_anchor_mismatch',
  dev_anchor_not_trusted: 'error_anchor_mismatch',
  expired: 'error_expired',
  org_mismatch: 'error_org_mismatch',
  same_licence: 'error_same_licence',
  not_a_purchase_key: 'error_not_a_purchase_key',
  no_expiry: 'error_no_expiry',
  no_presented_key: 'error_no_presented_key',
  renewal_claim_unavailable: 'error_unavailable',
}

const purchaseKey = ref('')
const previousKey = ref('')
const submitting = ref(false)
const errorCode = ref<string | null>(null)
const errorDetail = ref<string | null>(null)
const clientError = ref<string | null>(null)
const result = ref<RenewalClaimResponse | null>(null)
const copied = ref(false)
const copyFailed = ref(false)
let copiedTimer: ReturnType<typeof setTimeout> | null = null

const errorMessage = computed(() => {
  if (clientError.value) return clientError.value
  if (errorCode.value === null) return null
  const key = REJECTION_KEYS[errorCode.value] ?? 'error_unknown'
  return t(`views.LicenceClaimView.${key}`)
})

/**
 * Days that were left on the previous licence when the new term started.
 *
 * Derived only from the response's ``previous_expires_at`` and the current
 * instant — the same quantity the backend's "you kept N days" narrative
 * describes. ``null`` when the response carries no previous expiry, so the
 * line is omitted rather than rendered as a misleading zero.
 */
const daysKept = computed<number | null>(() => {
  const previous = result.value?.previous_expires_at
  if (!previous) return null
  const expiry = parseISO(previous)
  if (Number.isNaN(expiry.getTime())) return null
  return Math.ceil((expiry.getTime() - Date.now()) / 86_400_000)
})

function formatDate(iso: string): string {
  try {
    return format(parseISO(iso), 'MMMM d, yyyy')
  } catch {
    return iso
  }
}

function reset(): void {
  result.value = null
  purchaseKey.value = ''
  previousKey.value = ''
  errorCode.value = null
  errorDetail.value = null
  clientError.value = null
  copied.value = false
}

/** Re-open the form with the purchase key kept, so the customer only adds the previous licence. */
function retryWithPrevious(): void {
  result.value = null
  previousKey.value = ''
  errorCode.value = null
  errorDetail.value = null
  clientError.value = null
  copied.value = false
}

async function submit(): Promise<void> {
  clientError.value = null
  errorCode.value = null
  errorDetail.value = null

  const purchase = purchaseKey.value.trim()
  const previous = previousKey.value.trim()
  if (!purchase) {
    clientError.value = t('views.LicenceClaimView.error_purchase_required')
    return
  }

  submitting.value = true
  try {
    const { data, error } = await api.POST(CLAIM_PATH, {
      body: { purchase_key: purchase, presented_key: previous === '' ? null : previous },
    })

    if (error) {
      // The 422 body is the house problem shape: ``code`` carries the
      // machine-readable rejection reason, ``detail`` the server prose.
      const problem = isProblemDetail(error) ? error : null
      errorCode.value = problem?.code ?? 'unknown'
      errorDetail.value = problem?.detail ?? null
      return
    }
    if (!data) {
      errorCode.value = 'unknown'
      return
    }
    result.value = data
  } catch {
    errorCode.value = 'unknown'
  } finally {
    submitting.value = false
  }
}

async function copyKey(): Promise<void> {
  const key = result.value?.license_key
  if (!key) return
  copyFailed.value = false
  try {
    await navigator.clipboard.writeText(key)
    copied.value = true
  } catch {
    copyFailed.value = true
    return
  }
  if (copiedTimer) clearTimeout(copiedTimer)
  copiedTimer = setTimeout(() => {
    copied.value = false
    copiedTimer = null
  }, 2000)
}

onBeforeUnmount(() => {
  if (copiedTimer) clearTimeout(copiedTimer)
})
</script>
