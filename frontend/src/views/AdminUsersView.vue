<template>
  <FeatureGate feature-name="user_management" required-tier="community" show-disabled>
  <div class="page-wide">
    <div class="flex items-center justify-between">
      <PageHeader title="Users" :subtitle="$t('views.AdminUsersView.manage_user_accounts_and_permissions')" />
      <Button class="border-primary/30" data-testid="admin-users-add-user" @click="showCreate = true">
        + Add User
      </Button>
    </div>

    <LoadingSpinner v-if="loading" />

    <div v-else-if="error" class="rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-destructive text-sm">
      {{ error }}
    </div>

    <EmptyState
      v-else-if="users.length === 0"
      :title="$t('views.AdminUsersView.no_users_found')"
      :description="$t('views.AdminUsersView.empty_state_description')"
    />

    <div v-else class="table-wrapper overflow-x-auto">
      <table class="w-full text-sm">
        <thead>
          <tr>
            <th class="table-header">{{ $t('views.AdminUsersView.user') }}</th>
            <th class="table-header">{{ $t('views.AdminUsersView.role') }}</th>
            <th class="table-header capitalize">{{ $t('views.AdminUsersView.status') }}</th>
            <th class="table-header">{{ $t('views.AdminUsersView.auth') }}</th>
            <th class="table-header">{{ $t('views.AdminUsersView.last_login') }}</th>
            <th class="table-header table-cell-numeric">{{ $t('views.AdminUsersView.created') }}</th>
            <th class="table-header table-cell-numeric">{{ $t('views.AdminUsersView.actions') }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="u in users" :key="u.id" class="border-b last:border-0 hover:bg-muted/20 transition-colors">
            <td class="table-cell">
              <div class="flex items-center gap-2">
                <div class="avatar-ring">
                  <div class="flex h-7 w-7 items-center justify-center rounded-full bg-primary text-xs font-bold text-primary-foreground">
                    {{ initialOf(u.display_name || u.email) }}
                  </div>
                </div>
                <div>
                  <span class="font-medium">{{ u.display_name || u.email }}</span>
                  <span class="block text-xs text-muted-foreground">{{ u.email }}</span>
                </div>
              </div>
            </td>
            <td class="table-cell">
              <Select
  aria-label="User role"
  :model-value="u.org_role"
  @update:model-value="updateRole(u, $event)"
  placeholder="Select role"
  :data-testid="`admin-users-role-${u.id}`"
  :options="[{ value: 'admin', label: $t('views.AdminUsersView.admin') }, { value: 'operator', label: $t('views.AdminUsersView.operator') }, { value: 'runner', label: $t('views.AdminUsersView.runner') }, { value: 'viewer', label: $t('views.AdminUsersView.viewer') }]"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
            </td>
            <td class="table-cell">
              <span v-if="u.is_active" class="inline-flex items-center gap-1.5 rounded-full bg-success/10 px-2.5 py-0.5 text-xs font-medium text-success">
                <span class="h-1.5 w-1.5 rounded-full bg-success" />
                Active
              </span>
              <span v-else class="inline-flex items-center gap-1.5 rounded-full bg-destructive/10 px-2.5 py-0.5 text-xs font-medium text-destructive">
                <span class="h-1.5 w-1.5 rounded-full bg-destructive" />
                Inactive
              </span>
            </td>
            <td class="table-cell text-xs text-muted-foreground">{{ u.auth_provider }}</td>
            <td class="table-cell">
              <span v-if="!u.last_login" class="text-xs text-muted-foreground italic">{{ $t('views.AdminUsersView.never_logged_in') }}</span>
              <span v-else class="text-xs text-muted-foreground" :title="formatDateShortWithTime(new Date(u.last_login))">
                {{ formatRelativeTime(u.last_login) }}
              </span>
            </td>
            <td class="table-cell-numeric text-xs text-muted-foreground">
              {{ u.created_at ? formatDateShort(new Date(u.created_at)) : '—' }}
            </td>
            <td class="table-cell-numeric">
              <TableActions :actions="rowActions(u)" />
            </td>
          </tr>
        </tbody>
      </table>

      <div v-if="total > pageSize" class="flex justify-center items-center gap-2 py-4 border-t border-border">
        <button type="button"
          :disabled="page <= 1"
          data-testid="admin-users-previous"
          class="px-3 py-1.5 text-sm border border-input bg-background rounded-lg disabled:opacity-30 hover:bg-accent transition-colors"
          @click="page--; loadUsers()"
        >
          Previous
        </button>
        <span class="text-sm text-muted-foreground">
          Page {{ page }} of {{ Math.ceil(total / pageSize) }}
        </span>
        <button type="button"
          :disabled="page >= Math.ceil(total / pageSize)"
          data-testid="admin-users-next"
          class="px-3 py-1.5 text-sm border border-input bg-background rounded-lg disabled:opacity-30 hover:bg-accent transition-colors"
          @click="page++; loadUsers()"
        >
          Next
        </button>
      </div>
    </div>

    <div v-if="flashMessage" :class="['rounded-lg border px-4 py-3 text-sm', flashMessage.type === 'success' ? 'border-success/50 bg-success/10 text-success' : 'border-destructive/50 bg-destructive/10 text-destructive']">
      {{ flashMessage.text }}
    </div>

    <FormDialog
      :open="showCreate"
      @update:open="showCreate = false"
      title="Create User"
      confirmText="Create"
      :loading="createLoading"
      :confirmDisabled="createLoading"
      @confirm="createUser"
    >
      <form @submit.prevent="createUser">
        <div>
          <label for="adminusersview-field-4" class="block text-sm font-medium mb-1">{{ $t('common.email') }}</label>
          <input id="adminusersview-field-4" v-model="newUser.email" data-testid="admin-users-create-email" type="email" class="w-full px-3 py-2 border border-input bg-background rounded-lg text-sm" required />
        </div>
        <div>
          <label for="adminusersview-field-3" class="block text-sm font-medium mb-1">{{ $t('views.AdminModelBackendsView.display_name') }}</label>
          <input id="adminusersview-field-3" v-model="newUser.display_name" data-testid="admin-users-create-display-name" type="text" class="w-full px-3 py-2 border border-input bg-background rounded-lg text-sm" required />
        </div>
        <div>
          <label for="adminusersview-field-2" class="block text-sm font-medium mb-1">{{ $t('common.password') }}</label>
          <div class="flex gap-2">
            <input id="adminusersview-field-2" v-model="newUser.password" data-testid="admin-users-create-password" type="password" class="w-full px-3 py-2 border border-input bg-background rounded-lg text-sm" minlength="8" required />
            <Button type="button" severity="secondary" outlined class="shrink-0 border-primary/30" data-testid="admin-users-generate-password" @click="generatePassword">
              {{ $t('views.AdminUsersView.generate_password') }}
            </Button>
          </div>
        </div>
        <div>
          <label for="adminusersview-field-1" class="block text-sm font-medium mb-1">{{ $t('views.AdminUsersView.role') }}</label>
          <Select
  aria-label="Role"
  v-model="newUser.org_role"
  placeholder="Select role"
  data-testid="admin-users-create-role"
  class="w-full"
  :options="[{ value: 'runner', label: $t('views.AdminUsersView.runner') }, { value: 'operator', label: $t('views.AdminUsersView.operator') }, { value: 'admin', label: $t('views.AdminUsersView.admin') }, { value: 'viewer', label: $t('views.AdminUsersView.viewer') }]"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
        </div>
        <p v-if="createError" class="text-sm text-destructive">{{ createError }}</p>
        <button type="submit" hidden>{{ $t('common.create') }}</button>
      </form>
    </FormDialog>

    <Dialog :visible="showCredentialDialog" :modal="true" :dismissable-mask="true" :style="{ width: '28rem' }" @update:visible="showCredentialDialog = false">
      <template #header>
        <div class="text-lg font-semibold">{{ credentialTitle }}</div>
      </template>
      <p v-if="credentialMode === 'reset'" class="text-sm text-muted-foreground">
        {{ $t('views.AdminUsersView.credential_body_reset', { email: credentialEmail }) }}
      </p>
      <p v-else class="text-sm text-muted-foreground">
        {{ $t('views.AdminUsersView.credential_body_created', { email: credentialEmail }) }}
      </p>
      <div class="flex items-center gap-2 bg-muted rounded-lg px-4 py-3 mt-2">
        <code class="flex-1 text-sm font-mono break-all">{{ credentialPassword }}</code>
        <Button class="shrink-0" data-testid="admin-users-copy-password" @click="copyPassword">
          {{ copied ? 'Copied!' : 'Copy' }}
        </Button>
      </div>
      <template #footer>
        <div class="flex justify-end">
          <Button data-testid="admin-users-reset-done" @click="showCredentialDialog = false">
            Done
          </Button>
        </div>
      </template>
    </Dialog>
  </div>
  </FeatureGate>
</template>

<script setup lang="ts">
import PageHeader from '../components/shared/PageHeader.vue'
import { ref, computed, onBeforeUnmount } from 'vue'
import { useI18n } from 'vue-i18n'
import { useApi } from '../composables/useApi'
import { useDataFetch } from '../composables/useDataFetch'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import Button from 'primevue/button'
import EmptyState from '../components/shared/EmptyState.vue'
import FormDialog from '../components/shared/FormDialog.vue'
import Dialog from 'primevue/dialog'
import TableActions from '../components/shared/TableActions.vue'
import FeatureGate from '../components/FeatureGate.vue'
import { formatDateShort, formatDateShortWithTime, formatRelativeTime } from '../lib/formatDate'
import { generateStrongPassword } from '../utils/password'
import Select from 'primevue/select'

interface UserItem {
  id: string
  email: string
  display_name: string
  org_role: string
  is_active: boolean
  auth_provider: string
  created_at: string
  last_login: string | null
}

interface UserListResponse {
  items: UserItem[]
  total: number
  page: number
  page_size: number
}

const { t } = useI18n()
const { get, put: httpPut, post } = useApi()

const page = ref(1)
const pageSize = ref(50)

const { data: usersResp, loading, error, load: loadUsers } = useDataFetch(
  () => get<UserListResponse>(`/api/v1/admin/users?page=${page.value}&page_size=${pageSize.value}`).then(d => ({ data: d })),
  { initialValue: { items: [] as UserItem[], total: 0, page: 1, page_size: 50 } as UserListResponse }
)

const users = computed(() => usersResp.value?.items ?? [])
const total = computed(() => usersResp.value?.total ?? 0)
const showCreate = ref(false)
const createError = ref('')
const createLoading = ref(false)
const newUser = ref({ email: '', display_name: '', password: '', org_role: 'runner' })

// FAR-460: one reusable credential dialog shared by reset-password and
// create-user so the admin can copy the credential exactly once.
type CredentialMode = 'reset' | 'created'
const showCredentialDialog = ref(false)
const credentialMode = ref<CredentialMode>('reset')
const credentialEmail = ref('')
const credentialPassword = ref('')
const credentialTitle = computed(() =>
  credentialMode.value === 'reset'
    ? t('views.AdminUsersView.password_reset')
    : t('views.AdminUsersView.credentials')
)
const copied = ref(false)
const flashMessage = ref<{ type: 'success' | 'error'; text: string } | null>(null)
const actionLoading = ref<Record<string, boolean>>({})
let copyTimeout: ReturnType<typeof setTimeout> | null = null
let flashTimeout: ReturnType<typeof setTimeout> | null = null

function initialOf(name: string): string {
  return name ? name.charAt(0).toUpperCase() : '?'
}

function showFlash(type: 'success' | 'error', text: string) {
  flashMessage.value = { type, text }
  if (flashTimeout) clearTimeout(flashTimeout)
  flashTimeout = setTimeout(() => { flashMessage.value = null }, 4000)
}

function updateUserInList(data: UserItem) {
  const idx = users.value.findIndex(x => x.id === data.id)
  if (idx !== -1) users.value[idx] = data
}

async function updateRole(u: UserItem, newRole: unknown) {
  const prevRole = u.org_role
  if (prevRole === String(newRole)) return
  u.org_role = String(newRole)
  actionLoading.value[u.id] = true
  try {
    const data = await httpPut<UserItem>(`/api/v1/admin/users/${u.id}`, { org_role: newRole })
    updateUserInList(data)
    showFlash('success', `Role changed to ${data.org_role} for ${u.email}`)
  } catch (e) {
    u.org_role = prevRole
    showFlash('error', e instanceof Error ? e.message : 'Failed to update role')
  } finally {
    actionLoading.value[u.id] = false
  }
}

async function deactivate(u: UserItem) {
  actionLoading.value[u.id] = true
  try {
    const data = await post<UserItem>(`/api/v1/admin/users/${u.id}/deactivate`)
    updateUserInList(data)
    showFlash('success', `User ${u.email} deactivated`)
  } catch (e) {
    showFlash('error', e instanceof Error ? e.message : 'Failed to deactivate user')
  } finally {
    actionLoading.value[u.id] = false
  }
}

async function reactivate(u: UserItem) {
  actionLoading.value[u.id] = true
  try {
    const data = await post<UserItem>(`/api/v1/admin/users/${u.id}/reactivate`)
    updateUserInList(data)
    showFlash('success', `User ${u.email} reactivated`)
  } catch (e) {
    showFlash('error', e instanceof Error ? e.message : 'Failed to reactivate user')
  } finally {
    actionLoading.value[u.id] = false
  }
}

async function resetPassword(u: UserItem) {
  actionLoading.value[u.id] = true
  try {
    const data = await post<{ temporary_password: string }>(`/api/v1/admin/users/${u.id}/reset-password`)
    openCredentialDialog('reset', u.email, data.temporary_password)
  } catch {
    showFlash('error', 'Failed to reset password')
  } finally {
    actionLoading.value[u.id] = false
  }
}

function openCredentialDialog(mode: CredentialMode, email: string, password: string) {
  credentialMode.value = mode
  credentialEmail.value = email
  credentialPassword.value = password
  copied.value = false
  showCredentialDialog.value = true
}

function rowActions(u: UserItem) {
  const actions: { key: string; label: string; onClick: () => void; disabled?: boolean; danger?: boolean }[] = [
    {
      key: 'reset-password',
      label: 'Reset Password',
      onClick: () => resetPassword(u),
      disabled: actionLoading.value[u.id],
    },
  ]
  if (u.is_active) {
    actions.push({
      key: 'deactivate',
      label: 'Deactivate',
      onClick: () => deactivate(u),
      disabled: actionLoading.value[u.id],
      danger: true,
    })
  } else {
    actions.push({
      key: 'reactivate',
      label: 'Reactivate',
      onClick: () => reactivate(u),
      disabled: actionLoading.value[u.id],
    })
  }
  return actions
}

function copyPassword() {
  navigator.clipboard.writeText(credentialPassword.value)
  copied.value = true
  if (copyTimeout) clearTimeout(copyTimeout)
  copyTimeout = setTimeout(() => { copied.value = false }, 2000)
}

function generatePassword() {
  newUser.value.password = generateStrongPassword()
}

async function createUser() {
  createError.value = ''
  const { email, display_name, password } = newUser.value
  if (!email || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
    createError.value = 'Please enter a valid email address'
    return
  }
  if (!display_name || !display_name.trim()) {
    createError.value = 'Display name is required'
    return
  }
  if (!password || password.length < 8) {
    createError.value = 'Password must be at least 8 characters'
    return
  }
  if (!/[A-Z]/.test(password) || !/[a-z]/.test(password) || !/\d/.test(password)) {
    createError.value = 'Password must contain at least one uppercase letter, one lowercase letter, and one digit'
    return
  }
  createLoading.value = true
  try {
    await post('/api/v1/admin/users', newUser.value)
    showCreate.value = false
    // FAR-460: surface the hand-typed credential once before it is discarded.
    openCredentialDialog('created', email, password)
    newUser.value = { ...newUser.value, password: '' }
    showFlash('success', `User ${email} created`)
    loadUsers()
  } catch (e: any) {
    createError.value = e instanceof Error ? e.message : 'Failed to create user'
  } finally {
    createLoading.value = false
  }
}

onBeforeUnmount(() => {
  if (copyTimeout) clearTimeout(copyTimeout)
  if (flashTimeout) clearTimeout(flashTimeout)
})
/* onMounted handled by useDataFetch */
</script>
