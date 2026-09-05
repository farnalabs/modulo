import { describe, it, expect, vi, beforeEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import type { ParameterPort } from '../../types/pipeline'

const { patchMock, postMock } = vi.hoisted(() => ({
  patchMock: vi.fn(),
  postMock: vi.fn(),
}))

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({
    get: vi.fn(),
    post: postMock,
    put: vi.fn(),
    patch: patchMock,
    delete: vi.fn(),
  }),
}))

import PublishCompositeFlow from '../../components/pipeline/composite/PublishCompositeFlow.vue'
import { useRouter } from 'vue-router'

function makePort(overrides: Partial<ParameterPort> = {}): ParameterPort {
  return {
    id: 'port-1',
    name: 'model_temperature',
    label: 'Temperature',
    description: 'Controls randomness',
    type: 'number',
    required: true,
    default_value: 0.7,
    options: null,
    multiline: false,
    target_injection: { mode: 'prompt_replace', node_id: '', injection_point: 'prompt_template' },
    ...overrides,
  }
}

function mountFlow(ports: ParameterPort[] = [makePort()]) {
  return mount(PublishCompositeFlow, {
    props: { compositeId: 'comp-1', ports },
  })
}

/** Advances from step 1 to `step` (absolute). Only valid on a fresh wrapper. */
async function advanceToStep(wrapper: ReturnType<typeof mount>, step: number) {
  for (let s = 1; s < step; s++) {
    await clickNext(wrapper, s === 1)
  }
}

/** Clicks the primary Next/Publish button once. */
async function clickNext(wrapper: ReturnType<typeof mount>, fillName = false) {
  if (fillName && wrapper.find('#publishcompositeflow-field-3').exists()) {
    await wrapper.find('#publishcompositeflow-field-3').setValue('Code Review Assistant')
  }
  const next = wrapper.findAll('button').find(b => ['Next', 'Publish'].includes(b.text().trim()))
  await next!.trigger('click')
  await flushPromises()
}

describe('PublishCompositeFlow', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    patchMock.mockResolvedValue({})
    postMock.mockResolvedValue({})
  })

  it('renders step 1 and disables Next until a name is entered', async () => {
    const wrapper = mountFlow()
    expect(wrapper.text()).toContain('Name & Description')
    const next = wrapper.findAll('button').find(b => b.text().trim() === 'Next')
    expect(next!.attributes('disabled')).toBeDefined()
    await wrapper.find('#publishcompositeflow-field-3').setValue('Code Review Assistant')
    expect(next!.attributes('disabled')).toBeUndefined()
  })

  it('patches the composite name and moves to step 2', async () => {
    const wrapper = mountFlow()
    await wrapper.find('#publishcompositeflow-field-3').setValue('Code Review Assistant')
    await wrapper.find('#publishcompositeflow-field-2').setValue('Reviews code')
    const next = wrapper.findAll('button').find(b => b.text().trim() === 'Next')
    await next!.trigger('click')
    await flushPromises()
    expect(patchMock).toHaveBeenCalledWith('/api/v1/composite-templates/comp-1', {
      name: 'Code Review Assistant',
      description: 'Reviews code',
    })
    expect(wrapper.text()).toContain('Review Ports')
  })

  it('sends a null description when left blank', async () => {
    const wrapper = mountFlow()
    await wrapper.find('#publishcompositeflow-field-3').setValue('Code Review Assistant')
    const next = wrapper.findAll('button').find(b => b.text().trim() === 'Next')
    await next!.trigger('click')
    await flushPromises()
    expect(patchMock).toHaveBeenCalledWith('/api/v1/composite-templates/comp-1', {
      name: 'Code Review Assistant',
      description: null,
    })
  })

  it('shows a step-1 patch error inline and stays on step 1', async () => {
    patchMock.mockRejectedValue(new Error('name already taken'))
    const wrapper = mountFlow()
    await wrapper.find('#publishcompositeflow-field-3').setValue('Code Review Assistant')
    const next = wrapper.findAll('button').find(b => b.text().trim() === 'Next')
    await next!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('name already taken')
    expect(wrapper.text()).toContain('Name & Description')
  })

  it('reviews ports on step 2 and shows the empty message when none exist', async () => {
    const wrapper = mountFlow([makePort()])
    await advanceToStep(wrapper, 2)
    expect(wrapper.text()).toContain('Temperature')
    expect(wrapper.text()).toContain('{{parameter.model_temperature}}')
    expect(wrapper.text()).toContain('required')

    const emptyWrapper = mountFlow([])
    await advanceToStep(emptyWrapper, 2)
    expect(emptyWrapper.text()).toContain('No parameter ports defined')
  })

  it('carries the version through step 3 and confirms on step 4', async () => {
    const wrapper = mountFlow()
    await advanceToStep(wrapper, 3)
    expect(wrapper.text()).toContain('Version')
    expect((wrapper.find('#publishcompositeflow-field-1').element as HTMLInputElement).value).toBe('1.0.0')
    await wrapper.find('#publishcompositeflow-field-1').setValue('2.1.0')
    await clickNext(wrapper)
    expect(wrapper.text()).toContain('Ready to publish')
    expect(wrapper.text()).toContain('Code Review Assistant')
    expect(wrapper.text()).toContain('2.1.0')
  })

  it('publishes on the final step, emits published and shows success', async () => {
    const wrapper = mountFlow()
    await advanceToStep(wrapper, 3)
    await clickNext(wrapper)
    const publish = wrapper.findAll('button').find(b => b.text().trim() === 'Publish')
    await publish!.trigger('click')
    await flushPromises()
    expect(postMock).toHaveBeenCalledWith('/api/v1/composite-templates/comp-1/publish', { version: '1.0.0' })
    expect(wrapper.emitted('published')).toHaveLength(1)
    expect(wrapper.text()).toContain('Published!')
  })

  it('shows a publish error inline and stays un-successful', async () => {
    postMock.mockRejectedValue(new Error('version conflict'))
    const wrapper = mountFlow()
    await advanceToStep(wrapper, 3)
    await clickNext(wrapper)
    const publish = wrapper.findAll('button').find(b => b.text().trim() === 'Publish')
    await publish!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('version conflict')
    expect(wrapper.text()).not.toContain('Published!')
    expect(wrapper.emitted('published')).toBeUndefined()
  })

  it('navigates to the library from the success panel', async () => {
    const wrapper = mountFlow()
    await advanceToStep(wrapper, 3)
    await clickNext(wrapper)
    const publish = wrapper.findAll('button').find(b => b.text().trim() === 'Publish')
    await publish!.trigger('click')
    await flushPromises()
    const goToLibrary = wrapper.findAll('button').find(b => b.text().includes('Go to Library'))
    await goToLibrary!.trigger('click')
    expect(useRouter().push).toHaveBeenCalledWith({ name: 'library' })
  })

  it('emits close from the success "Stay Here" button', async () => {
    const wrapper = mountFlow()
    await advanceToStep(wrapper, 3)
    await clickNext(wrapper)
    const publish = wrapper.findAll('button').find(b => b.text().trim() === 'Publish')
    await publish!.trigger('click')
    await flushPromises()
    const stayHere = wrapper.findAll('button').find(b => b.text().includes('Stay Here'))
    await stayHere!.trigger('click')
    expect(wrapper.emitted('close')).toHaveLength(1)
  })

  it('steps back with the Back button', async () => {
    const wrapper = mountFlow()
    await advanceToStep(wrapper, 2)
    const back = wrapper.findAll('button').find(b => b.text().trim() === 'Back')
    await back!.trigger('click')
    expect(wrapper.text()).toContain('Name & Description')
  })

  it('closes via backdrop click', async () => {
    const wrapper = mountFlow()
    await wrapper.find('.fixed.inset-0').trigger('click')
    expect(wrapper.emitted('close')).toHaveLength(1)
  })
})
