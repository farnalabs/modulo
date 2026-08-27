<template>
  <div class="relative mx-auto flex min-h-screen max-w-md items-center justify-center overflow-x-hidden p-6">
    <div
      class="pointer-events-none fixed inset-0 -z-10"
      style="background-image: radial-gradient(circle at 1px 1px, var(--dot-color) 1px, transparent 0); background-size: 32px 32px;"
    />

    <div class="relative w-full space-y-6">
      <div class="text-center space-y-1">
        <h1 class="text-2xl font-bold tracking-tight">{{ $t('views.ForceChangePasswordView.change_your_password') }}</h1>
        <p class="text-sm text-muted-foreground">
          {{ $t('views.ForceChangePasswordView.you_must_set_a_new_password_before_continuing') }}
        </p>
      </div>

      <div class="rounded-xl border bg-card p-6 shadow-sm">
        <ChangePasswordForm quiet @changed="onChanged" />
      </div>

      <p v-if="changed" class="text-center text-sm text-success" role="status">
        {{ $t('views.ForceChangePasswordView.password_changed_sign_in_again') }}
      </p>

      <div class="text-center">
        <button
          type="button"
          data-testid="force-change-password-sign-out"
          class="text-sm text-muted-foreground underline-offset-4 transition-colors hover:text-foreground hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2"
          @click="onSignOut"
        >
          {{ $t('common.sign_out') }}
        </button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
// FAR-460: full-screen gate rendered by App.vue whenever the account carries
// must_change_password=true. It replaces the entire app surface, so all
// navigation is blocked until the user sets a new password — which is exactly
// why it also carries an explicit Sign out escape hatch.
import { onBeforeUnmount, ref } from 'vue'
import { useRouter } from 'vue-router'
import ChangePasswordForm from '../components/shared/ChangePasswordForm.vue'
import { setMustChangePassword } from '../lib/mustChangePassword'
import { clearAccessToken } from '../lib/api/client'

const router = useRouter()

// Long enough for the role="status" announcement to register before the view
// unmounts into LoginView.
const STATUS_VISIBLE_MS = 1500

const changed = ref(false)
let navigateTimer: ReturnType<typeof setTimeout> | undefined

function exitGateAndRouteToLogin(delayMs = 0) {
  const navigate = () => {
    void router.replace('/login')
  }
  if (delayMs > 0) {
    navigateTimer = setTimeout(navigate, delayMs)
  } else {
    navigate()
  }
}

function onChanged() {
  // Backend cleared the flag in the same transaction as the hash swap, but the
  // token family used before the swap is blacklisted server-side, so silently
  // continuing to '/' would race dead-token 401s. Make the correct path
  // explicit: end the session and sign back in with the new password.
  changed.value = true
  setMustChangePassword(false)
  clearAccessToken()
  exitGateAndRouteToLogin(STATUS_VISIBLE_MS)
}

function onSignOut() {
  setMustChangePassword(false)
  clearAccessToken()
  exitGateAndRouteToLogin()
}

onBeforeUnmount(() => {
  if (navigateTimer) clearTimeout(navigateTimer)
})
</script>

<style scoped>
/* The change handler exits the gate; nothing else to do on unmount. */
</style>
