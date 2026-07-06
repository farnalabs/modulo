<template>
  <div class="p-6 max-w-5xl mx-auto space-y-6">
    <div class="flex items-center justify-between">
      <div>
        <h1 class="text-2xl font-bold tracking-tight">Users</h1>
        <p class="text-muted-foreground mt-1">{{ $t('views.AdminUsersView.manage_user_accounts_and_permissions') }}</p>
      </div>
      <button
        @click="showCreate = true"
        data-testid="admin-users-add-user"
        class="px-4 py-2 bg-primary text-primary-foreground text-sm font-medium rounded-lg border border-primary/30 hover:brightness-110 transition-all"
      >
        + Add User
      </button>
    </div>

    <LoadingSpinner v-if="loading" />

    <div v-else-if="error" class="rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-destructive text-sm">
      {{ error }}
    </div>

    <div v-else-if="users.length === 0" class="card p-8 text-center">
      <p class="text-lg font-medium">{{ $t('views.AdminUsersView.no_users_found') }}</p>
      <p class="mt-1 text-sm text-muted-foreground">
        Users will appear here once they are created or sign up.
      </p>
    </div>

    <div v-else class="card overflow-hidden">
      <table class="w-full text-sm">
        <thead>
          <tr class="border-b bg-muted/30">
            <th class="text-left px-4 py-3 font-medium text-muted-foreground">User</th>
            <th class="text-left px-4 py-3 font-medium text-muted-foreground">Role</th>
            <th class="text-left px-4 py-3 font-medium text-muted-foreground">Status</th>
            <th class="text-left px-4 py-3 font-medium text-muted-foreground">Auth</th>
            <th class="text-right px-4 py-3 font-medium text-muted-foreground">Created</th>
            <th class="text-right px-4 py-3 font-medium text-muted-foreground">Actions</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="u in users" :key="u.id" class="border-b last:border-0 hover:bg-muted/20 transition-colors">
            <td class="px-4 py-3">
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
            <td class="px-4 py-3">
              <select
                v-model="u.org_role"
                :data-testid="`admin-users-role-${u.id}`"
                class="text-xs border border-input bg-background rounded-md px-2 py-1"
                @change="updateRole(u)"
                @focus="captureRole(u.org_role)"
              >
                <option value="admin">Admin</option>
                <option value="operator">Operator</option>
                <option value="runner">Runner</option>
                <option value="viewer">Viewer</option>
              </select>
            </td>
            <td class="px-4 py-3">
              <span v-if="u.is_active" class="inline-flex items-center gap-1.5 rounded-full bg-success/10 px-2.5 py-0.5 text-xs font-medium text-success">
                <span class="h-1.5 w-1.5 rounded-full bg-success" />
                Active
              </span>
              <span v-else class="inline-flex items-center gap-1.5 rounded-full bg-destructive/10 px-2.5 py-0.5 text-xs font-medium text-destructive">
                <span class="h-1.5 w-1.5 rounded-full bg-destructive" />
                Inactive
              </span>
            </td>
            <td class="px-4 py-3 text-xs text-muted-foreground">{{ u.auth_provider }}</td>
            <td class="px-4 py-3 text-right text-xs text-muted-foreground">
              {{ u.created_at ? new Date(u.created_at).toLocaleDateString() : '—' }}
            </td>
            <td class="px-4 py-3 text-right">
              <div class="flex items-center justify-end gap-2">
                <button
                  :data-testid="`admin-users-reset-password-${u.id}`"
                  @click="resetPassword(u)"
                  :disabled="actionLoading[u.id]"
                  class="text-xs text-muted-foreground hover:text-foreground hover:underline disabled:opacity-30"
                >
                  {{ actionLoading[u.id] ? '...' : 'Reset Password' }}
                </button>
                <button
                  v-if="u.is_active"
                  :data-testid="`admin-users-deactivate-${u.id}`"
                  @click="deactivate(u)"
                  :disabled="actionLoading[u.id]"
                  class="text-xs text-destructive hover:underline disabled:opacity-30"
                >
                  {{ actionLoading[u.id] ? '...' : 'Deactivate' }}
                </button>
                <button
                  v-else
                  :data-testid="`admin-users-reactivate-${u.id}`"
                  @click="reactivate(u)"
                  :disabled="actionLoading[u.id]"
                  class="text-xs text-success hover:underline disabled:opacity-30"
                >
                  {{ actionLoading[u.id] ? '...' : 'Reactivate' }}
                </button>
              </div>
            </td>
          </tr>
        </tbody>
      </table>

      <div v-if="total > pageSize" class="flex justify-center items-center gap-2 py-4 border-t border-border">
        <button
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
        <button
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

    <div v-if="showCreate" class="fixed inset-0 z-50 flex items-center justify-center bg-black/50" @click.self="showCreate = false">
      <div class="bg-background rounded-xl border shadow-lg p-6 w-full max-w-md mx-4 space-y-4">
        <h2 class="text-lg font-semibold">{{ $t('views.AdminUsersView.create_user') }}</h2>
        <form @submit.prevent="createUser">
          <div>
            <label class="block text-sm font-medium mb-1">Email</label>
            <input v-model="newUser.email" data-testid="admin-users-create-email" type="email" class="w-full px-3 py-2 border border-input bg-background rounded-lg text-sm" required />
          </div>
          <div>
            <label class="block text-sm font-medium mb-1">{{ $t('views.AdminModelBackendsView.display_name') }}</label>
            <input v-model="newUser.display_name" data-testid="admin-users-create-display-name" type="text" class="w-full px-3 py-2 border border-input bg-background rounded-lg text-sm" required />
          </div>
          <div>
            <label class="block text-sm font-medium mb-1">Password</label>
            <input v-model="newUser.password" data-testid="admin-users-create-password" type="password" class="w-full px-3 py-2 border border-input bg-background rounded-lg text-sm" minlength="8" required />
          </div>
          <div>
            <label class="block text-sm font-medium mb-1">Role</label>
            <select v-model="newUser.org_role" data-testid="admin-users-create-role" class="w-full px-3 py-2 border border-input bg-background rounded-lg text-sm">
              <option value="runner">Runner</option>
              <option value="operator">Operator</option>
              <option value="admin">Admin</option>
              <option value="viewer">Viewer</option>
            </select>
          </div>
          <p v-if="createError" class="text-sm text-destructive">{{ createError }}</p>
          <div class="flex justify-end gap-2 pt-2">
            <button type="button" @click="showCreate = false" data-testid="admin-users-cancel" class="px-4 py-2 border border-input bg-background rounded-lg text-sm">Cancel</button>
            <button type="submit" data-testid="admin-users-create" class="px-4 py-2 bg-primary text-primary-foreground text-sm font-medium rounded-lg">Create</button>
          </div>
        </form>
      </div>
    </div>

    <div v-if="showResetDialog" class="fixed inset-0 z-50 flex items-center justify-center bg-black/50" @click.self="showResetDialog = false">
      <div class="bg-background rounded-xl border shadow-lg p-6 w-full max-w-md mx-4 space-y-4">
        <h2 class="text-lg font-semibold">{{ $t('views.AdminUsersView.password_reset') }}</h2>
        <p class="text-sm text-muted-foreground">
          A temporary password has been generated for <strong>{{ resetUserEmail }}</strong>.
          Share this password with the user — they will be prompted to change it on next login.
        </p>
        <div class="flex items-center gap-2 bg-muted rounded-lg px-4 py-3">
          <code class="flex-1 text-sm font-mono break-all">{{ tempPassword }}</code>
          <button
            @click="copyPassword"
            data-testid="admin-users-copy-password"
            class="shrink-0 px-3 py-1.5 text-xs bg-primary text-primary-foreground font-medium rounded-md hover:brightness-110 transition-all"
          >
            {{ copied ? 'Copied!' : 'Copy' }}
          </button>
        </div>
        <div class="flex justify-end pt-2">
          <button
            @click="showResetDialog = false"
            data-testid="admin-users-reset-done"
            class="px-4 py-2 bg-primary text-primary-foreground text-sm font-medium rounded-lg"
          >
            Done
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount } from 'vue'
import { useApi } from '../composables/useApi'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'

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

const { get, put: httpPut, post } = useApi()

const users = ref<UserItem[]>([])
const loading = ref(true)
const error = ref<string | null>(null)
const page = ref(1)
const pageSize = ref(50)
const total = ref(0)
const showCreate = ref(false)
const createError = ref('')
const newUser = ref({ email: '', display_name: '', password: '', org_role: 'runner' })
const showResetDialog = ref(false)
const tempPassword = ref('')
const resetUserEmail = ref('')
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

async function loadUsers() {
  loading.value = true
  error.value = null
  try {
    const data = await get<UserListResponse>(`/api/v1/admin/users?page=${page.value}&page_size=${pageSize.value}`)
    if (data && Array.isArray(data.items)) {
      users.value = data.items
      total.value = data.total ?? 0
    } else {
      users.value = []
      total.value = 0
    }
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Failed to load users'
  } finally {
    loading.value = false
  }
}

const selectedRole = ref<string>('')
function captureRole(role: string) {
  selectedRole.value = role
}

async function updateRole(u: UserItem) {
  const prevRole = selectedRole.value
  actionLoading.value[u.id] = true
  try {
    const data = await httpPut<UserItem>(`/api/v1/admin/users/${u.id}`, { org_role: u.org_role })
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
    tempPassword.value = data.temporary_password
    resetUserEmail.value = u.email
    copied.value = false
    showResetDialog.value = true
  } catch {
    showFlash('error', 'Failed to reset password')
  } finally {
    actionLoading.value[u.id] = false
  }
}

function copyPassword() {
  navigator.clipboard.writeText(tempPassword.value)
  copied.value = true
  if (copyTimeout) clearTimeout(copyTimeout)
  copyTimeout = setTimeout(() => { copied.value = false }, 2000)
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
  try {
    await post('/api/v1/admin/users', newUser.value)
    showCreate.value = false
    newUser.value = { email: '', display_name: '', password: '', org_role: 'runner' }
    showFlash('success', `User ${email} created`)
    loadUsers()
  } catch (e: any) {
    createError.value = e instanceof Error ? e.message : 'Failed to create user'
  }
}

onBeforeUnmount(() => {
  if (copyTimeout) clearTimeout(copyTimeout)
  if (flashTimeout) clearTimeout(flashTimeout)
})
onMounted(loadUsers)
</script>
