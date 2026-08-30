<template>
  <FeatureGate feature-name="user_management" required-tier="community" show-disabled>
  <div class="page-wide">
    <div class="flex items-center justify-between">
      <PageHeader title="Users" :subtitle="$t('views.AdminUsersView.manage_user_accounts_and_permissions')" />
      <Button class="border-primary/30" data-testid="admin-users-add-user" @click="openAddUser">
        {{ $t('views.AdminUsersView.add_user') }}
      </Button>
    </div>

    <LoadingSpinner v-if="loading" />

    <div v-else-if="error" class="rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-destructive text-sm">
      {{ error }}
    </div>

    <EmptyState
      v-else-if="users.length === 0"
      :title="$t('views.AdminUsersView.no_users_found')"
      :description="$t('views.AdminUsersView.users_appear_once_created')"
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
                {{ $t('views.AdminUsersView.active') }}
              </span>
              <span v-else class="inline-flex items-center gap-1.5 rounded-full bg-destructive/10 px-2.5 py-0.5 text-xs font-medium text-destructive">
                <span class="h-1.5 w-1.5 rounded-full bg-destructive" />
                {{ $t('views.AdminUsersView.inactive') }}
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
          {{ $t('views.AdminUsersView.previous') }}
        </button>
        <span class="text-sm text-muted-foreground">
          {{ $t('views.AdminUsersView.page_of', { page, pages: Math.ceil(total / pageSize) }) }}
        </span>
        <button type="button"
          :disabled="page >= Math.ceil(total / pageSize)"
          data-testid="admin-users-next"
          class="px-3 py-1.5 text-sm border border-input bg-background rounded-lg disabled:opacity-30 hover:bg-accent transition-colors"
          @click="page++; loadUsers()"
        >
          {{ $t('views.AdminUsersView.next') }}
        </button>
      </div>
    </div>

    <!-- Pending Invitations (FAR-461) -->
    <section class="mt-8" data-testid="admin-invitations-section">
      <h2 class="text-base font-semibold mb-2">{{ $t('views.AdminUsersView.pending_invitations') }}</h2>
      <LoadingSpinner v-if="invitationsLoading" />
      <p
        v-else-if="invitationsError"
        class="text-sm text-destructive"
        data-testid="admin-invitations-error"
      >
        {{ invitationsError }}
      </p>
      <p
        v-else-if="pendingInvitations.length === 0"
        class="text-sm text-muted-foreground"
        data-testid="admin-invitations-empty"
      >
        {{ $t('views.AdminUsersView.no_pending_invitations') }}
      </p>
      <div v-else class="table-wrapper overflow-x-auto">
        <table class="w-full text-sm">
          <thead>
            <tr>
              <th class="table-header">{{ $t('common.email') }}</th>
              <th class="table-header">{{ $t('views.AdminUsersView.role') }}</th>
              <th class="table-header table-cell-numeric">{{ $t('views.AdminUsersView.invited_col') }}</th>
              <th class="table-header table-cell-numeric">{{ $t('views.AdminUsersView.expires_col') }}</th>
              <th class="table-header table-cell-numeric">{{ $t('views.AdminUsersView.actions') }}</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="inv in pendingInvitations"
              :key="inv.id"
              class="border-b last:border-0 hover:bg-muted/20 transition-colors"
              data-testid="admin-invitations-row"
            >
              <td class="table-cell">
                <span class="font-medium">{{ inv.display_name || inv.email }}</span>
                <span class="block text-xs text-muted-foreground">{{ inv.email }}</span>
              </td>
              <td class="table-cell capitalize">{{ inv.org_role }}</td>
              <td class="table-cell-numeric text-xs text-muted-foreground">
                {{ formatDateShort(new Date(inv.created_at)) }}
              </td>
              <td class="table-cell-numeric text-xs text-muted-foreground">
                {{ formatDateShort(new Date(inv.expires_at)) }}
              </td>
              <td class="table-cell-numeric">
                <Button severity="danger" outlined :disabled="actionLoading[inv.id]" @click="openRevoke(inv)">
                  {{ $t('views.AdminUsersView.revoke_invitation') }}
                </Button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <div v-if="flashMessage" :class="['rounded-lg border px-4 py-3 text-sm', flashMessage.type === 'success' ? 'border-success/50 bg-success/10 text-success' : 'border-destructive/50 bg-destructive/10 text-destructive']">
      {{ flashMessage.text }}
    </div>

    <FormDialog
      :open="showCreate"
      @update:open="showCreate = false"
      :title="createMode === 'invite' ? $t('views.AdminUsersView.invite_user') : $t('views.AdminUsersView.create_user')"
      :confirm-text="createMode === 'invite' ? $t('views.AdminUsersView.send_invitation') : $t('common.create')"
      :loading="createLoading"
      :confirm-disabled="createLoading"
      @confirm="submitCreate"
    >
      <form @submit.prevent="submitCreate">
        <div class="mb-3 flex gap-2" role="tablist" aria-label="Add user mode">
          <button
            type="button"
            role="tab"
            data-testid="admin-users-mode-create"
            :aria-selected="createMode === 'password'"
            :class="['px-3 py-1.5 rounded-lg text-sm border transition-colors', createMode === 'password' ? 'bg-primary text-primary-foreground border-primary' : 'border-input hover:bg-accent']"
            @click="createMode = 'password'"
          >
            {{ $t('views.AdminUsersView.mode_password') }}
          </button>
          <button
            type="button"
            role="tab"
            data-testid="admin-users-mode-invite"
            :aria-selected="createMode === 'invite'"
            :class="['px-3 py-1.5 rounded-lg text-sm border transition-colors', createMode === 'invite' ? 'bg-primary text-primary-foreground border-primary' : 'border-input hover:bg-accent']"
            @click="createMode = 'invite'"
          >
            {{ $t('views.AdminUsersView.mode_invite') }}
          </button>
        </div>
        <div>
          <label for="adminusersview-field-4" class="block text-sm font-medium mb-1">{{ $t('common.email') }}</label>
          <input id="adminusersview-field-4" v-model="newUser.email" data-testid="admin-users-create-email" type="email" class="w-full px-3 py-2 border border-input bg-background rounded-lg text-sm" required />
        </div>
        <div>
          <label for="adminusersview-field-3" class="block text-sm font-medium mb-1">{{ $t('views.AdminModelBackendsView.display_name') }}</label>
          <input id="adminusersview-field-3" v-model="newUser.display_name" data-testid="admin-users-create-display-name" type="text" class="w-full px-3 py-2 border border-input bg-background rounded-lg text-sm" required />
        </div>
        <div v-if="createMode === 'password'">
          <label for="adminusersview-field-2" class="block text-sm font-medium mb-1">{{ $t('common.password') }}</label>
          <div class="flex gap-2">
            <input id="adminusersview-field-2" v-model="newUser.password" data-testid="admin-users-create-password" type="password" class="w-full px-3 py-2 border border-input bg-background rounded-lg text-sm" minlength="8" required />
            <Button type="button" severity="secondary" outlined class="shrink-0 border-primary/30" data-testid="admin-users-generate-password" @click="generatePassword">
              {{ $t('views.AdminUsersView.generate_password') }}
            </Button>
          </div>
        </div>
        <p v-if="createMode === 'invite'" class="mt-2 text-xs text-muted-foreground">
          {{ $t('views.AdminUsersView.invite_mode_hint') }}
        </p>
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

    <!-- Reusable credential dialog: temporary password OR invite link -->
    <Dialog
      :visible="showCredentialDialog"
      :modal="true"
      :dismissable-mask="true"
      :style="{ width: '28rem' }"
      @update:visible="dismissCredentialDialog"
    >
      <template #header>
        <div class="text-lg font-semibold">{{ credentialTitle }}</div>
      </template>

      <template v-if="credentialKind === 'password'">
        <p v-if="credentialMode === 'reset'" class="text-sm text-muted-foreground">
          {{ $t('views.AdminUsersView.credential_body_reset', { email: credentialEmail }) }}
        </p>
        <p v-else class="text-sm text-muted-foreground">
          {{ $t('views.AdminUsersView.credential_body_created', { email: credentialEmail }) }}
        </p>
      </template>
      <template v-else>
        <p class="text-sm text-muted-foreground">
          {{ $t('views.AdminUsersView.invitation_link_created_for', { email: credentialEmail }) }}
        </p>
        <div class="my-3 flex justify-center" data-testid="admin-users-invite-qr-wrapper">
          <img
            v-if="inviteQrDataUrl"
            :src="inviteQrDataUrl"
            :alt="$t('views.AdminUsersView.invite_qr_alt')"
            class="h-40 w-40 rounded-lg border border-border bg-white p-1"
            data-testid="admin-users-invite-qr"
          />
        </div>
        <p class="mb-2 text-xs text-muted-foreground">{{ $t('views.AdminUsersView.invite_expiry_note', { expires: credentialExpiresAt }) }}</p>
      </template>

      <p class="mb-1 mt-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">{{ $t('views.AdminUsersView.credentials') }}</p>
      <div class="flex items-center gap-2 bg-muted rounded-lg px-4 py-3">
        <code class="flex-1 text-sm font-mono break-all select-all" data-testid="admin-users-credential-value">{{ credentialValue }}</code>
        <Button
          class="shrink-0"
          :data-testid="credentialKind === 'invite' ? 'admin-users-invite-copy-url' : 'admin-users-copy-password'"
          @click="copyCredential"
        >
          {{ copied ? $t('views.AdminUsersView.copied') : $t('views.AdminUsersView.copy') }}
        </Button>
      </div>
      <template #footer>
        <div class="flex justify-end">
          <Button
            :data-testid="credentialKind === 'invite' ? 'admin-users-invite-done' : 'admin-users-reset-done'"
            @click="dismissCredentialDialog"
          >
            {{ $t('views.AdminUsersView.done') }}
          </Button>
        </div>
      </template>
    </Dialog>

    <!-- Revoke invitation confirmation -->
    <Dialog
      :visible="showRevokeDialog"
      :modal="true"
      :dismissable-mask="true"
      :style="{ width: '24rem' }"
      @update:visible="showRevokeDialog = false"
    >
      <template #header>
        <div class="text-lg font-semibold">{{ $t('views.AdminUsersView.revoke_invitation') }}</div>
      </template>
      <p class="text-sm text-muted-foreground">{{ $t('views.AdminUsersView.confirm_revoke_invitation', { email: revokeTarget?.email ?? '' }) }}</p>
      <template #footer>
        <div class="flex justify-end gap-2">
          <Button severity="secondary" outlined @click="showRevokeDialog = false">{{ $t('common.cancel') }}</Button>
          <Button severity="danger" data-testid="admin-invitations-confirm-revoke" @click="revokeInvitation">{{ $t('common.confirm') }}</Button>
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
import QRCode from 'qrcode'
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
import { passwordRuleKey, validatePasswordClient } from '../lib/passwordRules'
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

interface InvitationItem {
  id: string
  email: string
  display_name: string
  org_role: string
  invited_by: string
  created_at: string
  expires_at: string
}

interface InvitationListResponse {
  items: InvitationItem[]
  total: number
  page: number
  page_size: number
}

const { t } = useI18n()
const { get, put: httpPut, post, delete: httpDelete } = useApi()

const page = ref(1)
const pageSize = ref(50)

const { data: usersResp, loading, error, load: loadUsers } = useDataFetch(
  () => get<UserListResponse>(`/api/v1/admin/users?page=${page.value}&page_size=${pageSize.value}`).then(d => ({ data: d })),
  { initialValue: { items: [] as UserItem[], total: 0, page: 1, page_size: 50 } as UserListResponse }
)

const users = computed(() => usersResp.value?.items ?? [])
const total = computed(() => usersResp.value?.total ?? 0)

// ── Pending invitations ──────────────────────────────────────

const { data: invitationsResp, loading: invitationsLoading, error: invitationsError, load: loadInvitations } = useDataFetch(
  () => get<InvitationListResponse>('/api/v1/admin/users/invitations?page=1&page_size=100').then(d => ({ data: d })),
  { initialValue: { items: [] as InvitationItem[], total: 0, page: 1, page_size: 100 } as InvitationListResponse }
)
const pendingInvitations = computed(() => invitationsResp.value?.items ?? [])

// ── Create / invite dialog ───────────────────────────────────

const showCreate = ref(false)
const createMode = ref<'password' | 'invite'>('password')
const createError = ref('')
const createLoading = ref(false)
const newUser = ref({ email: '', display_name: '', password: '', org_role: 'runner' })

function resetNewUser() {
  newUser.value = { email: '', display_name: '', password: '', org_role: 'runner' }
}

function openAddUser() {
  createMode.value = 'password'
  createError.value = ''
  resetNewUser()
  showCreate.value = true
}

function submitCreate() {
  if (createMode.value === 'invite') return sendInvite()
  return createUser()
}

// ─── Credential dialog (shared: temp password / invite link) ───

type CredentialKind = 'password' | 'invite'
// FAR-460: one reusable credential dialog shared by reset-password and
// create-user so the admin can copy the credential exactly once.
type CredentialMode = 'reset' | 'created'
const showCredentialDialog = ref(false)
const credentialKind = ref<CredentialKind>('password')
const credentialMode = ref<CredentialMode>('reset')
const credentialValue = ref('')
const credentialEmail = ref('')
const credentialExpiresAt = ref('')
const inviteQrDataUrl = ref('')
const credentialTitle = computed(() => {
  if (credentialKind.value === 'invite') return t('views.AdminUsersView.invitation_ready')
  return credentialMode.value === 'created'
    ? t('views.AdminUsersView.credentials')
    : t('views.AdminUsersView.password_reset')
})
const copied = ref(false)

async function showCredential(kind: CredentialKind, value: string, email: string, expiresAt = '') {
  // Clear any previous secret before swapping the dialog contents.
  dismissCredentialState()
  credentialKind.value = kind
  credentialValue.value = value
  credentialEmail.value = email
  credentialExpiresAt.value = expiresAt
  copied.value = false
  inviteQrDataUrl.value = ''
  showCredentialDialog.value = true
  if (kind === 'invite') {
    try {
      inviteQrDataUrl.value = await QRCode.toDataURL(value, { margin: 1 })
    } catch {
      inviteQrDataUrl.value = '' // QR is a convenience — copy link still works.
    }
  }
}

function dismissCredentialState() {
  credentialValue.value = ''
  credentialEmail.value = ''
  credentialExpiresAt.value = ''
  inviteQrDataUrl.value = ''
  copied.value = false
}

function dismissCredentialDialog() {
  showCredentialDialog.value = false
  // Minor b: the secret must not linger in component memory once dismissed.
  dismissCredentialState()
}

let copyTimeout: ReturnType<typeof setTimeout> | null = null

function copyCredential() {
  navigator.clipboard.writeText(credentialValue.value)
  copied.value = true
  if (copyTimeout) clearTimeout(copyTimeout)
  copyTimeout = setTimeout(() => { copied.value = false }, 2000)
}

// ── Flash + user actions ─────────────────────────────────────

const flashMessage = ref<{ type: 'success' | 'error'; text: string } | null>(null)
const actionLoading = ref<Record<string, boolean>>({})
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
    showFlash('success', t('views.AdminUsersView.role_changed_to', { role: data.org_role, email: u.email }))
  } catch (e) {
    u.org_role = prevRole
    showFlash('error', e instanceof Error ? e.message : t('views.AdminUsersView.failed_to_update_role'))
  } finally {
    actionLoading.value[u.id] = false
  }
}

async function deactivate(u: UserItem) {
  actionLoading.value[u.id] = true
  try {
    const data = await post<UserItem>(`/api/v1/admin/users/${u.id}/deactivate`)
    updateUserInList(data)
    showFlash('success', t('views.AdminUsersView.user_deactivated', { email: u.email }))
  } catch (e) {
    showFlash('error', e instanceof Error ? e.message : t('views.AdminUsersView.failed_to_deactivate_user'))
  } finally {
    actionLoading.value[u.id] = false
  }
}

async function reactivate(u: UserItem) {
  actionLoading.value[u.id] = true
  try {
    const data = await post<UserItem>(`/api/v1/admin/users/${u.id}/reactivate`)
    updateUserInList(data)
    showFlash('success', t('views.AdminUsersView.user_reactivated', { email: u.email }))
  } catch (e) {
    showFlash('error', e instanceof Error ? e.message : t('views.AdminUsersView.failed_to_reactivate_user'))
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
    showFlash('error', t('views.AdminUsersView.failed_to_reset_password'))
  } finally {
    actionLoading.value[u.id] = false
  }
}

function openCredentialDialog(mode: CredentialMode, email: string, password: string) {
  credentialMode.value = mode
  void showCredential('password', password, email)
}

function rowActions(u: UserItem) {
  const actions: { key: string; label: string; onClick: () => void; disabled?: boolean; danger?: boolean }[] = [
    {
      key: 'reset-password',
      label: t('views.AdminUsersView.reset_password'),
      onClick: () => resetPassword(u),
      disabled: actionLoading.value[u.id],
    },
  ]
  if (u.is_active) {
    actions.push({
      key: 'deactivate',
      label: t('views.AdminUsersView.deactivate'),
      onClick: () => deactivate(u),
      disabled: actionLoading.value[u.id],
      danger: true,
    })
  } else {
    actions.push({
      key: 'reactivate',
      label: t('views.AdminUsersView.reactivate'),
      onClick: () => reactivate(u),
      disabled: actionLoading.value[u.id],
    })
  }
  return actions
}

// ─── Create / invite submissions ────────────────────────────────

function validateEmailAndName(): string {
  const { email, display_name } = newUser.value
  if (!email || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
    return t('views.AdminUsersView.please_enter_a_valid_email_address')
  }
  if (!display_name || !display_name.trim()) {
    return t('views.AdminUsersView.display_name_is_required')
  }
  return ''
}

function generatePassword() {
  newUser.value.password = generateStrongPassword()
}

async function createUser() {
  createError.value = ''
  const { email, password } = newUser.value
  const preError = validateEmailAndName()
  if (preError) {
    createError.value = preError
    return
  }
  const ruleCode = validatePasswordClient(password)
  if (ruleCode) {
    createError.value = t(passwordRuleKey(ruleCode))
    return
  }
  createLoading.value = true
  try {
    await post('/api/v1/admin/users', newUser.value)
    showCreate.value = false
    // FAR-460: surface the hand-typed credential once before it is discarded.
    openCredentialDialog('created', email, password)
    resetNewUser()
    showFlash('success', t('views.AdminUsersView.user_created', { email }))
    loadUsers()
  } catch (e: unknown) {
    createError.value = e instanceof Error ? e.message : t('views.AdminUsersView.failed_to_create_user')
  } finally {
    createLoading.value = false
  }
}

async function sendInvite() {
  createError.value = ''
  const { email, display_name, org_role } = newUser.value
  const preError = validateEmailAndName()
  if (preError) {
    createError.value = preError
    return
  }
  createLoading.value = true
  try {
    const data = await post<{ id: string; invite_url: string; expires_at: string }>('/api/v1/admin/users/invite', {
      email,
      display_name,
      org_role,
    })
    showCreate.value = false
    resetNewUser()
    loadInvitations()
    await showCredential(
      'invite',
      data.invite_url,
      email,
      data.expires_at ? formatDateShort(new Date(data.expires_at)) : '',
    )
    showFlash('success', t('views.AdminUsersView.invitation_sent', { email }))
  } catch (e: unknown) {
    createError.value = e instanceof Error ? e.message : t('views.AdminUsersView.failed_to_send_invitation')
  } finally {
    createLoading.value = false
  }
}

// ── Revoke invitation ────────────────────────────────────────

const showRevokeDialog = ref(false)
const revokeTarget = ref<InvitationItem | null>(null)

function openRevoke(inv: InvitationItem) {
  revokeTarget.value = inv
  showRevokeDialog.value = true
}

async function revokeInvitation() {
  const inv = revokeTarget.value
  if (!inv) return
  actionLoading.value[inv.id] = true
  try {
    await httpDelete(`/api/v1/admin/users/invitations/${inv.id}`)
    showRevokeDialog.value = false
    showFlash('success', t('views.AdminUsersView.invitation_revoked', { email: inv.email }))
    loadInvitations()
  } catch (e) {
    showFlash('error', e instanceof Error ? e.message : t('views.AdminUsersView.failed_to_revoke_invitation'))
  } finally {
    actionLoading.value[inv.id] = false
  }
}

onBeforeUnmount(() => {
  if (copyTimeout) clearTimeout(copyTimeout)
  if (flashTimeout) clearTimeout(flashTimeout)
})
/* onMounted handled by useDataFetch */
</script>
