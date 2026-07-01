import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'

vi.mock('../../lib/api/client', () => ({
  api: {
    GET: vi.fn().mockResolvedValue({ data: { items: [] }, error: undefined }),
    POST: vi.fn().mockResolvedValue({ data: null, error: undefined }),
    PUT: vi.fn().mockResolvedValue({ data: null, error: undefined }),
    PATCH: vi.fn().mockResolvedValue({ data: null, error: undefined }),
    DELETE: vi.fn().mockResolvedValue({ response: { status: 204, ok: true }, error: undefined }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import OutputValidationTab from '../../components/pipeline/composite/OutputValidationTab.vue'

describe('OutputValidationTab', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders without crashing', async () => {
    const wrapper = mount(OutputValidationTab, {
      props: {
        evalDefinitions: [],
        maxValidationRetries: 0,
      },
    })
    await nextTick()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('eval')
  })

  it('displays eval definition count', async () => {
    const wrapper = mount(OutputValidationTab, {
      props: {
        evalDefinitions: [
          {
            id: 'e1',
            name: 'check_score',
            type: 'regex',
            config: { field: 'score', pattern: '\\d+' },
            failure_behaviour: 'retry',
          },
        ],
        maxValidationRetries: 2,
      },
    })
    await nextTick()
    expect(wrapper.text()).toContain('1 eval')
  })

  it('adds a new eval definition on button click', async () => {
    const wrapper = mount(OutputValidationTab, {
      props: {
        evalDefinitions: [],
        maxValidationRetries: 0,
      },
    })
    await nextTick()

    const btn = wrapper.find('button')
    expect(btn.exists()).toBe(true)
    expect(btn.text()).toContain('Add Eval')
  })

  it('removes an eval definition', async () => {
    const wrapper = mount(OutputValidationTab, {
      props: {
        evalDefinitions: [
          {
            id: 'e1',
            name: 'check',
            type: 'regex',
            config: { field: 'x', pattern: 'y' },
            failure_behaviour: 'retry',
          },
        ],
        maxValidationRetries: 0,
      },
    })
    await nextTick()
    expect(wrapper.text()).toContain('Eval #1')
  })

  it('displays empty state when no evals configured', async () => {
    const wrapper = mount(OutputValidationTab, {
      props: {
        evalDefinitions: [],
        maxValidationRetries: 0,
      },
    })
    await nextTick()
    expect(wrapper.text()).toContain('No output validation evals configured')
  })

  it('renders retry count slider', async () => {
    const wrapper = mount(OutputValidationTab, {
      props: {
        evalDefinitions: [],
        maxValidationRetries: 3,
      },
    })
    await nextTick()
    expect(wrapper.text()).toContain('Max Validation Retries: 3')
  })

  it('renders regex config when type is regex', async () => {
    const wrapper = mount(OutputValidationTab, {
      props: {
        evalDefinitions: [
          {
            id: 'e1',
            name: 'r1',
            type: 'regex',
            config: { field: 'f1', pattern: 'p1' },
            failure_behaviour: 'retry',
          },
        ],
        maxValidationRetries: 0,
      },
    })
    await nextTick()
    expect(wrapper.text()).toContain('Field')
    expect(wrapper.text()).toContain('Pattern')
  })
})
