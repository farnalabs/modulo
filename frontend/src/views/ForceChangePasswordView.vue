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
    </div>
  </div>
</template>

<script setup lang="ts">
// FAR-460: full-screen gate rendered by App.vue whenever the account carries
// must_change_password=true. It replaces the entire app surface, so all
// navigation is blocked until the user sets a new password.
import { useRouter } from 'vue-router'
import ChangePasswordForm from '../components/shared/ChangePasswordForm.vue'
import { setMustChangePassword } from '../lib/mustChangePassword'

const router = useRouter()

function onChanged() {
  // Backend cleared the flag in the same transaction as the hash swap;
  // drop the gate and land on the dashboard.
  setMustChangePassword(false)
  void router.push('/')
}
</script>

<style scoped>
/* The change handler exits the gate; nothing else to do on unmount. */
</style>
