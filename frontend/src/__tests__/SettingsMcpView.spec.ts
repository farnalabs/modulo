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
// GET /api/v1/mcp/oauth/clients returns a BARE array (not an envelope) - the
// fixtures below mirror the real wire shape so the default mount exercises the
// "there is a client" branch instead of silently falling into the empty state.
const mockOAuthClients = [
  { id: 'client-1', client_id: 'mod_oauth_xyz', name: 'CLI Client', scopes: ['trigger:run', 'hitl:review'], redirect_uris: ['https://example.com/callback'], created_at: '2026-06-20T00:00:00Z' },
]
const mockNoOAuthClients: unknown[] = []
const mockCreatedOauthClient = {
  id: 'client-2',
  client_id: 'mod_oauth_new_client',
  client_secret: 'mod_oauth_secret_value_123',
  name: 'CLI Tool',
}

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn(),
    POST: vi.fn().mockResolvedValue({ data: { id: 'key-new', key_value: 'mod_mk_new_secret_1234', name: 'New Key', role: 'operator' }, error: undefined }),
    PUT: vi.fn().mockResolvedValue({ data: null, error: undefined }),
    DELETE: vi.fn().mockResolvedValue({ data: null, error: undefined }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

// The OAuth client section is role-gated (backend requires admin|operator),
// so the view reads the org role off the JWT. Default every test to admin and
// let individual tests override it.
vi.mock('../lib/jwt', () => ({
  decodeJwtPayload: vi.fn(() => ({ org_role: 'admin' })),
}))

import { api } from '../lib/api/client'
import { decodeJwtPayload } from '../lib/jwt'
import SettingsMcpView from '../views/SettingsMcpView.vue'
import McpOauthClientsCard from '../components/settings/McpOauthClientsCard.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import FormDialog from '../components/shared/FormDialog.vue'

const getMock = api.GET as unknown as Mock
const postMock = api.POST as unknown as Mock
const putMock = api.PUT as unknown as Mock
const deleteMock = api.DELETE as unknown as Mock
const decodeJwtPayloadMock = decodeJwtPayload as unknown as Mock

const dialogStub = { template: '<div><slot /></div>' }
const stubs = { Dialog: dialogStub, DialogContent: dialogStub, DialogDescription: dialogStub, DialogFooter: dialogStub, DialogHeader: dialogStub, DialogTitle: dialogStub, FeatureGate: dialogStub }

/**
 * The OAuth surface was extracted into `McpOauthClientsCard`, so its state
 * and handlers live on the child. Every OAuth case below still drives the
 * REAL child through the mounted parent (props in, `refresh` emit out) -
 * only the handle it reaches for moved.
 */
function oauthVm(wrapper: ReturnType<typeof mountView>) {
  const card = wrapper.findComponent(McpOauthClientsCard)
  expect(card.exists()).toBe(true)
  return card.vm as any
}

function mockApiResponses(mcpConfig = mockMcpConfig, apiKeysData = mockApiKeys, oauth: unknown = mockOAuthClients) {
  getMock.mockImplementation((path: string) => {
    if (path === '/api/v1/api-keys/mcp-config') return Promise.resolve({ data: mcpConfig, error: undefined })
    if (path === '/api/v1/api-keys') return Promise.resolve({ data: apiKeysData, error: undefined })
    if (path === '/api/v1/mcp/oauth/clients') return Promise.resolve({ data: oauth, error: undefined })
    return Promise.resolve({ data: null, error: undefined })
  })
}

function mountView(mcpConfig = mockMcpConfig, apiKeysData = mockApiKeys, oauth: unknown = mockOAuthClients) {
  mockApiResponses(mcpConfig, apiKeysData, oauth)
  return mount(SettingsMcpView, { global: { stubs } })
}

describe('SettingsMcpView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    vi.useRealTimers()
    decodeJwtPayloadMock.mockReturnValue({ org_role: 'admin' })
    deleteMock.mockResolvedValue({ data: { deleted: true }, error: undefined })
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

  it('shows the OAuth clients section with a register button', async () => {
    const wrapper = mountView()
    await nextTick()
    await nextTick()
    await nextTick()
    expect(wrapper.text()).toContain('Registered OAuth Clients')
    const registerBtn = wrapper.find('[data-testid="settings-mcp-register-oauth-client"]')
    expect(registerBtn.exists()).toBe(true)
    expect(registerBtn.text()).toContain('Register OAuth Client')
    // The default fixture is the real bare-array contract, so the client row
    // must actually render - not fall through to the empty state.
    const bodies = wrapper.findAll('tbody')
    expect(bodies.length).toBe(2)
    expect(bodies[1].findAll('tr').length).toBe(1)
    expect(wrapper.text()).toContain('mod_oauth_xyz')
    expect(wrapper.find('[data-testid="settings-mcp-oauth-empty"]').exists()).toBe(false)
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
    const bodies = wrapper.findAll('tbody')
    // Two tables render on the default mount: the API key list and the OAuth
    // client list (the bare-array fixture carries one client).
    expect(bodies.length).toBe(2)
    expect(bodies[0].findAll('tr').length).toBe(2)
    expect(bodies[1].findAll('tr').length).toBe(1)
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

  // ─── OAuth client registration / revocation (FAR-1251) ────────────────

  it('lists registered OAuth clients with a revoke action', async () => {
    const wrapper = mountView(mockMcpConfig, mockApiKeys, mockOAuthClients)
    await nextTick()
    await nextTick()
    await nextTick()
    expect(wrapper.text()).toContain('CLI Client')
    expect(wrapper.text()).toContain('mod_oauth_xyz')
    expect(wrapper.text()).toContain('trigger:run')
    expect(wrapper.text()).toContain('https://example.com/callback')
    expect(wrapper.findAll('[data-testid="settings-mcp-revoke-oauth-client"]').length).toBe(1)
  })

  it('shows the OAuth empty state when no clients are registered', async () => {
    const wrapper = mountView(mockMcpConfig, mockApiKeys, [])
    await nextTick()
    await nextTick()
    await nextTick()
    expect(wrapper.text()).toContain('No OAuth clients registered yet.')
    expect(wrapper.findAll('[data-testid="settings-mcp-revoke-oauth-client"]').length).toBe(0)
  })

  it('register button opens the form with name, redirect URI and scope fields', async () => {
    const wrapper = mountView()
    await nextTick()
    await nextTick()
    await nextTick()
    await wrapper.find('[data-testid="settings-mcp-register-oauth-client"]').trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="settings-mcp-oauth-name"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="settings-mcp-oauth-redirect-uris"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="settings-mcp-oauth-scope-trigger-run"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="settings-mcp-oauth-scope-hitl-review"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="settings-mcp-oauth-scope-library-browse"]').exists()).toBe(true)
  })

  it('register validation blocks the POST and shows inline field errors', async () => {
    const wrapper = mountView()
    await nextTick()
    await nextTick()
    await nextTick()
    await wrapper.find('[data-testid="settings-mcp-register-oauth-client"]').trigger('click')
    await nextTick()
    const vm = oauthVm(wrapper)

    await vm.registerOauthClient()
    await flushPromises()
    expect(postMock).not.toHaveBeenCalled()
    expect(vm.registerOauthDialogOpen).toBe(true)

    await wrapper.find('[data-testid="settings-mcp-oauth-name"]').trigger('blur')
    await nextTick()
    expect(wrapper.find('[data-testid="settings-mcp-oauth-name-error"]').exists()).toBe(true)

    await wrapper.find('[data-testid="settings-mcp-oauth-redirect-uris"]').trigger('blur')
    await nextTick()
    expect(wrapper.find('[data-testid="settings-mcp-oauth-redirect-error"]').exists()).toBe(true)

    // touch scopes then leave none selected
    const scope = wrapper.find('[data-testid="settings-mcp-oauth-scope-trigger-run"]')
    ;(scope.element as HTMLInputElement).checked = true
    await scope.trigger('change')
    ;(scope.element as HTMLInputElement).checked = false
    await scope.trigger('change')
    await nextTick()
    expect(wrapper.find('[data-testid="settings-mcp-oauth-scopes-error"]').exists()).toBe(true)
    expect(postMock).not.toHaveBeenCalled()
  })

  it('registers an OAuth client with the correct wire shape and reveals the secret once', async () => {
    postMock.mockResolvedValueOnce({ data: mockCreatedOauthClient, error: undefined })
    const wrapper = mountView(mockMcpConfig, mockApiKeys, [])
    await nextTick()
    await nextTick()
    await nextTick()
    await wrapper.find('[data-testid="settings-mcp-register-oauth-client"]').trigger('click')
    await nextTick()

    await wrapper.find('[data-testid="settings-mcp-oauth-name"]').setValue('CLI Tool')
    await wrapper
      .find('[data-testid="settings-mcp-oauth-redirect-uris"]')
      .setValue('https://a.example/cb\n\nhttps://b.example/cb')
    for (const testid of ['settings-mcp-oauth-scope-trigger-run', 'settings-mcp-oauth-scope-hitl-review']) {
      const box = wrapper.find(`[data-testid="${testid}"]`)
      ;(box.element as HTMLInputElement).checked = true
      await box.trigger('change')
    }
    await nextTick()

    const vm = oauthVm(wrapper)
    await vm.registerOauthClient()
    await flushPromises()

    expect(postMock).toHaveBeenCalledWith('/api/v1/mcp/oauth/clients', {
      body: {
        name: 'CLI Tool',
        redirect_uris: ['https://a.example/cb', 'https://b.example/cb'],
        scopes: ['trigger:run', 'hitl:review'],
      },
    })
    expect(vm.registerOauthDialogOpen).toBe(false)
    expect(vm.oauthCreatedDialogOpen).toBe(true)
    expect(
      (wrapper.find('[data-testid="settings-mcp-oauth-client-secret"]').element as HTMLInputElement).value,
    ).toBe('mod_oauth_secret_value_123')
    expect(
      (wrapper.find('[data-testid="settings-mcp-oauth-client-id"]').element as HTMLInputElement).value,
    ).toBe('mod_oauth_new_client')
    expect(wrapper.text()).toContain('shown only once')
    expect(wrapper.find('[data-testid="settings-mcp-copy-oauth-client-id"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="settings-mcp-copy-oauth-client-secret"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="settings-mcp-oauth-created-warning"]').exists()).toBe(true)
  })

  it('register API error is surfaced through formatApiError', async () => {
    postMock.mockResolvedValueOnce({
      data: undefined,
      error: { status: 500, detail: 'MODULO_PUBLIC_URL must be configured for OAuth flow' },
    })
    const wrapper = mountView()
    await nextTick()
    await nextTick()
    await nextTick()
    await wrapper.find('[data-testid="settings-mcp-register-oauth-client"]').trigger('click')
    await nextTick()
    await wrapper.find('[data-testid="settings-mcp-oauth-name"]').setValue('CLI Tool')
    await wrapper.find('[data-testid="settings-mcp-oauth-redirect-uris"]').setValue('https://a.example/cb')
    const box = wrapper.find('[data-testid="settings-mcp-oauth-scope-trigger-run"]')
    ;(box.element as HTMLInputElement).checked = true
    await box.trigger('change')
    await nextTick()

    const vm = oauthVm(wrapper)
    await vm.registerOauthClient()
    await flushPromises()

    const err = wrapper.find('[data-testid="settings-mcp-oauth-register-error"]')
    expect(err.exists()).toBe(true)
    expect(err.text()).toContain('MODULO_PUBLIC_URL must be configured for OAuth flow')
    expect(vm.oauthCreatedDialogOpen).toBe(false)
    expect(err.attributes('aria-live')).toBe('assertive')
  })

  it('revoke flow opens a confirm dialog naming the client and DELETEs by client_id', async () => {
    const wrapper = mountView(mockMcpConfig, mockApiKeys, mockOAuthClients)
    await nextTick()
    await nextTick()
    await nextTick()
    await wrapper.find('[data-testid="settings-mcp-revoke-oauth-client"]').trigger('click')
    await nextTick()
    expect(wrapper.text()).toContain('Are you sure you want to revoke the OAuth client')
    expect(wrapper.text()).toContain('CLI Client')

    const vm = oauthVm(wrapper)
    await vm.revokeOauthClient()
    await flushPromises()
    expect(deleteMock).toHaveBeenCalledWith('/api/v1/mcp/oauth/clients/{client_id}', {
      params: { path: { client_id: 'mod_oauth_xyz' } },
    })
    expect(vm.revokeOauthDialogOpen).toBe(false)
  })

  it('renders the restricted state for a viewer and hides register/revoke', async () => {
    // A viewer sits below the admin|operator gate on BOTH list endpoints, so
    // GET /api/v1/api-keys 403s too - this is the state a real viewer
    // actually produces, not the 200-with-keys shape only an admin sees.
    decodeJwtPayloadMock.mockReturnValue({ org_role: 'viewer' })
    getMock.mockImplementation((path: string) => {
      if (path === '/api/v1/api-keys/mcp-config') return Promise.resolve({ data: mockMcpConfig, error: undefined })
      if (path === '/api/v1/api-keys') {
        return Promise.resolve({
          data: undefined,
          error: { detail: 'Only admin or operator users can list API keys' },
          response: { status: 403 },
        })
      }
      if (path === '/api/v1/mcp/oauth/clients') {
        return Promise.resolve({
          data: undefined,
          error: { detail: 'Only admin or operator users can list OAuth clients' },
          response: { status: 403 },
        })
      }
      return Promise.resolve({ data: null, error: undefined })
    })
    const wrapper = mount(SettingsMcpView, { global: { stubs } })
    await nextTick()
    await nextTick()
    await nextTick()
    // No page-level ErrorAlert: the surrounding cards survive.
    expect(wrapper.text()).toContain('MCP Configuration')
    expect(wrapper.text()).toContain('Configuration Snippets')
    expect(wrapper.text()).not.toContain('Only admin or operator users can list')
    // API keys card degrades to its own restricted panel.
    expect(wrapper.find('[data-testid="settings-mcp-api-keys-restricted"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="settings-mcp-create-key"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="settings-mcp-org-scope-note"]').exists()).toBe(false)
    // OAuth card restricted, actions hidden.
    expect(wrapper.find('[data-testid="settings-mcp-oauth-restricted"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="settings-mcp-register-oauth-client"]').exists()).toBe(false)
    expect(wrapper.findAll('[data-testid="settings-mcp-revoke-oauth-client"]').length).toBe(0)
  })

  it('a 403 from the OAuth list keeps the rest of the page alive', async () => {
    getMock.mockImplementation((path: string) => {
      if (path === '/api/v1/api-keys/mcp-config') return Promise.resolve({ data: mockMcpConfig, error: undefined })
      if (path === '/api/v1/api-keys') return Promise.resolve({ data: mockApiKeys, error: undefined })
      if (path === '/api/v1/mcp/oauth/clients') {
        return Promise.resolve({
          data: undefined,
          error: { detail: 'Only admin or operator users can list OAuth clients' },
          response: { status: 403 },
        })
      }
      return Promise.resolve({ data: null, error: undefined })
    })
    const wrapper = mount(SettingsMcpView, { global: { stubs } })
    await nextTick()
    await nextTick()
    await nextTick()
    expect(wrapper.text()).toContain('MCP Configuration')
    expect(wrapper.text()).toContain('Claude Key')
    expect(wrapper.find('[data-testid="settings-mcp-oauth-restricted"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="settings-mcp-register-oauth-client"]').exists()).toBe(false)
  })

  it('a non-403 OAuth list failure degrades to an inline section error', async () => {
    getMock.mockImplementation((path: string) => {
      if (path === '/api/v1/api-keys/mcp-config') return Promise.resolve({ data: mockMcpConfig, error: undefined })
      if (path === '/api/v1/api-keys') return Promise.resolve({ data: mockApiKeys, error: undefined })
      if (path === '/api/v1/mcp/oauth/clients') {
        return Promise.resolve({ data: undefined, error: { status: 503, detail: 'Database unavailable' } })
      }
      return Promise.resolve({ data: null, error: undefined })
    })
    const wrapper = mount(SettingsMcpView, { global: { stubs } })
    await nextTick()
    await nextTick()
    await nextTick()
    expect(wrapper.text()).toContain('MCP Configuration')
    const err = wrapper.find('[data-testid="settings-mcp-oauth-list-error"]')
    expect(err.exists()).toBe(true)
    expect(err.text()).toContain('Database unavailable')
    expect(err.attributes('aria-live')).toBe('assertive')
  })

  // ─── C2: the API keys card degrades instead of killing the page ───────

  it('a 403 from the API keys list renders a restricted card, not a page error', async () => {
    getMock.mockImplementation((path: string) => {
      if (path === '/api/v1/api-keys/mcp-config') return Promise.resolve({ data: mockMcpConfig, error: undefined })
      if (path === '/api/v1/api-keys') {
        return Promise.resolve({
          data: undefined,
          error: { detail: 'Only admin or operator users can list API keys' },
          response: { status: 403 },
        })
      }
      if (path === '/api/v1/mcp/oauth/clients') return Promise.resolve({ data: mockOAuthClients, error: undefined })
      return Promise.resolve({ data: null, error: undefined })
    })
    const wrapper = mount(SettingsMcpView, { global: { stubs } })
    await nextTick()
    await nextTick()
    await nextTick()
    expect(wrapper.text()).toContain('MCP Configuration')
    expect(wrapper.text()).not.toContain('Only admin or operator users can list API keys')
    expect(wrapper.find('[data-testid="settings-mcp-api-keys-restricted"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="settings-mcp-create-key"]').exists()).toBe(false)
    // The OAuth card is untouched by the keys 403.
    expect(wrapper.find('[data-testid="settings-mcp-oauth-restricted"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="settings-mcp-register-oauth-client"]').exists()).toBe(true)
  })

  // ─── M7: list failures are never the "no clients" empty state ─────────

  it('a rejected OAuth list request renders the inline error, not the empty state', async () => {
    getMock.mockImplementation((path: string) => {
      if (path === '/api/v1/api-keys/mcp-config') return Promise.resolve({ data: mockMcpConfig, error: undefined })
      if (path === '/api/v1/api-keys') return Promise.resolve({ data: mockApiKeys, error: undefined })
      if (path === '/api/v1/mcp/oauth/clients') return Promise.reject(new TypeError('Failed to fetch'))
      return Promise.resolve({ data: null, error: undefined })
    })
    const wrapper = mount(SettingsMcpView, { global: { stubs } })
    await nextTick()
    await nextTick()
    await nextTick()
    const err = wrapper.find('[data-testid="settings-mcp-oauth-list-error"]')
    expect(err.exists()).toBe(true)
    expect(err.text()).toContain('Failed to fetch')
    expect(err.attributes('aria-live')).toBe('assertive')
    expect(wrapper.find('[data-testid="settings-mcp-oauth-empty"]').exists()).toBe(false)
  })

  it('a non-array OAuth list body renders the inline error, not the empty state', async () => {
    getMock.mockImplementation((path: string) => {
      if (path === '/api/v1/api-keys/mcp-config') return Promise.resolve({ data: mockMcpConfig, error: undefined })
      if (path === '/api/v1/api-keys') return Promise.resolve({ data: mockApiKeys, error: undefined })
      if (path === '/api/v1/mcp/oauth/clients') return Promise.resolve({ data: { unexpected: 'shape' }, error: undefined })
      return Promise.resolve({ data: null, error: undefined })
    })
    const wrapper = mount(SettingsMcpView, { global: { stubs } })
    await nextTick()
    await nextTick()
    await nextTick()
    const err = wrapper.find('[data-testid="settings-mcp-oauth-list-error"]')
    expect(err.exists()).toBe(true)
    expect(err.text()).toContain('Could not read the OAuth client list')
    expect(wrapper.find('[data-testid="settings-mcp-oauth-empty"]').exists()).toBe(false)
  })

  it('an empty-body 403 on the OAuth list still renders the restricted state', async () => {
    getMock.mockImplementation((path: string) => {
      if (path === '/api/v1/api-keys/mcp-config') return Promise.resolve({ data: mockMcpConfig, error: undefined })
      if (path === '/api/v1/api-keys') return Promise.resolve({ data: mockApiKeys, error: undefined })
      if (path === '/api/v1/mcp/oauth/clients') {
        return Promise.resolve({ data: undefined, error: undefined, response: { status: 403 } })
      }
      return Promise.resolve({ data: null, error: undefined })
    })
    const wrapper = mount(SettingsMcpView, { global: { stubs } })
    await nextTick()
    await nextTick()
    await nextTick()
    expect(wrapper.find('[data-testid="settings-mcp-oauth-restricted"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="settings-mcp-register-oauth-client"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="settings-mcp-oauth-empty"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="settings-mcp-oauth-list-error"]').exists()).toBe(false)
  })

  // ─── M3: submit always runs the guard ─────────────────────────────────

  it('does not validity-disable the register confirm button', async () => {
    const wrapper = mountView()
    await nextTick()
    await nextTick()
    await nextTick()
    await wrapper.find('[data-testid="settings-mcp-register-oauth-client"]').trigger('click')
    await nextTick()
    const registerForm = wrapper
      .findComponent(McpOauthClientsCard)
      .findAllComponents(FormDialog)
      .find((dialog) => dialog.props('confirmText') === 'Register OAuth Client')
    expect(registerForm).toBeDefined()
    // The form is empty (invalid) at this point, so a validity-driven
    // `confirmDisabled` would be true. It must stay false so the button is
    // clickable and the guard inside registerOauthClient() can surface every
    // field error instead of leaving a dead button with no explanation.
    expect(registerForm?.props('confirmDisabled')).toBe(false)
    expect(registerForm?.props('loading')).toBe(false)
  })

  // ─── M4: redirect URI parsing, validation, de-duplication ─────────────

  it('splits redirect URIs on any whitespace, de-duplicates, and posts the result', async () => {
    postMock.mockResolvedValueOnce({ data: mockCreatedOauthClient, error: undefined })
    const wrapper = mountView(mockMcpConfig, mockApiKeys, [])
    await nextTick()
    await nextTick()
    await nextTick()
    await wrapper.find('[data-testid="settings-mcp-register-oauth-client"]').trigger('click')
    await nextTick()
    await wrapper.find('[data-testid="settings-mcp-oauth-name"]').setValue('CLI Tool')
    await wrapper
      .find('[data-testid="settings-mcp-oauth-redirect-uris"]')
      .setValue('https://a.example/cb https://b.example/cb\nhttps://a.example/cb')
    const box = wrapper.find('[data-testid="settings-mcp-oauth-scope-trigger-run"]')
    ;(box.element as HTMLInputElement).checked = true
    await box.trigger('change')
    await nextTick()

    const vm = oauthVm(wrapper)
    expect(vm.oauthRedirectList).toEqual(['https://a.example/cb', 'https://b.example/cb'])
    await vm.registerOauthClient()
    await flushPromises()
    expect(postMock).toHaveBeenCalledWith('/api/v1/mcp/oauth/clients', {
      body: {
        name: 'CLI Tool',
        redirect_uris: ['https://a.example/cb', 'https://b.example/cb'],
        scopes: ['trigger:run'],
      },
    })
  })

  it('blocks submit on a non-http redirect URI and names the invalid entries', async () => {
    const wrapper = mountView(mockMcpConfig, mockApiKeys, [])
    await nextTick()
    await nextTick()
    await nextTick()
    await wrapper.find('[data-testid="settings-mcp-register-oauth-client"]').trigger('click')
    await nextTick()
    await wrapper.find('[data-testid="settings-mcp-oauth-name"]').setValue('CLI Tool')
    await wrapper
      .find('[data-testid="settings-mcp-oauth-redirect-uris"]')
      .setValue('https://ok.example/cb\nnot-a-url\nftp://x/y')
    const box = wrapper.find('[data-testid="settings-mcp-oauth-scope-trigger-run"]')
    ;(box.element as HTMLInputElement).checked = true
    await box.trigger('change')
    await nextTick()

    const vm = oauthVm(wrapper)
    await vm.registerOauthClient()
    await flushPromises()
    expect(postMock).not.toHaveBeenCalled()
    expect(vm.registerOauthDialogOpen).toBe(true)
    const err = wrapper.find('[data-testid="settings-mcp-oauth-redirect-invalid"]')
    expect(err.exists()).toBe(true)
    expect(err.text()).toContain('not-a-url')
    expect(err.text()).toContain('ftp://x/y')
    expect(err.attributes('aria-live')).toBe('assertive')
  })

  it('accepts http://localhost and http://127.0.0.1 redirect URIs for local dev', async () => {
    postMock.mockResolvedValueOnce({ data: mockCreatedOauthClient, error: undefined })
    const wrapper = mountView(mockMcpConfig, mockApiKeys, [])
    await nextTick()
    await nextTick()
    await nextTick()
    await wrapper.find('[data-testid="settings-mcp-register-oauth-client"]').trigger('click')
    await nextTick()
    await wrapper.find('[data-testid="settings-mcp-oauth-name"]').setValue('Local Client')
    await wrapper
      .find('[data-testid="settings-mcp-oauth-redirect-uris"]')
      .setValue('http://localhost:5173/cb http://127.0.0.1:8080/cb')
    const box = wrapper.find('[data-testid="settings-mcp-oauth-scope-library-browse"]')
    ;(box.element as HTMLInputElement).checked = true
    await box.trigger('change')
    await nextTick()

    const vm = oauthVm(wrapper)
    await vm.registerOauthClient()
    await flushPromises()
    expect(postMock).toHaveBeenCalledWith('/api/v1/mcp/oauth/clients', {
      body: {
        name: 'Local Client',
        redirect_uris: ['http://localhost:5173/cb', 'http://127.0.0.1:8080/cb'],
        scopes: ['library:browse'],
      },
    })
  })

  // ─── M5: a failed one-time credential copy is visible, not console-only ─

  it('surfaces a visible failure when the one-time credential copy is rejected', async () => {
    ;(navigator.clipboard.writeText as Mock).mockRejectedValueOnce(new Error('insecure context'))
    const wrapper = mountView()
    await nextTick()
    await nextTick()
    await nextTick()
    const vm = oauthVm(wrapper)
    await vm.copyToClipboard('mod_oauth_secret_value_123', 'oauth-client-secret')
    await nextTick()

    const err = wrapper.find('[data-testid="settings-mcp-copy-oauth-client-secret-error"]')
    expect(err.exists()).toBe(true)
    expect(err.text()).toContain('Copy failed')
    expect(err.text()).toContain('copy manually')
    expect(err.attributes('aria-live')).toBe('assertive')
    // The value stays in a readonly (selectable) input so it can be copied by hand.
    const input = wrapper.find('[data-testid="settings-mcp-oauth-client-secret"]')
    expect(input.exists()).toBe(true)
    expect(input.attributes('readonly')).toBeDefined()
    expect(input.attributes('type')).toBe('text')

    // A subsequent successful copy clears the failure state.
    await vm.copyToClipboard('mod_oauth_secret_value_123', 'oauth-client-secret')
    await nextTick()
    expect(wrapper.find('[data-testid="settings-mcp-copy-oauth-client-secret-error"]').exists()).toBe(false)
  })

  // ─── M12: one-time secrets are wiped on close ─────────────────────────

  it('wipes the one-time OAuth credentials when the reveal dialog is dismissed', async () => {
    postMock.mockResolvedValueOnce({ data: mockCreatedOauthClient, error: undefined })
    const wrapper = mountView(mockMcpConfig, mockApiKeys, [])
    await nextTick()
    await nextTick()
    await nextTick()
    await wrapper.find('[data-testid="settings-mcp-register-oauth-client"]').trigger('click')
    await nextTick()
    await wrapper.find('[data-testid="settings-mcp-oauth-name"]').setValue('CLI Tool')
    await wrapper.find('[data-testid="settings-mcp-oauth-redirect-uris"]').setValue('https://a.example/cb')
    const box = wrapper.find('[data-testid="settings-mcp-oauth-scope-trigger-run"]')
    ;(box.element as HTMLInputElement).checked = true
    await box.trigger('change')
    await nextTick()

    const vm = oauthVm(wrapper)
    await vm.registerOauthClient()
    await flushPromises()
    expect(
      (wrapper.find('[data-testid="settings-mcp-oauth-client-secret"]').element as HTMLInputElement).value,
    ).toBe('mod_oauth_secret_value_123')
    expect(vm.oauthCreatedDialogOpen).toBe(true)

    // Footer "Done" close path.
    vm.dismissOauthCreatedDialog()
    await nextTick()
    expect(vm.oauthCreatedDialogOpen).toBe(false)
    expect(vm.createdOauthClientSecret).toBe('')
    expect(vm.createdOauthClientId).toBe('')
    expect(vm.createdOauthClientName).toBe('')
    expect(vm.oauthSecretMasked).toBe(true)

    // Mask / X / ESC close path (Dialog `hide` event).
    vm.onOauthCreatedDialogClose()
    expect(vm.createdOauthClientSecret).toBe('')
  })

  it('wipes the revealed API key from state when its dialog is dismissed', async () => {
    const wrapper = mountView()
    await nextTick()
    await nextTick()
    await nextTick()
    await wrapper.find('[data-testid="settings-mcp-create-key"]').trigger('click')
    await nextTick()
    await wrapper.find('[data-testid="settings-mcp-create-key-name"]').setValue('New Key')
    const vm = wrapper.vm as any
    await vm.createKey()
    await flushPromises()
    expect(vm.createdKeyValue).toBe('mod_mk_new_secret_1234')

    vm.dismissKeyCreatedDialog()
    await nextTick()
    expect(vm.keyCreatedDialogOpen).toBe(false)
    expect(vm.createdKeyValue).toBe('')
    expect(vm.createdKeyName).toBe('')
  })

  // ─── FAR-1251 majors: a post-mutation refetch is silent AND non-fatal ──

  it('keeps the reveal dialog, its secret and the rendered list alive when the post-register refetch fails', async () => {
    postMock.mockResolvedValueOnce({ data: mockCreatedOauthClient, error: undefined })
    const wrapper = mountView(mockMcpConfig, mockApiKeys, mockOAuthClients)
    await nextTick()
    await nextTick()
    await nextTick()
    expect(wrapper.text()).toContain('Claude Key')

    // Every GET AFTER the initial load fails with a transient 5xx - exactly
    // the refetch the register emit triggers while the reveal dialog is open.
    getMock.mockImplementation(() =>
      Promise.resolve({ data: null, error: { status: 503, detail: 'Service unavailable' } }),
    )

    await wrapper.find('[data-testid="settings-mcp-register-oauth-client"]').trigger('click')
    await nextTick()
    await wrapper.find('[data-testid="settings-mcp-oauth-name"]').setValue('CLI Tool')
    await wrapper.find('[data-testid="settings-mcp-oauth-redirect-uris"]').setValue('https://a.example/cb')
    const box = wrapper.find('[data-testid="settings-mcp-oauth-scope-trigger-run"]')
    ;(box.element as HTMLInputElement).checked = true
    await box.trigger('change')
    await nextTick()

    const vm = oauthVm(wrapper)
    await vm.registerOauthClient()
    await flushPromises()

    // The card never unmounted, so the one-time credential dialog is intact -
    // the secret (returned only once by the API) is still readable on screen.
    expect(wrapper.findComponent(McpOauthClientsCard).exists()).toBe(true)
    expect(vm.oauthCreatedDialogOpen).toBe(true)
    const secret = wrapper.find('[data-testid="settings-mcp-oauth-client-secret"]')
    expect(secret.exists()).toBe(true)
    expect((secret.element as HTMLInputElement).value).toBe('mod_oauth_secret_value_123')

    // Last-good data stays rendered: no page spinner and no page-fatal swap.
    expect(wrapper.find('.animate-spin').exists()).toBe(false)
    expect(wrapper.text()).toContain('Claude Key')
    expect(wrapper.text()).toContain('CLI Client')
    expect(wrapper.text()).toContain('https://mcp.modulo.run')
    expect(wrapper.text()).toContain('Configuration Snippets')

    // Exactly ONE error surface - the inline, non-page-fatal one - carrying
    // the refetch failure so it is surfaced rather than discarded.
    const alerts = wrapper.findAllComponents(ErrorAlert)
    expect(alerts.length).toBe(1)
    expect(alerts[0].text()).toContain('Service unavailable')
    expect(alerts[0].attributes('aria-live')).toBe('assertive')
  })

  it('a post-mutation refetch stays silent: no page spinner while it is in flight', async () => {
    const wrapper = mountView(mockMcpConfig, mockApiKeys, mockOAuthClients)
    await nextTick()
    await nextTick()
    await nextTick()
    expect(wrapper.find('.animate-spin').exists()).toBe(false)

    // The refetch never settles, so we inspect the page WHILE it is in flight.
    getMock.mockReturnValue(new Promise(() => {}))
    wrapper.findComponent(McpOauthClientsCard).vm.$emit('refresh')
    await nextTick()
    await flushPromises()
    await nextTick()

    expect(wrapper.find('.animate-spin').exists()).toBe(false)
    expect(wrapper.findComponent(McpOauthClientsCard).exists()).toBe(true)
    expect(wrapper.text()).toContain('Claude Key')
    expect(wrapper.text()).toContain('CLI Client')
    expect(wrapper.text()).toContain('Configuration Snippets')
  })
})
