<template>
  <div class="p-6 max-w-2xl mx-auto space-y-6">
    <PageHeader :title="$t('views.MyProfileView.my_profile')" :subtitle="$t('views.MyProfileView.manage_your_account_settings_and_password')" />

    <div
      v-if="profileLoadError"
      role="alert"
      aria-live="assertive"
      class="my-profile-profile-error"
    >
      <ErrorAlert
        :message="profileLoadError"
        :on-retry="loadProfile"
      />
    </div>

    <div v-else class="card p-6 space-y-6">
      <div class="flex items-center gap-4 pb-4 border-b border-border">
        <div class="flex h-14 w-14 items-center justify-center rounded-full bg-primary text-xl font-bold text-primary-foreground" aria-hidden="true">
          {{ userInitial }}
        </div>
        <div>
          <p class="text-lg font-medium">{{ profile.display_name || profile.email }}</p>
          <p class="text-sm text-muted-foreground">{{ profile.email }}</p>
          <span class="inline-flex items-center rounded-md border border-primary/20 bg-primary/5 px-2 py-0.5 text-xs font-medium text-primary-text mt-1">{{ profile.org_role || '—' }}</span>
        </div>
      </div>

      <div class="text-sm text-muted-foreground">
        {{ $t('views.MyProfileView.member_since', { date: formatMemberSince(profile.created_at) }) }}
      </div>
    </div>

    <FeatureGate feature-name="team_rbac" required-tier="team" show-disabled>
      <div class="card p-6">
        <h2 class="text-base font-semibold mb-4">{{ $t('views.MyProfileView.my_teams') }}</h2>
        <div v-if="myTeamsLoading" data-testid="my-profile-my-teams-loading">
          <!-- Live region carries only the status text; the skeleton blocks are
               layout, not phrasing content, so they are siblings. -->
          <output class="sr-only" aria-live="polite">{{ $t('common.loading') }}</output>
          <div class="space-y-2" aria-busy="true">
            <SkeletonBlock v-for="n in 2" :key="'my-team-skeleton-' + n" height-class="h-10 w-full" />
          </div>
        </div>
        <div
          v-else-if="myTeamsError"
          role="alert"
          aria-live="assertive"
          class="my-profile-my-teams-error"
        >
          <ErrorAlert
            :message="myTeamsError"
            :on-retry="loadMyTeams"
            data-testid="my-profile-my-teams-retry"
          />
        </div>
        <EmptyState
          v-else-if="myTeams.length === 0"
          :title="$t('views.MyProfileView.not_a_member_of_any_team')"
        />
        <div v-else class="space-y-2">
          <div v-for="team in myTeams" :key="team.team_id" class="flex items-center justify-between rounded-lg border bg-muted/30 px-3 py-2" data-testid="my-profile-my-team">
            <span class="font-medium">{{ team.team_name }}</span>
            <span class="inline-flex items-center rounded-md border border-primary/20 bg-primary/5 px-2 py-0.5 text-xs font-medium text-primary-text">{{ teamRoleLabel(team.role) }}</span>
          </div>
        </div>
      </div>
    </FeatureGate>

    <div class="card p-6" data-testid="my-profile-hitl-email-section">
      <h2 class="text-base font-semibold mb-1">{{ $t('views.MyProfileView.hitl_email_alerts') }}</h2>
      <p class="text-sm text-muted-foreground mb-4">{{ $t('views.MyProfileView.hitl_email_alerts_description') }}</p>

      <div v-if="hitlPrefsLoading" data-testid="my-profile-hitl-email-loading">
        <output class="sr-only" aria-live="polite">{{ $t('common.loading') }}</output>
        <div class="space-y-4" aria-busy="true">
          <SkeletonBlock height-class="h-4 w-2/3" />
          <SkeletonBlock height-class="h-10 w-full" />
          <SkeletonBlock height-class="h-9 w-28" />
        </div>
      </div>
       <div
         v-else-if="hitlPrefsLoadError"
          role="alert"
          aria-live="assertive"
           data-testid="my-profile-hitl-email-load-error"
           class="my-profile-hitl-email-load-error"
        >
          <ErrorAlert
            :message="hitlPrefsLoadError"
            :on-retry="loadHitlEmailPrefs"
            data-testid="my-profile-hitl-email-retry"
          />
       </div>
      <div v-else class="space-y-4">
        <div>
          <label for="my-profile-hitl-email-default" class="flex items-center gap-2 text-sm font-medium">
            <input
              id="my-profile-hitl-email-default"
              v-model="hitlDefault"
              type="checkbox"
              class="h-4 w-4"
              data-testid="my-profile-hitl-email-default"
            />
            <span>{{ $t('views.MyProfileView.hitl_email_default_label') }}</span>
          </label>
          <p class="mt-1 text-xs text-muted-foreground">{{ $t('views.MyProfileView.hitl_email_default_help') }}</p>
        </div>

        <div>
          <h3 class="text-sm font-medium mb-1">{{ $t('views.MyProfileView.hitl_email_per_pipeline') }}</h3>
          <p class="text-xs text-muted-foreground mb-2">{{ $t('views.MyProfileView.hitl_email_overrides_help') }}</p>
            <div
              v-if="hitlPipelinesLoadError"
              role="alert"
              aria-live="assertive"
              class="my-profile-hitl-email-pipelines-error"
            >
              <ErrorAlert
                :message="hitlPipelinesLoadError"
                :on-retry="loadHitlPipelines"
              />
            </div>
           <p v-else-if="hitlPipelines.length === 0" class="py-2 text-sm text-muted-foreground">{{ $t('views.MyProfileView.hitl_email_no_pipelines') }}</p>
           <div v-else class="space-y-2">
            <div v-for="pipeline in hitlPipelines" :key="pipeline.id" class="flex items-center justify-between gap-3 rounded-lg border bg-muted/30 px-3 py-2" data-testid="my-profile-hitl-email-pipeline">
              <span class="font-medium">{{ pipeline.name }}</span>
              <select
                :value="hitlOverrideChoice(pipeline.id)"
                :aria-label="$t('views.MyProfileView.hitl_email_select_aria', { name: pipeline.name })"
                class="rounded-lg border border-input bg-background px-2 py-1 text-sm"
                data-testid="my-profile-hitl-email-pipeline-select"
                @change="onHitlOverrideChange(pipeline.id, $event)"
              >
                <option value="default">{{ $t('views.MyProfileView.hitl_email_override_default') }}</option>
                <option value="on">{{ $t('views.MyProfileView.hitl_email_override_on') }}</option>
                <option value="off">{{ $t('views.MyProfileView.hitl_email_override_off') }}</option>
              </select>
            </div>
          </div>
        </div>

        <p
          v-if="hitlSaveError"
          role="alert"
          aria-live="assertive"
          class="text-sm text-destructive"
          data-testid="my-profile-hitl-email-error"
        >{{ hitlSaveError }}</p>
        <p v-if="hitlSaveSuccess" class="text-sm text-success-text" aria-live="polite" data-testid="my-profile-hitl-email-success">{{ $t('views.MyProfileView.hitl_email_saved') }}</p>

        <Button type="button" :disabled="hitlSaving" class="border border-primary/30 w-full sm:w-auto" data-testid="my-profile-hitl-email-save" @click="saveHitlEmailPrefs">
          {{ hitlSaving ? $t('common.saving') : $t('common.save') }}
        </Button>
      </div>
    </div>

    <div class="card p-6">
      <h2 class="text-base font-semibold mb-4">{{ $t('views.MyProfileView.change_password') }}</h2>
      <ChangePasswordForm />
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import PageHeader from '../components/shared/PageHeader.vue'
import FeatureGate from '../components/FeatureGate.vue'
import ChangePasswordForm from '../components/shared/ChangePasswordForm.vue'
import EmptyState from '../components/shared/EmptyState.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import SkeletonBlock from '../components/shared/SkeletonBlock.vue'
import Button from 'primevue/button'
import { api } from '../lib/api/client'
import { formatApiError } from '../lib/api/formatError'
import type { components } from '../lib/api/client'
import { formatDateShort } from '../lib/formatDate'

type Profile = components['schemas']['modulo__api__routes__auth__MeResponse']
type MyTeam = components['schemas']['MyTeamResponse']
type PipelineItem = components['schemas']['PipelineResponse']

const EMPTY_PROFILE: Profile = { id: '', email: '', display_name: '', org_role: '', active: true, created_at: '', is_system_admin: false, must_change_password: false }

const profile = ref<Profile>({ ...EMPTY_PROFILE })
const profileLoadError = ref('')

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

const { t, te } = useI18n()

function teamRoleLabel(role: string): string {
  const key = `views.SettingsTeamsView.${role}`
  return te(key) ? t(key) : role
}

async function loadProfile() {
  profileLoadError.value = ''
  try {
    const { data, error } = await api.GET('/api/v1/auth/me')
    if (error) {
      profileLoadError.value = formatApiError(error)
      return
    }
    if (data) {
      profile.value = { ...EMPTY_PROFILE, ...data }
    }
  } catch (e) {
    profileLoadError.value = formatApiError(e)
  }
}

// ── HITL email alerts (FAR-605) ───────────────────────────────────────

const hitlPrefsLoading = ref(false)
const hitlPrefsLoadError = ref('')
const hitlDefault = ref(false)
const hitlOverrides = ref<Record<string, boolean>>({})
const hitlSaving = ref(false)
const hitlSaveError = ref('')
const hitlSaveSuccess = ref(false)
const hitlPipelines = ref<PipelineItem[]>([])
const hitlPipelinesLoadError = ref('')

function hitlOverrideChoice(pipelineId: string): 'default' | 'on' | 'off' {
  if (pipelineId in hitlOverrides.value) return hitlOverrides.value[pipelineId] ? 'on' : 'off'
  return 'default'
}

function onHitlOverrideChange(pipelineId: string, event: Event) {
  const value = (event.target as HTMLSelectElement).value
  const next = { ...hitlOverrides.value }
  if (value === 'on') next[pipelineId] = true
  else if (value === 'off') next[pipelineId] = false
  else delete next[pipelineId]
  hitlOverrides.value = next
}

async function loadHitlEmailPrefs() {
  hitlPrefsLoading.value = true
  hitlPrefsLoadError.value = ''
  try {
    const { data, error } = await api.GET('/api/v1/me/hitl-email-preferences')
    if (error) {
      hitlPrefsLoadError.value = formatApiError(error)
      return
    }
    if (data) {
      hitlDefault.value = data.default
      hitlOverrides.value = { ...(data.pipeline_overrides ?? {}) }
    } else {
      hitlDefault.value = false
      hitlOverrides.value = {}
    }
  } catch (e) {
    hitlPrefsLoadError.value = formatApiError(e)
  } finally {
    hitlPrefsLoading.value = false
  }
}

async function loadHitlPipelines() {
  hitlPipelinesLoadError.value = ''
  try {
    const { data, error } = await api.GET('/api/v1/pipelines')
    if (error) {
      hitlPipelinesLoadError.value = formatApiError(error)
      return
    }
    if (!data) {
      hitlPipelinesLoadError.value = t('views.MyProfileView.hitl_email_pipelines_load_failed')
      return
    }
    hitlPipelines.value = data.items ?? []
  } catch (e) {
    hitlPipelinesLoadError.value = formatApiError(e)
  }
}

async function saveHitlEmailPrefs() {
  hitlSaving.value = true
  hitlSaveError.value = ''
  hitlSaveSuccess.value = false
  try {
    const { data, error } = await api.PUT('/api/v1/me/hitl-email-preferences', {
      body: {
        default: hitlDefault.value,
        pipeline_overrides: { ...hitlOverrides.value },
      },
    })
    if (error) {
      hitlSaveError.value = formatApiError(error)
      return
    }
    if (data) {
      hitlSaveSuccess.value = true
    }
  } catch (e) {
    hitlSaveError.value = formatApiError(e)
  } finally {
    hitlSaving.value = false
  }
}

onMounted(() => {
  loadProfile()
  loadMyTeams()
  loadHitlPipelines()
  loadHitlEmailPrefs()
})
</script>
