<template>
  <div class="min-h-screen flex items-center justify-center bg-background px-4 py-10">
    <div class="w-full max-w-md rounded-xl border border-border bg-card p-6 shadow-sm">
      <h1 class="text-xl font-semibold">{{ $t('views.AcceptInviteView.title') }}</h1>
      <p class="mt-1 text-sm text-muted-foreground">{{ $t('views.AcceptInviteView.subtitle') }}</p>

      <div
        v-if="missingToken"
        role="alert"
        data-testid="accept-invite-missing-token"
        class="mt-4 rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive"
      >
        {{ $t('views.AcceptInviteView.missing_token') }}
      </div>

      <form v-else-if="!success" class="mt-5 space-y-4" novalidate @submit.prevent="submit">
        <div>
          <label for="accept-invite-password" class="block text-sm font-medium mb-1">{{ $t('views.AcceptInviteView.password_label') }}</label>
          <input
            id="accept-invite-password"
            v-model="password"
            type="password"
            autocomplete="new-password"
            required
            :aria-invalid="!!error"
            data-testid="accept-invite-password"
            class="w-full px-3 py-2 border border-input bg-background rounded-lg text-sm"
          />
        </div>
        <div>
          <label for="accept-invite-confirm" class="block text-sm font-medium mb-1">{{ $t('views.AcceptInviteView.confirm_label') }}</label>
          <input
            id="accept-invite-confirm"
            v-model="confirmPassword"
            type="password"
            autocomplete="new-password"
            required
            aria-describedby="accept-invite-requirements"
            data-testid="accept-invite-confirm"
            class="w-full px-3 py-2 border border-input bg-background rounded-lg text-sm"
          />
        </div>
        <ul id="accept-invite-requirements" class="space-y-1 text-xs text-muted-foreground list-disc pl-4">
          <li>{{ $t('views.AcceptInviteView.requirement_length') }}</li>
          <li>{{ $t('views.AcceptInviteView.requirement_mixed') }}</li>
        </ul>
        <p
          v-if="error"
          role="alert"
          data-testid="accept-invite-error"
          class="rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive"
        >
          {{ error }}
        </p>
        <button
          type="submit"
          :disabled="loading"
          data-testid="accept-invite-submit"
          class="w-full px-4 py-2 rounded-lg bg-primary text-primary-foreground text-sm font-medium disabled:opacity-50 transition-colors hover:bg-primary/90"
        >
          {{ loading ? $t('views.AcceptInviteView.submitting_button') : $t('views.AcceptInviteView.submit_button') }}
        </button>
      </form>

      <div
        v-else
        role="status"
        data-testid="accept-invite-success"
        class="mt-5 rounded-lg border border-success/50 bg-success/10 p-4 text-sm text-success"
      >
        {{ successMessage }}
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { useApi } from '../composables/useApi'
import { passwordRuleKey, validatePasswordClient } from '../lib/passwordRules'

const { t } = useI18n()
const router = useRouter()
const { post } = useApi()

const password = ref('')
const confirmPassword = ref('')
const error = ref('')
const loading = ref(false)
const success = ref(false)
// Branch (d): the invited email already has a local account with a password.
const usedExistingAccount = ref(false)

// The one-time invite token is delivered in the URL FRAGMENT (#token=...) —
// never the query string — so it is not sent to the server nor leaked via
// Referer/access logs (same convention as ModelBackendSetupView). Strip it
// from the address bar / history so the secret does not linger.
const tokenFromFragment = readFragmentToken()
if (tokenFromFragment) {
  history.replaceState(null, '', window.location.pathname + window.location.search)
}
const inviteToken = ref(tokenFromFragment)

function readFragmentToken(): string {
  const token = new URLSearchParams(window.location.hash.slice(1)).get('token')
  return token ?? ''
}

const missingToken = computed(() => inviteToken.value === '')
const successMessage = computed(() =>
  usedExistingAccount.value ? t('views.AcceptInviteView.existing_success_message') : t('views.AcceptInviteView.success_message'),
)

function clientValidation(): string {
  const ruleCode = validatePasswordClient(password.value)
  if (ruleCode) {
    return t(passwordRuleKey(ruleCode))
  }
  if (password.value !== confirmPassword.value) {
    return t('views.AcceptInviteView.mismatch_error')
  }
  return ''
}

let redirectTimer: ReturnType<typeof setTimeout> | null = null

async function submit() {
  error.value = ''
  const preError = clientValidation()
  if (preError) {
    error.value = preError
    return
  }
  loading.value = true
  try {
    const resp = await post<{ detail: string; existing_account?: boolean }>('/api/v1/auth/accept-invite', {
      token: inviteToken.value,
      password: password.value,
    })
    // Defer any auth-state flip until after this announce window: there is no
    // session here, so simply show the confirmation then navigate to /login.
    usedExistingAccount.value = resp.existing_account === true
    success.value = true
    redirectTimer = setTimeout(() => {
      redirectTimer = null
      void router.replace('/login')
    }, 1600)
  } catch (e: unknown) {
    error.value = e instanceof Error && e.message ? e.message : t('views.AcceptInviteView.invalid_token_error')
  } finally {
    loading.value = false
  }
}

onBeforeUnmount(() => {
  if (redirectTimer) clearTimeout(redirectTimer)
})
</script>
