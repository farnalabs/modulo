import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { nextTick } from 'vue'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('../lib/api/client', () => ({
  getAccessToken: vi.fn().mockReturnValue('eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhZG1pbkBleGFtcGxlLmNvbSJ9.AAA'),
}))

const { mockGet, mockPut, mockPost } = vi.hoisted(() => ({
  mockGet: vi.fn(),
  mockPut: vi.fn(),
  mockPost: vi.fn(),
}))

vi.mock('../composables/useApi', () => ({
  useApi: () => ({ get: mockGet, put: mockPut, post: mockPost }),
}))

vi.mock('../composables/useDataFetch', async () => {
  const { ref } = await import('vue')
  return {
    useDataFetch: (
      fetcher: () => Promise<{ data: unknown }>,
      options?: { initialValue?: unknown },
    ) => {
      const data = ref(options?.initialValue)
      const loading = ref(false)
      const error = ref('')
      const load = async () => {
        loading.value = true
        try {
          const result = await fetcher()
          ;(data as { value: unknown }).value = result.data ?? options?.initialValue
        } catch {
          error.value = 'Failed to load'
        } finally {
          loading.value = false
        }
      }
      void load()
      return { data, loading, error, load }
    },
  }
})

import AdminUsersView from '../views/AdminUsersView.vue'
import { generateStrongPassword } from '../utils/password'
import { usePlanStore } from '../stores/planStore'

const USERS_RESPONSE = {
  items: [
    {
      id: 'u-1',
      email: 'alice@example.com',
      display_name: 'Alice',
      org_role: 'admin',
      is_active: true,
      auth_provider: 'local',
      created_at: '2026-01-01T00:00:00Z',
      last_login: null,
    },
    {
      id: 'u-2',
      email: 'bob@example.com',
      display_name: 'Bob',
      org_role: 'runner',
      is_active: true,
      auth_provider: 'local',
      created_at: '2026-01-01T00:00:00Z',
      last_login: new Date(Date.now() - 60_000).toISOString(), // nosemgrep: new-date-without-guard
    },
  ],
  total: 2,
  page: 1,
  page_size: 50,
}

function mountView() {
  return mount(AdminUsersView, {
    global: {
      stubs: {
        FeatureGate: { template: '<div><slot /></div>' },
        Select: { template: '<div />' },
        // PrimeVue Dialog teleports to document.body; render inline like
        // RunDetailGuardrail.spec.ts does so dialog content is reachable.
        Dialog: {
          template: '<div class="p-dialog"><slot name="header" /><slot /><slot name="footer" /></div>',
        },
      },
    },
  })
}

// The FAR-462 gating tests assert on FeatureGate's own markup, so they mount
// the REAL FeatureGate (no stubs) with a patched plan store. The users list is
// served by the module-level useApi mock above rather than a global fetch stub.
async function mountViewWithPlan(planPatch: Record<string, unknown>) {
  const pinia = createPinia()
  setActivePinia(pinia)
  const store = usePlanStore()
  store.$patch(planPatch)
  const wrapper = mount(AdminUsersView, {
    global: { plugins: [pinia] },
  })
  await flushPromises()
  for (let i = 0; i < 3; i++) {
    await nextTick()
  }
  return wrapper
}

describe('AdminUsersView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    mockGet.mockResolvedValue(USERS_RESPONSE)
    Object.assign(navigator, { clipboard: { writeText: vi.fn().mockResolvedValue(undefined) } })
  })

  it('renders without crashing', async () => {
    const wrapper = mountView()
    await nextTick()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Users')
  })

  it('shows "Never logged in" when last_login is null and relative time otherwise', async () => {
    const wrapper = mountView()
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).toContain('Never logged in')
    expect(wrapper.text()).toContain('minute ago')
    // Full timestamp tooltip carries the absolute date too.
    const badge = wrapper.find('span[title]')
    expect(badge.exists()).toBe(true)
  })

  it('does not claim there is a signup flow in the empty state', async () => {
    mockGet.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 50 })
    const wrapper = mountView()
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).toContain('Users will appear here once they are created by an admin.')
    expect(wrapper.text()).not.toContain('sign up')
  })

  it('generate button fills a password meeting the displayed complexity rules', async () => {
    const wrapper = mountView()
    await flushPromises()

    // Open the create-user dialog first — FormDialog renders its slot only when open.
    await wrapper.find('[data-testid="admin-users-add-user"]').trigger('click')
    await nextTick()

    await wrapper.find('[data-testid="admin-users-generate-password"]').trigger('click')

    const input = wrapper.find<HTMLInputElement>('[data-testid="admin-users-create-password"]')
    expect((input.element as HTMLInputElement).value.length).toBeGreaterThanOrEqual(8)
    expect((input.element as HTMLInputElement).value).toMatch(/[a-z]/)
    expect((input.element as HTMLInputElement).value).toMatch(/[A-Z]/)
    expect((input.element as HTMLInputElement).value).toMatch(/\d/)
  })

  it('shows the shared credential dialog with a copy button after create user succeeds', async () => {
    mockPost.mockResolvedValue({ id: 'u-new', email: 'carol@example.com', display_name: 'Carol', org_role: 'runner' })
    const wrapper = mountView()
    await flushPromises()

    await wrapper.find('[data-testid="admin-users-add-user"]').trigger('click')
    await nextTick()

    await wrapper.find('[data-testid="admin-users-create-email"]').setValue('carol@example.com')
    await wrapper.find('[data-testid="admin-users-create-display-name"]').setValue('Carol')
    await wrapper.find('[data-testid="admin-users-create-password"]').setValue('Sup3rSecret!')

    await wrapper.find('form').trigger('submit')
    await flushPromises()
    await nextTick()

    // Reusable credential dialog carries the typed credential + copy wiring.
    expect(wrapper.text()).toContain('Copy it now')
    expect(wrapper.text()).toContain('Credentials')

    expect(mockPost).toHaveBeenCalledWith('/api/v1/admin/users', {
      email: 'carol@example.com',
      display_name: 'Carol',
      password: 'Sup3rSecret!',
      org_role: 'runner',
    })

    // Copying from the shared dialog copies the create-time credential.
    await wrapper.find('[data-testid="admin-users-copy-password"]').trigger('click')
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith('Sup3rSecret!')
  })

  it('reuses the same dialog after reset password with the enforced-change wording', async () => {
    mockPost.mockResolvedValue({ temporary_password: 'temp-passw0rd' })
    const wrapper = mountView()
    await flushPromises()
    await nextTick()

    // Real TableActions renders clickable row-action buttons.
    await wrapper.findAll('table tbody tr')[0].findAll('button')
      .find(b => b.text() === 'Reset Password')!
      .trigger('click')
    await flushPromises()
    await nextTick()

    expect(mockPost).toHaveBeenCalledWith('/api/v1/admin/users/u-1/reset-password')

    // Dialog header switches to Password Reset; the wording now matches the
    // enforced behaviour (user IS prompted to change it on next login).
    expect(wrapper.text()).toContain('Password Reset')
    expect(wrapper.text()).toContain("they will be prompted to change it on their next login")

    // Copying delivers the temporary credential from the reset response.
    await wrapper.find('[data-testid="admin-users-copy-password"]').trigger('click')
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith('temp-passw0rd')
  })

  it('renders the users table with no lock overlay on community tier (FAR-462)', async () => {
    const wrapper = await mountViewWithPlan({
      features: { user_management: true },
      currentTier: 'community',
    })

    expect(wrapper.find('[data-testid="feature-gate"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="feature-gate-disabled"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="feature-gate-lock"]').exists()).toBe(false)
    expect(wrapper.find('table').exists()).toBe(true)
    expect(wrapper.text()).toContain('alice@example.com')
  })

  it('stays unlocked via the community required-tier fallback when flags have not loaded', async () => {
    const wrapper = await mountViewWithPlan({ features: {}, currentTier: 'community' })

    expect(wrapper.find('[data-testid="feature-gate-disabled"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="feature-gate-lock"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('Users')
  })
})

describe('generateStrongPassword', () => {
  it('always meets complexity rules across many draws', () => {
    for (let i = 0; i < 200; i++) {
      const pw = generateStrongPassword()
      expect(pw.length).toBeGreaterThanOrEqual(8)
      expect(pw).toMatch(/[a-z]/)
      expect(pw).toMatch(/[A-Z]/)
      expect(pw).toMatch(/\d/)
    }
  })
})
