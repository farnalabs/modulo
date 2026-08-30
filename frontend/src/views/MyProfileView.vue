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
      <ChangePasswordForm />
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import PageHeader from '../components/shared/PageHeader.vue'
import FeatureGate from '../components/FeatureGate.vue'
import ChangePasswordForm from '../components/shared/ChangePasswordForm.vue'
import { api } from '../lib/api/client'
import { formatApiError } from '../lib/api/formatError'
import type { components } from '../lib/api/client'
import { formatDateShort } from '../lib/formatDate'

type Profile = components['schemas']['modulo__api__routes__auth__MeResponse']
type MyTeam = components['schemas']['MyTeamResponse']

const EMPTY_PROFILE: Profile = { id: '', email: '', display_name: '', org_role: '', active: true, created_at: '', is_system_admin: false, must_change_password: false }

const profile = ref<Profile>({ ...EMPTY_PROFILE })

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
      profile.value = { ...EMPTY_PROFILE, ...data }
    }
  } catch (e) {
    console.warn('Failed to load profile', e)
    profile.value = { ...EMPTY_PROFILE }
  }
}

onMounted(() => {
  loadProfile()
  loadMyTeams()
})
</script>
