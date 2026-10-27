import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import AdminSystemOrgsView from '../views/AdminSystemOrgsView.vue'

describe('AdminSystemOrgsView', () => {
  it('renders the cross-tenant organisation management guidance and payloads', () => {
    const wrapper = mount(AdminSystemOrgsView)

    expect(wrapper.text()).toContain('System Admin — Organisations')
    expect(wrapper.text()).toContain('Create Organisation')
    expect(wrapper.text()).toContain('POST /api/v1/admin/orgs')
    expect(wrapper.text()).toContain('{ "name": "...", "slug": "..." }')
    expect(wrapper.text()).toContain('Create User in Organisation')
    expect(wrapper.text()).toContain('POST /api/v1/admin/orgs//users')
    expect(wrapper.text()).toContain('admin')
    expect(wrapper.text()).toContain('operator')
    expect(wrapper.text()).toContain('runner')
    expect(wrapper.text()).toContain('viewer')
  })
})
