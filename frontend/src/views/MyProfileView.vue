<template>
  <div class="p-6 max-w-2xl mx-auto space-y-6">
    <PageHeader :title="$t('views.MyProfileView.my_profile')" :subtitle="$t('views.MyProfileView.manage_your_account_settings_and_password')" />

    <div class="card p-6 space-y-6">
      <div class="flex items-center gap-4 pb-4 border-b border-border">
        <div class="flex h-14 w-14 items-center justify-center rounded-full bg-primary text-xl font-bold text-primary-foreground">
          {{ userInitial }}
        </div>
        <div>
          <p class="text-lg font-medium">{{ profile.display_name || profile.email }}</p>
          <p class="text-sm text-muted-foreground">{{ profile.email }}</p>
          <span class="inline-flex items-center rounded-md border border-primary/20 bg-primary/5 px-2 py-0.5 text-xs font-medium text-primary mt-1">{{ profile.org_role }}</span>
        </div>
      </div>

      <div v-if="profile.created_at" class="text-sm text-muted-foreground">
        Member since {{ formatMemberSince(profile.created_at) }}
      </div>
    </div>

    <div class="card p-6">
      <h2 class="text-base font-semibold mb-4">{{ $t('views.MyProfileView.change_password') }}</h2>
      <form @submit.prevent="changePassword" class="space-y-4">
        <div>
          <label for="myprofileview-field-3" class="block text-sm font-medium mb-1">{{ $t('views.MyProfileView.current_password') }}</label>
          <input id="myprofileview-field-3"
            v-model="currentPassword"
            type="password"
            class="w-full px-3 py-2 border border-input bg-background rounded-lg text-sm"
            required
            data-testid="my-profile-current-password"
          />
        </div>
        <div>
          <label for="myprofileview-field-2" class="block text-sm font-medium mb-1">{{ $t('views.MyProfileView.new_password') }}</label>
          <input id="myprofileview-field-2"
            v-model="newPassword"
            type="password"
            class="w-full px-3 py-2 border border-input bg-background rounded-lg text-sm"
            minlength="8"
            required
            data-testid="my-profile-new-password"
          />
        </div>
        <div>
          <label for="myprofileview-field-1" class="block text-sm font-medium mb-1">{{ $t('views.MyProfileView.confirm_new_password') }}</label>
          <input id="myprofileview-field-1"
            v-model="confirmPassword"
            type="password"
            class="w-full px-3 py-2 border border-input bg-background rounded-lg text-sm"
            minlength="8"
            required
            data-testid="my-profile-confirm-password"
          />
        </div>
        <p v-if="passError" class="text-sm text-destructive">{{ passError }}</p>
        <p v-if="passSuccess" class="text-sm text-success">{{ passSuccess }}</p>
        <Button
          type="submit"
          :disabled="passSaving"
          variant="default"
          class="border border-primary/30"
          data-testid="my-profile-update-password"
        >
          {{ passSaving ? $t('common.saving') : $t('views.MyProfileView.update_password') }}
        </Button>
      </form>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import PageHeader from '../components/shared/PageHeader.vue'
import { Button } from '@/components/ui/button'
import { useApi } from '../composables/useApi'
import { formatDateShort } from '../lib/formatDate'

const { t } = useI18n()

interface Profile {
  id: string
  email: string
  display_name: string
  org_role: string
  active: boolean
  created_at: string
}

const { get, put } = useApi()

const profile = ref<Profile>({ id: '', email: '', display_name: '', org_role: '', active: true, created_at: '' })
const currentPassword = ref('')
const newPassword = ref('')
const confirmPassword = ref('')
const passError = ref('')
const passSuccess = ref('')
const passSaving = ref(false)

const userInitial = computed(() => {
  const email = profile.value.email
  if (!email) return '?'
  return email.charAt(0).toUpperCase()
})

function formatMemberSince(dateStr: string): string {
  const d = new Date(dateStr)
  if (isNaN(d.getTime())) return '—'
  return formatDateShort(d)
}

async function loadProfile() {
  try {
    profile.value = await get<Profile>('/api/v1/me')
  } catch {
    profile.value = { id: '', email: '', display_name: '', org_role: '', active: true, created_at: '' }
  }
}

async function changePassword() {
  passError.value = ''
  passSuccess.value = ''
  if (newPassword.value !== confirmPassword.value) {
    passError.value = t('views.MyProfileView.passwords_do_not_match')
    return
  }
  if (newPassword.value.length < 8) {
    passError.value = t('views.MyProfileView.password_must_be_at_least_8_characters')
    return
  }
  passSaving.value = true
  try {
    await put('/api/v1/me/password', {
      current_password: currentPassword.value,
      new_password: newPassword.value,
    })
    passSuccess.value = t('views.MyProfileView.password_changed_successfully')
    currentPassword.value = ''
    newPassword.value = ''
    confirmPassword.value = ''
  } catch (e: any) {
    passError.value = e instanceof Error ? e.message : t('views.MyProfileView.failed_to_change_password')
  } finally {
    passSaving.value = false
  }
}

onMounted(loadProfile)
</script>
