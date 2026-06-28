import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import Button from '../components/ui/button/Button.vue'
import Badge from '../components/ui/badge/Badge.vue'
import Input from '../components/ui/input/Input.vue'
import { Card, CardHeader, CardTitle, CardContent } from '../components/ui/card'
import { h } from 'vue'

describe('shadcn-vue primitives', () => {
  it('renders Button', () => {
    const wrapper = mount(Button, {
      props: { variant: 'default' },
      slots: { default: 'Click me' },
    })
    expect(wrapper.text()).toContain('Click me')
    expect(wrapper.attributes('data-variant')).toBe('default')
  })

  it('renders Badge with secondary variant', () => {
    const wrapper = mount(Badge, {
      props: { variant: 'secondary' },
      slots: { default: 'New' },
    })
    expect(wrapper.text()).toContain('New')
    expect(wrapper.attributes('data-variant')).toBe('secondary')
  })

  it('renders Input', () => {
    const wrapper = mount(Input, {
      props: { placeholder: 'Enter name' },
    })
    expect(wrapper.find('input').attributes('placeholder')).toBe('Enter name')
  })

  it('renders Card with header and content', () => {
    const wrapper = mount(Card, {
      slots: {
        default: [
          h(CardHeader, () => h(CardTitle, {}, () => 'Card Title')),
          h(CardContent, () => 'Content body'),
        ],
      },
    })
    expect(wrapper.text()).toContain('Card Title')
    expect(wrapper.text()).toContain('Content body')
  })
})
