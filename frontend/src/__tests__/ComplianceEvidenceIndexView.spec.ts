import { describe, it, expect, vi, beforeEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import type { components } from '../lib/api/schema'

const { mockGet } = vi.hoisted(() => ({ mockGet: vi.fn() }))

vi.mock('../lib/api/client', () => ({
  api: { GET: mockGet },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import ComplianceEvidenceIndexView from '../views/ComplianceEvidenceIndexView.vue'

type EvidenceIndex = components['schemas']['EvidenceIndex']
type ArtifactEntry = components['schemas']['ArtifactEntry']

const artifact = (overrides: Partial<ArtifactEntry> = {}): ArtifactEntry => ({
  artifact_type: 'audit_events',
  status: 'present',
  provenance: 'observed',
  description: 'Recorded audit events',
  location: 'audit_events table',
  identifiers: ['event-1'],
  record_count: 1,
  detail: {},
  mapping_row_ids: ['map-1'],
  absence_reason: null,
  ...overrides,
})

const payload = (overrides: Partial<EvidenceIndex> = {}): EvidenceIndex => ({
  pack_version: '2026.10',
  pack_content_hash: 'sha256:abc',
  pack_published_at: '2026-10-20T12:00:00Z',
  generated_at: '2026-10-22T12:00:00Z',
  product_version: '0.9.0',
  organisation_id: 'org-1',
  framework_ids: ['soc2'],
  coverage: { soc2: { security: 3 } },
  complete: true,
  missing_artifact_types: [],
  artifacts: [artifact()],
  ...overrides,
})

async function settle(): Promise<void> {
  await nextTick()
  await flushPromises()
  await nextTick()
}

function mountView() {
  return mount(ComplianceEvidenceIndexView)
}

describe('ComplianceEvidenceIndexView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockGet.mockResolvedValue({ data: payload(), error: undefined })
  })

  it('fetches the exact endpoint and renders the response shape, metadata, and localized labels', async () => {
    const wrapper = mountView()
    await settle()

    expect(mockGet).toHaveBeenCalledWith('/api/v1/compliance/evidence-index')
    expect(wrapper.find('[data-testid="compliance-evidence-index-coverage"]').text()).toContain('3')
    expect(wrapper.text()).toContain('2026.10')
    expect(wrapper.text()).toContain('sha256:abc')
    expect(wrapper.text()).toContain('0.9.0')
    expect(wrapper.text()).toContain('soc2')
    expect(wrapper.text()).toContain('audit_events')
    expect(wrapper.text()).toContain('Recorded audit events')
    expect(wrapper.text()).toContain('audit_events table')
    expect(wrapper.text()).toContain('event-1')
    expect(wrapper.text()).toContain('map-1')
    expect(wrapper.text()).toContain('1')
    expect(wrapper.text()).toContain('Present')
    expect(wrapper.text()).toContain('Observed')
    expect(wrapper.text()).toContain('10/20/2026')
    expect(wrapper.text()).toContain('10/22/2026')
    expect(wrapper.find('dl').exists()).toBe(true)
    expect(wrapper.findAll('dl dt')).not.toHaveLength(0)
    expect(wrapper.findAll('dl dd')).not.toHaveLength(0)
  })

  it('renders incomplete indexes and every missing artefact type', async () => {
    mockGet.mockResolvedValue({
      data: payload({ complete: false, missing_artifact_types: ['release_sbom', 'build_identity'], artifacts: [] }),
      error: undefined,
    })
    const wrapper = mountView()
    await settle()

    const warning = wrapper.find('[data-testid="compliance-evidence-index-incomplete"]')
    expect(warning.exists()).toBe(true)
    expect(warning.text()).toContain('release_sbom')
    expect(warning.text()).toContain('build_identity')
  })

  it('explains lower-bound human-only counts without treating them as exact', async () => {
    mockGet.mockResolvedValue({
      data: payload({
        artifacts: [artifact({ artifact_type: 'human_decisions', detail: { human_only_counts_are_lower_bounds: true } })],
      }),
      error: undefined,
    })
    const wrapper = mountView()
    await settle()

    const warning = wrapper.find('[data-testid="compliance-evidence-index-lower-bounds"]')
    expect(warning.exists()).toBe(true)
    expect(warning.text()).toContain('lower bounds')
    expect(warning.text()).toContain('human_decisions')
  })

  it('describes all customer-owned omission categories, including vulnerability management', async () => {
    const wrapper = mountView()
    await settle()

    const explanation = wrapper.find('[data-testid="compliance-evidence-index-customer-responsibility"]')
    expect(explanation.text()).toContain('vulnerability-management records')
    expect(explanation.text()).toContain('cannot list them')
  })

  it('renders unknown future status and provenance values without missing-key warnings', async () => {
    mockGet.mockResolvedValue({
      data: payload({ artifacts: [artifact({ status: 'future_status', provenance: 'future_source' })] }),
      error: undefined,
    })
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    const wrapper = mountView()
    await settle()

    expect(wrapper.text()).toContain('Unknown (future_status)')
    expect(wrapper.text()).toContain('Unknown (future_source)')
    expect(warn.mock.calls.some(([message]) => String(message).includes('[intlify] Not found'))).toBe(false)
    warn.mockRestore()
  })

  it('renders the empty state when the index payload is absent', async () => {
    mockGet.mockResolvedValue({ data: null, error: undefined })
    const wrapper = mountView()
    await settle()

    expect(wrapper.text()).toContain('No evidence index available')
    expect(wrapper.find('[data-testid="compliance-evidence-index-coverage"]').exists()).toBe(false)
  })

  it('shows absence reasons and treats missing record counts and mapping rows as unavailable', async () => {
    mockGet.mockResolvedValue({
      data: payload({
        artifacts: [
          artifact({
            artifact_type: 'vendor_assessments',
            status: 'absent',
            absence_reason: 'Not produced by this deployment',
            record_count: null,
            mapping_row_ids: [],
          }),
        ],
      }),
      error: undefined,
    })
    const wrapper = mountView()
    await settle()

    const text = wrapper.text()
    expect(text).toContain('Not produced by this deployment')
    expect(text).toContain('Not available')
  })

  it('falls back to the raw timestamp and labels customer-supplied evidence', async () => {
    mockGet.mockResolvedValue({
      data: payload({
        pack_published_at: 'not-a-timestamp',
        artifacts: [artifact({ status: 'customer_supplied', provenance: 'customer_supplied' })],
      }),
      error: undefined,
    })
    const wrapper = mountView()
    await settle()

    expect(wrapper.text()).toContain('not-a-timestamp')
    expect(wrapper.text()).toContain('Customer supplied')
  })

  it('renders a permission-restricted state instead of a generic retry error', async () => {
    mockGet.mockResolvedValue({
      data: undefined,
      error: { detail: 'Forbidden: missing compliance.evidence_index.read permission' },
    })
    const wrapper = mountView()
    await settle()

    expect(wrapper.find('[data-testid="compliance-evidence-index-permission-denied"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="compliance-evidence-index-error"]').exists()).toBe(false)
  })

  it('shows a request failure and recovers when retry is clicked', async () => {
    mockGet.mockRejectedValueOnce(new Error('index unavailable'))
    const wrapper = mountView()
    await settle()

    expect(wrapper.find('[data-testid="compliance-evidence-index-error"]').text()).toContain('index unavailable')
    mockGet.mockResolvedValue({ data: payload(), error: undefined })
    await wrapper.find('[data-testid="error-alert-retry"]').trigger('click')
    await settle()

    expect(wrapper.find('[data-testid="compliance-evidence-index-error"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('Compliance Evidence Index')
    expect(mockGet).toHaveBeenCalledTimes(2)
  })
})
