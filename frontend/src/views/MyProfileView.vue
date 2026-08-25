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
        {{ $t('views.MyProfileView.member_since', { date: formatMemberSince(profile.created_at) }) }}
      </div>
    </div>

    <FeatureGate feature-name="team_rbac" required-tier="team" show-disabled>
      <div class="card p-6">
        <h2 class="text-base font-semibold mb-4">{{ $t('views.MyProfileView.my_teams') }}</h2>
        <div v-if="myTeamsLoading" class="flex items-center justify-center py-4">
          <div class="h-5 w-5 animate-spin rounded-full border-2 border-primary border-t-transparent"></div>
        </div>
        <div v-else-if="myTeamsError" class="py-2 text-sm text-destructive">
          {{ myTeamsError }}
          <button type="button" class="ml-2 underline" data-testid="my-profile-my-teams-retry" @click="loadMyTeams">{{ $t('views.SettingsTeamsView.retry') }}</button>
        </div>
        <div v-else-if="myTeams.length === 0" class="py-2 text-sm text-muted-foreground">
          {{ $t('views.MyProfileView.not_a_member_of_any_team') }}
        </div>
        <div v-else class="space-y-2">
          <div v-for="team in myTeams" :key="team.team_id" class="flex items-center justify-between rounded-lg border bg-muted/30 px-3 py-2" data-testid="my-profile-my-team">
            <span class="font-medium">{{ team.team_name }}</span>
            <span class="inline-flex items-center rounded-md border border-primary/20 bg-primary/5 px-2 py-0.5 text-xs font-medium text-primary">{{ $t('views.SettingsTeamsView.' + team.role) }}</span>
          </div>
        </div>
      </div>
    </FeatureGate>

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
        <Button type="submit" :disabled="passSaving" class="border border-primary/30" data-testid="my-profile-update-password">
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
import FeatureGate from '../components/FeatureGate.vue'
import Button from 'primevue/button'
import { api } from '../lib/api/client'
import { formatApiError } from '../lib/api/formatError'
import type { components } from '../lib/api/client'
import { formatDateShort } from '../lib/formatDate'

const { t } = useI18n()

type Profile = components['schemas']['modulo__api__routes__auth__MeResponse']
type MyTeam = components['schemas']['MyTeamResponse']

const EMPTY_PROFILE: Profile = { id: '', email: '', display_name: '', org_role: '', active: true, created_at: '', is_system_admin: false }

const profile = ref<Profile>({ ...EMPTY_PROFILE })
const currentPassword = ref('')
const newPassword = ref('')
const confirmPassword = ref('')
const passError = ref('')
const passSuccess = ref('')
const passSaving = ref(false)

const myTeams = ref<MyTeam[]>([])
const myTeamsLoading = ref(false)
const myTeamsError = ref('')

async function loadMyTeams() {
  myTeamsLoading.value = true
  myTeamsError.value = ''
  try {
    const { data, error } = await api.GET('/api/v1/teams/my')
    if (error) {
      myTeamsError.value = formatApiError(error)
      myTeams.value = []
      return
    }
    if (data) {
      myTeams.value = data
    }
  } catch (e) {
    myTeamsError.value = formatApiError(e)
    myTeams.value = []
  } finally {
    myTeamsLoading.value = false
  }
}

const userInitial = computed(() => {
  const email = profile.value.email
  if (!email) return '?'
  return email.charAt(0).toUpperCase()
})

function formatMemberSince(dateStr: string): string {
  const d = new Date(dateStr)
  if (Number.isNaN(d.getTime())) return '—'
  return formatDateShort(d)
}

async function loadProfile() {
  try {
    const { data, error } = await api.GET('/api/v1/auth/me')
    if (error) {
      profile.value = { ...EMPTY_PROFILE }
      return
    }
    if (data) {
      const { id, email, display_name, org_role, active, created_at, is_system_admin } = data
      profile.value = { id, email, display_name, org_role, active, created_at, is_system_admin }
    }
  } catch (e) {
    console.warn('Failed to load profile', e)
    profile.value = { ...EMPTY_PROFILE }
  }
}

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
      passSuccess.value = t('views.MyProfileView.password_changed_successfully')
      currentPassword.value = ''
      newPassword.value = ''
      confirmPassword.value = ''
    }
  } catch (e) {
    passError.value = formatApiError(e)
  } finally {
    passSaving.value = false
  }
}

onMounted(() => {
  loadProfile()
  loadMyTeams()
})
</script>
