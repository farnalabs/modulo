import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

const mockMcpConfig = {
  mcp_url: 'https://mcp.modulo.run',
  config_snippet: '',
}

const mockApiKeys = {
  items: [
    { id: 'key-1', prefix: 'mod_mk_abc', name: 'Claude Key', role: 'operator', is_active: true, last_used_at: '2026-06-28T12:00:00Z', created_at: '2026-06-01T00:00:00Z' },
    { id: 'key-2', prefix: 'mod_mk_def', name: 'Cursor Key', role: 'runner', is_active: false, last_used_at: null, created_at: '2026-06-15T00:00:00Z' },
  ],
}

const mockMcpConfigEmpty = {
  mcp_url: '',
  config_snippet: '',
}

const mockApiKeysNoActive = {
  items: [
    { id: 'key-3', prefix: 'mod_mk_ghi', name: 'Revoked Key', role: 'operator', is_active: false, last_used_at: null, created_at: '2026-06-10T00:00:00Z' },
  ],
}

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

import SettingsMcpView from '../views/SettingsMcpView.vue'

const dialogStub = { template: '<div><slot /></div>' }
const stubs = { Dialog: dialogStub, DialogContent: dialogStub, DialogDescription: dialogStub, DialogFooter: dialogStub, DialogHeader: dialogStub, DialogTitle: dialogStub, FeatureGate: dialogStub }

function mockApiResponses(getMock: any, mcpConfig = mockMcpConfig, apiKeys = mockApiKeys, oauthClients = mockOAuthClients) {
  getMock.mockImplementation((path: string) => {
    if (path === '/api/v1/api-keys/mcp-config') {
      return Promise.resolve({ data: mcpConfig, error: undefined })
    }
    if (path === '/api/v1/api-keys') {
      return Promise.resolve({ data: apiKeys, error: undefined })
    }
    if (path === '/api/v1/mcp/oauth/clients') {
      return Promise.resolve({ data: oauthClients, error: undefined })
    }
    return Promise.resolve({ data: null, error: undefined })
  })
}

describe('SettingsMcpView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: vi.fn().mockResolvedValue(undefined) },
      writable: true,
      configurable: true,
    })
  })

  it('renders without crashing', async () => {
    const { api } = await import('../lib/api/client')
    mockApiResponses(api.GET)

    const wrapper = mount(SettingsMcpView, {
      global: { stubs },
    })
    await nextTick()
    await nextTick()
    await nextTick()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('MCP Configuration')
  })

  it('shows loading spinner initially', async () => {
    const { api } = await import('../lib/api/client');
    (api.GET as any).mockReturnValue(new Promise(() => {}))

    const wrapper = mount(SettingsMcpView, {
      global: { stubs },
    })
    await nextTick()
    expect(wrapper.find('.animate-spin').exists()).toBe(true)
  })

  it('shows error alert on API failure', async () => {
    const { api } = await import('../lib/api/client');
    (api.GET as any).mockImplementation(() => Promise.resolve({ data: null, error: 'Failed to load MCP config: Server error' }))

    const wrapper = mount(SettingsMcpView, {
      global: { stubs },
    })
    await nextTick()
    await nextTick()
    await nextTick()
    expect(wrapper.text()).toContain('Failed to load MCP config')
  })

  it('shows MCP URL and Active badge', async () => {
    const { api } = await import('../lib/api/client')
    mockApiResponses(api.GET)

    const wrapper = mount(SettingsMcpView, {
      global: { stubs },
    })
    await nextTick()
    await nextTick()
    await nextTick()
    expect(wrapper.text()).toContain('https://mcp.modulo.run')
    expect(wrapper.text()).toContain('Active')
  })

  it('shows MODULO_PUBLIC_URL warning when url is empty', async () => {
    const { api } = await import('../lib/api/client')
    mockApiResponses(api.GET, mockMcpConfigEmpty, mockApiKeysNoActive, mockNoOAuthClients)

    const wrapper = mount(SettingsMcpView, {
      global: { stubs },
    })
    await nextTick()
    await nextTick()
    await nextTick()
    expect(wrapper.text()).toContain('MODULO_PUBLIC_URL not set')
    expect(wrapper.text()).toContain('Local Only')
    expect(wrapper.text()).toContain('http://localhost:8000')
  })

  it('shows API keys in the table', async () => {
    const { api } = await import('../lib/api/client')
    mockApiResponses(api.GET)

    const wrapper = mount(SettingsMcpView, {
      global: { stubs },
    })
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

  it('opens create key dialog with name and role fields', async () => {
    const { api } = await import('../lib/api/client')
    mockApiResponses(api.GET)

    const wrapper = mount(SettingsMcpView, {
      global: { stubs },
    })
    await nextTick()
    await nextTick()
    await nextTick()

    const createBtn = wrapper.find('[data-testid="settings-mcp-create-key"]')
    expect(createBtn.exists()).toBe(true)
    expect(createBtn.text()).toContain('Create MCP API Key')

    expect(wrapper.text()).toContain('Create MCP API Key')
  })

  it('revoke button shows in the API key table', async () => {
    const { api } = await import('../lib/api/client')
    mockApiResponses(api.GET)

    const wrapper = mount(SettingsMcpView, {
      global: { stubs },
    })
    await nextTick()
    await nextTick()
    await nextTick()

    const revokeBtns = wrapper.findAll('[data-testid="settings-mcp-revoke-key"]')
    expect(revokeBtns.length).toBe(1)
    expect(revokeBtns[0].text()).toContain('Revoke')
  })

  it('shows config snippets when an active key exists', async () => {
    const { api } = await import('../lib/api/client')
    mockApiResponses(api.GET)

    const wrapper = mount(SettingsMcpView, {
      global: { stubs },
    })
    await nextTick()
    await nextTick()
    await nextTick()

    expect(wrapper.text()).toContain('Configuration Snippets')

    const copyBtns = wrapper.findAll('[data-testid^="settings-mcp-copy-"]')
    expect(copyBtns.length).toBeGreaterThanOrEqual(1)
  })

  it('shows placeholder when no active key for snippets', async () => {
    const { api } = await import('../lib/api/client')
    mockApiResponses(api.GET, mockMcpConfig, mockApiKeysNoActive, mockNoOAuthClients)

    const wrapper = mount(SettingsMcpView, {
      global: { stubs },
    })
    await nextTick()
    await nextTick()
    await nextTick()

    expect(wrapper.text()).toContain('Coming soon')
  })

  it('copy button copies server URL to clipboard', async () => {
    const { api } = await import('../lib/api/client')
    mockApiResponses(api.GET)

    const wrapper = mount(SettingsMcpView, {
      global: { stubs },
    })
    await nextTick()
    await nextTick()
    await nextTick()

    const copyBtn = wrapper.find('[data-testid="settings-mcp-copy-url"]')
    expect(copyBtn.exists()).toBe(true)
    expect(copyBtn.text()).toBe('Copy')
  })

  it('shows OAuth clients placeholder', async () => {
    const { api } = await import('../lib/api/client')
    mockApiResponses(api.GET)

    const wrapper = mount(SettingsMcpView, {
      global: { stubs },
    })
    await nextTick()
    await nextTick()
    await nextTick()
    expect(wrapper.text()).toContain('Registered OAuth Clients')
    expect(wrapper.text()).toContain('Coming soon')
  })
})
