<template>
  <Card>
    <template #title>{{ $t('views.SettingsMcpView.registered_oauth_clients') }}</template>
    <template #subtitle>{{ $t('views.SettingsMcpView.mcp_oauth_client_applications_registered_for_token_based_auth') }}</template>
    <template #content>
    <div class="space-y-4">
      <div
        v-if="oauthRestricted"
        class="rounded-lg border border-muted bg-muted/30 p-4 text-sm text-muted-foreground"
        data-testid="settings-mcp-oauth-restricted"
        aria-live="polite"
      >
        {{ $t('views.SettingsMcpView.oauth_clients_restricted') }}
      </div>

      <template v-else>
        <div class="flex flex-row items-center justify-between gap-3">
          <p class="text-sm text-muted-foreground">{{ $t('views.SettingsMcpView.configure_oauth_client_applications_for_mcp_token_based_auth') }}</p>
          <Button
            data-testid="settings-mcp-register-oauth-client"
            :disabled="!publicUrlConfigured"
            @click="openRegisterOauthDialog"
          >
            {{ $t('views.SettingsMcpView.register_oauth_client') }}
          </Button>
        </div>

        <div
          v-if="!publicUrlConfigured"
          class="rounded-lg border border-warning/50 bg-warning/10 p-3 text-sm text-warning"
          data-testid="settings-mcp-oauth-public-url-warning"
          aria-live="polite"
        >
          {{ $t('views.SettingsMcpView.set_modulo_public_url_before_registering_oauth') }}
        </div>

        <div
          v-if="listError"
          class="text-sm text-destructive"
          data-testid="settings-mcp-oauth-list-error"
          aria-live="assertive"
        >{{ listError }}</div>

        <div
          v-else-if="clients.length === 0"
          class="py-8 text-center text-sm text-muted-foreground"
          data-testid="settings-mcp-oauth-empty"
          aria-live="polite"
        >
          {{ $t('views.SettingsMcpView.no_oauth_clients_registered_yet') }}
        </div>

        <div v-else class="overflow-x-auto">
          <table class="w-full text-sm">
            <thead>
              <tr class="border-b text-left text-muted-foreground">
                <th class="pb-2 font-medium">{{ $t('views.SettingsMcpView.name') }}</th>
                <th class="pb-2 font-medium">{{ $t('views.SettingsMcpView.client_id') }}</th>
                <th class="pb-2 font-medium">{{ $t('views.SettingsMcpView.scopes') }}</th>
                <th class="pb-2 font-medium">{{ $t('views.SettingsMcpView.redirect_uris') }}</th>
                <th class="pb-2 font-medium">{{ $t('views.SettingsMcpView.created') }}</th>
                <th class="pb-2 font-medium" />
              </tr>
            </thead>
            <tbody class="divide-y divide-border">
              <tr v-for="client in clients" :key="client.id" class="transition-colors hover:bg-muted/20">
                <td class="py-2.5 font-medium">{{ client.name }}</td>
                <td class="py-2.5 font-mono text-muted-foreground">{{ client.client_id }}</td>
                <td class="py-2.5">
                  <span
                    v-for="scope in client.scopes"
                    :key="scope"
                    class="mr-1 inline-block rounded bg-muted px-1.5 py-0.5 font-mono text-xs"
                  >{{ scope }}</span>
                </td>
                <td class="max-w-xs py-2.5 font-mono text-xs break-all text-muted-foreground">{{ client.redirect_uris.join(', ') }}</td>
                <td class="py-2.5 text-muted-foreground">{{ formatDate(client.created_at) }}</td>
                <td class="py-2.5 text-right">
                  <Button severity="danger" size="small" data-testid="settings-mcp-revoke-oauth-client" @click="confirmRevokeOauth(client)">
                    {{ $t('views.SettingsMcpView.revoke') }}
                  </Button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </template>
      </div>
    </template>
  </Card>

  <FormDialog
    v-model:open="registerOauthDialogOpen"
    :title="$t('views.SettingsMcpView.register_oauth_client')"
    :description="$t('views.SettingsMcpView.register_oauth_client_description')"
    :confirmText="$t('views.SettingsMcpView.register_oauth_client')"
    :loading="registeringOauth"
    @confirm="registerOauthClient"
  >
    <div class="space-y-4 py-2">
      <div>
        <label for="settingsmcpview-oauth-name" class="mb-1 block text-sm font-medium">{{ $t('views.SettingsMcpView.oauth_client_name') }}</label>
        <input id="settingsmcpview-oauth-name"
          v-model="oauthName"
          type="text"
          data-testid="settings-mcp-oauth-name"
          class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          :placeholder="$t('views.SettingsMcpView.oauth_client_name_placeholder')"
          @blur="oauthNameTouched = true"
        />
        <p
          v-if="oauthNameTouched && !oauthName.trim()"
          class="mt-1 text-sm text-destructive"
          data-testid="settings-mcp-oauth-name-error"
          aria-live="assertive"
        >{{ $t('views.SettingsMcpView.oauth_client_name_required') }}</p>
      </div>

      <div>
        <label for="settingsmcpview-oauth-redirects" class="mb-1 block text-sm font-medium">{{ $t('views.SettingsMcpView.redirect_uris') }}</label>
        <textarea id="settingsmcpview-oauth-redirects"
          v-model="oauthRedirectUris"
          rows="3"
          data-testid="settings-mcp-oauth-redirect-uris"
          class="w-full rounded-lg border border-input bg-background px-3 py-2 font-mono text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          :placeholder="$t('views.SettingsMcpView.redirect_uri_placeholder')"
          @blur="oauthRedirectTouched = true"
        />
        <p class="mt-1 text-xs text-muted-foreground">{{ $t('views.SettingsMcpView.redirect_uris_hint') }}</p>
        <p
          v-if="oauthRedirectTouched && oauthRedirectList.length === 0"
          class="mt-1 text-sm text-destructive"
          data-testid="settings-mcp-oauth-redirect-error"
          aria-live="assertive"
        >{{ $t('views.SettingsMcpView.redirect_uris_required') }}</p>
        <p
          v-else-if="oauthRedirectTouched && oauthRedirectInvalid.length > 0"
          class="mt-1 text-sm text-destructive"
          data-testid="settings-mcp-oauth-redirect-invalid"
          aria-live="assertive"
        >{{ $t('views.SettingsMcpView.redirect_uris_invalid', { list: oauthRedirectInvalid.join(', ') }) }}</p>
      </div>

      <fieldset>
        <legend class="mb-1 block text-sm font-medium">{{ $t('views.SettingsMcpView.scopes') }}</legend>
        <label for="settingsmcpview-oauth-scope-trigger-run" class="mb-2 flex items-start gap-3 rounded-lg border p-3">
          <input id="settingsmcpview-oauth-scope-trigger-run"
            type="checkbox"
            class="mt-1 h-4 w-4 rounded border-muted-foreground"
            data-testid="settings-mcp-oauth-scope-trigger-run"
            :checked="oauthScopes.includes('trigger:run')"
            @change="toggleOauthScope('trigger:run')"
          />
          <span>
            <span class="block text-sm font-mono">trigger:run</span>
            <span class="block text-xs text-muted-foreground">{{ $t('views.SettingsMcpView.scope_trigger_run_desc') }}</span>
          </span>
        </label>
        <label for="settingsmcpview-oauth-scope-hitl-review" class="mb-2 flex items-start gap-3 rounded-lg border p-3">
          <input id="settingsmcpview-oauth-scope-hitl-review"
            type="checkbox"
            class="mt-1 h-4 w-4 rounded border-muted-foreground"
            data-testid="settings-mcp-oauth-scope-hitl-review"
            :checked="oauthScopes.includes('hitl:review')"
            @change="toggleOauthScope('hitl:review')"
          />
          <span>
            <span class="block text-sm font-mono">hitl:review</span>
            <span class="block text-xs text-muted-foreground">{{ $t('views.SettingsMcpView.scope_hitl_review_desc') }}</span>
          </span>
        </label>
        <label for="settingsmcpview-oauth-scope-library-browse" class="flex items-start gap-3 rounded-lg border p-3">
          <input id="settingsmcpview-oauth-scope-library-browse"
            type="checkbox"
            class="mt-1 h-4 w-4 rounded border-muted-foreground"
            data-testid="settings-mcp-oauth-scope-library-browse"
            :checked="oauthScopes.includes('library:browse')"
            @change="toggleOauthScope('library:browse')"
          />
          <span>
            <span class="block text-sm font-mono">library:browse</span>
            <span class="block text-xs text-muted-foreground">{{ $t('views.SettingsMcpView.scope_library_browse_desc') }}</span>
          </span>
        </label>
        <p
          v-if="oauthScopesTouched && oauthScopes.length === 0"
          class="mt-1 text-sm text-destructive"
          data-testid="settings-mcp-oauth-scopes-error"
          aria-live="assertive"
        >{{ $t('views.SettingsMcpView.scopes_required') }}</p>
      </fieldset>

      <div
        v-if="registerOauthError"
        class="text-sm text-destructive"
        data-testid="settings-mcp-oauth-register-error"
        aria-live="assertive"
      >{{ registerOauthError }}</div>
    </div>
  </FormDialog>

  <Dialog v-model:visible="oauthCreatedDialogOpen" :modal="true" :dismissable-mask="true" class="sm:max-w-lg" @hide="onOauthCreatedDialogClose">
    <template #header>
      <div class="text-lg font-semibold">{{ $t('views.SettingsMcpView.oauth_client_created') }}</div>
    </template>
    <div class="space-y-4 py-2">
      <p class="text-sm text-muted-foreground" data-testid="settings-mcp-oauth-created-warning">
        {{ $t('views.SettingsMcpView.copy_oauth_credentials_warning') }}
      </p>
      <div>
        <p class="mb-1 text-sm font-medium">{{ $t('views.SettingsMcpView.oauth_client_name') }}</p>
        <p class="text-sm text-muted-foreground">{{ createdOauthClientName }}</p>
      </div>
      <div>
        <p class="mb-1 text-sm font-medium">{{ $t('views.SettingsMcpView.client_id') }}</p>
        <div class="relative">
          <input :aria-label="$t('views.SettingsMcpView.client_id')"
            type="text"
            :value="createdOauthClientId"
            readonly
            data-testid="settings-mcp-oauth-client-id"
            class="w-full rounded-lg border border-input bg-muted px-3 py-2 font-mono text-sm"
          />
          <Button severity="secondary" outlined size="small" class="absolute right-1 top-1" data-testid="settings-mcp-copy-oauth-client-id" @click="copyToClipboard(createdOauthClientId, 'oauth-client-id')">
            {{ copiedField === 'oauth-client-id' ? $t('views.SettingsMcpView.copied') : $t('views.SettingsMcpView.copy') }}
          </Button>
        </div>
        <p
          v-if="copyFailedField === 'oauth-client-id'"
          class="mt-1 text-sm text-destructive"
          data-testid="settings-mcp-copy-oauth-client-id-error"
          aria-live="assertive"
        >{{ $t('views.SettingsMcpView.copy_failed_manual') }}</p>
      </div>
      <div>
        <p class="mb-1 text-sm font-medium">{{ $t('views.SettingsMcpView.client_secret') }}</p>
        <div class="relative">
          <input :aria-label="$t('views.SettingsMcpView.client_secret')"
            :type="oauthSecretMasked ? 'password' : 'text'"
            :value="createdOauthClientSecret"
            readonly
            data-testid="settings-mcp-oauth-client-secret"
            class="w-full rounded-lg border border-input bg-muted px-3 py-2 font-mono text-sm"
          />
          <Button severity="secondary" outlined size="small" class="absolute right-1 top-1" data-testid="settings-mcp-copy-oauth-client-secret" @click="copyToClipboard(createdOauthClientSecret, 'oauth-client-secret')">
            {{ copiedField === 'oauth-client-secret' ? $t('views.SettingsMcpView.copied') : $t('views.SettingsMcpView.copy') }}
          </Button>
        </div>
        <p
          v-if="copyFailedField === 'oauth-client-secret'"
          class="mt-1 text-sm text-destructive"
          data-testid="settings-mcp-copy-oauth-client-secret-error"
          aria-live="assertive"
        >{{ $t('views.SettingsMcpView.copy_failed_manual') }}</p>
        <p v-if="!oauthSecretMasked" class="mt-1 text-xs text-muted-foreground">
          {{ $t('views.SettingsMcpView.oauth_secret_will_be_masked_in', { seconds: oauthSecretCountdown }) }}
        </p>
      </div>
    </div>
    <template #footer>
      <Button data-testid="settings-mcp-oauth-created-done" @click="dismissOauthCreatedDialog">{{ $t('views.SettingsMcpView.done') }}</Button>
    </template>
  </Dialog>

  <FormDialog
    v-model:open="revokeOauthDialogOpen"
    :title="$t('views.SettingsMcpView.revoke_oauth_client')"
    :confirmText="$t('views.SettingsMcpView.confirm_revoke')"
    :loading="revokingOauth"
    @confirm="revokeOauthClient"
  >
    <p class="text-sm text-muted-foreground">
      {{ $t('views.SettingsMcpView.revoke_oauth_confirmation_part1') }} <strong>{{ revokeOauthTarget?.name }}</strong>?
      {{ $t('views.SettingsMcpView.revoke_oauth_confirmation_part2') }}
    </p>
    <div
      v-if="revokeOauthError"
      class="text-sm text-destructive"
      data-testid="settings-mcp-oauth-revoke-error"
      aria-live="assertive"
    >{{ revokeOauthError }}</div>
  </FormDialog>
</template>

<script setup lang="ts">
import { ref, computed, onUnmounted } from 'vue'
import { api } from '../../lib/api/client'
import { formatApiError } from '../../lib/api/formatError'
import type { components } from '../../lib/api/client'
import Button from 'primevue/button'
import Card from 'primevue/card'
import Dialog from 'primevue/dialog'
import FormDialog from '../shared/FormDialog.vue'
import { formatDateShort } from '../../lib/formatDate'
import { useSecretReveal } from '../../composables/useSecretReveal'

type OAuthClientItem = components['schemas']['OAuthClientItem']

const props = defineProps<{
  /** Registered OAuth clients from `GET /api/v1/mcp/oauth/clients`. */
  clients: OAuthClientItem[]
  /** True when that list call came back 403 (stale/downgraded JWT). */
  forbidden: boolean
  /** Non-fatal list failure message, or null when the list loaded. */
  listError: string | null
  /** Org role is admin|operator - the backend gate on all three endpoints. */
  canManage: boolean
  /**
   * MODULO_PUBLIC_URL is configured well enough for the OAuth flow (FAR-1282).
   * Derived ONCE by the owning view from the MCP status URL and passed down,
   * so this card never re-derives it. When false the server-side guard in
   * `api/routes/mcp_oauth.py` would reject the register call with a 500, so
   * the action is disabled up front instead of after a doomed submit.
   */
  publicUrlConfigured: boolean
}>()

const emit = defineEmits<{
  /** Ask the owning view to re-run its data fetch (after register/revoke). */
  refresh: []
}>()

// Role gate: the backend requires org role admin|operator for all three OAuth
// client endpoints, so a viewer never sees the table or the actions. The 403
// from the list call is the belt-and-braces fallback for a stale/downgraded
// JWT - it forces the same restricted state.
const oauthRestricted = computed(() => !props.canManage || props.forbidden)

const registerOauthDialogOpen = ref(false)
const oauthName = ref('')
const oauthNameTouched = ref(false)
const oauthRedirectUris = ref('')
const oauthRedirectTouched = ref(false)
const oauthScopes = ref<string[]>([])
const oauthScopesTouched = ref(false)
const registeringOauth = ref(false)
const registerOauthError = ref<string | null>(null)

const oauthCreatedDialogOpen = ref(false)
const createdOauthClientId = ref('')
const createdOauthClientName = ref('')
const {
  value: createdOauthClientSecret,
  masked: oauthSecretMasked,
  countdown: oauthSecretCountdown,
  reveal: revealOauthClientSecret,
  onClose: closeOauthClientSecret,
} = useSecretReveal()

const revokeOauthDialogOpen = ref(false)
const revokeOauthTarget = ref<OAuthClientItem | null>(null)
const revokingOauth = ref(false)
const revokeOauthError = ref<string | null>(null)

const copiedField = ref<string | null>(null)
// Set when a clipboard write REJECTED. `navigator.clipboard` only exists in a
// secure context, so a self-hosted instance on plain HTTP can never copy - and
// for the one-time credential dialog the clipboard is the only way to keep
// the value. A console warning alone would lose the credential silently, so
// the failing field carries a visible "copy manually" message instead.
const copyFailedField = ref<string | null>(null)
let oauthCopyTimeout: ReturnType<typeof setTimeout> | null = null

function formatDate(iso: string): string {
  try {
    return formatDateShort(new Date(iso))
  } catch {
    return iso
  }
}

// ─── OAuth client registration ─────────────────────────────────────────

/**
 * Tokenise the textarea on ANY whitespace, not just newlines.
 *
 * The backend stores `" ".join(req.redirect_uris)` and reads back with
 * `.split()`, so an entry containing an internal space would be persisted as
 * ONE value and returned as TWO - the table would show something the user
 * never typed and the OAuth flow could never match the original URI.
 * Splitting on whitespace makes the round-trip lossless, and identical
 * entries are de-duplicated (the server echoes them back verbatim).
 */
function parseRedirectUris(raw: string): string[] {
  const seen = new Set<string>()
  const entries: string[] = []
  for (const entry of raw.split(/\s+/)) {
    if (!entry || seen.has(entry)) continue
    seen.add(entry)
    entries.push(entry)
  }
  return entries
}

/**
 * Redirect URIs must be absolute http(s) URIs. `new URL` rejects relative and
 * malformed input outright; http://localhost / http://127.0.0.1 stay valid so
 * local development keeps working.
 */
function isAbsoluteHttpUri(value: string): boolean {
  try {
    const protocol = new URL(value).protocol
    return protocol === 'http:' || protocol === 'https:'
  } catch {
    return false
  }
}

const oauthRedirectList = computed(() => parseRedirectUris(oauthRedirectUris.value))
const oauthRedirectInvalid = computed(() =>
  oauthRedirectList.value.filter((entry) => !isAbsoluteHttpUri(entry)),
)
const registerOauthValid = computed(
  () =>
    oauthName.value.trim().length > 0 &&
    oauthRedirectList.value.length > 0 &&
    oauthRedirectInvalid.value.length === 0 &&
    oauthScopes.value.length > 0,
)

function toggleOauthScope(scope: string) {
  oauthScopesTouched.value = true
  oauthScopes.value = oauthScopes.value.includes(scope)
    ? oauthScopes.value.filter((s) => s !== scope)
    : [...oauthScopes.value, scope]
}

function openRegisterOauthDialog() {
  // FAR-1282: the button is disabled when MODULO_PUBLIC_URL is missing, but
  // this guard keeps the invariant true for any other entry point (keyboard,
  // a future caller) rather than relying on the disabled attribute alone.
  if (!props.publicUrlConfigured) return
  oauthName.value = ''
  oauthNameTouched.value = false
  oauthRedirectUris.value = ''
  oauthRedirectTouched.value = false
  oauthScopes.value = []
  oauthScopesTouched.value = false
  registerOauthError.value = null
  registerOauthDialogOpen.value = true
}

async function registerOauthClient() {
  if (!registerOauthValid.value) {
    oauthNameTouched.value = true
    oauthRedirectTouched.value = true
    oauthScopesTouched.value = true
    return
  }
  registeringOauth.value = true
  registerOauthError.value = null
  try {
    const { data, error: err } = await api.POST('/api/v1/mcp/oauth/clients', {
      body: {
        name: oauthName.value.trim(),
        redirect_uris: oauthRedirectList.value,
        scopes: [...oauthScopes.value],
      },
    })
    if (err) {
      registerOauthError.value = formatApiError(err)
    } else if (data) {
      createdOauthClientId.value = data.client_id
      createdOauthClientName.value = data.name
      registerOauthDialogOpen.value = false
      oauthCreatedDialogOpen.value = true
      revealOauthClientSecret(data.client_secret)
      emit('refresh')
    }
  } catch (e: unknown) {
    registerOauthError.value = formatApiError(e)
  } finally {
    registeringOauth.value = false
  }
}

/**
 * Wipe the one-time OAuth credentials from component memory on close - the
 * client secret cannot be retrieved again, so it must not linger for the
 * component's lifetime (same convention as AdminUsersView's
 * `dismissCredentialState`). `useSecretReveal` owns the secret itself; this
 * clears the dialog-local bits around it.
 */
function dismissOauthCredentialState() {
  createdOauthClientId.value = ''
  createdOauthClientName.value = ''
  copyFailedField.value = null
  if (copiedField.value === 'oauth-client-id' || copiedField.value === 'oauth-client-secret') {
    copiedField.value = null
  }
}

/**
 * Bound to the Dialog's `hide` event, which PrimeVue emits for EVERY close
 * path (mask click, X, ESC, and the prop-driven close) - unlike
 * `update:visible`, which never fires when the parent flips the v-model.
 */
function onOauthCreatedDialogClose() {
  closeOauthClientSecret()
  dismissOauthCredentialState()
}

/** The footer "Done" button: close and wipe in one step, no event ordering games. */
function dismissOauthCreatedDialog() {
  oauthCreatedDialogOpen.value = false
  onOauthCreatedDialogClose()
}

function confirmRevokeOauth(client: OAuthClientItem) {
  revokeOauthTarget.value = client
  revokeOauthError.value = null
  revokeOauthDialogOpen.value = true
}

async function revokeOauthClient() {
  if (!revokeOauthTarget.value) return
  revokingOauth.value = true
  revokeOauthError.value = null
  try {
    const { error: err } = await api.DELETE('/api/v1/mcp/oauth/clients/{client_id}', {
      params: { path: { client_id: revokeOauthTarget.value.client_id } },
    })
    if (err) {
      revokeOauthError.value = formatApiError(err)
    } else {
      revokeOauthDialogOpen.value = false
      revokeOauthTarget.value = null
      emit('refresh')
    }
  } catch (e: unknown) {
    revokeOauthError.value = formatApiError(e)
  } finally {
    revokingOauth.value = false
  }
}

async function copyToClipboard(text: string, field: string) {
  copyFailedField.value = null
  try {
    await navigator.clipboard.writeText(text)
    copiedField.value = field
    if (oauthCopyTimeout) clearTimeout(oauthCopyTimeout)
    oauthCopyTimeout = setTimeout(() => {
      if (copiedField.value === field) {
        copiedField.value = null
      }
    }, 2000)
  } catch (e) {
    // `navigator.clipboard` is undefined outside a secure context (a
    // self-hosted instance on plain HTTP), so this is a realistic failure -
    // and for the one-time credential dialog the clipboard is the only way
    // to keep the value. Surface it on the field itself, not just in the
    // console, and leave the readonly input selectable so the value can be
    // copied by hand.
    console.warn('Failed to copy MCP config', e)
    copiedField.value = null
    copyFailedField.value = field
  }
}

onUnmounted(() => {
  if (oauthCopyTimeout) clearTimeout(oauthCopyTimeout)
})
</script>
