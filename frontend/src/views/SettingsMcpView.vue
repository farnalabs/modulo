<template>
  <FeatureGate feature-name="mcp_server" show-disabled>

    <div data-theme="agent" class="page-wide">
    <PageHeader :title="$t('views.SettingsMcpView.mcp_configuration')" :subtitle="$t('views.SettingsMcpView.configure_mcp_server_settings_and_api_keys')" />

    <LoadingSpinner v-if="loading" />
    <ErrorAlert v-else-if="loadError" :message="loadError" :on-retry="loadAll" />

    <template v-else>
      <!-- MCP Server Status -->
      <Card>
        <template #title>{{ $t('views.SettingsMcpView.mcp_server_status') }}</template>
        <template #subtitle>{{ $t('views.SettingsMcpView.the_url_clients_use_to_connect_to_the_mcp_server') }}</template>
        <template #content>
        <div class="space-y-4">
          <div
            v-if="!mcpUrl"
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
              <Badge :severity="mcpUrl ? 'info' : 'secondary'">
                {{ mcpUrl ? $t('views.SettingsMcpView.active') : $t('views.SettingsMcpView.local_only') }}
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
            <Button data-testid="settings-mcp-create-key" @click="openCreateKeyDialog">
              {{ $t('views.SettingsMcpView.create_mcp_api_key') }}
            </Button>
          </div>
        </template>
        <template #content>
        <div>
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
                <td class="py-2.5">
                  <Badge :severity="key.is_active ? 'success' : 'secondary'">
                    {{ key.is_active ? $t('views.SettingsMcpView.active') : $t('views.SettingsMcpView.revoked') }}
                  </Badge>
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

      <!-- Registered OAuth Clients -->
      <Card>
        <template #title>{{ $t('views.SettingsMcpView.registered_oauth_clients') }}</template>
        <template #subtitle>{{ $t('views.SettingsMcpView.mcp_oauth_client_applications_registered_for_token_based_auth') }}</template>
        <template #content>
        <div class="space-y-4">
          <p class="text-sm text-muted-foreground">{{ $t('views.SettingsMcpView.configure_oauth_client_applications_for_mcp_token_based_auth') }}</p>
          <Button severity="secondary" outlined size="small" disabled>{{ $t('views.SettingsMcpView.register_oauth_client_coming_in_v04') }}</Button>
          </div>
        </template>
      </Card>
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

    <Dialog v-model:visible="keyCreatedDialogOpen" :modal="true" :dismissable-mask="true" class="sm:max-w-lg" @update:visible="onKeyCreatedDialogClose">
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
        <Button data-testid="settings-mcp-key-created-done" @click="keyCreatedDialogOpen = false">{{ $t('views.SettingsMcpView.done') }}</Button>
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
import Select from 'primevue/select'

const planStore = usePlanStore()

interface ApiKeyItem {
  id: string
  name: string
  role: string
  lookup_prefix: string
  is_active: boolean
  last_used_at: string | null
  created_at: string
}

interface McpPageData {
  mcpUrl: string
  apiKeys: ApiKeyItem[]
}

type ApiKeyCreatedResponse = components['schemas']['ApiKeyCreatedResponse']

const { loading, error: loadError, data: mcpData, load: loadAll } = useDataFetch<McpPageData>(
  async () => {
    const [mcpResp, keysResp] = await Promise.all([
      (api as any).GET('/api/v1/api-keys/mcp-config').catch(() => null),
      (api as any).GET('/api/v1/api-keys').catch(() => null),
    ])
    if (mcpResp.error) return { error: mcpResp.error }
    if (keysResp.error) return { error: keysResp.error }
    return { data: { mcpUrl: mcpResp.data.mcp_url, apiKeys: keysResp.data as ApiKeyItem[] } }
  },
  { initialValue: { mcpUrl: '', apiKeys: [] } }
)

const mcpUrl = computed(() => mcpData.value?.mcpUrl ?? '')
const apiKeys = computed(() => mcpData.value?.apiKeys ?? [])
const createKeyDialogOpen = ref(false)
const createKeyName = ref('')
const createKeyNameTouched = ref(false)
const createKeyRole = ref('operator')
const creatingKey = ref(false)
const createKeyError = ref<string | null>(null)

const keyCreatedDialogOpen = ref(false)
const createdKeyValue = ref('')
const createdKeyName = ref('')
const keyMasked = ref(false)
const keyMaskCountdown = ref(10)
let keyMaskTimer: ReturnType<typeof setInterval> | null = null

const revokeKeyDialogOpen = ref(false)
const revokeKeyTarget = ref<ApiKeyItem | null>(null)
const revokingKey = ref(false)
const revokeKeyError = ref<string | null>(null)

const copiedField = ref<string | null>(null)
let mcpCopyTimeout: ReturnType<typeof setTimeout> | null = null

const selectedMcpClient = ref('opencode')

const mcpConfigSnippet = computed(() => {
  const url = mcpUrl.value || 'http://localhost:8000'
  switch (selectedMcpClient.value) {
    case 'opencode':
      return `mcp {\n  server = "${url}"\n}`
    case 'claude':
      return JSON.stringify({
        mcpServers: {
          modulo: { url, apiKey: '<YOUR_API_KEY>' },
        },
      }, null, 2)
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

function clearKeyMaskTimer() {
  if (keyMaskTimer !== null) {
    clearInterval(keyMaskTimer)
    keyMaskTimer = null
  }
}

function startKeyMaskCountdown() {
  clearKeyMaskTimer()
  keyMasked.value = false
  keyMaskCountdown.value = 10
  keyMaskTimer = setInterval(() => {
    keyMaskCountdown.value--
    if (keyMaskCountdown.value <= 0) {
      keyMasked.value = true
      clearKeyMaskTimer()
    }
  }, 1000)
}

function onKeyCreatedDialogClose(open: boolean) {
  if (!open) {
    clearKeyMaskTimer()
    keyMasked.value = true
  }
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
    const { data, error: err } = await (api as any).POST('/api/v1/api-keys', {
      body: { name: createKeyName.value.trim(), role: createKeyRole.value },
    })
    if (err) {
      createKeyError.value = formatApiError(err)
    } else if (data) {
      const created = data as ApiKeyCreatedResponse
      createdKeyValue.value = created.key_value
      createdKeyName.value = created.name
      createKeyDialogOpen.value = false
      keyCreatedDialogOpen.value = true
      startKeyMaskCountdown()
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

async function revokeKey() {
  if (!revokeKeyTarget.value) return
  revokingKey.value = true
  revokeKeyError.value = null
  try {
    const { error: err } = await (api as any).PUT('/api/v1/api-keys/{key_id}', {
      params: { path: { key_id: revokeKeyTarget.value.id } },
      body: { is_active: false },
    })
    if (err) {
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
    console.warn('Failed to copy MCP config', e)
  }
}

onMounted(() => { planStore.fetchPlan() })
onUnmounted(() => {
  clearKeyMaskTimer()
  if (mcpCopyTimeout) clearTimeout(mcpCopyTimeout)
})
</script>
