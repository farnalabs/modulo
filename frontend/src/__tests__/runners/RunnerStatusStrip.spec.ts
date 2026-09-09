import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import RunnerStatusStrip from '../../components/runners/RunnerStatusStrip.vue'
import type { RunnersStatus } from '../../lib/runnersStatus'

function makeStatus(overrides: Partial<RunnersStatus> = {}): RunnersStatus {
  return {
    aggregate_state: 'healthy',
    probe_interval_seconds: 60,
    staleness_threshold_seconds: 120,
    machines: [
      {
        machine_id: 'machine-1',
        state: 'healthy',
        engine_reachable: true,
        images_present: true,
        probed_at: new Date().toISOString(),
        age_seconds: 20,
        engine_info: { cpu_count: 8, mem_total_mb: 16384 },
        image_checks: {},
        probe_error: null,
      },
    ],
    profiles: [
      {
        id: 'p1',
        name: 'Bundled Runner (Docker)',
        description: null,
        provider_type: 'runner_docker',
        image_ref: 'modulo-runner:opencode',
        config_json: {},
        network_policy: 'outbound',
        persistence_policy: 'ephemeral',
        status: 'active',
        health_state: 'healthy',
        available: true,
        placeholder_digest: false,
        drift: { is_seeded: true, drifted: false, drifted_fields: [] },
      },
    ],
    concurrency: {
      sandbox_concurrency_limit: 4,
      is_default: true,
      preflight: {
        state: 'ok',
        detail: null,
        engine_cpu_count: 8,
        engine_mem_total_mb: 16384,
        needed_cpu: 4,
        needed_mem_mb: 4096,
      },
    },
    ...overrides,
  }
}

describe('RunnerStatusStrip', () => {
  it('renders healthy when the aggregate state is healthy', () => {
    const wrapper = mount(RunnerStatusStrip, { props: { status: makeStatus() } })
    expect(wrapper.find('[data-testid="runner-status-strip-state"]').text()).toBe('✓ healthy')
  })

  it('renders "not enabled on this deployment" when no runner_docker profile exists', () => {
    const status = makeStatus({
      profiles: [
        {
          id: 'p2',
          name: 'E2B',
          description: null,
          provider_type: 'e2b',
          image_ref: null,
          config_json: {},
          network_policy: 'outbound',
          persistence_policy: 'ephemeral',
          status: 'active',
          health_state: null,
          available: true,
          placeholder_digest: false,
          drift: { is_seeded: false, drifted: false, drifted_fields: [] },
        },
      ],
    })
    const wrapper = mount(RunnerStatusStrip, { props: { status } })
    expect(wrapper.find('[data-testid="runner-status-strip-state"]').text()).toBe(
      'not enabled on this deployment',
    )
  })

  it('renders the engine-unreachable warning with the probe error detail', () => {
    const status = makeStatus({
      aggregate_state: 'engine_unreachable',
      machines: [
        {
          machine_id: 'machine-1',
          state: 'engine_unreachable',
          engine_reachable: false,
          images_present: null,
          probed_at: new Date().toISOString(),
          age_seconds: 15,
          engine_info: {},
          image_checks: {},
          probe_error: 'connection refused',
        },
      ],
    })
    const wrapper = mount(RunnerStatusStrip, { props: { status } })
    expect(wrapper.find('[data-testid="runner-status-strip-state"]').text()).toBe('⚠ engine unreachable')
    expect(wrapper.text()).toContain('connection refused')
  })

  it('renders the image-not-pulled warning', () => {
    const status = makeStatus({
      aggregate_state: 'image_not_pulled',
      machines: [
        {
          machine_id: 'machine-1',
          state: 'image_not_pulled',
          engine_reachable: true,
          images_present: false,
          probed_at: new Date().toISOString(),
          age_seconds: 15,
          engine_info: {},
          image_checks: {},
          probe_error: null,
        },
      ],
    })
    const wrapper = mount(RunnerStatusStrip, { props: { status } })
    expect(wrapper.find('[data-testid="runner-status-strip-state"]').text()).toBe('⚠ image not pulled')
  })

  it('renders status unknown with the age when the cached probe is stale', () => {
    const status = makeStatus({
      aggregate_state: 'stale',
      machines: [
        {
          machine_id: 'machine-1',
          state: 'stale',
          engine_reachable: true,
          images_present: true,
          probed_at: new Date(Date.now() - 600_000).toISOString(),
          age_seconds: 600,
          engine_info: {},
          image_checks: {},
          probe_error: null,
        },
      ],
    })
    const wrapper = mount(RunnerStatusStrip, { props: { status } })
    expect(wrapper.find('[data-testid="runner-status-strip-state"]').text()).toBe(
      'status unknown (last checked 600s ago)',
    )
  })

  it('renders the machine count only for multi-machine deployments', () => {
    const single = makeStatus()
    const wrapperSingle = mount(RunnerStatusStrip, { props: { status: single } })
    expect(wrapperSingle.find('[data-testid="runner-status-strip-machines"]').exists()).toBe(false)

    const multi = makeStatus({
      machines: [
        single.machines![0],
        { ...single.machines![0], machine_id: 'machine-2' },
      ],
    })
    const wrapperMulti = mount(RunnerStatusStrip, { props: { status: multi } })
    expect(wrapperMulti.find('[data-testid="runner-status-strip-machines"]').text()).toContain('2')
  })

  it('renders an explicit loading label while the status is absent (qa F11)', () => {
    const wrapper = mount(RunnerStatusStrip, { props: { status: null } })
    expect(wrapper.find('[data-testid="runner-status-strip-state"]').text()).toBe('checking runner status…')
  })

  it('renders the unavailable state when the status fetch failed (qa F11)', () => {
    const wrapper = mount(RunnerStatusStrip, { props: { status: null, errored: true } })
    expect(wrapper.find('[data-testid="runner-status-strip-state"]').text()).toBe('runner status unavailable')
    expect(wrapper.find('[data-testid="runner-status-strip"]').classes().join(' ')).toContain('text-destructive')
  })

  it('renders an unknown probe state AS ITSELF, never as an empty label (qa F17)', () => {
    const status = makeStatus({ aggregate_state: 'brand_new_state' as unknown as RunnersStatus['aggregate_state'] })
    const wrapper = mount(RunnerStatusStrip, { props: { status } })
    const label = wrapper.find('[data-testid="runner-status-strip-state"]').text()
    expect(label).toBe('brand_new_state')
    expect(label).not.toBe('')
  })
})
