<template>
  <div class="p-6 max-w-5xl mx-auto space-y-6">
    <div class="flex items-center justify-between">
      <div>
        <h1 class="text-2xl font-bold tracking-tight">Users</h1>
        <p class="text-muted-foreground mt-1">Manage user accounts and permissions.</p>
      </div>
      <button
        @click="showCreate = true"
        data-testid="admin-users-add-user"
        class="px-4 py-2 bg-primary text-primary-foreground text-sm font-medium rounded-lg border border-primary/30 hover:brightness-110 transition-all"
      >
        + Add User
      </button>
    </div>

    <div v-if="error" class="rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-destructive text-sm">
      {{ error }}
    </div>

    <div class="card overflow-hidden">
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
              <button
                v-if="u.is_active"
                :data-testid="`admin-users-deactivate-${u.id}`"
                @click="deactivate(u)"
                class="text-xs text-destructive hover:underline"
              >
                Deactivate
              </button>
              <button
                v-else
                :data-testid="`admin-users-reactivate-${u.id}`"
                @click="reactivate(u)"
                class="text-xs text-success hover:underline"
              >
                Reactivate
              </button>
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

    <div v-if="showCreate" class="fixed inset-0 z-50 flex items-center justify-center bg-black/50" @click.self="showCreate = false">
      <div class="bg-background rounded-xl border shadow-lg p-6 w-full max-w-md mx-4 space-y-4">
        <h2 class="text-lg font-semibold">Create User</h2>
        <div>
          <label class="block text-sm font-medium mb-1">Email</label>
          <input v-model="newUser.email" data-testid="admin-users-create-email" type="email" class="w-full px-3 py-2 border border-input bg-background rounded-lg text-sm" required />
        </div>
        <div>
          <label class="block text-sm font-medium mb-1">Display Name</label>
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
          <button @click="showCreate = false" data-testid="admin-users-cancel" class="px-4 py-2 border border-input bg-background rounded-lg text-sm">Cancel</button>
          <button @click="createUser" data-testid="admin-users-create" class="px-4 py-2 bg-primary text-primary-foreground text-sm font-medium rounded-lg">Create</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useApi } from '../composables/useApi'

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
const error = ref('')
const page = ref(1)
const pageSize = ref(50)
const total = ref(0)
const showCreate = ref(false)
const createError = ref('')
const newUser = ref({ email: '', display_name: '', password: '', org_role: 'runner' })

function initialOf(name: string): string {
  return name ? name.charAt(0).toUpperCase() : '?'
}

async function loadUsers() {
  error.value = ''
  try {
    const data = await get<UserListResponse>(`/api/v1/admin/users?page=${page.value}&page_size=${pageSize.value}`)
    users.value = data.items
    total.value = data.total
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Failed to load users'
  }
}

async function updateRole(u: UserItem) {
  try {
    await httpPut(`/api/v1/admin/users/${u.id}`, { org_role: u.org_role })
  } catch {
    loadUsers()
  }
}

async function deactivate(u: UserItem) {
  try {
    await post(`/api/v1/admin/users/${u.id}/deactivate`)
    u.is_active = false
  } catch {
    loadUsers()
  }
}

async function reactivate(u: UserItem) {
  try {
    await post(`/api/v1/admin/users/${u.id}/reactivate`)
    u.is_active = true
  } catch {
    loadUsers()
  }
}

async function createUser() {
  createError.value = ''
  try {
    await post('/api/v1/admin/users', newUser.value)
    showCreate.value = false
    newUser.value = { email: '', display_name: '', password: '', org_role: 'runner' }
    loadUsers()
  } catch (e: any) {
    createError.value = e instanceof Error ? e.message : 'Failed to create user'
  }
}

onMounted(loadUsers)
</script>
