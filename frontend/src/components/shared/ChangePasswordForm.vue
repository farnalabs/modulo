<template>
  <form @submit.prevent="changePassword" class="space-y-4">
    <div>
      <label for="changepassword-current" class="block text-sm font-medium mb-1">{{ $t('views.MyProfileView.current_password') }}</label>
      <input id="changepassword-current"
        v-model="currentPassword"
        type="password"
        autocomplete="current-password"
        class="w-full px-3 py-2 border border-input bg-background rounded-lg text-sm"
        required
        data-testid="change-password-current"
      />
    </div>
    <div>
      <label for="changepassword-new" class="block text-sm font-medium mb-1">{{ $t('views.MyProfileView.new_password') }}</label>
      <input id="changepassword-new"
        v-model="newPassword"
        type="password"
        autocomplete="new-password"
        class="w-full px-3 py-2 border border-input bg-background rounded-lg text-sm"
        minlength="8"
        required
        data-testid="change-password-new"
      />
    </div>
    <div>
      <label for="changepassword-confirm" class="block text-sm font-medium mb-1">{{ $t('views.MyProfileView.confirm_new_password') }}</label>
      <input id="changepassword-confirm"
        v-model="confirmPassword"
        type="password"
        autocomplete="new-password"
        class="w-full px-3 py-2 border border-input bg-background rounded-lg text-sm"
        minlength="8"
        required
        data-testid="change-password-confirm"
      />
    </div>
    <p v-if="passError" class="text-sm text-destructive">{{ passError }}</p>
    <p v-if="passSuccess" class="text-sm text-success">{{ passSuccess }}</p>
    <Button type="submit" :disabled="passSaving" class="border border-primary/30 w-full sm:w-auto" data-testid="change-password-submit">
      {{ passSaving ? $t('common.saving') : $t('views.MyProfileView.update_password') }}
    </Button>
  </form>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useI18n } from 'vue-i18n'
import Button from 'primevue/button'
import { api } from '../../lib/api/client'
import { formatApiError } from '../../lib/api/formatError'

const emit = defineEmits<{ (e: 'changed'): void }>()

// When `quiet` is set, the success note is suppressed (the forced-change flow
// leaves the screen immediately, so the message would only flash).
const props = defineProps<{ quiet?: boolean }>()

const { t } = useI18n()

const currentPassword = ref('')
const newPassword = ref('')
const confirmPassword = ref('')
const passError = ref('')
const passSuccess = ref('')
const passSaving = ref(false)

async function changePassword() {
  passError.value = ''
  passSuccess.value = ''
  if (newPassword.value !== confirmPassword.value) {
    passError.value = t('views.MyProfileView.passwords_do_not_match')
    return
  }
  if (newPassword.value === currentPassword.value) {
    passError.value = t('views.MyProfileView.new_password_must_differ')
    return
  }
  if (newPassword.value.length < 8) {
    passError.value = t('views.MyProfileView.password_must_be_at_least_8_characters')
    return
  }
  passSaving.value = true
  try {
    const { data, error } = await api.PUT('/api/v1/me/password', {
      body: {
        current_password: currentPassword.value,
        new_password: newPassword.value,
      },
    })
    if (error) {
      passError.value = formatApiError(error)
    } else if (data) {
      if (!props.quiet) passSuccess.value = t('views.MyProfileView.password_changed_successfully')
      currentPassword.value = ''
      newPassword.value = ''
      confirmPassword.value = ''
      emit('changed')
    }
  } catch (e) {
    passError.value = formatApiError(e)
  } finally {
    passSaving.value = false
  }
}
</script>
