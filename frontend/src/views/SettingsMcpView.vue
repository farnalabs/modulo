<template>
  <FeatureGate feature-name="mcp_server" required-tier="community" show-disabled>

    <div data-theme="agent" class="page-wide">
    <PageHeader :title="$t('views.SettingsMcpView.mcp_configuration')" :subtitle="$t('views.SettingsMcpView.configure_mcp_server_settings_and_api_keys')" />

    <!-- The INITIAL load owns the page-level states: the spinner while it
         runs, and the ErrorAlert with retry if it FAILS (nothing was ever
         rendered then, so there is no last-good content to keep on screen).
         After the first successful load, `loading` never flips again
         (silentRefetch) and a later failure is non-fatal - see `refetchError`
         at the top of the branch below. -->
    <LoadingSpinner v-if="loading" />
    <ErrorAlert v-else-if="initialLoadError" :message="initialLoadError" :on-retry="loadAll" />

    <template v-else>
      <!-- A refetch failure AFTER a successful load must not replace what is
           rendered: the last-good data stays on screen and every card (plus
           any open one-time-secret dialog behind them) stays mounted, so a
           client secret shown "only once" is never destroyed by a transient
           5xx. The failure itself surfaces here instead - the existing error
           surface in a non-page-fatal form, in an aria-live region with the
           same retry affordance the page-level alert has. -->
      <ErrorAlert
        v-if="refetchError"
        :message="refetchError"
        :on-retry="loadAll"
        aria-live="assertive"
      />

      <!-- MCP Server Status -->
      <Card>
        <template #title>{{ $t('views.SettingsMcpView.mcp_server_status') }}</template>
        <template #subtitle>{{ $t('views.SettingsMcpView.the_url_clients_use_to_connect_to_the_mcp_server') }}</template>
        <template #content>
        <div class="space-y-4">
          <div
            v-if="!publicUrlConfigured"
            class="rounded-lg border border-warning/50 bg-warning/10 p-4 text-sm text-warning"
          >
            <p class="font-medium">{{ $t('views.SettingsMcpView.modulo_public_url_not_set') }}</p>
            <p class="mt-1">
              {{ $t('views.SettingsMcpView.modulo_public_url_not_configured') }} <code class="rounded bg-warning/10 px-1 py-0.5 text-xs">http://localhost:8000</code>.
            </p>
          </div>

          <div class="flex items-center justify-between rounded-lg bg-muted/30 p-4">
            <div class="min-w-0 flex-1">
              <p class="text-sm font-medium">{{ $t('views.SettingsMcpView.server_url') }}</p>
              <p class="mt-0.5 select-all cursor-text font-mono text-sm text-muted-foreground">{{ mcpUrl || 'http://localhost:8000' }}</p>
            </div>
            <div class="flex shrink-0 items-center gap-2">
              <Button severity="secondary" outlined size="small" data-testid="settings-mcp-copy-url" @click="copyServerUrl">
                {{ copiedField === 'server-url' ? $t('views.SettingsMcpView.copied') : $t('views.SettingsMcpView.copy') }}
              </Button>
              <Badge :severity="publicUrlConfigured ? 'info' : 'secondary'">
                {{ publicUrlConfigured ? $t('views.SettingsMcpView.active') : $t('views.SettingsMcpView.local_only') }}
              </Badge>
            </div>
          </div>
          </div>
        </template>
      </Card>

      <!-- API Key Management -->
      <Card>
        <template #header>
          <div class="flex flex-row items-center justify-between">
            <div>
              <div class="text-lg font-semibold">{{ $t('views.SettingsMcpView.api_keys') }}</div>
              <div class="text-sm text-muted-foreground">{{ $t('views.SettingsMcpView.create_and_manage_api_keys_for_mcp_client_authentication') }}</div>
            </div>
            <Button v-if="!apiKeysRestricted" data-testid="settings-mcp-create-key" @click="openCreateKeyDialog">
              {{ $t('views.SettingsMcpView.create_mcp_api_key') }}
            </Button>
          </div>
        </template>
        <template #content>
        <div>
          <div
            v-if="apiKeysRestricted"
            class="rounded-lg border border-muted bg-muted/30 p-4 text-sm text-muted-foreground"
            data-testid="settings-mcp-api-keys-restricted"
            aria-live="polite"
          >
            {{ $t('views.SettingsMcpView.api_keys_restricted') }}
          </div>

          <template v-else>
          <p class="mb-3 text-xs text-muted-foreground" data-testid="settings-mcp-org-scope-note">
            {{ $t('views.SettingsMcpView.api_keys_act_org_wide_note') }}
          </p>
          <div v-if="apiKeys.length === 0" class="py-8 text-center text-sm text-muted-foreground">
            {{ $t('views.SettingsMcpView.no_api_keys_created_yet') }}
          </div>

          <div v-else class="overflow-x-auto">
            <table class="w-full text-sm">
            <thead>
              <tr class="border-b text-left text-muted-foreground">
                <th class="pb-2 font-medium">{{ $t('views.SettingsMcpView.name') }}</th>
                <th class="pb-2 font-medium">{{ $t('views.SettingsMcpView.key_prefix') }}</th>
                <th class="pb-2 font-medium">{{ $t('views.SettingsMcpView.role') }}</th>
                <th class="pb-2 font-medium capitalize">{{ $t('views.SettingsMcpView.status') }}</th>
                <th class="pb-2 font-medium">{{ $t('views.SettingsMcpView.last_used') }}</th>
                <th class="pb-2 font-medium" />
              </tr>
            </thead>
            <tbody class="divide-y divide-border">
              <tr v-for="key in apiKeys" :key="key.id" class="transition-colors hover:bg-muted/20">
                <td class="py-2.5 font-medium">{{ key.name }}</td>
                <td class="py-2.5 font-mono text-muted-foreground">{{ key.lookup_prefix }}</td>
                <td class="py-2.5 capitalize">{{ key.role }}</td>
                <td class="py-2.5" data-testid="settings-mcp-key-status">
                  <Badge :severity="apiKeyStatusMeta(key).severity">{{ apiKeyStatusMeta(key).label }}</Badge>
                </td>
                <td class="py-2.5 text-muted-foreground">
                  {{ key.last_used_at ? formatDate(key.last_used_at) : $t('views.SettingsMcpView.never') }}
                </td>
                <td class="py-2.5 text-right">
                  <Button v-if="key.is_active" severity="danger" size="small" data-testid="settings-mcp-revoke-key" @click="confirmRevokeKey(key)">
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

      <!-- Config Snippets -->
      <Card>
        <template #title>{{ $t('views.SettingsMcpView.configuration_snippets') }}</template>
        <template #subtitle>{{ $t('views.SettingsMcpView.copy_these_snippets_to_configure_mcp_clients') }}</template>
        <template #content>
        <div class="space-y-4">
          <div class="flex items-center gap-2">
            <label for="settingsmcpview-client" class="text-sm font-medium whitespace-nowrap">{{ $t('views.SettingsMcpView.client') }}:</label>
            <Select
  aria-label="Client"
  v-model="selectedMcpClient"
  :placeholder="$t('views.SettingsMcpView.client')"
  id="settingsmcpview-client"
  class="w-full"
  :options="[{ value: 'opencode', label: 'opencode / Claude Code' }, { value: 'claude', label: $t('views.SettingsMcpView.claude_desktop') }, { value: 'cursor', label: $t('views.SettingsMcpView.cursor') }, { value: 'continue', label: $t('views.SettingsMcpView.continue_dev') }, { value: 'custom', label: $t('views.SettingsMcpView.custom') }]"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
          </div>
          <div class="rounded-lg bg-muted/30 p-4">
            <pre class="text-xs font-mono whitespace-pre-wrap break-all">{{ mcpConfigSnippet }}</pre>
            <Button severity="secondary" outlined size="small" class="mt-2" data-testid="settings-mcp-copy-snippet" @click="copySnippet">{{ $t('views.SettingsMcpView.copy') }}</Button>
          </div>
          </div>
        </template>
      </Card>

      <!-- Registered OAuth Clients (extracted to McpOauthClientsCard).
           Rendered behind the SAME data gate as every other card: the
           post-mutation refetch is silent (silentRefetch) and a refetch
           failure is non-fatal (see `refetchError` above), so nothing
           unmounts while the one-time-secret reveal dialog is open - the
           exemption this card used to carry is no longer needed, and all
           four cards now appear and disappear together. -->
      <McpOauthClientsCard
        :clients="oauthClients"
        :forbidden="oauthForbidden"
        :list-error="oauthListError"
        :can-manage="canManageOauth"
        :public-url-configured="publicUrlConfigured"
        @refresh="refreshAfterOauthMutation"
      />
    </template>

    <FormDialog
      v-model:open="createKeyDialogOpen"
      :title="$t('views.SettingsMcpView.create_mcp_api_key')"
      :description="$t('views.SettingsMcpView.generate_new_api_key_description')"
      :confirmText="$t('views.SettingsMcpView.create_mcp_api_key')"
      :confirmDisabled="!createKeyName.trim()"
      :loading="creatingKey"
      @confirm="createKey"
    >
      <div class="space-y-4 py-2">
        <div>
          <label for="settingsmcpview-field-2" class="mb-1 block text-sm font-medium">{{ $t('views.SettingsMcpView.key_name') }}</label>
          <input id="settingsmcpview-field-2"
            v-model="createKeyName"
            type="text"
            data-testid="settings-mcp-create-key-name"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            :placeholder="$t('views.SettingsMcpView.key_placeholder_example')"
            @blur="createKeyNameTouched = true"
          />
          <p
            v-if="createKeyNameTouched && !createKeyName.trim()"
            class="mt-1 text-sm text-destructive"
          >{{ $t('views.SettingsMcpView.key_name_is_required') }}</p>
        </div>
        <div>
          <label for="settingsmcpview-role" class="mb-1 block text-sm font-medium">{{ $t('views.SettingsMcpView.role') }}</label>
          <Select
  aria-label="Role"
  v-model="createKeyRole"
  :placeholder="$t('views.SettingsMcpView.role')"
  data-testid="settings-mcp-create-key-role"
  id="settingsmcpview-role"
  class="w-full"
  :options="[{ value: 'operator', label: $t('views.SettingsMcpView.operator') }, { value: 'runner', label: $t('views.SettingsMcpView.runner') }]"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
        </div>
        <div v-if="createKeyError" class="text-sm text-destructive">{{ createKeyError }}</div>
      </div>
    </FormDialog>

    <Dialog v-model:visible="keyCreatedDialogOpen" :modal="true" :dismissable-mask="true" class="sm:max-w-lg" @hide="onKeyCreatedDialogClose">
      <template #header>
        <div class="text-lg font-semibold">{{ $t('views.SettingsMcpView.api_key_created') }}</div>
      </template>
      <div class="space-y-4 py-2">
        <p class="text-sm text-muted-foreground">
          {{ $t('views.SettingsMcpView.copy_key_now_warning') }}
        </p>
        <div class="space-y-4">
          <div>
            <p class="mb-1 text-sm font-medium">{{ $t('views.SettingsMcpView.key_name') }}</p>
            <p class="text-sm text-muted-foreground">{{ createdKeyName }}</p>
          </div>
          <div>
            <p class="mb-1 text-sm font-medium">{{ $t('views.SettingsMcpView.api_key') }}</p>
            <div class="relative">
              <input :aria-label="$t('views.SettingsMcpView.api_key')"
                :type="keyMasked ? 'password' : 'text'"
                :value="createdKeyValue"
                readonly
                class="w-full rounded-lg border border-input bg-muted px-3 py-2 font-mono text-sm"
              />
              <Button severity="secondary" outlined size="small" class="absolute right-1 top-1" data-testid="settings-mcp-copy-key-value" @click="copyToClipboard(createdKeyValue, 'key-value')">
                {{ copiedField === 'key-value' ? $t('views.SettingsMcpView.copied') : $t('views.SettingsMcpView.copy') }}
              </Button>
            </div>
            <p v-if="!keyMasked" class="mt-1 text-xs text-muted-foreground">
              {{ $t('views.SettingsMcpView.key_will_be_masked_in', { seconds: keyMaskCountdown }) }}
            </p>
          </div>
        </div>
      </div>
      <template #footer>
        <Button data-testid="settings-mcp-key-created-done" @click="dismissKeyCreatedDialog">{{ $t('views.SettingsMcpView.done') }}</Button>
      </template>
    </Dialog>

    <FormDialog
      v-model:open="revokeKeyDialogOpen"
      :title="$t('views.SettingsMcpView.revoke_api_key')"
      :confirmText="$t('views.SettingsMcpView.confirm_revoke')"
      :loading="revokingKey"
      @confirm="revokeKey"
    >
      <p class="text-sm text-muted-foreground">
        {{ $t('views.SettingsMcpView.revoke_key_confirmation_part1') }} <strong>{{ revokeKeyTarget?.name }}</strong>?
        {{ $t('views.SettingsMcpView.revoke_key_confirmation_part2') }}
      </p>
      <div v-if="revokeKeyError" class="text-sm text-destructive">{{ revokeKeyError }}</div>
    </FormDialog>

  </div>
  </FeatureGate>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useDataFetch } from '../composables/useDataFetch'
import { api } from '../lib/api/client'
import { formatApiError } from '../lib/api/formatError'
import type { components } from '../lib/api/client'
import PageHeader from '../components/shared/PageHeader.vue'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import Badge from 'primevue/badge'
import Button from 'primevue/button'
import Card from 'primevue/card'
import Dialog from 'primevue/dialog'
import FormDialog from '../components/shared/FormDialog.vue'
import { usePlanStore } from '../stores/planStore'
import FeatureGate from '../components/FeatureGate.vue'
import { formatDateShort } from '../lib/formatDate'
import Select from '../components/shared/AppSelect.vue'
import McpOauthClientsCard from '../components/settings/McpOauthClientsCard.vue'
import { useCurrentUser } from '../composables/useCurrentUser'
import { useSecretReveal } from '../composables/useSecretReveal'
import { useI18n } from 'vue-i18n'

const planStore = usePlanStore()
const { orgRole } = useCurrentUser()
const { t } = useI18n()

interface ApiKeyItem {
  id: string
  name: string
  role: string
  lookup_prefix: string
  is_active: boolean
  // Serialized by _serialize_key (backend/src/modulo/auth/api_key.py) alongside
  // is_active - always present on the wire, null when the key never expires.
  expires_at: string | null
  last_used_at: string | null
  created_at: string
}

interface McpPageData {
  mcpUrl: string
  apiKeys: ApiKeyItem[]
  oauthClients: OAuthClientItem[]
  oauthForbidden: boolean
  apiKeysForbidden: boolean
  oauthListError: string | null
}

type OAuthClientItem = components['schemas']['OAuthClientItem']

/**
 * A request that REJECTS (network failure, abort, parse error) never reaches
 * the typed client's `{ data, error, response }` result shape, so it is
 * normalised here into that same shape with a `detail`-carrying error. The
 * rejection reason is flattened through `formatApiError` first because
 * `useDataFetch` accepts only an error *object* (`{ detail?: unknown }`) -
 * wrapping keeps every rejection path rendering exactly what the resolved
 * `{ error }` path renders.
 */
function rejectedResult(e: unknown): {
  data?: undefined
  error?: { detail?: unknown }
  response?: undefined
} {
  return { error: { detail: formatApiError(e) } }
}

/**
 * A GET that only the admin/operator roles may perform can come back 403 for a
 * viewer (or for a stale JWT whose role was downgraded). The list response
 * carries the HTTP status on `response`, and the api client normalises error
 * bodies to a ProblemDetail carrying `status` - check both shapes.
 */
function isForbiddenResult(resp: { error?: unknown; response?: { status?: number } } | null): boolean {
  if (!resp) return false
  if (resp.response?.status === 403) return true
  const err = resp.error
  if (err && typeof err === 'object' && (err as { status?: unknown }).status === 403) return true
  return false
}

const {
  loading,
  error: loadError,
  data: mcpData,
  fetched,
  load: loadAll,
} = useDataFetch<McpPageData>(
  async () => {
    const [mcpResp, keysResp, oauthResp] = await Promise.all([
      api.GET('/api/v1/api-keys/mcp-config').catch(rejectedResult),
      api.GET('/api/v1/api-keys').catch(rejectedResult),
      api.GET('/api/v1/mcp/oauth/clients').catch(rejectedResult),
    ])
    if (mcpResp.error) return { error: mcpResp.error }

    // The API key list carries the SAME admin|operator gate as the OAuth
    // endpoints (`GET /api/v1/api-keys` raises 403 for any role below
    // operator), so a 403 here is a RESTRICTED STATE for this card - not a
    // page failure. Failing the whole page on it would make the OAuth
    // restricted panel below unreachable in production and would take the MCP
    // server status / snippet cards down with it. Any NON-403 failure keeps
    // the pre-existing fatal behaviour.
    let apiKeys: ApiKeyItem[] = []
    let apiKeysForbidden = false
    if (keysResp.error) {
      if (isForbiddenResult(keysResp)) {
        apiKeysForbidden = true
      } else {
        return { error: keysResp.error }
      }
    } else if (Array.isArray(keysResp.data)) {
      // The generated OpenAPI type for GET /api/v1/api-keys is a bare
      // `{ [key: string]: unknown }[]` (FastAPI serialises the rows without a
      // response model), so the row shape is asserted at this one boundary
      // where the data enters the page - it stays null-safe behind the
      // Array.isArray guard above, and everything downstream is ApiKeyItem.
      apiKeys = keysResp.data as unknown as ApiKeyItem[]
    }

    // The OAuth client list is deliberately NON-fatal: a 403 (viewer role) or
    // any other failure must not take down the MCP config / API key cards.
    const oauthClients: OAuthClientItem[] = []
    let oauthForbidden = false
    let oauthListError: string | null = null
    if (isForbiddenResult(oauthResp)) {
      oauthForbidden = true
    } else if (oauthResp.error) {
      oauthListError = formatApiError(oauthResp.error)
    } else if (Array.isArray(oauthResp.data)) {
      oauthClients.push(...(oauthResp.data as OAuthClientItem[]))
    } else {
      // A rejection already arrives as `{ error }`; this arm covers a success
      // body that is not the array the endpoint documents (malformed JSON
      // degrades here too). Either way it is a FAILURE, never an empty
      // registry - "No OAuth clients registered yet." would be a lie.
      oauthListError = t('views.SettingsMcpView.oauth_list_unexpected_response')
    }
    return {
      data: {
        mcpUrl: mcpResp.data?.mcp_url ?? '',
        apiKeys,
        oauthClients,
        oauthForbidden,
        apiKeysForbidden,
        oauthListError,
      },
    }
  },
  {
    initialValue: {
      mcpUrl: '',
      apiKeys: [],
      oauthClients: [],
      oauthForbidden: false,
      apiKeysForbidden: false,
      oauthListError: null,
    },
    // Silent refetch: only the FIRST load flips `loading`, so the
    // post-mutation refetches (create key, revoke key, OAuth register/revoke)
    // update the cards in place instead of swapping the whole page for a
    // spinner and remounting every card mid-dialog (FAR-1251).
    silentRefetch: true,
  },
)

const mcpUrl = computed(() => mcpData.value?.mcpUrl ?? '')
const apiKeys = computed(() => mcpData.value?.apiKeys ?? [])
const oauthClients = computed(() => mcpData.value?.oauthClients ?? [])
const oauthListError = computed(() => mcpData.value?.oauthListError ?? null)

/**
 * The page's ONE concept of "MODULO_PUBLIC_URL is configured" (FAR-1282).
 *
 * The server-side OAuth guard (`api/routes/mcp_oauth.py`) refuses the
 * registration flow when the effective public URL is empty OR is the
 * `http://localhost:8000` fallback, and `GET /api/v1/api-keys/mcp-config`
 * reports that same value as `<public url>/mcp` (empty public URL -> `/mcp`).
 * So strip the `/mcp` suffix and test the base against exactly the two
 * rejecting values the server uses - `!mcpUrl` alone is not enough, because a
 * default/unset MODULO_PUBLIC_URL comes back as `http://localhost:8000/mcp`,
 * which is truthy but still fails the server guard.
 *
 * Everything that cares about that precondition reads THIS computed: the
 * MODULO_PUBLIC_URL warning card, the Active/Local Only badge, and the OAuth
 * register gate (passed to `McpOauthClientsCard` as `public-url-configured`).
 */
const publicUrlConfigured = computed(() => {
  const raw = mcpUrl.value
  if (!raw) return false
  const base = raw.endsWith('/mcp') ? raw.slice(0, -'/mcp'.length) : raw
  return base !== '' && base !== 'http://localhost:8000'
})

// Role gate: the backend requires org role admin|operator for all three OAuth
// client endpoints, so a viewer never sees the table or the actions. The 403
// from the list call is the belt-and-braces fallback for a stale/downgraded
// JWT - it forces the same restricted state. `McpOauthClientsCard` combines
// the two itself; this view keeps only what its own API key card needs.
const canManageOauth = computed(() => orgRole.value === 'admin' || orgRole.value === 'operator')
const oauthForbidden = computed(() => mcpData.value?.oauthForbidden ?? false)
// The API key list carries the identical admin|operator gate, so that card
// degrades the same way: a viewer (role gate, or a 403 from a stale JWT) still
// gets the MCP server status + snippet cards instead of a dead page.
const apiKeysRestricted = computed(() => !canManageOauth.value || (mcpData.value?.apiKeysForbidden ?? false))

/**
 * Split the fetch error by WHEN it happened, because the two cases must not
 * behave the same:
 *
 * - BEFORE the first successful load there is nothing rendered to preserve,
 *   so the failure stays page-fatal: the ErrorAlert above with retry.
 * - AFTER a successful load (`fetched` only ever flips true), the failure
 *   came from a refetch. Replacing the page would destroy last-good state -
 *   including a one-time client secret revealed in a dialog that can never be
 *   shown again - so it is surfaced inline instead, with everything left
 *   mounted on screen.
 */
const initialLoadError = computed(() => (fetched.value ? null : loadError.value))
const refetchError = computed(() => (fetched.value ? loadError.value : null))

/**
 * The OAuth card emits `refresh` after a successful register/revoke so the
 * table picks up the change. The refetch is SILENT (silentRefetch above), so
 * it never unmounts the card or any other while a reveal dialog is open, and
 * a failure is NOT discarded: a rejected refetch lands in `loadError`, which
 * `refetchError` renders in the inline aria-live region without replacing the
 * page. The catch only covers a rejection escaping `load()` itself (vue-query
 * reports most failures through its error state rather than by rejecting) -
 * record those the same way instead of swallowing them.
 */
async function refreshAfterOauthMutation(): Promise<void> {
  try {
    await loadAll()
  } catch (e: unknown) {
    loadError.value = formatApiError(e)
  }
}

const createKeyDialogOpen = ref(false)
const createKeyName = ref('')
const createKeyNameTouched = ref(false)
const createKeyRole = ref('operator')
const creatingKey = ref(false)
const createKeyError = ref<string | null>(null)

const keyCreatedDialogOpen = ref(false)
const createdKeyName = ref('')
// The one-time API key itself lives in `useSecretReveal`: value, the 10s
// countdown, auto-masking when it elapses, and the wipe on close.
const {
  value: createdKeyValue,
  masked: keyMasked,
  countdown: keyMaskCountdown,
  reveal: revealKey,
  onClose: closeKeySecret,
} = useSecretReveal()

const revokeKeyDialogOpen = ref(false)
const revokeKeyTarget = ref<ApiKeyItem | null>(null)
const revokingKey = ref(false)
const revokeKeyError = ref<string | null>(null)

const copiedField = ref<string | null>(null)
// Set when a clipboard write REJECTED. `navigator.clipboard` only exists in a
// secure context, so a self-hosted instance on plain HTTP can never copy - and
// for the one-time credential dialogs the clipboard is the only way to keep
// the value. A console warning alone would lose the credential silently, so
// the failing field carries a visible "copy manually" message instead.
const copyFailedField = ref<string | null>(null)
let mcpCopyTimeout: ReturnType<typeof setTimeout> | null = null

const selectedMcpClient = ref('opencode')

const mcpConfigSnippet = computed(() => {
  const url = mcpUrl.value || 'http://localhost:8000'
  switch (selectedMcpClient.value) {
    case 'opencode':
      return `mcp {\n  server = "${url}"\n}`
    case 'claude':
    case 'cursor':
      return JSON.stringify({
        mcpServers: {
          modulo: { url, apiKey: '<YOUR_API_KEY>' },
        },
      }, null, 2)
    case 'continue':
      return JSON.stringify({
        experimental: {
          mcp: {
            servers: {
              modulo: { url, apiKey: '<YOUR_API_KEY>' },
            },
          },
        },
      }, null, 2)
    case 'custom':
      return `MCP_SERVER_URL=${url}`
    default:
      return ''
  }
})

function formatDate(iso: string): string {
  try {
    return formatDateShort(new Date(iso))
  } catch {
    return iso
  }
}

type ApiKeyStatus = 'active' | 'expired' | 'revoked'

/**
 * FAR-1296: derive the key's status.
 *
 * `is_active` is DERIVED backend state, not a stored flag: the serializer
 * computes `revoked_at is None and (expires_at is None or expires_at > now)`
 * (backend/src/modulo/auth/api_key.py:359). So `is_active: false` says only
 * that the key stopped working - never WHY - and labelling every inactive key
 * "Revoked" called an expired key by an admin's action.
 *
 * The list payload carries `expires_at`, which separates the two causes:
 *
 * - `active`   - `is_active` is true.
 * - `expired`  - inactive AND the expiry has elapsed: nobody revoked it, the
 *                clock did.
 * - `revoked`  - inactive with no elapsed expiry. An expiry that never came
 *                due cannot explain the inactive state, so revocation can.
 *
 * The payload does not carry `revoked_at`, and the list endpoint filters
 * revoked rows out entirely (`include_revoked=False` - api_key.py:380/384-385),
 * so `revoked` is the sound complement of the other two rather than a directly
 * observed field. An unparseable `expires_at` on an inactive key falls to
 * `revoked`, matching the backend's own "not provably expired" reading.
 */
function apiKeyStatus(key: ApiKeyItem): ApiKeyStatus {
  if (key.is_active) return 'active'
  if (key.expires_at && Date.parse(key.expires_at) <= Date.now()) return 'expired'
  return 'revoked'
}

function apiKeyStatusMeta(key: ApiKeyItem): { severity: 'success' | 'warn' | 'secondary'; label: string } {
  switch (apiKeyStatus(key)) {
    case 'active':
      return { severity: 'success', label: t('views.SettingsMcpView.active') }
    case 'expired':
      return { severity: 'warn', label: t('views.SettingsMcpView.expired') }
    default:
      return { severity: 'secondary', label: t('views.SettingsMcpView.revoked') }
  }
}

/**
 * Wipe the revealed API key dialog's surrounding state on close - the value
 * is only ever shown once, so it must not outlive the dialog (same convention
 * as AdminUsersView's `dismissCredentialState`). `useSecretReveal` owns the
 * secret itself (see `closeKeySecret` below); this clears the bits around it.
 */
function dismissKeyCredentialState() {
  createdKeyName.value = ''
  if (copyFailedField.value === 'key-value') copyFailedField.value = null
}

/**
 * Bound to the Dialog's `hide` event, which PrimeVue emits for EVERY close
 * path (mask click, X, ESC, and the prop-driven close) - unlike
 * `update:visible`, which never fires when the parent flips the v-model.
 */
function onKeyCreatedDialogClose() {
  closeKeySecret() // stop the countdown and wipe the secret
  dismissKeyCredentialState()
}

function dismissKeyCreatedDialog() {
  keyCreatedDialogOpen.value = false
  onKeyCreatedDialogClose()
}

function openCreateKeyDialog() {
  createKeyName.value = ''
  createKeyNameTouched.value = false
  createKeyRole.value = 'operator'
  createKeyError.value = null
  createKeyDialogOpen.value = true
}

async function createKey() {
  if (!createKeyName.value.trim()) return
  creatingKey.value = true
  createKeyError.value = null
  try {
    const { data, error: err } = await api.POST('/api/v1/api-keys', {
      body: { name: createKeyName.value.trim(), role: createKeyRole.value },
    })
    if (err) {
      createKeyError.value = formatApiError(err)
    } else if (data) {
      createdKeyName.value = data.name
      createKeyDialogOpen.value = false
      keyCreatedDialogOpen.value = true
      revealKey(data.key_value)
      await loadAll()
    }
  } catch (e: unknown) {
    createKeyError.value = formatApiError(e)
  } finally {
    creatingKey.value = false
  }
}

function confirmRevokeKey(key: ApiKeyItem) {
  revokeKeyTarget.value = key
  revokeKeyError.value = null
  revokeKeyDialogOpen.value = true
}

/**
 * Revoke = DELETE (FAR-1291). The previous implementation PUT `{ is_active:
 * false }` to `/api/v1/api-keys/{key_id}`, but `ApiKeyUpdate` declares only
 * name/role/team_id/expires_at/scope and Pydantic drops the unknown
 * `is_active` silently - the request succeeded, the dialog closed, and the key
 * stayed Active. Revocation is its own endpoint (DELETE), which sets
 * `revoked_at`, writes the `api_key_revoked` audit event and returns
 * `ApiKeyRevokeResponse { id, revoked }` (200).
 *
 * A 404 means the key is already gone (revoked/deleted elsewhere), i.e. the
 * desired end state already holds - so it is treated as success (close +
 * refresh) rather than surfaced as a failure.
 */
async function revokeKey() {
  if (!revokeKeyTarget.value) return
  revokingKey.value = true
  revokeKeyError.value = null
  try {
    const { error: err, response } = await api.DELETE('/api/v1/api-keys/{key_id}', {
      params: { path: { key_id: revokeKeyTarget.value.id } },
    })
    const alreadyGone = response?.status === 404
    if (err && !alreadyGone) {
      revokeKeyError.value = formatApiError(err)
    } else {
      revokeKeyDialogOpen.value = false
      revokeKeyTarget.value = null
      await loadAll()
    }
  } catch (e: unknown) {
    revokeKeyError.value = formatApiError(e)
  } finally {
    revokingKey.value = false
  }
}

function copyServerUrl() {
  copyToClipboard(mcpUrl.value || 'http://localhost:8000', 'server-url')
}
function copySnippet() {
  copyToClipboard(mcpConfigSnippet.value, 'mcp-snippet')
}

async function copyToClipboard(text: string, field: string) {
  copyFailedField.value = null
  try {
    await navigator.clipboard.writeText(text)
    copiedField.value = field
    if (mcpCopyTimeout) clearTimeout(mcpCopyTimeout)
    mcpCopyTimeout = setTimeout(() => {
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

onMounted(() => { planStore.fetchPlan() })
// The API key reveal timer is owned by `useSecretReveal`, which clears it on
// unmount; only the copy "Copied" flash timeout is left to clean up here.
onUnmounted(() => {
  if (mcpCopyTimeout) clearTimeout(mcpCopyTimeout)
})
</script>
