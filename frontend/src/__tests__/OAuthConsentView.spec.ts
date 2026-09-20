import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { nextTick } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import type { Mock } from 'vitest'

vi.mock('primevue/button', () => ({
  default: {
    name: 'Button',
    template: '<button type="button" :disabled="disabled" @click="$emit(\'click\')"><slot /></button>',
    props: ['disabled', 'class'],
    emits: ['click'],
  },
}))

const mockGetAccessToken = vi.fn()

vi.mock('../lib/api/client', () => ({
  getAccessToken: (..._args: unknown[]) => mockGetAccessToken(),
}))

vi.mock('../lib/api/formatError', () => ({
  formatApiError: (e: unknown) => (e instanceof Error ? e.message : String(e)),
}))

const origFetch = globalThis.fetch
let fetchMock: ReturnType<typeof vi.fn>

import OAuthConsentView from '../views/OAuthConsentView.vue'

describe('OAuthConsentView', () => {
  beforeEach(async () => {
    vi.clearAllMocks()
    mockGetAccessToken.mockReturnValue(null)
    fetchMock = vi.fn()
    globalThis.fetch = fetchMock as unknown as typeof fetch
    localStorage.clear()
  })

  afterEach(() => {
    globalThis.fetch = origFetch
  })

  function setQuery(query: Record<string, string>) {
    const route = (useRoute as Mock)()
    route.query = query
    route.fullPath = `/oauth/consent?${Object.entries(query).map(([k, v]) => `${k}=${v}`).join('&')}`
  }

  it('renders heading and consent description', async () => {
    setQuery({ client_id: 'test', scope: 'read' })
    const wrapper = mount(OAuthConsentView)
    await nextTick()

    expect(wrapper.text()).toContain('Authorize access')
    expect(wrapper.text()).toContain('Approve this application')
  })

  it('shows login prompt when no token is present', async () => {
    mockGetAccessToken.mockReturnValue(null)
    setQuery({ client_id: 'test', scope: 'read' })
    const wrapper = mount(OAuthConsentView)
    await nextTick()

    expect(wrapper.find('[data-testid="oauth-consent-login"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('You must be signed in')
  })

  it('goToLogin navigates to login with redirect query', async () => {
    mockGetAccessToken.mockReturnValue(null)
    setQuery({ client_id: 'test', scope: 'read' })
    const router = (useRouter as Mock)()
    const wrapper = mount(OAuthConsentView)
    await nextTick()

    await wrapper.find('[data-testid="oauth-consent-login"]').trigger('click')
    await nextTick()
    expect(router.push).toHaveBeenCalledWith(
      expect.objectContaining({ name: 'login' }),
    )
  })

  it('shows consent form when token is present', async () => {
    const header = btoa(JSON.stringify({ alg: 'none' }))
    const payload = btoa(JSON.stringify({ username: 'alice' }))
    const token = `${header}.${payload}.`
    mockGetAccessToken.mockReturnValue(token)

    setQuery({ client_id: 'myapp', scope: 'read write' })
    const wrapper = mount(OAuthConsentView)
    await flushPromises()
    await nextTick()

    expect(wrapper.find('[data-testid="oauth-consent-login"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="oauth-consent-approve"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="oauth-consent-client-name"]').text()).toBe('alice')
  })

  it('displays scope list from query string', async () => {
    const header = btoa(JSON.stringify({ alg: 'none' }))
    const payload = btoa(JSON.stringify({ username: 'bob' }))
    const token = `${header}.${payload}.`
    mockGetAccessToken.mockReturnValue(token)

    setQuery({ client_id: 'myapp', scope: 'openid profile email' })
    const wrapper = mount(OAuthConsentView)
    await flushPromises()
    await nextTick()

    const scopes = wrapper.findAll('[data-testid="oauth-consent-scope"]')
    expect(scopes.length).toBe(3)
    expect(scopes.map(s => s.text())).toEqual(['openid', 'profile', 'email'])
  })

  it('falls back to client_id when clientName is empty', async () => {
    const header = btoa(JSON.stringify({ alg: 'none' }))
    const payload = btoa(JSON.stringify({}))
    const token = `${header}.${payload}.`
    mockGetAccessToken.mockReturnValue(token)

    setQuery({ client_id: 'fallback-client' })
    const wrapper = mount(OAuthConsentView)
    await flushPromises()
    await nextTick()

    expect(wrapper.find('[data-testid="oauth-consent-client-name"]').text()).toBe('fallback-client')
  })

  it('sets empty clientName on JWT parse error', async () => {
    mockGetAccessToken.mockReturnValue('invalid.token.here')

    setQuery({ client_id: 'some-client' })
    const wrapper = mount(OAuthConsentView)
    await flushPromises()
    await nextTick()

    expect(wrapper.find('[data-testid="oauth-consent-client-name"]').text()).toBe('some-client')
  })

  it('approve button is disabled while approving', async () => {
    const header = btoa(JSON.stringify({ alg: 'none' }))
    const payload = btoa(JSON.stringify({ username: 'alice' }))
    const token = `${header}.${payload}.`
    mockGetAccessToken.mockReturnValue(token)

    fetchMock.mockReturnValue(new Promise(() => {}))

    setQuery({ state: 'abc123' })
    const wrapper = mount(OAuthConsentView)
    await flushPromises()
    await nextTick()

    await wrapper.find('[data-testid="oauth-consent-approve"]').trigger('click')
    await nextTick()

    expect(
      (wrapper.find('[data-testid="oauth-consent-approve"]').element as HTMLButtonElement).disabled,
    ).toBe(true)
  })

  it('approve action makes POST and redirects', async () => {
    const header = btoa(JSON.stringify({ alg: 'none' }))
    const payload = btoa(JSON.stringify({ username: 'alice' }))
    const token = `${header}.${payload}.`
    mockGetAccessToken.mockReturnValue(token)

    fetchMock.mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ redirect_url: 'https://example.com/callback' }),
    })

    const hrefs: string[] = []
    Object.defineProperty(window, 'location', {
      value: { set href(v: string) { hrefs.push(v) }, get href() { return '' }, pathname: '/' },
      writable: true,
    })

    setQuery({ state: 'xyz789' })
    const wrapper = mount(OAuthConsentView)
    await flushPromises()
    await nextTick()

    await wrapper.find('[data-testid="oauth-consent-approve"]').trigger('click')
    await flushPromises()

    expect(fetchMock).toHaveBeenCalledWith('/api/v1/mcp/oauth/consent/approve', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({ state: 'xyz789' }),
    })
    expect(hrefs).toContain('https://example.com/callback')
  })

  it('approve shows success message before redirect', async () => {
    const header = btoa(JSON.stringify({ alg: 'none' }))
    const payload = btoa(JSON.stringify({ username: 'alice' }))
    const token = `${header}.${payload}.`
    mockGetAccessToken.mockReturnValue(token)

    fetchMock.mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ redirect_url: 'https://example.com/callback' }),
    })

    setQuery({ state: 'abc' })
    const wrapper = mount(OAuthConsentView)
    await flushPromises()
    await nextTick()

    await wrapper.find('[data-testid="oauth-consent-approve"]').trigger('click')
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).toContain('Approved')
  })

  it('approve shows error on non-ok response', async () => {
    const header = btoa(JSON.stringify({ alg: 'none' }))
    const payload = btoa(JSON.stringify({ username: 'alice' }))
    const token = `${header}.${payload}.`
    mockGetAccessToken.mockReturnValue(token)

    fetchMock.mockResolvedValue({
      ok: false,
      statusText: 'Bad Request',
      json: () => Promise.resolve({ detail: 'Invalid state' }),
    })

    setQuery({ state: 'bad' })
    const wrapper = mount(OAuthConsentView)
    await flushPromises()
    await nextTick()

    await wrapper.find('[data-testid="oauth-consent-approve"]').trigger('click')
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).toContain('Invalid state')
  })

  it('approve shows error on network exception', async () => {
    const header = btoa(JSON.stringify({ alg: 'none' }))
    const payload = btoa(JSON.stringify({ username: 'alice' }))
    const token = `${header}.${payload}.`
    mockGetAccessToken.mockReturnValue(token)

    fetchMock.mockRejectedValue(new Error('Network failure'))

    setQuery({ state: 'net' })
    const wrapper = mount(OAuthConsentView)
    await flushPromises()
    await nextTick()

    await wrapper.find('[data-testid="oauth-consent-approve"]').trigger('click')
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).toContain('Network failure')
  })

  it('approve uses statusText when response body has no detail', async () => {
    const header = btoa(JSON.stringify({ alg: 'none' }))
    const payload = btoa(JSON.stringify({ username: 'alice' }))
    const token = `${header}.${payload}.`
    mockGetAccessToken.mockReturnValue(token)

    fetchMock.mockResolvedValue({
      ok: false,
      statusText: 'Internal Server Error',
      json: () => Promise.resolve({}),
    })

    setQuery({ state: 'err' })
    const wrapper = mount(OAuthConsentView)
    await flushPromises()
    await nextTick()

    await wrapper.find('[data-testid="oauth-consent-approve"]').trigger('click')
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).toContain('Internal Server Error')
  })

  it('approve button shows approving text while pending', async () => {
    const header = btoa(JSON.stringify({ alg: 'none' }))
    const payload = btoa(JSON.stringify({ username: 'alice' }))
    const token = `${header}.${payload}.`
    mockGetAccessToken.mockReturnValue(token)

    fetchMock.mockReturnValue(new Promise(() => {}))

    setQuery({ state: 'pend' })
    const wrapper = mount(OAuthConsentView)
    await flushPromises()
    await nextTick()

    await wrapper.find('[data-testid="oauth-consent-approve"]').trigger('click')
    await nextTick()

    expect(wrapper.find('[data-testid="oauth-consent-approve"]').text()).toContain('Approving...')
  })

  it('approve is a no-op when state is empty', async () => {
    const header = btoa(JSON.stringify({ alg: 'none' }))
    const payload = btoa(JSON.stringify({ username: 'alice' }))
    const token = `${header}.${payload}.`
    mockGetAccessToken.mockReturnValue(token)

    setQuery({})
    const wrapper = mount(OAuthConsentView)
    await flushPromises()
    await nextTick()

    await wrapper.find('[data-testid="oauth-consent-approve"]').trigger('click')
    await flushPromises()

    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('renders the Modulo logo SVG', async () => {
    setQuery({})
    const wrapper = mount(OAuthConsentView)
    await nextTick()

    const svg = wrapper.find('svg')
    expect(svg.exists()).toBe(true)
    expect(svg.attributes('role')).toBe('img')
    expect(svg.attributes('aria-label')).toBe('Modulo logo')
  })

  it('scope list handles null scope query gracefully', async () => {
    const header = btoa(JSON.stringify({ alg: 'none' }))
    const payload = btoa(JSON.stringify({ username: 'alice' }))
    const token = `${header}.${payload}.`
    mockGetAccessToken.mockReturnValue(token)

    setQuery({ client_id: 'test' })
    const wrapper = mount(OAuthConsentView)
    await flushPromises()
    await nextTick()

    const scopes = wrapper.findAll('[data-testid="oauth-consent-scope"]')
    expect(scopes.length).toBe(0)
  })

  it('does not show consent form when token is absent', async () => {
    mockGetAccessToken.mockReturnValue(null)
    setQuery({ client_id: 'myapp', scope: 'read' })
    const wrapper = mount(OAuthConsentView)
    await flushPromises()
    await nextTick()

    expect(wrapper.find('[data-testid="oauth-consent-approve"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="oauth-consent-client-name"]').exists()).toBe(false)
  })

  it('approve resets error before new attempt', async () => {
    const header = btoa(JSON.stringify({ alg: 'none' }))
    const payload = btoa(JSON.stringify({ username: 'alice' }))
    const token = `${header}.${payload}.`
    mockGetAccessToken.mockReturnValue(token)

    fetchMock.mockResolvedValueOnce({
      ok: false,
      statusText: 'Bad Request',
      json: () => Promise.resolve({ detail: 'First error' }),
    })

    setQuery({ state: 'retry' })
    const wrapper = mount(OAuthConsentView)
    await flushPromises()
    await nextTick()

    await wrapper.find('[data-testid="oauth-consent-approve"]').trigger('click')
    await flushPromises()
    await nextTick()
    expect(wrapper.text()).toContain('First error')

    fetchMock.mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve({ redirect_url: 'https://ok.com' }),
    })

    await wrapper.find('[data-testid="oauth-consent-approve"]').trigger('click')
    await flushPromises()
    await nextTick()
    expect(wrapper.text()).not.toContain('First error')
  })
})
