import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

const mockRoute = {
  path: '/oauth/authorize',
  fullPath: '/oauth/authorize?state=st-1',
  params: {} as Record<string, string>,
  query: { state: 'st-1' } as Record<string, string>,
  hash: '',
  matched: [],
  name: 'oauth-authorize',
  redirectedFrom: undefined,
}

const routerPush = vi.fn()
let accessToken: string | null = 'jwt-token'

vi.mock('vue-router', () => ({
  useRoute: () => mockRoute,
  useRouter: () => ({ push: routerPush }),
  createRouter: vi.fn(),
  createWebHistory: vi.fn(),
}))

vi.mock('../../lib/api/client', () => ({
  getAccessToken: () => accessToken,
}))

import OAuthConsentView from '../../views/OAuthConsentView.vue'

const TEAM_CONTEXT = {
  client_name: 'My MCP App',
  scopes: ['trigger:run', 'hitl:review', 'library:browse'],
  team: { id: 'team-1', name: 'Platform' },
}

const ORG_WIDE_CONTEXT = {
  client_name: 'Org-wide App',
  scopes: ['trigger:run'],
  team: null,
}

function okResponse(payload: unknown) {
  return { ok: true, status: 200, json: async () => payload }
}

function errorResponse(status: number, payload: unknown) {
  return { ok: false, status, json: async () => payload }
}

const fetchMock = vi.fn()

describe('OAuthConsentView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    accessToken = 'jwt-token'
    mockRoute.query = { state: 'st-1' }
    fetchMock.mockReset()
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  async function mountWithContext(context: unknown) {
    fetchMock.mockResolvedValueOnce(okResponse(context))
    const wrapper = mount(OAuthConsentView)
    await flushPromises()
    return wrapper
  }

  it('renders the client name, the grant set and the team-boundary line', async () => {
    const wrapper = await mountWithContext(TEAM_CONTEXT)

    expect(wrapper.find('[data-testid="oauth-consent-client-name"]').text()).toBe('My MCP App')
    const rows = wrapper.findAll('label[data-testid^="oauth-consent-scope-"]')
    expect(rows).toHaveLength(3)
    // The registration picker's i18n labels are reused (not a second scheme).
    expect(wrapper.text()).toContain('Allow this client to trigger pipeline runs.')
    expect(wrapper.text()).toContain('trigger:run')
    const teamLine = wrapper.find('[data-testid="oauth-consent-team-line"]')
    expect(teamLine.exists()).toBe(true)
    expect(teamLine.text()).toContain('Platform')
    // Every requested scope starts granted.
    const checkboxes = wrapper.findAll('input[type="checkbox"]')
    expect(checkboxes).toHaveLength(3)
    for (const box of checkboxes) {
      expect((box.element as HTMLInputElement).checked).toBe(true)
    }
  })

  it('omits the team line for an org-wide client', async () => {
    const wrapper = await mountWithContext(ORG_WIDE_CONTEXT)

    expect(wrapper.find('[data-testid="oauth-consent-client-name"]').text()).toBe('Org-wide App')
    expect(wrapper.find('[data-testid="oauth-consent-team-line"]').exists()).toBe(false)
  })

  it('excludes a toggled-off scope from the approve payload', async () => {
    const wrapper = await mountWithContext(TEAM_CONTEXT)

    const libraryToggle = wrapper.find('[data-testid="oauth-consent-scope-toggle-library:browse"]')
    expect(libraryToggle.exists()).toBe(true)
    await libraryToggle.setValue(false)

    // The approve response is an error so the view never navigates away.
    fetchMock.mockResolvedValueOnce(errorResponse(500, { detail: 'boom' }))
    await wrapper.find('[data-testid="oauth-consent-approve"]').trigger('click')
    await flushPromises()

    const approveCalls = fetchMock.mock.calls.filter((call) => String(call[0]).includes('/consent/approve'))
    expect(approveCalls).toHaveLength(1)
    const options = approveCalls[0][1] as RequestInit
    const body = JSON.parse(String(options.body)) as { state: string; granted_scopes: string[] }
    expect(body.state).toBe('st-1')
    expect(body.granted_scopes).toEqual(['trigger:run', 'hitl:review'])
    // The 500 surfaces as a visible alert (no silent failure).
    expect(wrapper.find('[data-testid="oauth-consent-error"]').text()).toContain('boom')
  })

  it('disables Approve and shows a hint when every scope is denied', async () => {
    const wrapper = await mountWithContext(TEAM_CONTEXT)

    for (const box of wrapper.findAll('input[type="checkbox"]')) {
      await box.setValue(false)
    }

    const approve = wrapper.find('[data-testid="oauth-consent-approve"]')
    expect((approve.element as HTMLButtonElement).disabled).toBe(true)
    expect(wrapper.find('[data-testid="oauth-consent-no-scopes-hint"]').exists()).toBe(true)
  })

  it('renders the context error state when the context fetch fails', async () => {
    fetchMock.mockResolvedValueOnce(errorResponse(404, { detail: 'Consent request not found' }))
    const wrapper = mount(OAuthConsentView)
    await flushPromises()

    expect(wrapper.find('[data-testid="oauth-consent-context-error"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="oauth-consent-client-name"]').exists()).toBe(false)
  })

  it('renders the context error state when the state query param is missing', async () => {
    mockRoute.query = {}
    const wrapper = mount(OAuthConsentView)
    await flushPromises()

    expect(wrapper.find('[data-testid="oauth-consent-context-error"]').exists()).toBe(true)
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('shows the login card (not the consent card) when signed out', async () => {
    accessToken = null
    fetchMock.mockResolvedValueOnce(okResponse(TEAM_CONTEXT))
    const wrapper = mount(OAuthConsentView)
    await flushPromises()

    expect(wrapper.find('[data-testid="oauth-consent-login"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="oauth-consent-approve"]').exists()).toBe(false)
  })

  it('renders a clear declined state on Decline', async () => {
    const wrapper = await mountWithContext(TEAM_CONTEXT)

    await wrapper.find('[data-testid="oauth-consent-decline"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-testid="oauth-consent-declined"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('You declined this authorization request.')
    // The consent card is replaced, not merely hidden below the fold.
    expect(wrapper.find('[data-testid="oauth-consent-approve"]').exists()).toBe(false)
  })
})
