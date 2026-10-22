import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { useEvidenceStore } from '../stores/evidence'
import type { components } from '../lib/api/schema'

const { mockGet } = vi.hoisted(() => ({ mockGet: vi.fn() }))

vi.mock('../lib/api/client', () => ({
  api: { GET: mockGet },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import EvidenceIndexView from '../views/EvidenceIndexView.vue'

type EvidenceIndex = components['schemas']['EvidenceIndex']

const INDEX: EvidenceIndex = {
  pack_version: '2026.01',
  pack_content_hash: 'sha256:pack',
  pack_published_at: '2026-01-01T00:00:00Z',
  generated_at: '2026-01-02T00:00:00Z',
  product_version: '0.3.323',
  organisation_id: 'org-1',
  framework_ids: ['soc2'],
  coverage: { soc2: { CC1: 1 } },
  complete: true,
  missing_artifact_types: [],
  artifacts: [
    {
      artifact_type: 'audit_chain',
      status: 'present',
      provenance: 'observed',
      description: 'Audit chain records',
      location: 'audit-events.ndjson',
      identifiers: ['event-1'],
      record_count: 4,
      detail: {},
      mapping_row_ids: ['CC1'],
      absence_reason: null,
    },
  ],
}

const REFRESHED_INDEX: EvidenceIndex = {
  ...INDEX,
  pack_version: '2026.02',
  generated_at: '2026-01-03T00:00:00Z',
  complete: false,
  missing_artifact_types: ['release_sbom'],
  artifacts: [
    {
      ...INDEX.artifacts[0],
      record_count: 5,
      location: 'audit-events-new.ndjson',
    },
  ],
}

const EMPTY_INDEX: EvidenceIndex = {
  ...INDEX,
  complete: false,
  missing_artifact_types: ['release_sbom', 'decision_records'],
  artifacts: [],
}

const INCOMPLETE_ARTIFACT_INDEX: EvidenceIndex = {
  ...INDEX,
  complete: false,
  artifacts: [
    {
      ...INDEX.artifacts[0],
      status: 'missing',
      provenance: 'derived',
      record_count: null,
      location: '',
      absence_reason: 'The source was not available.',
    },
  ],
}

function success(data: EvidenceIndex) {
  return { data, error: undefined, response: { status: 200, ok: true } }
}

function failure(detail: string) {
  return {
    data: undefined,
    error: { type: 'urn:problem:modulo:service_unavailable', title: 'Service Unavailable', status: 503, detail },
    response: { status: 503, ok: false },
  }
}

async function settle(): Promise<void> {
  await flushPromises()
}

function mountView() {
  return mount(EvidenceIndexView)
}

describe('EvidenceIndexView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    mockGet.mockReset()
  })

  it('shows the initial loading state before the evidence index request resolves', async () => {
    let resolveRequest!: (value: unknown) => void
    mockGet.mockReturnValue(new Promise((resolve) => { resolveRequest = resolve }))

    const wrapper = mountView()
    await wrapper.vm.$nextTick()

    expect(wrapper.find('[data-testid="evidence-index-page"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="evidence-index-loading"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="evidence-index-refresh"]').attributes('disabled')).toBeDefined()
    expect(mockGet).toHaveBeenCalledWith('/api/v1/compliance/evidence-index')

    resolveRequest(success(INDEX))
    await settle()
    expect(wrapper.find('[data-testid="evidence-index-loading"]').exists()).toBe(false)
  })

  it('renders the loaded index and refreshes it through the evidence API contract', async () => {
    mockGet.mockResolvedValueOnce(success(INDEX)).mockResolvedValueOnce(success(REFRESHED_INDEX))

    const wrapper = mountView()
    await settle()

    expect(wrapper.text()).toContain('2026.01')
    expect(wrapper.text()).toContain('audit-events.ndjson')
    expect(wrapper.text()).toContain('4')

    await wrapper.find('[data-testid="evidence-index-refresh"]').trigger('click')
    await settle()

    expect(mockGet).toHaveBeenCalledTimes(2)
    expect(mockGet.mock.calls[1]).toEqual(['/api/v1/compliance/evidence-index'])
    expect(wrapper.text()).toContain('2026.02')
    expect(wrapper.text()).toContain('audit-events-new.ndjson')
    expect(wrapper.text()).toContain('5')
    expect(wrapper.text()).toContain('1 artefact type is missing from the index.')
  })

  it('keeps the last-known-good index visible when a refresh fails', async () => {
    mockGet.mockResolvedValueOnce(success(INDEX)).mockResolvedValueOnce(failure('Evidence index is temporarily unavailable.'))

    const wrapper = mountView()
    await settle()
    await wrapper.find('[data-testid="evidence-index-refresh"]').trigger('click')
    await settle()

    expect(wrapper.text()).toContain('2026.01')
    expect(wrapper.text()).toContain('audit-events.ndjson')
    expect(wrapper.text()).toContain('Evidence index is temporarily unavailable.')
    expect(wrapper.text()).not.toContain('2026.02')
    expect(wrapper.find('[data-testid="evidence-index-refresh"]').attributes('disabled')).toBeUndefined()
  })

  it('renders the empty artefact state and plural missing-artifact message', async () => {
    mockGet.mockResolvedValueOnce(success(EMPTY_INDEX))

    const wrapper = mountView()
    await settle()

    expect(wrapper.find('[data-testid="evidence-index-loading"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('No indexed artefacts')
    expect(wrapper.text()).toContain('This instance has not produced any evidence artefacts yet.')
    expect(wrapper.text()).toContain('2 artefact types are missing from the index.')
    expect(wrapper.find('table').exists()).toBe(false)
  })

  it('renders missing artifact details and the non-complete coverage state', async () => {
    mockGet.mockResolvedValueOnce(success(INCOMPLETE_ARTIFACT_INDEX))

    const wrapper = mountView()
    await settle()

    expect(wrapper.text()).toContain('Incomplete')
    expect(wrapper.text()).toContain('missing')
    expect(wrapper.text()).toContain('The source was not available.')
    expect(wrapper.text()).toContain('—')
  })

  it('reports fallback HTTP errors when the API omits an error detail', async () => {
    mockGet.mockResolvedValueOnce({ data: undefined, error: '', response: { status: 500, ok: false } })
      .mockResolvedValueOnce({ data: undefined, error: '', response: undefined })

    const wrapper = mountView()
    await settle()
    expect(wrapper.text()).toContain('HTTP 500')

    await wrapper.find('[data-testid="error-alert-retry"]').trigger('click')
    await settle()
    expect(wrapper.text()).toContain('HTTP 0')
  })

  it('ignores a concurrent fetch while the first request is loading', async () => {
    let resolveRequest!: (value: unknown) => void
    mockGet.mockReturnValue(new Promise((resolve) => { resolveRequest = resolve }))

    const wrapper = mountView()
    await wrapper.vm.$nextTick()
    const store = useEvidenceStore()

    void store.fetchIndex()
    await store.fetchIndex()
    expect(mockGet).toHaveBeenCalledTimes(1)

    resolveRequest(success(INDEX))
    await settle()
  })
})
