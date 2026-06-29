<template>
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
            <div>
              <p class="text-sm font-medium">Server URL</p>
              <p class="mt-0.5 font-mono text-sm text-muted-foreground">{{ mcpUrl || 'http://localhost:8000' }}</p>
            </div>
            <Badge :variant="mcpUrl ? 'default' : 'outline'">
              {{ mcpUrl ? 'Active' : 'Local Only' }}
            </Badge>
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
          <div v-if="!hasActiveKey" class="py-8 text-center text-sm text-muted-foreground">
            Create an API key first to see configuration snippets.
          </div>

          <Tabs v-else default-value="claude" class="w-full">
            <TabsList>
              <TabsTrigger value="claude">Claude Desktop</TabsTrigger>
              <TabsTrigger value="cursor">Cursor</TabsTrigger>
              <TabsTrigger value="generic">Generic (curl)</TabsTrigger>
            </TabsList>

            <TabsContent value="claude" class="mt-4">
              <div class="relative">
                <pre
                  class="max-h-64 overflow-x-auto rounded-lg bg-muted p-4 font-mono text-xs leading-relaxed"
                >{{ claudeSnippet }}</pre>
                <Button
                  variant="outline"
                  size="sm"
                  class="absolute right-2 top-2"
                  data-testid="settings-mcp-copy-claude"
                  @click="copyToClipboard(claudeSnippet, 'claude')"
                >
                  {{ copiedField === 'claude' ? 'Copied!' : 'Copy' }}
                </Button>
              </div>
            </TabsContent>

            <TabsContent value="cursor" class="mt-4">
              <div class="relative">
                <pre
                  class="max-h-64 overflow-x-auto rounded-lg bg-muted p-4 font-mono text-xs leading-relaxed"
                >{{ cursorSnippet }}</pre>
                <Button
                  variant="outline"
                  size="sm"
                  class="absolute right-2 top-2"
                  data-testid="settings-mcp-copy-cursor"
                  @click="copyToClipboard(cursorSnippet, 'cursor')"
                >
                  {{ copiedField === 'cursor' ? 'Copied!' : 'Copy' }}
                </Button>
              </div>
            </TabsContent>

            <TabsContent value="generic" class="mt-4">
              <div class="relative">
                <pre
                  class="max-h-64 overflow-x-auto rounded-lg bg-muted p-4 font-mono text-xs leading-relaxed"
                >{{ genericSnippet }}</pre>
                <Button
                  variant="outline"
                  size="sm"
                  class="absolute right-2 top-2"
                  data-testid="settings-mcp-copy-generic"
                  @click="copyToClipboard(genericSnippet, 'generic')"
                >
                  {{ copiedField === 'generic' ? 'Copied!' : 'Copy' }}
                </Button>
              </div>
            </TabsContent>
          </Tabs>
        </CardContent>
      </Card>

      <!-- Registered OAuth Clients -->
      <Card>
        <CardHeader>
          <CardTitle>Registered OAuth Clients</CardTitle>
          <CardDescription>MCP OAuth client applications registered for token-based auth</CardDescription>
        </CardHeader>
        <CardContent>
          <div v-if="oauthClients.length === 0" class="py-8 text-center text-sm text-muted-foreground">
            No OAuth clients registered.
          </div>

          <table v-else class="w-full text-sm">
            <thead>
              <tr class="border-b text-left text-muted-foreground">
                <th class="pb-2 font-medium">Name</th>
                <th class="pb-2 font-medium">Client ID</th>
                <th class="pb-2 font-medium">Scopes</th>
                <th class="pb-2 font-medium">Created</th>
                <th class="pb-2 font-medium" />
              </tr>
            </thead>
            <tbody class="divide-y divide-border">
              <tr v-for="client in oauthClients" :key="client.id" class="transition-colors hover:bg-muted/20">
                <td class="py-2.5 font-medium">{{ client.name }}</td>
                <td class="py-2.5 font-mono text-xs text-muted-foreground">{{ client.client_id }}</td>
                <td class="py-2.5 text-muted-foreground">{{ client.scopes.join(', ') || '—' }}</td>
                <td class="py-2.5 text-muted-foreground">{{ formatDate(client.created_at) }}</td>
                <td class="py-2.5 text-right">
                  <Button
                    variant="destructive"
                    size="sm"
                    data-testid="settings-mcp-revoke-client"
                    @click="confirmRevokeClient(client)"
                  >
                    Revoke
                  </Button>
                </td>
              </tr>
            </tbody>
          </table>
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
            />
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

    <!-- Revoke OAuth Client Confirmation Dialog -->
    <Dialog v-model:open="revokeClientDialogOpen">
      <DialogContent class="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Revoke OAuth Client</DialogTitle>
          <DialogDescription>
            Are you sure you want to revoke access for <strong>{{ revokeClientTarget?.name }}</strong>?
            This cannot be undone.
          </DialogDescription>
        </DialogHeader>
        <div v-if="revokeClientError" class="text-sm text-destructive">{{ revokeClientError }}</div>
        <DialogFooter class="gap-2 sm:justify-end">
          <Button variant="outline" @click="revokeClientDialogOpen = false">Cancel</Button>
          <Button
            variant="destructive"
            :disabled="revokingClient"
            data-testid="settings-mcp-revoke-client-confirm"
            @click="revokeClient"
          >
            {{ revokingClient ? 'Revoking...' : 'Confirm Revoke' }}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { api } from '../lib/api/client'
import type { components } from '../lib/api/client'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import { Badge } from '../components/ui/badge'
import { Button } from '../components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '../components/ui/card'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '../components/ui/dialog'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '../components/ui/tabs'

type McpConfigResponse = components['schemas']['McpConfigResponse']
type ApiKeyItem = components['schemas']['ApiKeyItem']
type ApiKeyCreatedResponse = components['schemas']['ApiKeyCreatedResponse']
type OAuthClientItem = components['schemas']['OAuthClientItem']

const loading = ref(true)
const loadError = ref<string | null>(null)

const mcpUrl = ref('')
const configSnippet = ref('')

const apiKeys = ref<ApiKeyItem[]>([])
const createKeyDialogOpen = ref(false)
const createKeyName = ref('')
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

const oauthClients = ref<OAuthClientItem[]>([])
const revokeClientDialogOpen = ref(false)
const revokeClientTarget = ref<OAuthClientItem | null>(null)
const revokingClient = ref(false)
const revokeClientError = ref<string | null>(null)

const copiedField = ref<string | null>(null)

const hasActiveKey = computed(() => apiKeys.value.some(k => k.is_active))

const claudeSnippet = computed(() => {
  if (configSnippet.value) return configSnippet.value
  return JSON.stringify({
    mcpServers: {
      modulo: {
        url: mcpUrl.value || 'http://localhost:8000',
        apiKey: 'YOUR_API_KEY',
      },
    },
  }, null, 2)
})

const cursorSnippet = computed(() => {
  return JSON.stringify({
    mcpServers: {
      modulo: {
        url: (mcpUrl.value || 'http://localhost:8000') + '/sse',
        apiKey: 'YOUR_API_KEY',
      },
    },
  }, null, 2)
})

const genericSnippet = computed(() => {
  const baseUrl = mcpUrl.value || 'http://localhost:8000'
  return [
    `curl -X POST "${baseUrl}/sse" \\`,
    `  -H "Authorization: Bearer YOUR_API_KEY" \\`,
    `  -H "Content-Type: application/json" \\`,
    `  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'`,
  ].join('\n')
})

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
    const [mcpResp, keysResp, clientsResp] = await Promise.all([
      (api as any).GET('/api/v1/api-keys/mcp-config'),
      (api as any).GET('/api/v1/api-keys'),
      (api as any).GET('/api/v1/mcp/oauth/clients'),
    ])

    if (mcpResp.error) {
      loadError.value = `Failed to load MCP config: ${mcpResp.error}`
      return
    }
    const mcpData = mcpResp.data as McpConfigResponse
    mcpUrl.value = mcpData.mcp_url
    configSnippet.value = mcpData.config_snippet

    if (keysResp.error) {
      loadError.value = `Failed to load API keys: ${keysResp.error}`
      return
    }
    apiKeys.value = (keysResp.data as { items: ApiKeyItem[] }).items

    if (clientsResp.error) {
      loadError.value = `Failed to load OAuth clients: ${clientsResp.error}`
      return
    }
    oauthClients.value = (clientsResp.data as { items: OAuthClientItem[] }).items
  } catch (e: unknown) {
    loadError.value = `Failed to load data: ${e instanceof Error ? e.message : String(e)}`
  } finally {
    loading.value = false
  }
}

function openCreateKeyDialog() {
  createKeyName.value = ''
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
      createKeyError.value = String(err)
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
    createKeyError.value = e instanceof Error ? e.message : String(e)
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
      revokeKeyError.value = String(err)
    } else {
      revokeKeyDialogOpen.value = false
      revokeKeyTarget.value = null
      await loadAll()
    }
  } catch (e: unknown) {
    revokeKeyError.value = e instanceof Error ? e.message : String(e)
  } finally {
    revokingKey.value = false
  }
}

function confirmRevokeClient(client: OAuthClientItem) {
  revokeClientTarget.value = client
  revokeClientError.value = null
  revokeClientDialogOpen.value = true
}

async function revokeClient() {
  if (!revokeClientTarget.value) return
  revokingClient.value = true
  revokeClientError.value = null
  try {
    const { error: err } = await (api as any).DELETE('/api/v1/mcp/oauth/clients/{client_id}', {
      params: { path: { client_id: revokeClientTarget.value.id } },
    })
    if (err) {
      revokeClientError.value = String(err)
    } else {
      revokeClientDialogOpen.value = false
      revokeClientTarget.value = null
      await loadAll()
    }
  } catch (e: unknown) {
    revokeClientError.value = e instanceof Error ? e.message : String(e)
  } finally {
    revokingClient.value = false
  }
}

async function copyToClipboard(text: string, field: string) {
  try {
    await navigator.clipboard.writeText(text)
    copiedField.value = field
    setTimeout(() => {
      if (copiedField.value === field) {
        copiedField.value = null
      }
    }, 2000)
  } catch {
    // Clipboard access denied
  }
}

onMounted(loadAll)
onUnmounted(clearKeyMaskTimer)
</script>
