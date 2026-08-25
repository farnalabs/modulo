<template>
  <FeatureGate feature-name="team_rbac" required-tier="team" show-disabled>
    <div class="page-wide">
    <header class="flex items-center justify-between">
      <PageHeader title="Teams" subtitle="Manage teams and team membership" />
      <Button class="border-primary/30 hover:border-primary/60" data-testid="settings-teams-create-team" @click="showCreateForm = true">
        Create Team
      </Button>
    </header>

    <LoadingSpinner v-if="loading" />
    <ErrorAlert v-else-if="error" :message="error" />

    <div v-if="!loading && !error">
      <div v-if="showCreateForm" class="card max-w-3xl p-6">
        <h2 class="mb-4 text-base font-semibold">{{ $t('views.SettingsTeamsView.new_team') }}</h2>
        <div class="space-y-4">
          <div>
            <label for="settingsteamsview-field-2" class="mb-1 block text-sm font-medium">{{ $t('views.SettingsTeamsView.name') }}</label>
            <input id="settingsteamsview-field-2"
              v-model="createName"
              type="text"
              data-testid="settings-teams-create-name"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              placeholder="e.g. Platform Engineering"
            />
          </div>
          <div>
            <label for="settingsteamsview-field-1" class="mb-1 block text-sm font-medium">{{ $t('views.SettingsTeamsView.description') }}</label>
            <textarea id="settingsteamsview-field-1"
              v-model="createDescription"
              rows="2"
              data-testid="settings-teams-create-description"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              placeholder="Optional description"
            ></textarea>
          </div>
          <div class="flex items-center gap-2">
            <Button :disabled="!createName.trim() || creatingTeam" data-testid="settings-teams-create-submit" @click="createTeam">
              {{ creatingTeam ? 'Creating...' : 'Create' }}
            </Button>
            <button type="button" class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent" data-testid="settings-teams-create-cancel" @click="cancelCreate">
              Cancel
            </button>
          </div>
        </div>
        <div v-if="createError" class="mt-3 text-sm text-destructive">{{ createError }}</div>
        <div v-if="createSuccess" class="mt-3 text-sm text-success">{{ createSuccess }}</div>
      </div>

      <div v-if="teams.length === 0" class="card p-8 text-center">
        <p class="text-lg font-medium">{{ $t('views.SettingsTeamsView.no_teams_yet') }}</p>
        <p class="mt-1 text-sm text-muted-foreground">{{ $t('views.SettingsTeamsView.create_your_first_team_to_organize_members_and_resources') }}</p>
      </div>

      <div class="space-y-3">
        <div v-for="team in teams" :key="team.id" class="card">
          <div class="flex items-center p-4" :class="{ 'border-b': expandedTeamId === team.id }">
            <button
              type="button"
              :data-testid="'settings-teams-toggle-' + team.id"
              class="flex flex-1 cursor-pointer items-center gap-3 border-none bg-transparent p-0 text-left"
              :aria-expanded="expandedTeamId === team.id"
              :aria-controls="'settings-teams-panel-' + team.id"
              @click="toggleExpand(team.id)"
            >
              <svg class="h-4 w-4 shrink-0 text-muted-foreground transition-transform" :class="{ 'rotate-90': expandedTeamId === team.id }" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="m9 18 6-6-6-6" />
              </svg>
              <div>
                <p class="font-medium">{{ team.name }}</p>
                <p v-if="team.description" class="text-sm text-muted-foreground">{{ team.description }}</p>
              </div>
            </button>
            <div class="flex shrink-0 items-center gap-3">
              <span class="text-sm text-muted-foreground">{{ team.member_count }} member{{ team.member_count !== 1 ? 's' : '' }}</span>
              <span class="text-sm text-muted-foreground" data-testid="settings-teams-owned-resource-count">
                {{ $t('views.SettingsTeamsView.owned_resource_count', { count: team.owned_resource_count ?? 0 }) }}
              </span>
              <TableActions :actions="teamActions(team)" />
            </div>
          </div>

          <section v-if="expandedTeamId === team.id" :id="'settings-teams-panel-' + team.id" class="p-4" :aria-label="$t('views.SettingsTeamsView.team_details', { name: team.name })">
            <div v-if="renameTeamId === team.id" class="mb-4 flex items-center gap-2">
              <input aria-label="text" v-model="renameName" type="text" data-testid="settings-teams-rename-name" class="flex-1 rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" @keyup.enter="saveRename" />
              <Button :disabled="!renameName.trim() || renamingTeam" data-testid="settings-teams-rename-save" @click="saveRename">
                {{ renamingTeam ? 'Saving...' : 'Save' }}
              </Button>
              <button type="button" class="rounded-lg border border-input bg-background px-3 py-2 text-sm font-medium hover:bg-accent" data-testid="settings-teams-rename-cancel" @click="cancelRename">
                Cancel
              </button>
            </div>

            <div v-if="deleteConfirmTeamId === team.id" class="mb-4 rounded-lg border border-destructive/50 bg-destructive/10 p-4">
              <p class="text-sm font-medium text-destructive">Delete "{{ team.name }}"?</p>
              <p class="mt-1 text-sm text-destructive/80">{{ $t('views.SettingsTeamsView.this_action_cannot_be_undone') }}</p>
              <div class="mt-3 flex items-center gap-2">
                <button type="button" :disabled="deletingTeam" data-testid="settings-teams-delete-confirm" class="rounded-lg bg-destructive px-4 py-2 text-sm font-medium text-destructive-foreground hover:bg-destructive/90 disabled:opacity-50" @click="deleteTeam(team.id)">
                  {{ deletingTeam ? 'Deleting...' : 'Delete' }}
                </button>
                <button type="button" class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent" data-testid="settings-teams-delete-cancel" @click="deleteConfirmTeamId = null; deleteError = null">
                  Cancel
                </button>
                <div v-if="deleteError" class="mt-2 text-sm text-destructive">{{ deleteError }}</div>
              </div>
            </div>

            <h3 class="mb-3 text-sm font-semibold text-muted-foreground uppercase tracking-wider">{{ $t('views.SettingsTeamsView.members') }}</h3>

            <div v-if="membersLoading[team.id]" class="flex items-center justify-center py-4">
              <div class="h-5 w-5 animate-spin rounded-full border-2 border-primary border-t-transparent"></div>
            </div>
            <div v-else-if="membersError[team.id]" class="mb-3 text-sm text-destructive">
              {{ membersError[team.id] }}
              <button type="button" class="ml-2 underline" data-testid="settings-teams-members-retry" @click="loadMembers(team.id)">{{ $t('views.SettingsTeamsView.retry') }}</button>
            </div>
            <div v-else>
              <div v-if="membersByTeam[team.id]?.length === 0" class="py-4 text-center text-sm text-muted-foreground">
                No members yet.
              </div>
              <div v-else class="overflow-x-auto">
                <table class="w-full text-sm">
                <thead>
                  <tr class="border-b text-left text-muted-foreground">
                    <th class="pb-2 font-medium">{{ $t('views.SettingsTeamsView.name') }}</th>
                    <th class="pb-2 font-medium">{{ $t('views.SettingsTeamsView.email') }}</th>
                    <th class="pb-2 font-medium">{{ $t('views.SettingsTeamsView.role') }}</th>
                    <th class="pb-2 font-medium">{{ $t('views.SettingsTeamsView.actions') }}</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="member in membersByTeam[team.id]" :key="member.id" class="border-b last:border-0">
                    <td class="py-2">{{ userDisplayName(member.user_id) }}</td>
                    <td class="py-2 text-muted-foreground">{{ userEmail(member.user_id) }}</td>
                    <td class="py-2">
                      <Select
  aria-label="Member role"
  v-model="member.role"
  @update:model-value="changeMemberRole(team.id, member)"
  placeholder="Select role"
  data-testid="settings-teams-member-role"
  :options="[{ value: 'viewer', label: $t('views.SettingsTeamsView.viewer') }, { value: 'runner', label: $t('views.SettingsTeamsView.runner') }, { value: 'operator', label: $t('views.SettingsTeamsView.operator') }]"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
                    </td>
                    <td class="py-2 text-right">
                      <TableActions :actions="memberActions(team.id, member)" />
                    </td>
                  </tr>
                </tbody>
              </table>
              </div>
            </div>

            <div v-if="addMemberTeamId === team.id" class="mt-4 flex items-center gap-2 rounded-lg border bg-muted/30 p-3">
              <Select
  aria-label="Select user"
  v-model="addMemberUserId"
  placeholder="Select a user..."
  data-testid="settings-teams-add-member-user"
  class="flex-1"
  :options="availableUsers(team.id).map(user => ({ value: user.id, label: user.display_name + '(' + user.email + ')' }))"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
              <Select
  aria-label="Select role"
  v-model="addMemberRole"
  placeholder="Select role"
  data-testid="settings-teams-add-member-role"
  :options="[{ value: 'viewer', label: $t('views.SettingsTeamsView.viewer') }, { value: 'runner', label: $t('views.SettingsTeamsView.runner') }, { value: 'operator', label: $t('views.SettingsTeamsView.operator') }]"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
              <Button :disabled="!addMemberUserId || addingMember" data-testid="settings-teams-add-member-submit" @click="addMember(team.id)">
                {{ addingMember ? 'Adding...' : 'Add' }}
              </Button>
              <button type="button" class="rounded-lg border border-input bg-background px-3 py-2 text-sm font-medium hover:bg-accent" data-testid="settings-teams-add-member-cancel" @click="addMemberTeamId = null">
                Cancel
              </button>
            </div>

            <button type="button" v-else class="mt-3 flex items-center gap-1 text-sm text-primary hover:underline" data-testid="settings-teams-add-member" @click="addMemberTeamId = team.id">
              <svg class="h-4 w-4" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M5 12h14" /><path d="M12 5v14" />
              </svg>
              Add member
            </button>

            <div v-if="memberActionError[team.id]" class="mt-2 text-sm text-destructive">
              {{ memberActionError[team.id] }}
            </div>

            <h3 class="mb-3 mt-6 text-sm font-semibold text-muted-foreground uppercase tracking-wider">{{ $t('views.SettingsTeamsView.webhook_notifications') }}</h3>
            <TeamNotificationEndpoints :team-id="team.id" />
          </section>
        </div>
      </div>
    </div>
    </div>
  </FeatureGate>
</template>

<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount } from 'vue'
import { useDataFetch } from '../composables/useDataFetch'
import Button from 'primevue/button'
import TableActions from '../components/shared/TableActions.vue'
import { api } from '../lib/api/client'
import type { components } from '../lib/api/client'
import PageHeader from '../components/shared/PageHeader.vue'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import TeamNotificationEndpoints from '../components/TeamNotificationEndpoints.vue'
import FeatureGate from '../components/FeatureGate.vue'
import { usePlanStore } from '../stores/planStore'
import { formatApiError } from '../lib/api/formatError'
import { shortId } from '../utils/format'
import Select from 'primevue/select'

const planStore = usePlanStore()

type AdminTeamItem = components['schemas']['AdminTeamItem']
type MembershipResponse = components['schemas']['MembershipResponse']
interface AdminUserListItem {
  id: string
  display_name: string | null
  email: string
}

const { loading, error, data: teams, load: loadTeams } = useDataFetch<AdminTeamItem[]>(
  async () => {
    const res = await api.GET('/api/v1/admin/teams')
    if (res.error) return { error: res.error }
    return { data: res.data.items }
  },
  { initialValue: [] as AdminTeamItem[] }
)
const users = ref<AdminUserListItem[]>([])

const expandedTeamId = ref<string | null>(null)
const membersByTeam = ref<Record<string, MembershipResponse[]>>({})
const membersLoading = ref<Record<string, boolean>>({})
const membersError = ref<Record<string, string | null>>({})
const memberActionError = ref<Record<string, string | null>>({})

const showCreateForm = ref(false)
const createName = ref('')
const createDescription = ref('')
const creatingTeam = ref(false)
const createError = ref<string | null>(null)
const createSuccess = ref<string | null>(null)
let teamsCreateTimeout: ReturnType<typeof setTimeout> | null = null

const renameTeamId = ref<string | null>(null)
const renameName = ref('')
const renamingTeam = ref(false)

const deleteConfirmTeamId = ref<string | null>(null)
const deletingTeam = ref(false)
const deleteError = ref<string | null>(null)

const addMemberTeamId = ref<string | null>(null)
const addMemberUserId = ref('')
const addMemberRole = ref('viewer')
const addingMember = ref(false)

const userMap = ref<Record<string, AdminUserListItem>>({})

function userDisplayName(userId: string): string {
  return userMap.value[userId]?.display_name ?? shortId(userId)
}

function userEmail(userId: string): string {
  return userMap.value[userId]?.email ?? ''
}

function availableUsers(teamId: string): AdminUserListItem[] {
  const memberIds = new Set((membersByTeam.value[teamId] ?? []).map(m => m.user_id))
  return users.value.filter(u => !memberIds.has(u.id))
}

async function loadUsers() {
  try {
    const { data, error: err } = await api.GET('/api/v1/admin/users')
    if (!err && data) {
      users.value = data.items
      for (const user of data.items) {
        userMap.value[user.id] = user
      }
    }
  } catch (e) {
    console.warn('Failed to load users', e)
  }
}

async function loadMembers(teamId: string) {
  membersLoading.value[teamId] = true
  membersError.value[teamId] = null
  try {
    const { data, error: err } = await api.GET('/api/v1/teams/{team_id}/members', {
      params: { path: { team_id: teamId } },
    })
    if (err) {
      membersError.value[teamId] = `Failed to load members: ${formatApiError(err)}`
    } else if (data) {
      membersByTeam.value[teamId] = data.items
    }
  } catch (e: unknown) {
    membersError.value[teamId] = `Failed to load members: ${formatApiError(e)}`
  } finally {
    membersLoading.value[teamId] = false
  }
}

function toggleExpand(teamId: string) {
  if (expandedTeamId.value === teamId) {
    expandedTeamId.value = null
    addMemberTeamId.value = null
    renameTeamId.value = null
    deleteConfirmTeamId.value = null
  } else {
    expandedTeamId.value = teamId
    renameTeamId.value = null
    deleteConfirmTeamId.value = null
    addMemberTeamId.value = null
    if (!membersByTeam.value[teamId]) {
      loadMembers(teamId)
    }
  }
}

function cancelCreate() {
  showCreateForm.value = false
  createName.value = ''
  createDescription.value = ''
  createError.value = null
  createSuccess.value = null
}

async function createTeam() {
  if (!createName.value.trim()) return
  creatingTeam.value = true
  createError.value = null
  createSuccess.value = null
  try {
    const { data, error: err } = await api.POST('/api/v1/admin/teams', {
      body: {
        name: createName.value.trim(),
        description: createDescription.value.trim() || null,
      },
    })
    if (err) {
      createError.value = formatApiError(err)
    } else if (data) {
      createSuccess.value = `Team "${data.name}" created.`
      createName.value = ''
      createDescription.value = ''
      await loadTeams()
      if (teamsCreateTimeout) clearTimeout(teamsCreateTimeout)
      teamsCreateTimeout = setTimeout(() => { createSuccess.value = null; showCreateForm.value = false }, 1500)
    }
  } catch (e: unknown) {
    createError.value = formatApiError(e)
  } finally {
    creatingTeam.value = false
  }
}

function startRename(team: AdminTeamItem) {
  renameTeamId.value = team.id
  renameName.value = team.name
  deleteConfirmTeamId.value = null
  addMemberTeamId.value = null
}

function cancelRename() {
  renameTeamId.value = null
  renameName.value = ''
}

async function saveRename() {
  if (!renameTeamId.value || !renameName.value.trim()) return
  renamingTeam.value = true
  try {
    const team = teams.value.find(t => t.id === renameTeamId.value)
    const { error: err } = await api.PUT('/api/v1/admin/teams/{team_id}', {
      params: { path: { team_id: renameTeamId.value } },
      body: {
        name: renameName.value.trim(),
        expected_updated_at: team?.updated_at || undefined,
      },
    })
    if (err) {
      memberActionError.value[renameTeamId.value] = `Rename failed: ${formatApiError(err)}`
    } else {
      renameTeamId.value = null
      renameName.value = ''
      await loadTeams()
    }
  } catch (e: unknown) {
    memberActionError.value[renameTeamId.value ?? ''] = `Rename failed: ${formatApiError(e)}`
  } finally {
    renamingTeam.value = false
  }
}

function confirmDelete(team: AdminTeamItem) {
  deleteConfirmTeamId.value = team.id
  renameTeamId.value = null
  addMemberTeamId.value = null
  deleteError.value = null
}

async function deleteTeam(teamId: string) {
  deletingTeam.value = true
  deleteError.value = null
  try {
    const { error: err, response } = await api.DELETE('/api/v1/admin/teams/{team_id}', {
      params: { path: { team_id: teamId } },
    })
    if (err) {
      deleteError.value = formatApiError(err)
    } else if (response.status === 204 || response.ok) {
      deleteConfirmTeamId.value = null
      expandedTeamId.value = null
      await loadTeams()
    }
  } catch (e: unknown) {
    deleteError.value = formatApiError(e)
  } finally {
    deletingTeam.value = false
  }
}

async function addMember(teamId: string) {
  if (!addMemberUserId.value) return
  addingMember.value = true
  memberActionError.value[teamId] = ''
  try {
    const { data, error: err } = await api.POST('/api/v1/teams/{team_id}/members', {
      params: { path: { team_id: teamId } },
      body: {
        user_id: addMemberUserId.value,
        role: addMemberRole.value,
      },
    })
    if (err) {
      memberActionError.value[teamId] = `Add member failed: ${formatApiError(err)}`
    } else if (data) {
      membersByTeam.value[teamId] = [...(membersByTeam.value[teamId] ?? []), data]
      addMemberUserId.value = ''
      addMemberRole.value = 'viewer'
      addMemberTeamId.value = null
      const team = teams.value.find(t => t.id === teamId)
      if (team) team.member_count++
    }
  } catch (e: unknown) {
    memberActionError.value[teamId] = `Add member failed: ${formatApiError(e)}`
  } finally {
    addingMember.value = false
  }
}

async function changeMemberRole(teamId: string, member: MembershipResponse) {
  memberActionError.value[teamId] = ''
  try {
    const { data, error: err } = await api.PATCH('/api/v1/teams/{team_id}/members/{membership_id}', {
      params: { path: { team_id: teamId, membership_id: member.id } },
      body: { role: member.role },
    })
    if (err) {
      memberActionError.value[teamId] = `Role change failed: ${formatApiError(err)}`
      await loadMembers(teamId)
    } else if (data) {
      membersByTeam.value[teamId] = membersByTeam.value[teamId].map(m => m.id === data.id ? data : m)
    }
  } catch (e: unknown) {
    memberActionError.value[teamId] = `Role change failed: ${formatApiError(e)}`
    await loadMembers(teamId)
  }
}

async function removeMember(teamId: string, member: MembershipResponse) {
  memberActionError.value[teamId] = ''
  try {
    const { error: err, response } = await api.DELETE('/api/v1/teams/{team_id}/members/{membership_id}', {
      params: { path: { team_id: teamId, membership_id: member.id } },
    })
    if (err) {
      memberActionError.value[teamId] = `Remove failed: ${formatApiError(err)}`
    } else if (response.status === 204 || response.ok) {
      membersByTeam.value[teamId] = membersByTeam.value[teamId].filter(m => m.id !== member.id)
      const team = teams.value.find(t => t.id === teamId)
      if (team) team.member_count--
    }
  } catch (e: unknown) {
    memberActionError.value[teamId] = `Remove failed: ${formatApiError(e)}`
  }
}

onBeforeUnmount(() => {
  if (teamsCreateTimeout) clearTimeout(teamsCreateTimeout)
})

function teamActions(team: AdminTeamItem) {
  return [
    {
      key: 'rename',
      label: 'Rename',
      onClick: () => startRename(team),
    },
    {
      key: 'delete',
      label: 'Delete',
      onClick: () => confirmDelete(team),
      danger: true,
    },
  ]
}

function memberActions(teamId: string, member: MembershipResponse) {
  return [
    {
      key: 'remove',
      label: 'Remove',
      onClick: () => removeMember(teamId, member),
      danger: true,
    },
  ]
}

onMounted(async () => {
  planStore.fetchPlan()
  await loadUsers()
})
</script>
