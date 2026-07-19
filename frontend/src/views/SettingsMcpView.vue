<template>
  <FeatureGate feature-name="mcp_server" show-disabled>

    <div data-theme="agent" class="page-narrow">
    <PageHeader title="MCP Configuration" subtitle="Configure Model Context Protocol (MCP) server settings and API keys" />

    <LoadingSpinner v-if="loading" />
    <ErrorAlert v-else-if="loadError" :message="loadError" :on-retry="loadAll" />

    <template v-else>
      <!-- MCP Server Status -->
      <Card>
        <CardHeader>
          <CardTitle>MCP Server Status</CardTitle>
          <CardDescription>The URL clients use to connect to the MCP server</CardDescription>
        </CardHeader>
        <CardContent class="space-y-4">
          <div
            v-if="!mcpUrl"
            class="rounded-lg border border-warning/50 bg-warning/10 p-4 text-sm text-warning"
          >
            <p class="font-medium">MODULO_PUBLIC_URL not set</p>
            <p class="mt-1">
              The MODULO_PUBLIC_URL environment variable is not configured.
              The MCP server URL will fall back to <code class="rounded bg-warning/10 px-1 py-0.5 text-xs">http://localhost:8000</code>.
            </p>
          </div>

          <div class="flex items-center justify-between rounded-lg bg-muted/30 p-4">
            <div class="min-w-0 flex-1">
              <p class="text-sm font-medium">Server URL</p>
              <p class="mt-0.5 select-all cursor-text font-mono text-sm text-muted-foreground">{{ mcpUrl || 'http://localhost:8000' }}</p>
            </div>
            <div class="flex shrink-0 items-center gap-2">
              <Button
                variant="outline"
                size="sm"
                data-testid="settings-mcp-copy-url"
                @click="copyServerUrl"
              >
                {{ copiedField === 'server-url' ? 'Copied!' : 'Copy' }}
              </Button>
              <Badge :variant="mcpUrl ? 'default' : 'outline'">
                {{ mcpUrl ? 'Active' : 'Local Only' }}
              </Badge>
            </div>
          </div>
        </CardContent>
      </Card>

      <!-- API Key Management -->
      <Card>
        <CardHeader class="flex flex-row items-center justify-between">
          <div>
            <CardTitle>API Keys</CardTitle>
            <CardDescription>Create and manage API keys for MCP client authentication</CardDescription>
          </div>
          <Button data-testid="settings-mcp-create-key" @click="openCreateKeyDialog">
            Create MCP API Key
          </Button>
        </CardHeader>
        <CardContent>
          <div v-if="apiKeys.length === 0" class="py-8 text-center text-sm text-muted-foreground">
            No API keys created yet.
          </div>

          <table v-else class="w-full text-sm">
            <thead>
              <tr class="border-b text-left text-muted-foreground">
                <th class="pb-2 font-medium">Name</th>
                <th class="pb-2 font-medium">Key Prefix</th>
                <th class="pb-2 font-medium">Role</th>
                <th class="pb-2 font-medium">Status</th>
                <th class="pb-2 font-medium">Last Used</th>
                <th class="pb-2 font-medium" />
              </tr>
            </thead>
            <tbody class="divide-y divide-border">
              <tr v-for="key in apiKeys" :key="key.id" class="transition-colors hover:bg-muted/20">
                <td class="py-2.5 font-medium">{{ key.name }}</td>
                <td class="py-2.5 font-mono text-muted-foreground">{{ key.lookup_prefix }}</td>
                <td class="py-2.5 capitalize">{{ key.role }}</td>
                <td class="py-2.5">
                  <Badge :variant="key.is_active ? 'default' : 'secondary'">
                    {{ key.is_active ? 'Active' : 'Revoked' }}
                  </Badge>
                </td>
                <td class="py-2.5 text-muted-foreground">
                  {{ key.last_used_at ? formatDate(key.last_used_at) : 'Never' }}
                </td>
                <td class="py-2.5 text-right">
                  <Button
                    v-if="key.is_active"
                    variant="destructive"
                    size="sm"
                    data-testid="settings-mcp-revoke-key"
                    @click="confirmRevokeKey(key)"
                  >
                    Revoke
                  </Button>
                </td>
              </tr>
            </tbody>
          </table>
        </CardContent>
      </Card>

      <!-- Config Snippets -->
      <Card>
        <CardHeader>
          <CardTitle>Configuration Snippets</CardTitle>
          <CardDescription>Copy these snippets to configure MCP clients</CardDescription>
        </CardHeader>
        <CardContent class="space-y-4">
          <div class="flex items-center gap-2">
            <label for="settingsmcpview-client" class="text-sm font-medium whitespace-nowrap">Client:</label>
            <Select v-model="selectedMcpClient">
              <SelectTrigger id="settingsmcpview-client" class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm" aria-label="Client">
                <SelectValue placeholder="Select client" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="opencode">opencode / Claude Code</SelectItem>
                <SelectItem value="claude">Claude Desktop</SelectItem>
                <SelectItem value="cursor">Cursor</SelectItem>
                <SelectItem value="continue">Continue.dev</SelectItem>
                <SelectItem value="custom">Custom</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div class="rounded-lg bg-muted/30 p-4">
            <pre class="text-xs font-mono whitespace-pre-wrap break-all">{{ mcpConfigSnippet }}</pre>
            <Button variant="outline" size="sm" class="mt-2" @click="copySnippet">Copy</Button>
          </div>
        </CardContent>
      </Card>

      <!-- Registered OAuth Clients -->
      <Card>
        <CardHeader>
          <CardTitle>Registered OAuth Clients</CardTitle>
          <CardDescription>MCP OAuth client applications registered for token-based auth</CardDescription>
        </CardHeader>
        <CardContent class="space-y-4">
          <p class="text-sm text-muted-foreground">Configure OAuth client applications for MCP token-based auth.</p>
          <Button variant="outline" size="sm" disabled>Register OAuth Client (coming in v0.4)</Button>
        </CardContent>
      </Card>
    </template>

    <FormDialog
      v-model:open="createKeyDialogOpen"
      title="Create MCP API Key"
      description="Generate a new API key for MCP client authentication"
      confirmText="Create"
      :confirmDisabled="!createKeyName.trim()"
      :loading="creatingKey"
      @confirm="createKey"
    >
      <div class="space-y-4 py-2">
        <div>
          <label for="settingsmcpview-field-2" class="mb-1 block text-sm font-medium">Key Name</label>
          <input id="settingsmcpview-field-2"
            v-model="createKeyName"
            type="text"
            data-testid="settings-mcp-create-key-name"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            placeholder="e.g. Claude Desktop"
            @blur="createKeyNameTouched = true"
          />
          <p
            v-if="createKeyNameTouched && !createKeyName.trim()"
            class="mt-1 text-sm text-destructive"
          >Key name is required.</p>
        </div>
        <div>
          <label for="settingsmcpview-role" class="mb-1 block text-sm font-medium">Role</label>
          <Select v-model="createKeyRole">
            <SelectTrigger id="settingsmcpview-role" class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" aria-label="Role" data-testid="settings-mcp-create-key-role">
              <SelectValue placeholder="Select role" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="operator">Operator</SelectItem>
              <SelectItem value="runner">Runner</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div v-if="createKeyError" class="text-sm text-destructive">{{ createKeyError }}</div>
      </div>
    </FormDialog>

    <Dialog v-model:open="keyCreatedDialogOpen" @update:open="onKeyCreatedDialogClose">
      <DialogContent class="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>API Key Created</DialogTitle>
          <DialogDescription>
            Copy this key now. You will not be able to see it again.
          </DialogDescription>
        </DialogHeader>
        <div class="space-y-4 py-2">
          <div>
            <p class="mb-1 text-sm font-medium">Key Name</p>
            <p class="text-sm text-muted-foreground">{{ createdKeyName }}</p>
          </div>
          <div>
            <p class="mb-1 text-sm font-medium">API Key</p>
            <div class="relative">
              <input aria-label="keyMasked ? "
                :type="keyMasked ? 'password' : 'text'"
                :value="createdKeyValue"
                readonly
                class="w-full rounded-lg border border-input bg-muted px-3 py-2 font-mono text-sm"
              />
              <Button
                variant="outline"
                size="sm"
                class="absolute right-1 top-1"
                data-testid="settings-mcp-copy-key-value"
                @click="copyToClipboard(createdKeyValue, 'key-value')"
              >
                {{ copiedField === 'key-value' ? 'Copied!' : 'Copy' }}
              </Button>
            </div>
            <p v-if="!keyMasked" class="mt-1 text-xs text-muted-foreground">
              This key will be masked in {{ keyMaskCountdown }}s
            </p>
          </div>
        </div>
        <DialogFooter class="gap-2 sm:justify-end">
          <Button @click="keyCreatedDialogOpen = false">Done</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>

    <FormDialog
      v-model:open="revokeKeyDialogOpen"
      title="Revoke API Key"
      confirmText="Confirm Revoke"
      :loading="revokingKey"
      @confirm="revokeKey"
    >
      <p class="text-sm text-muted-foreground">
        Are you sure you want to revoke the key <strong>{{ revokeKeyTarget?.name }}</strong>?
        Any clients using this key will lose access immediately.
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
import { Badge } from '../components/ui/badge'
import { Button } from '../components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '../components/ui/card'
import FormDialog from '../components/shared/FormDialog.vue'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '../components/ui/dialog'
import { usePlanStore } from '../stores/planStore'
import FeatureGate from '../components/FeatureGate.vue'
import { formatDateShort } from '../lib/formatDate'
import {
  Select,
  SelectTrigger,
  SelectContent,
  SelectItem,
  SelectValue,
} from '@/components/ui/select'

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
