<template>
  <FeatureGate feature-name="mcp_server" required-tier="team">
    <template #locked="{ tooltip }">
      <div data-theme="agent" class="mx-auto max-w-4xl space-y-8 p-6">
        <div class="mb-4 flex items-center gap-2 rounded-lg border border-warning/30 bg-warning/5 p-4 text-sm text-warning">
          <LockIcon :locked="true" :tooltip="tooltip" />
          <span>{{ $t('views.SettingsMcpView.mcp_server_is_not_available_on_your_current_plan') }}</span>
        </div>
      </div>
    </template>

    <div data-theme="agent" class="mx-auto max-w-4xl space-y-8 p-6">
    <header>
      <h1 class="text-3xl font-bold tracking-tight">MCP Configuration</h1>
      <p class="mt-1 text-muted-foreground">Configure Model Context Protocol (MCP) server settings and API keys</p>
    </header>

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
                <td class="py-2.5 font-mono text-muted-foreground">{{ key.prefix }}...</td>
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
        <CardContent>
          <div class="rounded-lg border bg-card p-8 text-center">
            <p class="text-lg font-medium">Configuration Snippets</p>
            <p class="mt-1 text-sm text-muted-foreground">Coming soon — code snippets for popular MCP clients will appear here.</p>
          </div>
        </CardContent>
      </Card>

      <!-- Registered OAuth Clients -->
      <Card>
        <CardHeader>
          <CardTitle>Registered OAuth Clients</CardTitle>
          <CardDescription>MCP OAuth client applications registered for token-based auth</CardDescription>
        </CardHeader>
        <CardContent>
          <div class="rounded-lg border bg-card p-8 text-center">
            <p class="text-lg font-medium">OAuth Clients</p>
            <p class="mt-1 text-sm text-muted-foreground">Coming soon — OAuth client registration will appear here.</p>
          </div>
        </CardContent>
      </Card>
    </template>

    <!-- Create API Key Dialog -->
    <Dialog v-model:open="createKeyDialogOpen">
      <DialogContent class="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Create MCP API Key</DialogTitle>
          <DialogDescription>Generate a new API key for MCP client authentication</DialogDescription>
        </DialogHeader>
        <div class="space-y-4 py-2">
          <div>
            <label class="mb-1 block text-sm font-medium">Key Name</label>
            <input
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
            <label class="mb-1 block text-sm font-medium">Role</label>
            <select
              v-model="createKeyRole"
              data-testid="settings-mcp-create-key-role"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <option value="operator">Operator</option>
              <option value="runner">Runner</option>
            </select>
          </div>
          <div v-if="createKeyError" class="text-sm text-destructive">{{ createKeyError }}</div>
        </div>
        <DialogFooter class="gap-2 sm:justify-end">
          <Button variant="outline" @click="createKeyDialogOpen = false">Cancel</Button>
          <Button
            :disabled="!createKeyName.trim() || creatingKey"
            data-testid="settings-mcp-create-key-submit"
            @click="createKey"
          >
            {{ creatingKey ? 'Creating...' : 'Create' }}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>

    <!-- Key Created Dialog (one-time display) -->
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
              <input
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

    <!-- Revoke API Key Confirmation Dialog -->
    <Dialog v-model:open="revokeKeyDialogOpen">
      <DialogContent class="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Revoke API Key</DialogTitle>
          <DialogDescription>
            Are you sure you want to revoke the key <strong>{{ revokeKeyTarget?.name }}</strong>?
            Any clients using this key will lose access immediately.
          </DialogDescription>
        </DialogHeader>
        <div v-if="revokeKeyError" class="text-sm text-destructive">{{ revokeKeyError }}</div>
        <DialogFooter class="gap-2 sm:justify-end">
          <Button variant="outline" @click="revokeKeyDialogOpen = false">Cancel</Button>
          <Button
            variant="destructive"
            :disabled="revokingKey"
            data-testid="settings-mcp-revoke-key-confirm"
            @click="revokeKey"
          >
            {{ revokingKey ? 'Revoking...' : 'Confirm Revoke' }}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>

  </div>
  </FeatureGate>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import { api } from '../lib/api/client'
import { formatApiError, type ProblemDetail } from '../lib/api/formatError'
import type { components } from '../lib/api/client'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import { Badge } from '../components/ui/badge'
import { Button } from '../components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '../components/ui/card'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '../components/ui/dialog'
import { usePlanStore } from '../stores/planStore'
import FeatureGate from '../components/FeatureGate.vue'
import LockIcon from '../components/LockIcon.vue'

const planStore = usePlanStore()

type McpConfigResponse = components['schemas']['McpConfigResponse']
type ApiKeyItem = components['schemas']['ApiKeyItem']
type ApiKeyCreatedResponse = components['schemas']['ApiKeyCreatedResponse']

const loading = ref(true)
const loadError = ref<string | null>(null)

const mcpUrl = ref('')

const apiKeys = ref<ApiKeyItem[]>([])
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

function formatDate(iso: string): string {
  try {
    return new Date(iso).toLocaleDateString('en-US', {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
    })
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

async function loadAll() {
  loading.value = true
  loadError.value = null
  try {
    const [mcpResp, keysResp] = await Promise.all([
      (api as any).GET('/api/v1/api-keys/mcp-config'),
      (api as any).GET('/api/v1/api-keys'),
    ])

    if (mcpResp.error) {
      loadError.value = `Failed to load MCP config: ${formatApiError(mcpResp.error)}`
      return
    }
    const mcpData = mcpResp.data as McpConfigResponse
    mcpUrl.value = mcpData.mcp_url

    if (keysResp.error) {
      loadError.value = `Failed to load API keys: ${formatApiError(keysResp.error)}`
      return
    }
    apiKeys.value = (keysResp.data as { items: ApiKeyItem[] }).items
  } catch (e: unknown) {
      loadError.value = e && typeof e === 'object' && 'detail' in e
        ? `Failed to load data: ${(e as ProblemDetail).detail}`
        : `Failed to load data: ${formatApiError(e)}`
  } finally {
    loading.value = false
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
      createKeyError.value = err
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
    createKeyError.value = e
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
      revokeKeyError.value = err
    } else {
      revokeKeyDialogOpen.value = false
      revokeKeyTarget.value = null
      await loadAll()
    }
  } catch (e: unknown) {
    revokeKeyError.value = e
  } finally {
    revokingKey.value = false
  }
}

function copyServerUrl() {
  copyToClipboard(mcpUrl.value || 'http://localhost:8000', 'server-url')
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
  } catch {
    // Clipboard access denied
  }
}

onMounted(() => { planStore.fetchPlan(); loadAll() })
onUnmounted(() => {
  clearKeyMaskTimer()
  if (mcpCopyTimeout) clearTimeout(mcpCopyTimeout)
})
</script>
