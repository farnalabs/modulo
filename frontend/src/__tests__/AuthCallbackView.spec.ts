import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createRouter, createWebHistory } from 'vue-router'

vi.mock('../lib/api/client', () => ({
  setAccessToken: vi.fn(),
  getAccessToken: vi.fn().mockReturnValue(null),
}))

import AuthCallbackView from '../views/AuthCallbackView.vue'
import { setAccessToken } from '../lib/api/client'

describe('AuthCallbackView', () => {
  let router: ReturnType<typeof createRouter>

  beforeEach(() => {
    vi.mocked(setAccessToken).mockClear()
    router = createRouter({
      history: createWebHistory(),
      routes: [
        { path: '/auth/callback', name: 'auth-callback', component: AuthCallbackView },
        { path: '/', name: 'dashboard', component: { template: '<div/>' } },
        { path: '/login', name: 'login', component: { template: '<div/>' } },
      ],
    })
  })

  afterEach(() => {
    history.replaceState(null, '', '/')
  })

  it('persists the access token fragment and strips it from the URL', async () => {
    history.replaceState(null, '', '/auth/callback#access_token=acc')

    mount(AuthCallbackView, { global: { plugins: [router] } })
    await flushPromises()

    expect(setAccessToken).toHaveBeenCalledWith('acc')
    expect(window.location.hash).toBe('')
  })

  it('redirects to login when no access token is present in the fragment', async () => {
    history.replaceState(null, '', '/auth/callback#refresh_token=ref')

    mount(AuthCallbackView, { global: { plugins: [router] } })
    await flushPromises()

    expect(setAccessToken).not.toHaveBeenCalled()
  })
})
