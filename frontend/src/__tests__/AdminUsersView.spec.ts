import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { nextTick } from 'vue'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('../lib/api/client', () => ({
  getAccessToken: vi.fn().mockReturnValue('eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhZG1pbkBleGFtcGxlLmNvbSJ9.AAA'),
}))

const { mockGet, mockPut, mockPost, mockDelete } = vi.hoisted(() => ({
  mockGet: vi.fn(),
  mockPut: vi.fn(),
  mockPost: vi.fn(),
  mockDelete: vi.fn(),
}))

vi.mock('../composables/useApi', () => ({
  useApi: () => ({ get: mockGet, put: mockPut, post: mockPost, delete: mockDelete }),
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

function baseMocks() {
  mockGet.mockImplementation((path: string) => {
    if (path.startsWith('/api/v1/admin/users?')) {
      return Promise.resolve(USERS_RESPONSE)
    }
    if (path.startsWith('/api/v1/admin/users/invitations')) {
      return Promise.resolve({
        items: [
          {
            id: 'inv-1',
            email: 'newbie@example.com',
            display_name: 'Newbie',
            org_role: 'runner',
            invited_by: 'u-9',
            created_at: '2026-08-20T10:00:00+00:00',
            expires_at: '2026-08-23T10:00:00+00:00',
          },
        ],
        total: 1,
        page: 1,
        page_size: 100,
      })
    }
    throw new Error(`unexpected GET ${path}`)
  })
}

describe('AdminUsersView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    baseMocks()
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
    // Success state (not the error state) after a resolved copy.
    await flushPromises()
    expect(wrapper.find('[data-testid="admin-users-copy-error"]').exists()).toBe(false)
  })

  it('shows a visible error state when the clipboard write is rejected', async () => {
    mockPost.mockResolvedValue({ id: 'u-new', email: 'carol@example.com', display_name: 'Carol', org_role: 'runner' })
    Object.assign(navigator, { clipboard: { writeText: vi.fn().mockRejectedValue(new Error('denied')) } })
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

    await wrapper.find('[data-testid="admin-users-copy-password"]').trigger('click')
    await flushPromises()

    // A rejected copy must NOT claim "Copied!" — the show-once secret needs an
    // honest visible error state instead.
    expect(wrapper.find('[data-testid="admin-users-copy-error"]').exists()).toBe(true)
    expect(wrapper.text()).not.toContain('Copied!')
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

  it('lists pending invitations with role and a Revoke action', async () => {
    const wrapper = mountView()
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="admin-invitations-row"]').exists()).toBe(true)
    })
    const row = wrapper.findAll('[data-testid="admin-invitations-row"]')[0]
    expect(row.text()).toContain('newbie@example.com')
    expect(row.text()).toContain('runner')
  })

  it('revokes a pending invitation after confirmation via DELETE', async () => {
    mockDelete.mockResolvedValue(undefined)
    const wrapper = mountView()
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="admin-invitations-row"]').exists()).toBe(true)
    })
    await wrapper.findAll('[data-testid="admin-invitations-revoke"]')[0].trigger('click')
    await nextTick()
    const confirmButton = wrapper.find('[data-testid="admin-invitations-confirm-revoke"]')
    expect(confirmButton.exists()).toBe(true)
    await confirmButton.trigger('click')
    await vi.waitFor(() => {
      expect(mockDelete).toHaveBeenCalledWith('/api/v1/admin/users/invitations/inv-1')
    })
  })

  it('invite mode hides the password field; password mode keeps it', async () => {
    const wrapper = mountView()
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="admin-users-add-user"]').exists()).toBe(true)
    })
    await wrapper.find('[data-testid="admin-users-add-user"]').trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="admin-users-create-password"]').exists()).toBe(true)
    await wrapper.find('[data-testid="admin-users-mode-invite"]').trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="admin-users-create-password"]').exists()).toBe(false)
  })

  it('submits an invite and surfaces the single-use invite link', async () => {
    mockPost.mockResolvedValue({
      id: 'inv-2',
      invite_url: 'https://app.test/accept-invite#token=abc123',
      expires_at: '2026-08-23T10:00:00+00:00',
    })
    const wrapper = mountView()
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="admin-users-add-user"]').exists()).toBe(true)
    })
    await wrapper.find('[data-testid="admin-users-add-user"]').trigger('click')
    await nextTick()
    await wrapper.find('[data-testid="admin-users-mode-invite"]').trigger('click')
    await nextTick()
    await wrapper.find('[data-testid="admin-users-create-email"]').setValue('invitee@example.com')
    await wrapper.find('[data-testid="admin-users-create-display-name"]').setValue('Invitee')
    await wrapper.find('form').trigger('submit')
    await vi.waitFor(() => {
      expect(mockPost).toHaveBeenCalledWith('/api/v1/admin/users/invite', {
        email: 'invitee@example.com',
        display_name: 'Invitee',
        org_role: 'runner',
      })
    })
    // The one-time invite URL is handed to the admin exactly once, in the
    // shared credential dialog (never persisted by the frontend).
    await vi.waitFor(() => {
      const cred = wrapper.find('[data-testid="admin-users-credential-value"]')
      expect(cred.exists()).toBe(true)
      expect(cred.text()).toContain('token=abc123')
    })
  })

  it('shows an inline error when sending an invite fails', async () => {
    mockPost.mockRejectedValue(new Error('A user with this email already exists in this organisation'))
    const wrapper = mountView()
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="admin-users-add-user"]').exists()).toBe(true)
    })
    await wrapper.find('[data-testid="admin-users-add-user"]').trigger('click')
    await nextTick()
    await wrapper.find('[data-testid="admin-users-mode-invite"]').trigger('click')
    await nextTick()
    await wrapper.find('[data-testid="admin-users-create-email"]').setValue('invitee@example.com')
    await wrapper.find('[data-testid="admin-users-create-display-name"]').setValue('Invitee')
    await wrapper.find('form').trigger('submit')
    await vi.waitFor(() => {
      expect(wrapper.text()).toContain(
        'A user with this email already exists in this organisation',
      )
    })
  })

  it('clears the temporary password when the credential dialog is dismissed', async () => {
    mockPost.mockResolvedValue({ temporary_password: '$2b$12$tempsecret' })
    const wrapper = mountView()
    await vi.waitFor(() => {
      expect(wrapper.findAll('button').some(b => b.text() === 'Reset Password')).toBe(true)
    })
    const resetButton = wrapper.findAll('button').find(b => b.text() === 'Reset Password')
    expect(resetButton).toBeDefined()
    await resetButton!.trigger('click')
    await vi.waitFor(() => {
      const code = wrapper.find('[data-testid="admin-users-credential-value"]')
      expect(code.exists()).toBe(true)
      expect(code.text()).toBe('$2b$12$tempsecret')
    })
    const doneButton = wrapper.findAll('button').find(b => b.text() === 'Done')
    expect(doneButton).toBeDefined()
    await doneButton!.trigger('click')
    await nextTick()
    const codeAfter = wrapper.find('[data-testid="admin-users-credential-value"]')
    // Either unmounted or rendered empty — the secret must be gone either way.
    if (codeAfter.exists()) expect(codeAfter.text()).toBe('')
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
