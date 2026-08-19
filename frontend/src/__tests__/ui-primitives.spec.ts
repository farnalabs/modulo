import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import Button from 'primevue/button'
import Badge from 'primevue/badge'
import InputText from 'primevue/inputtext'
import Card from 'primevue/card'

describe('PrimeVue primitives', () => {
  it('renders Button', () => {
    const wrapper = mount(Button, {
      slots: { default: 'Click me' },
    })
    expect(wrapper.text()).toContain('Click me')
  })

  it('renders Badge with secondary severity', () => {
    const wrapper = mount(Badge, {
      props: { severity: 'secondary' },
      slots: { default: 'New' },
    })
    expect(wrapper.text()).toContain('New')
  })

  it('renders InputText', () => {
    const wrapper = mount(InputText, {
      props: { placeholder: 'Enter name' },
    })
    expect(wrapper.find('input').attributes('placeholder')).toBe('Enter name')
  })

  it('renders Card with title and content', () => {
    const wrapper = mount(Card, {
      slots: {
        title: 'Card Title',
        content: 'Content body',
      },
    })
    expect(wrapper.text()).toContain('Card Title')
    expect(wrapper.text()).toContain('Content body')
  })
})
