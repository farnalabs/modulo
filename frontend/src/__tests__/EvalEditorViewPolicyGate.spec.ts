// Component tests for the eval-editor policy-gate UI (FAR-1106 chunk 6, spec §7a).
// Companion spec to EvalEditorView.spec.ts (which covers the pre-gate CRUD surface).
// Assertions target DOM state, not internal reactive state (spec §7a); where a
// criterion is only observable server-side, the backend route source itself is
// read as the contract surface (criteria 17 and 22).
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'
import { readFileSync } from 'fs'
import { join } from 'path'

vi.mock('../lib/api/client', () => ({
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
  api: {
    GET: vi.fn(),
    POST: vi.fn(),
    PUT: vi.fn(),
    DELETE: vi.fn(),
  },
}))

import EvalEditorView from '../views/EvalEditorView.vue'
import { api } from '../lib/api/client'
import { usePlanStore } from '../stores/planStore'
import { getNavGroups } from '../config/navigation'

const apiGET = api.GET as ReturnType<typeof vi.fn>
const apiPOST = api.POST as ReturnType<typeof vi.fn>
const apiPUT = api.PUT as ReturnType<typeof vi.fn>
const apiDELETE = api.DELETE as ReturnType<typeof vi.fn>

const GATE_URL = '/api/v1/evals/{eval_id}/policy-gate'
const EVAL_PUT_URL = '/api/v1/evals/{eval_id}'
const BACKEND_EVALS_ROUTE = join(
  __dirname, '..', '..', '..', 'backend', 'src', 'modulo', 'api', 'routes', 'evals.py',
)

// openapi-fetch envelope helpers: non-2xx resolves as { data: undefined, error }
// — it never throws (that is the bug class these tests pin down).
function ok(data: unknown) {
  return { data, error: undefined }
}
function fail(status: number, detail = 'request failed') {
  return { data: undefined, error: { status, detail } }
}

function evalItem(over: Record<string, unknown> = {}) {
  return {
    id: 'eval-1',
    pipeline_id: 'p1',
    node_id: null,
    name: 'Existing Eval',
    eval_type: 'regex',
    config_json: { pattern: '.*' },
    pass_threshold: 0.9,
    suite_id: null,
    created_by: 'user-1',
    ...over,
  }
}

let evalsList: Record<string, unknown>[] = []
const gateResponses: Record<string, { data: unknown; error: unknown }> = {}
const responders: Record<string, { data: unknown; error: unknown }> = {}

function installRouter() {
  apiGET.mockImplementation(async (url: string, init?: { params?: { path?: Record<string, string> } }) => {
    if (url === '/api/v1/pipelines') {
      return ok({ items: [{ id: 'p1', name: 'P One', description: null }] })
    }
    if (url === '/api/v1/pipelines/{pipeline_id}/graph') {
      return ok({
        nodes: [{ id: 'n1', node_type: 'agent', label: 'Writer', agent_id: 'a1', position: { x: 0, y: 0 } }],
      })
    }
    if (url === '/api/v1/evals') return ok({ items: evalsList })
    if (url === '/api/v1/admin/feature-flags') {
      return ok({
        license: { tier: 'community' },
        dev_mode: false,
        flags: [{ name: 'eval_system', currently_active: true }],
      })
    }
    if (url === '/api/v1/admin/license') return ok({ tier: 'community' })
    if (url === '/api/v1/admin/tiers') return ok({ tiers: [] })
    if (url === GATE_URL) {
      const id = init?.params?.path?.eval_id ?? ''
      return gateResponses[id] ?? fail(404, 'Policy gate not found')
    }
    return ok({ items: [] })
  })
  apiPOST.mockImplementation(async (url: string) => {
    if (url === '/api/v1/evals') return responders.evalPost
    if (url === GATE_URL) return responders.gatePost
    return ok({})
  })
  apiPUT.mockImplementation(async (url: string) => {
    if (url === EVAL_PUT_URL) return responders.evalPut
    if (url === GATE_URL) return responders.gatePut
    return ok({})
  })
  apiDELETE.mockImplementation(async (url: string) => {
    if (url === GATE_URL) return responders.gateDelete
    if (url === EVAL_PUT_URL) return responders.evalDelete
    return ok({})
  })
}

const viewStubs = {
  LoadingSpinner: true,
  ErrorAlert: true,
  PageHeader: { template: '<div />' },
}

type ViewWrapper = ReturnType<typeof mountView>

function mountView() {
  return mount(EvalEditorView, { global: { stubs: viewStubs } })
}

async function flush() {
  await flushPromises()
  await nextTick()
}

async function selectPipeline(wrapper: ViewWrapper) {
  ;(wrapper.vm as unknown as { selectedPipelineId: string }).selectedPipelineId = 'p1'
  await (wrapper.vm as unknown as { onPipelineChange: () => Promise<void> }).onPipelineChange()
}

async function openEditor(wrapper: ViewWrapper, evalId: string) {
  const idx = evalsList.findIndex((e) => e.id === evalId)
  const buttons = wrapper.findAll('[data-testid="eval-editor-edit"]')
  await buttons[idx].trigger('click')
  await flush()
}

function warnChecked(wrapper: ViewWrapper) {
  return (wrapper.find('[data-test-id="policy-gate-action-warn"]').element as HTMLInputElement).checked
}
function blockChecked(wrapper: ViewWrapper) {
  return (wrapper.find('[data-test-id="policy-gate-action-block"]').element as HTMLInputElement).checked
}

describe('EvalEditorView — policy gate (FAR-1106 chunk 6, spec §7a)', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    const plan = usePlanStore()
    plan.features = { eval_system: true }
    plan.loaded = true
    evalsList = []
    for (const key of Object.keys(gateResponses)) delete gateResponses[key]
    responders.evalPost = ok({ id: 'new-eval' })
    responders.evalPut = ok({})
    responders.gatePost = ok({ id: 'gate-1', action: 'warn', version: 1 })
    responders.gatePut = ok({ id: 'gate-1', action: 'block', version: 2 })
    responders.gateDelete = ok(null)
    responders.evalDelete = ok(null)
    installRouter()
  })

  // Criterion 1 — the gate section renders in the editor with warn as default.
  it('renders the gate section with warn selected by default (criterion 1)', async () => {
    const wrapper = mountView()
    await flush()

    expect(wrapper.find('[data-test-id="policy-gate-heading"]').text()).toBe('Policy Gate')
    expect(warnChecked(wrapper)).toBe(true)
    expect(blockChecked(wrapper)).toBe(false)
    expect(wrapper.text()).toContain('Log and continue')
  })

  // Criteria 2 + 13 — opening an eval loads its gate (action + identity).
  it('populates the existing gate on open (criteria 2, 13)', async () => {
    evalsList = [evalItem({ id: 'eval-1' })]
    gateResponses['eval-1'] = ok({ id: 'gate-1', action: 'block', version: 3 })
    const wrapper = mountView()
    await flush()
    await selectPipeline(wrapper)
    await flush()
    await openEditor(wrapper, 'eval-1')

    expect(apiGET).toHaveBeenCalledWith(GATE_URL, { params: { path: { eval_id: 'eval-1' } } })
    expect(blockChecked(wrapper)).toBe(true)
    expect(warnChecked(wrapper)).toBe(false)
    // exists=true → the delete affordance is present
    expect(wrapper.find('[data-test-id="policy-gate-delete"]').exists()).toBe(true)
  })

  // Criteria 13 + 24 — no gate on the server → defaults, no stale identity.
  it('falls back to defaults when the eval has no gate (criteria 13, 24)', async () => {
    evalsList = [evalItem({ id: 'eval-2', name: 'Gateless' })]
    const wrapper = mountView()
    await flush()
    await selectPipeline(wrapper)
    await flush()
    await openEditor(wrapper, 'eval-2')

    expect(apiGET).toHaveBeenCalledWith(GATE_URL, { params: { path: { eval_id: 'eval-2' } } })
    expect(warnChecked(wrapper)).toBe(true)
    expect(blockChecked(wrapper)).toBe(false)
    expect(wrapper.find('[data-test-id="policy-gate-delete"]').exists()).toBe(false)
    expect(wrapper.find('[data-test-id="policy-gate-error"]').exists()).toBe(false)
  })

  // Criterion 15 — policy gates are reachable only inside the eval editor.
  it('exposes no nav entry or route for policy gates (criterion 15)', () => {
    const nav = JSON.stringify(getNavGroups())
    // Positive control: the assertion is meaningful only if the nav is real.
    expect(nav).toContain('/evals/editor')
    expect(nav.toLowerCase()).not.toMatch(/policy[-_ ]?gate/)
  })

  // Criterion 16 — per-eval badges; none for evals without a gate.
  it('shows gate badges for gated evals only (criterion 16)', async () => {
    evalsList = [
      evalItem({ id: 'eval-1', name: 'Blocked Eval' }),
      evalItem({ id: 'eval-2', name: 'Warned Eval' }),
      evalItem({ id: 'eval-3', name: 'Gateless Eval' }),
    ]
    gateResponses['eval-1'] = ok({ id: 'g1', action: 'block', version: 1 })
    gateResponses['eval-2'] = ok({ id: 'g2', action: 'warn', version: 1 })
    const wrapper = mountView()
    await flush()
    await selectPipeline(wrapper)
    await flush()

    const badges = wrapper.findAll('[data-test-id="policy-gate-badge"]')
    expect(badges).toHaveLength(2)
    const texts = badges.map((b) => b.text())
    expect(texts).toContain('block')
    expect(texts).toContain('warn')
  })

  // Criterion 17 — the view's gate write is bound to the backend mutable-fields
  // contract, proven against the actual route source (not a mock of it).
  it('binds the gate update body to the backend mutable_fields contract (criterion 17)', async () => {
    const source = readFileSync(BACKEND_EVALS_ROUTE, 'utf-8')

    const mfMatch = source.match(/mutable_fields\s*=\s*\{([^}]*)\}/)
    expect(mfMatch).not.toBeNull()
    const mutableFields = mfMatch![1]
      .split(',')
      .map((s) => s.replace(/["'\s]/g, ''))
      .filter(Boolean)
    expect(mutableFields).toEqual(['action'])

    // The pre-delete snapshot iterates the mutable fields — not a hardcoded list.
    expect(source).toMatch(/for field in mutable_fields/)

    // PolicyGateUpdateRequest accepts exactly the mutable fields.
    const clsStart = source.indexOf('class PolicyGateUpdateRequest')
    expect(clsStart).toBeGreaterThan(-1)
    const clsRest = source.slice(clsStart)
    const nextClass = clsRest.indexOf('\nclass ', 1)
    const clsBody = nextClass === -1 ? clsRest : clsRest.slice(0, nextClass)
    const annotated = [...clsBody.matchAll(/^\s{4}(\w+)\s*:/gm)].map((m) => m[1])
    expect(annotated).toEqual(['action'])

    // Runtime half: the view's gate update body carries exactly those fields.
    evalsList = [evalItem({ id: 'eval-1' })]
    gateResponses['eval-1'] = ok({ id: 'g1', action: 'warn', version: 2 })
    const wrapper = mountView()
    await flush()
    await selectPipeline(wrapper)
    await flush()
    await openEditor(wrapper, 'eval-1')
    await wrapper.find('[data-test-id="policy-gate-action-block"]').setValue(true)
    await nextTick()
    await wrapper.find('[data-testid="eval-editor-save"]').trigger('click')
    await flush()

    const gatePuts = apiPUT.mock.calls.filter((c) => c[0] === GATE_URL)
    expect(gatePuts).toHaveLength(1)
    expect(Object.keys(gatePuts[0][1].body).sort()).toEqual([...mutableFields].sort())
  })

  // Criterion 21 — delete confirmation, block-gate warning wording, cancel.
  it('confirms gate deletion with the block warning and cancels cleanly (criterion 21)', async () => {
    evalsList = [evalItem({ id: 'eval-1' })]
    gateResponses['eval-1'] = ok({ id: 'g1', action: 'block', version: 1 })
    const wrapper = mountView()
    await flush()
    await selectPipeline(wrapper)
    await flush()
    await openEditor(wrapper, 'eval-1')

    const del = wrapper.find('[data-test-id="policy-gate-delete"]')
    expect(del.attributes('aria-label')).toBe('Delete policy gate')
    await del.trigger('click')
    await nextTick()
    expect(wrapper.text()).toContain('Are you sure you want to delete this policy gate?')
    expect(wrapper.text()).toContain('This gate is set to block runs. Deleting it will stop blocking.')

    // "No" cancels — no DELETE issued, back to the delete button.
    const noBtn = wrapper.findAll('button').find((b) => b.text() === 'No')
    expect(noBtn).toBeDefined()
    await noBtn!.trigger('click')
    await nextTick()
    expect(apiDELETE).not.toHaveBeenCalled()
    expect(wrapper.find('[data-test-id="policy-gate-confirm-delete"]').exists()).toBe(false)

    // Confirm issues DELETE and resets the section.
    await wrapper.find('[data-test-id="policy-gate-delete"]').trigger('click')
    await nextTick()
    await wrapper.find('[data-test-id="policy-gate-confirm-delete"]').trigger('click')
    await flush()
    expect(apiDELETE).toHaveBeenCalledWith(GATE_URL, { params: { path: { eval_id: 'eval-1' } } })
    expect(wrapper.find('[data-test-id="policy-gate-delete"]').exists()).toBe(false)
    expect(warnChecked(wrapper)).toBe(true)
  })

  it('shows no block warning in the delete confirmation for warn gates (criterion 21)', async () => {
    evalsList = [evalItem({ id: 'eval-1' })]
    gateResponses['eval-1'] = ok({ id: 'g1', action: 'warn', version: 1 })
    const wrapper = mountView()
    await flush()
    await selectPipeline(wrapper)
    await flush()
    await openEditor(wrapper, 'eval-1')

    await wrapper.find('[data-test-id="policy-gate-delete"]').trigger('click')
    await nextTick()
    expect(wrapper.text()).toContain('Are you sure you want to delete this policy gate?')
    expect(wrapper.text()).not.toContain('This gate is set to block runs. Deleting it will stop blocking.')
  })

  // Criterion 22 — audit trail. Source half: every gate mutation event is
  // preceded by an append_audit_event call in the route module. Runtime half:
  // the view actually issues the three mutation verbs those events record.
  it('records audit events for gate create/update/delete (criterion 22, source half)', () => {
    const source = readFileSync(BACKEND_EVALS_ROUTE, 'utf-8')
    expect(source).toContain('append_audit_event')
    for (const event of ['policy_gate.created', 'policy_gate.updated', 'policy_gate.deleted']) {
      const idx = source.indexOf(`event_type="${event}"`)
      expect(idx).toBeGreaterThan(-1)
      expect(source.slice(Math.max(0, idx - 600), idx)).toContain('append_audit_event')
    }
  })

  it('issues the gate create, update and delete mutations (criterion 22, runtime half)', async () => {
    // Create: saving a fresh eval with the default gate creates it (POST).
    responders.evalPost = ok({ id: 'new-eval' })
    responders.gatePost = ok({ id: 'g-new', action: 'warn', version: 1 })
    const wrapper = mountView()
    await flush()
    await selectPipeline(wrapper)
    await flush()
    await wrapper.find('[data-testid="eval-editor-name"]').setValue('Fresh Eval')
    await nextTick()
    await wrapper.find('[data-testid="eval-editor-save"]').trigger('click')
    await flush()
    expect(apiPOST).toHaveBeenCalledWith(GATE_URL, {
      params: { path: { eval_id: 'new-eval' } },
      body: { action: 'warn' },
    })

    // Update: editing the eval and changing the action issues PUT. The evals
    // list was loaded empty (create flow), so reload it after seeding.
    evalsList = [evalItem({ id: 'new-eval', name: 'Fresh Eval' })]
    gateResponses['new-eval'] = ok({ id: 'g-new', action: 'warn', version: 1 })
    await (wrapper.vm as unknown as { loadEvals: () => Promise<void> }).loadEvals()
    await flush()
    await openEditor(wrapper, 'new-eval')
    await wrapper.find('[data-test-id="policy-gate-action-block"]').setValue(true)
    await nextTick()
    await wrapper.find('[data-testid="eval-editor-save"]').trigger('click')
    await flush()
    expect(apiPUT).toHaveBeenCalledWith(GATE_URL, {
      params: { path: { eval_id: 'new-eval' } },
      body: { action: 'block' },
    })

    // Delete: the confirm flow issues DELETE. The successful save reset the
    // form and closed the editor, so re-open it first (the fetched gate marks
    // the section as existing → delete affordance present).
    await openEditor(wrapper, 'new-eval')
    await wrapper.find('[data-test-id="policy-gate-delete"]').trigger('click')
    await nextTick()
    await wrapper.find('[data-test-id="policy-gate-confirm-delete"]').trigger('click')
    await flush()
    expect(apiDELETE).toHaveBeenCalledWith(GATE_URL, { params: { path: { eval_id: 'new-eval' } } })
  })

  // Criterion 23 — two-phase save reporting on the update path.
  it('keeps the form and offers retry when the gate write fails after an eval update (criterion 23)', async () => {
    evalsList = [evalItem({ id: 'eval-1' })]
    gateResponses['eval-1'] = ok({ id: 'g1', action: 'warn', version: 2 })
    responders.gatePut = fail(500, 'gate write failed')
    const wrapper = mountView()
    await flush()
    await selectPipeline(wrapper)
    await flush()
    await openEditor(wrapper, 'eval-1')
    await wrapper.find('[data-test-id="policy-gate-action-block"]').setValue(true)
    await nextTick()
    await wrapper.find('[data-testid="eval-editor-save"]').trigger('click')
    await flush()

    // Gate half: explicit error state with retry; the form did NOT reset.
    const errBox = wrapper.find('[data-test-id="policy-gate-error"]')
    expect(errBox.exists()).toBe(true)
    expect(errBox.text()).toContain('Gate save failed. The eval was saved successfully.')
    expect(errBox.find('[data-test-id="policy-gate-retry"]').exists()).toBe(true)
    expect((wrapper.find('[data-testid="eval-editor-name"]').element as HTMLInputElement).value)
      .toBe('Existing Eval')
    expect(wrapper.find('[data-testid="eval-editor-cancel"]').exists()).toBe(true)
    expect(wrapper.text()).not.toContain('Eval created.')

    // Eval half: its own success indicator, separate from the gate error.
    expect(wrapper.text()).toContain('Eval updated.')

    // Retry 1 fails again — error state persists, retry still available.
    const gatePuts = apiPUT.mock.calls.filter((c) => c[0] === GATE_URL)
    expect(gatePuts).toHaveLength(1)
    expect(gatePuts[0][1].body).toEqual({ action: 'block' })
    await wrapper.find('[data-test-id="policy-gate-retry"]').trigger('click')
    await flush()
    expect(wrapper.find('[data-test-id="policy-gate-error"]').exists()).toBe(true)
    expect(apiPUT.mock.calls.filter((c) => c[0] === GATE_URL)).toHaveLength(2)

    // Retry 2 succeeds — error state exits.
    responders.gatePut = ok({ id: 'g1', action: 'block', version: 3 })
    await wrapper.find('[data-test-id="policy-gate-retry"]').trigger('click')
    await flush()
    expect(wrapper.find('[data-test-id="policy-gate-error"]').exists()).toBe(false)

    // Snapshot updated to the saved state: a subsequent save does not re-PUT
    // the gate. (The successful retry itself was one gate PUT — the claim is
    // that the next save adds no MORE gate writes.)
    const gatePutsBeforeFinalSave = apiPUT.mock.calls.filter((c) => c[0] === GATE_URL).length
    await wrapper.find('[data-testid="eval-editor-save"]').trigger('click')
    await flush()
    expect(apiPUT.mock.calls.filter((c) => c[0] === GATE_URL)).toHaveLength(gatePutsBeforeFinalSave)
    expect(apiPUT.mock.calls.filter((c) => c[0] === EVAL_PUT_URL)).toHaveLength(2)
  })

  // Criterion 23 — two-phase save reporting on the create path: the retry uses
  // the update endpoint with the eval id created in phase 1.
  it('retries a failed gate create via the update endpoint with the created eval id (criterion 23)', async () => {
    responders.evalPost = ok({ id: 'new-eval' })
    responders.gatePost = fail(500, 'gate write failed')
    const wrapper = mountView()
    await flush()
    await selectPipeline(wrapper)
    await flush()
    await wrapper.find('[data-testid="eval-editor-name"]').setValue('Fresh Eval')
    await nextTick()
    await wrapper.find('[data-testid="eval-editor-save"]').trigger('click')
    await flush()

    const errBox = wrapper.find('[data-test-id="policy-gate-error"]')
    expect(errBox.exists()).toBe(true)
    expect(errBox.text()).toContain('Gate save failed. The eval was saved successfully.')
    // The eval half succeeded and the form did not reset.
    expect(wrapper.text()).toContain('Eval created.')
    expect((wrapper.find('[data-testid="eval-editor-name"]').element as HTMLInputElement).value)
      .toBe('Fresh Eval')

    responders.gatePut = ok({ id: 'g-new', action: 'warn', version: 1 })
    await wrapper.find('[data-test-id="policy-gate-retry"]').trigger('click')
    await flush()
    expect(apiPUT).toHaveBeenCalledWith(GATE_URL, {
      params: { path: { eval_id: 'new-eval' } },
      body: { action: 'warn' },
    })
    expect(wrapper.find('[data-test-id="policy-gate-error"]').exists()).toBe(false)
  })

  // Criterion 24 — every lifecycle transition resets the gate state.
  it('returns the gate section to create-mode defaults on cancel (criterion 24)', async () => {
    evalsList = [evalItem({ id: 'eval-1' })]
    gateResponses['eval-1'] = ok({ id: 'g1', action: 'block', version: 1 })
    const wrapper = mountView()
    await flush()
    await selectPipeline(wrapper)
    await flush()
    await openEditor(wrapper, 'eval-1')
    expect(blockChecked(wrapper)).toBe(true)

    await wrapper.find('[data-testid="eval-editor-cancel"]').trigger('click')
    await nextTick()
    expect(warnChecked(wrapper)).toBe(true)
    expect(blockChecked(wrapper)).toBe(false)
    expect(wrapper.find('[data-test-id="policy-gate-delete"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="eval-editor-cancel"]').exists()).toBe(false)
  })

  it('re-populates gate state per eval without stale identity (criterion 24)', async () => {
    evalsList = [
      evalItem({ id: 'eval-1', name: 'Blocked Eval' }),
      evalItem({ id: 'eval-2', name: 'Gateless Eval' }),
    ]
    gateResponses['eval-1'] = ok({ id: 'g1', action: 'block', version: 1 })
    const wrapper = mountView()
    await flush()
    await selectPipeline(wrapper)
    await flush()

    await openEditor(wrapper, 'eval-1')
    expect(blockChecked(wrapper)).toBe(true)

    await openEditor(wrapper, 'eval-2')
    expect(warnChecked(wrapper)).toBe(true)
    expect(blockChecked(wrapper)).toBe(false)
    expect(wrapper.find('[data-test-id="policy-gate-delete"]').exists()).toBe(false)
  })
})
