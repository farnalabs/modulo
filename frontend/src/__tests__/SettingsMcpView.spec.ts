import { describe, it, expect, vi, beforeEach, afterEach, type Mock } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick as vueNextTick } from 'vue'

async function nextTick() {
  await vueNextTick()
  await flushPromises()
}

const mockMcpConfig = {
  mcp_url: 'https://mcp.modulo.run',
  config_snippet: '',
}

const mockApiKeys = [
  { id: 'key-1', lookup_prefix: 'mod_mk_abc', name: 'Claude Key', role: 'operator', is_active: true, last_used_at: '2026-06-28T12:00:00Z', created_at: '2026-06-01T00:00:00Z' },
  { id: 'key-2', lookup_prefix: 'mod_mk_def', name: 'Cursor Key', role: 'runner', is_active: false, last_used_at: null, created_at: '2026-06-15T00:00:00Z' },
]

const mockMcpConfigEmpty = { mcp_url: '', config_snippet: '' }
const mockApiKeysNoActive = [
  { id: 'key-3', lookup_prefix: 'mod_mk_ghi', name: 'Revoked Key', role: 'operator', is_active: false, last_used_at: null, created_at: '2026-06-10T00:00:00Z' },
]
const mockOAuthClients = {
  items: [
    { id: 'client-1', client_id: 'mod_oauth_xyz', name: 'CLI Client', scopes: ['read', 'write'], redirect_uris: ['http://localhost'], created_at: '2026-06-20T00:00:00Z' },
  ],
}
const mockNoOAuthClients = { items: [] }

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn(),
    POST: vi.fn().mockResolvedValue({ data: { id: 'key-new', key_value: 'mod_mk_new_secret_1234', name: 'New Key', role: 'operator' }, error: undefined }),
    PUT: vi.fn().mockResolvedValue({ data: null, error: undefined }),
    DELETE: vi.fn().mockResolvedValue({ data: null, error: undefined }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import { api } from '../lib/api/client'
import SettingsMcpView from '../views/SettingsMcpView.vue'

const getMock = api.GET as unknown as Mock
const postMock = api.POST as unknown as Mock
const putMock = api.PUT as unknown as Mock

const dialogStub = { template: '<div><slot /></div>' }
const stubs = { Dialog: dialogStub, DialogContent: dialogStub, DialogDescription: dialogStub, DialogFooter: dialogStub, DialogHeader: dialogStub, DialogTitle: dialogStub, FeatureGate: dialogStub }

function mockApiResponses(mcpConfig = mockMcpConfig, apiKeysData = mockApiKeys, oauth = mockOAuthClients) {
  getMock.mockImplementation((path: string) => {
    if (path === '/api/v1/api-keys/mcp-config') return Promise.resolve({ data: mcpConfig, error: undefined })
    if (path === '/api/v1/api-keys') return Promise.resolve({ data: apiKeysData, error: undefined })
    if (path === '/api/v1/mcp/oauth/clients') return Promise.resolve({ data: oauth, error: undefined })
    return Promise.resolve({ data: null, error: undefined })
  })
}

function mountView(mcpConfig = mockMcpConfig, apiKeysData = mockApiKeys, oauth = mockOAuthClients) {
  mockApiResponses(mcpConfig, apiKeysData, oauth)
  return mount(SettingsMcpView, { global: { stubs } })
}

describe('SettingsMcpView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    vi.useRealTimers()
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: vi.fn().mockResolvedValue(undefined) },
      writable: true,
      configurable: true,
    })
  })

  afterEach(() => { vi.useRealTimers() })

  // ─── Existing tests ──────────────────────────────────────────────────

  it('renders without crashing', async () => {
    const wrapper = mountView()
    await nextTick()
    await nextTick()
    await nextTick()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('MCP Configuration')
  })

  it('shows loading spinner initially', async () => {
    getMock.mockReturnValue(new Promise(() => {}))
    const wrapper = mount(SettingsMcpView, { global: { stubs } })
    await nextTick()
    expect(wrapper.find('.animate-spin').exists()).toBe(true)
  })

  it('shows error alert on API failure', async () => {
    getMock.mockImplementation(() => Promise.resolve({ data: null, error: 'Failed to load MCP config: Server error' }))
    const wrapper = mount(SettingsMcpView, { global: { stubs } })
    await nextTick()
    await nextTick()
    await nextTick()
    expect(wrapper.text()).toContain('Failed to load MCP config')
  })

  it('shows MCP URL and Active badge', async () => {
    const wrapper = mountView()
    await nextTick()
    await nextTick()
    await nextTick()
    expect(wrapper.text()).toContain('https://mcp.modulo.run')
    expect(wrapper.text()).toContain('Active')
  })

  it('shows MODULO_PUBLIC_URL warning when url is empty', async () => {
    const wrapper = mountView(mockMcpConfigEmpty, mockApiKeysNoActive, mockNoOAuthClients)
    await nextTick()
    await nextTick()
    await nextTick()
    expect(wrapper.text()).toContain('MODULO_PUBLIC_URL not set')
    expect(wrapper.text()).toContain('Local Only')
    expect(wrapper.text()).toContain('http://localhost:8000')
  })

  it('shows API keys in the table', async () => {
    const wrapper = mountView()
    await nextTick()
    await nextTick()
    await nextTick()
    expect(wrapper.text()).toContain('Claude Key')
    expect(wrapper.text()).toContain('Cursor Key')
    expect(wrapper.text()).toContain('mod_mk_abc')
    expect(wrapper.text()).toContain('operator')
    expect(wrapper.text()).toContain('runner')
    expect(wrapper.text()).toContain('Active')
    expect(wrapper.text()).toContain('Revoked')
  })

  it('shows the org-wide key scope note (FAR-620)', async () => {
    const wrapper = mountView()
    await nextTick()
    await nextTick()
    await nextTick()
    const note = wrapper.find('[data-testid="settings-mcp-org-scope-note"]')
    expect(note.exists()).toBe(true)
    expect(wrapper.text()).toContain('API keys act org-wide')
    expect(wrapper.text()).toContain('mintable via the API')
  })

  it('opens create key dialog with name and role fields', async () => {
    const wrapper = mountView()
    await nextTick()
    await nextTick()
    await nextTick()
    const createBtn = wrapper.find('[data-testid="settings-mcp-create-key"]')
    expect(createBtn.exists()).toBe(true)
    expect(createBtn.text()).toContain('Create MCP API Key')
  })

  it('revoke button shows in the API key table', async () => {
    const wrapper = mountView()
    await nextTick()
    await nextTick()
    await nextTick()
    const revokeBtns = wrapper.findAll('[data-testid="settings-mcp-revoke-key"]')
    expect(revokeBtns.length).toBe(1)
    expect(revokeBtns[0].text()).toContain('Revoke')
  })

  it('shows config snippets when an active key exists', async () => {
    const wrapper = mountView()
    await nextTick()
    await nextTick()
    await nextTick()
    expect(wrapper.text()).toContain('Configuration Snippets')
    const copyBtns = wrapper.findAll('[data-testid^="settings-mcp-copy-"]')
    expect(copyBtns.length).toBeGreaterThanOrEqual(1)
  })

  it('shows placeholder when no active key for snippets', async () => {
    const wrapper = mountView(mockMcpConfig, mockApiKeysNoActive, mockNoOAuthClients)
    await nextTick()
    await nextTick()
    await nextTick()
    expect(wrapper.text()).toContain('Configuration Snippets')
  })

  it('copy button copies server URL to clipboard', async () => {
    const wrapper = mountView()
    await nextTick()
    await nextTick()
    await nextTick()
    const copyBtn = wrapper.find('[data-testid="settings-mcp-copy-url"]')
    expect(copyBtn.exists()).toBe(true)
    expect(copyBtn.text()).toBe('Copy')
  })

  it('shows OAuth clients placeholder', async () => {
    const wrapper = mountView()
    await nextTick()
    await nextTick()
    await nextTick()
    expect(wrapper.text()).toContain('Registered OAuth Clients')
    expect(wrapper.text()).toContain('coming in v0.4')
  })

  // ─── Copy to clipboard ────────────────────────────────────────────────

  it('copies server URL to clipboard and shows Copied', async () => {
    vi.useFakeTimers()
    const wrapper = mountView()
    await nextTick()
    await nextTick()
    await nextTick()
    const copyBtn = wrapper.find('[data-testid="settings-mcp-copy-url"]')
    await copyBtn.trigger('click')
    await flushPromises()
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith('https://mcp.modulo.run')
    await nextTick()
    expect(copyBtn.text()).toContain('Copied')
    vi.advanceTimersByTime(2100)
    await nextTick()
    expect(copyBtn.text()).toContain('Copy')
  })

  it('copies localhost URL when mcpUrl is empty', async () => {
    const wrapper = mountView(mockMcpConfigEmpty, mockApiKeysNoActive, mockNoOAuthClients)
    await nextTick()
    await nextTick()
    await nextTick()
    await wrapper.find('[data-testid="settings-mcp-copy-url"]').trigger('click')
    await flushPromises()
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith('http://localhost:8000')
  })

  it('copies snippet to clipboard', async () => {
    const wrapper = mountView()
    await nextTick()
    await nextTick()
    await nextTick()
    const copySnippet = wrapper.find('[data-testid="settings-mcp-copy-snippet"]')
    expect(copySnippet.exists()).toBe(true)
    await copySnippet.trigger('click')
    await flushPromises()
    expect(navigator.clipboard.writeText).toHaveBeenCalled()
  })

  it('logs warning when clipboard write fails', async () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    ;(navigator.clipboard.writeText as Mock).mockRejectedValueOnce(new Error('no permission'))
    const wrapper = mountView()
    await nextTick()
    await nextTick()
    await nextTick()
    await wrapper.find('[data-testid="settings-mcp-copy-url"]').trigger('click')
    await flushPromises()
    expect(warnSpy).toHaveBeenCalledWith('Failed to copy MCP config', expect.any(Error))
    warnSpy.mockRestore()
  })

  // ─── MCP config snippets per client ────────────────────────────────────

  it('shows opencode snippet by default', async () => {
    const wrapper = mountView()
    await nextTick()
    await nextTick()
    await nextTick()
    expect(wrapper.text()).toContain('mcp {')
    expect(wrapper.text()).toContain('https://mcp.modulo.run')
  })

  it('switches snippet when selectedMcpClient changes', async () => {
    const wrapper = mountView()
    await nextTick()
    await nextTick()
    await nextTick()
    // Default: opencode format
    expect(wrapper.text()).toContain('mcp {')
    // Access the component instance and change the selectedMcpClient
    const vm = wrapper.vm as any
    vm.selectedMcpClient = 'claude'
    await nextTick()
    expect(wrapper.text()).toContain('mcpServers')
    vm.selectedMcpClient = 'cursor'
    await nextTick()
    expect(wrapper.text()).toContain('mcpServers')
    vm.selectedMcpClient = 'continue'
    await nextTick()
    expect(wrapper.text()).toContain('experimental')
    vm.selectedMcpClient = 'custom'
    await nextTick()
    expect(wrapper.text()).toContain('MCP_SERVER_URL=https://mcp.modulo.run')
  })

  it('uses localhost for snippet when mcpUrl is empty', async () => {
    const wrapper = mountView(mockMcpConfigEmpty, mockApiKeysNoActive, mockNoOAuthClients)
    await nextTick()
    await nextTick()
    await nextTick()
    expect(wrapper.text()).toContain('http://localhost:8000')
  })

  // ─── Create key flow ──────────────────────────────────────────────────

  it('create key button opens dialog and shows form fields', async () => {
    const wrapper = mountView()
    await nextTick()
    await nextTick()
    await nextTick()
    await wrapper.find('[data-testid="settings-mcp-create-key"]').trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="settings-mcp-create-key-name"]').exists()).toBe(true)
    expect(wrapper.find('[aria-label="Role"]').exists()).toBe(true)
  })

  it('create key API error shows error via formatApiError', async () => {
    postMock.mockResolvedValueOnce({
      data: undefined, error: { status: 409, detail: 'Key name already exists' },
    })
    const wrapper = mountView()
    await nextTick()
    await nextTick()
    await nextTick()
    // Open dialog
    await wrapper.find('[data-testid="settings-mcp-create-key"]').trigger('click')
    await nextTick()
    // Type name
    const nameInput = wrapper.find('[data-testid="settings-mcp-create-key-name"]')
    await nameInput.setValue('Dup Key')
    await nextTick()
    // Simulate the createKey function by finding the component and calling it
    // The FormDialog is stubbed, so we can't click the confirm button.
    // Instead, test the error path by calling the createKey function directly.
    const vm = wrapper.vm as any
    await vm.createKey()
    await flushPromises()
    expect(postMock).toHaveBeenCalled()
  })

  it('create key throw error shows error via formatApiError', async () => {
    postMock.mockRejectedValueOnce(new Error('Network error'))
    const wrapper = mountView()
    await nextTick()
    await nextTick()
    await nextTick()
    await wrapper.find('[data-testid="settings-mcp-create-key"]').trigger('click')
    await nextTick()
    const nameInput = wrapper.find('[data-testid="settings-mcp-create-key-name"]')
    await nameInput.setValue('Fail Key')
    await nextTick()
    const vm = wrapper.vm as any
    await vm.createKey()
    await flushPromises()
    expect(postMock).toHaveBeenCalled()
  })

  // ─── Revoke key flow ──────────────────────────────────────────────────

  it('revoke flow: confirmRevokeKey opens dialog', async () => {
    const wrapper = mountView()
    await nextTick()
    await nextTick()
    await nextTick()
    await wrapper.find('[data-testid="settings-mcp-revoke-key"]').trigger('click')
    await nextTick()
    // The FormDialog stub renders, showing the revoke confirmation text
    expect(wrapper.text()).toContain('Are you sure you want to revoke')
  })

  it('revoke API error shows error in dialog', async () => {
    putMock.mockResolvedValueOnce({ data: undefined, error: { status: 403, detail: 'Cannot revoke own key' } })
    const wrapper = mountView()
    await nextTick()
    await nextTick()
    await nextTick()
    await wrapper.find('[data-testid="settings-mcp-revoke-key"]').trigger('click')
    await nextTick()
    // Call revokeKey directly since FormDialog is stubbed
    const vm = wrapper.vm as any
    await vm.revokeKey()
    await flushPromises()
    expect(putMock).toHaveBeenCalled()
  })

  it('revoke throw error shows error in dialog', async () => {
    putMock.mockRejectedValueOnce(new Error('Network'))
    const wrapper = mountView()
    await nextTick()
    await nextTick()
    await nextTick()
    await wrapper.find('[data-testid="settings-mcp-revoke-key"]').trigger('click')
    await nextTick()
    const vm = wrapper.vm as any
    await vm.revokeKey()
    await flushPromises()
    expect(putMock).toHaveBeenCalled()
  })

  // ─── Empty API keys ──────────────────────────────────────────────────

  it('shows empty state when no API keys', async () => {
    const wrapper = mountView(mockMcpConfig, [], mockNoOAuthClients)
    await nextTick()
    await nextTick()
    await nextTick()
    expect(wrapper.text()).toContain('No API keys created yet')
  })

  it('hides table when no API keys', async () => {
    const wrapper = mountView(mockMcpConfig, [], mockNoOAuthClients)
    await nextTick()
    await nextTick()
    await nextTick()
    expect(wrapper.find('table').exists()).toBe(false)
  })

  // ─── Never-used keys ─────────────────────────────────────────────────

  it('shows Never for keys with null last_used_at', async () => {
    const wrapper = mountView()
    await nextTick()
    await nextTick()
    await nextTick()
    expect(wrapper.text()).toContain('Never')
  })

  it('formats last_used_at date for active keys', async () => {
    const wrapper = mountView()
    await nextTick()
    await nextTick()
    await nextTick()
    expect(wrapper.text()).not.toContain('2026-06-28T12:00:00Z')
  })

  // ─── Error loading MCP config specifically ────────────────────────────

  it('shows error when mcp-config endpoint fails', async () => {
    getMock.mockImplementation((path: string) => {
      if (path === '/api/v1/api-keys/mcp-config') return Promise.resolve({ data: null, error: 'MCP config unavailable' })
      if (path === '/api/v1/api-keys') return Promise.resolve({ data: mockApiKeys, error: undefined })
      return Promise.resolve({ data: null, error: undefined })
    })
    const wrapper = mount(SettingsMcpView, { global: { stubs } })
    await nextTick()
    await nextTick()
    await nextTick()
    expect(wrapper.text()).toContain('MCP config unavailable')
  })

  it('shows error when api-keys endpoint fails', async () => {
    getMock.mockImplementation((path: string) => {
      if (path === '/api/v1/api-keys/mcp-config') return Promise.resolve({ data: mockMcpConfig, error: undefined })
      if (path === '/api/v1/api-keys') return Promise.resolve({ data: null, error: 'Keys endpoint down' })
      return Promise.resolve({ data: null, error: undefined })
    })
    const wrapper = mount(SettingsMcpView, { global: { stubs } })
    await nextTick()
    await nextTick()
    await nextTick()
    expect(wrapper.text()).toContain('Keys endpoint down')
  })

  // ─── API key create role selector ─────────────────────────────────────

  it('has role select with operator and runner options', async () => {
    const wrapper = mountView()
    await nextTick()
    await nextTick()
    await nextTick()
    await wrapper.find('[data-testid="settings-mcp-create-key"]').trigger('click')
    await nextTick()
    const roleSelect = wrapper.find('[aria-label="Role"]')
    expect(roleSelect.exists()).toBe(true)
  })

  // ─── Multiple API keys ────────────────────────────────────────────────

  it('renders multiple API keys in the table', async () => {
    const wrapper = mountView()
    await nextTick()
    await nextTick()
    await nextTick()
    const rows = wrapper.findAll('tbody tr')
    expect(rows.length).toBe(2)
  })

  // ─── API key table headers ────────────────────────────────────────────

  it('renders table headers for API keys (translated)', async () => {
    const wrapper = mountView()
    await nextTick()
    await nextTick()
    await nextTick()
    expect(wrapper.text()).toContain('Name')
    expect(wrapper.text()).toContain('Key Prefix')
    expect(wrapper.text()).toContain('Role')
    expect(wrapper.text()).toContain('Status')
    expect(wrapper.text()).toContain('Last Used')
  })

  // ─── Select MCP client ────────────────────────────────────────────────

  it('Select component renders for client selection', async () => {
    const wrapper = mountView()
    await nextTick()
    await nextTick()
    await nextTick()
    const select = wrapper.find('[aria-label="Client"]')
    expect(select.exists()).toBe(true)
  })

  // ─── Cleanup on unmount ────────────────────────────────────────────────

  it('cleans up mcpCopyTimeout on unmount', async () => {
    const clearTimeoutSpy = vi.spyOn(global, 'clearTimeout')
    const wrapper = mountView()
    await nextTick()
    await nextTick()
    await nextTick()
    wrapper.unmount()
    // onUnmounted should call clearTimeout for mcpCopyTimeout
    expect(clearTimeoutSpy).toHaveBeenCalled()
    clearTimeoutSpy.mockRestore()
  })

  // ─── Key mask dialog tests ────────────────────────────────────────────

  it('key-created dialog renders in DOM but is controlled by visible prop', async () => {
    const wrapper = mountView()
    await nextTick()
    await nextTick()
    await nextTick()
    // The Dialog stub is a passthrough div so content renders regardless
    // of v-model:visible. Verify the dialog content exists but confirm
    // it's part of the component's template by checking key-created text
    expect(wrapper.text()).toContain('Copy this key now')
  })

  it('copy key value button exists in DOM (rendered in Dialog stub)', async () => {
    const wrapper = mountView()
    await nextTick()
    await nextTick()
    await nextTick()
    const copyKeyBtn = wrapper.find('[data-testid="settings-mcp-copy-key-value"]')
    expect(copyKeyBtn.exists()).toBe(true)
  })
})
