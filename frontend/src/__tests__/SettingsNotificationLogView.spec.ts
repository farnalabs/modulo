import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import SettingsNotificationLogView from '../views/SettingsNotificationLogView.vue'

describe('SettingsNotificationLogView', () => {
  it('renders the heading', () => {
    const wrapper = mount(SettingsNotificationLogView)
    expect(wrapper.text()).toContain('Notification Delivery Log')
  })
})
