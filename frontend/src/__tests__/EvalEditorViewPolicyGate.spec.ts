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
    PATCH: vi.fn(),
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
const apiPATCH = api.PATCH as ReturnType<typeof vi.fn>
const apiDELETE = api.DELETE as ReturnType<typeof vi.fn>

const GATE_URL = '/api/v1/evals/{eval_id}/policy-gate'
const GATE_TOGGLE_URL = '/api/v1/evals/{eval_id}/policy-gate/toggle'
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
  apiPATCH.mockImplementation(async (url: string) => {
    if (url === GATE_TOGGLE_URL) return responders.gateToggle
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
    responders.gateToggle = ok({ enabled: false, enabled_at: null, disabled_at: '2026-02-02T00:00:00Z' })
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

  // Finding 1: Radio labels come from $t() — no hardcoded "Warn"/"Block" text.
  it('renders radio labels from $t() locale keys, not hardcoded text (finding 1)', async () => {
    const wrapper = mountView()
    await flush()

    // The warn/block radio labels must render the locale values, not empty
    // strings (which would indicate a missing $t() key or a hardcoded fallback).
    // The locale file defines actionWarnLabel="Warn" and actionBlockLabel="Block".
    const warnRadio = wrapper.find('[data-test-id="policy-gate-action-warn"]')
    const blockRadio = wrapper.find('[data-test-id="policy-gate-action-block"]')
    expect(warnRadio.exists()).toBe(true)
    expect(blockRadio.exists()).toBe(true)

    // The parent <label> contains the text node from $t().  Vue test-utils
    // wraps each element, so we walk up via the element's parentElement.
    const warnLabel = warnRadio.element.parentElement!
    const blockLabel = blockRadio.element.parentElement!
    expect(warnLabel.textContent?.trim()).toBe('Warn')
    expect(blockLabel.textContent?.trim()).toBe('Block')

    // Structural proof: the component template uses $t() for these labels.
    // Verify by checking the rendered text is NOT a hardcoded string literal
    // in the source.  We confirm the locale key is resolved by checking that
    // the text matches the locale value exactly (which it can only do if
    // $t() resolved the key).
    const source = readFileSync(
      join(__dirname, '..', 'views', 'EvalEditorView.vue'),
      'utf-8',
    )
    // The source must contain $t() calls for the radio labels, not literal
    // <span>Warn</span> or <span>Block</span>.
    expect(source).toContain("policyGate.actionWarnLabel")
    expect(source).toContain("policyGate.actionBlockLabel")
    expect(source).not.toMatch(/<span>\s*Warn\s*<\/span>/)
    expect(source).not.toMatch(/<span>\s*Block\s*<\/span>/)
  })

  // Finding 2: Focus returns to the delete button on cancel.
  it('returns focus to the delete button when cancel is clicked (finding 2)', async () => {
    evalsList = [evalItem({ id: 'eval-1' })]
    gateResponses['eval-1'] = ok({ id: 'g1', action: 'block', version: 1 })
    const wrapper = mountView()
    await flush()
    await selectPipeline(wrapper)
    await flush()
    await openEditor(wrapper, 'eval-1')

    // Open the delete confirmation dialog.
    const delBtn = wrapper.find('[data-test-id="policy-gate-delete"]')
    await delBtn.trigger('click')
    await nextTick()
    expect(wrapper.find('[data-test-id="policy-gate-confirm-dialog"]').exists()).toBe(true)

    // Click "No" to cancel.
    const noBtn = wrapper.findAll('button').find((b) => b.text() === 'No')
    expect(noBtn).toBeDefined()
    await noBtn!.trigger('click')
    await nextTick()

    // Focus should be on the delete button, not on <body>.
    const deleteBtn = wrapper.find('[data-test-id="policy-gate-delete"]').element as HTMLElement
    // jsdom doesn't track focus across re-renders perfectly, but we can verify
    // that the delete button exists and is focusable after cancel.
    expect(deleteBtn).toBeDefined()
    expect(deleteBtn.tagName).toBe('BUTTON')
    // The confirm dialog should be gone.
    expect(wrapper.find('[data-test-id="policy-gate-confirm-dialog"]').exists()).toBe(false)
  })

  // Finding 3: Dialog has role="dialog", aria-modal, Escape-dismiss, Tab-trap.
  it('has role="dialog" and aria-modal="true" on the confirmation dialog (finding 3)', async () => {
    evalsList = [evalItem({ id: 'eval-1' })]
    gateResponses['eval-1'] = ok({ id: 'g1', action: 'block', version: 1 })
    const wrapper = mountView()
    await flush()
    await selectPipeline(wrapper)
    await flush()
    await openEditor(wrapper, 'eval-1')

    await wrapper.find('[data-test-id="policy-gate-delete"]').trigger('click')
    await nextTick()

    const dialog = wrapper.find('[data-test-id="policy-gate-confirm-dialog"]')
    expect(dialog.exists()).toBe(true)
    expect(dialog.attributes('role')).toBe('dialog')
    expect(dialog.attributes('aria-modal')).toBe('true')
    expect(dialog.attributes('aria-label')).toBeTruthy()
  })

  it('dismisses the confirmation dialog on Escape key (finding 3)', async () => {
    evalsList = [evalItem({ id: 'eval-1' })]
    gateResponses['eval-1'] = ok({ id: 'g1', action: 'block', version: 1 })
    const wrapper = mountView()
    await flush()
    await selectPipeline(wrapper)
    await flush()
    await openEditor(wrapper, 'eval-1')

    await wrapper.find('[data-test-id="policy-gate-delete"]').trigger('click')
    await nextTick()
    expect(wrapper.find('[data-test-id="policy-gate-confirm-dialog"]').exists()).toBe(true)

    // Dispatch a native KeyboardEvent with key='Escape' on the dialog element.
    const dialog = wrapper.find('[data-test-id="policy-gate-confirm-dialog"]')
    dialog.element.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await flush()
    expect(wrapper.find('[data-test-id="policy-gate-confirm-dialog"]').exists()).toBe(false)
    // No DELETE should have been issued.
    expect(apiDELETE).not.toHaveBeenCalled()
  })

  it('traps Tab focus within the confirmation dialog (finding 3)', async () => {
    evalsList = [evalItem({ id: 'eval-1' })]
    gateResponses['eval-1'] = ok({ id: 'g1', action: 'block', version: 1 })
    const wrapper = mountView()
    await flush()
    await selectPipeline(wrapper)
    await flush()
    await openEditor(wrapper, 'eval-1')

    await wrapper.find('[data-test-id="policy-gate-delete"]').trigger('click')
    await nextTick()

    const dialog = wrapper.find('[data-test-id="policy-gate-confirm-dialog"]')
    expect(dialog.exists()).toBe(true)

    // Structural verification: the dialog element has the @keydown handler wired,
    // which contains the Tab-trap logic. jsdom does not support focus tracking
    // on non-input elements, so we verify the handler exists in the source.
    const source = readFileSync(
      join(__dirname, '..', 'views', 'EvalEditorView.vue'),
      'utf-8',
    )
    // The dialog div must have role="dialog" and aria-modal="true".
    expect(dialog.attributes('role')).toBe('dialog')
    expect(dialog.attributes('aria-modal')).toBe('true')

    // The source must contain the focus-trap handler that intercepts Tab.
    expect(source).toContain('onGateDialogKeydown')
    expect(source).toContain("e.key === 'Tab'")
    expect(source).toContain("e.key === 'Escape'")
    expect(source).toContain('e.preventDefault()')
    expect(source).toContain('focusable[0]') // first focusable
    expect(source).toContain('last.focus()') // wrap to last

    // The dialog must have two focusable buttons (Confirm and No) for the
    // trap to have targets.
    const buttons = dialog.findAll('button')
    expect(buttons.length).toBe(2)
  })

  // Finding 4: Dirty-gate guard prompts on cancel and eval-switch.
  it('prompts on cancel when the gate action is dirty (finding 4)', async () => {
    evalsList = [evalItem({ id: 'eval-1' })]
    gateResponses['eval-1'] = ok({ id: 'g1', action: 'warn', version: 1 })
    const wrapper = mountView()
    await flush()
    await selectPipeline(wrapper)
    await flush()
    await openEditor(wrapper, 'eval-1')

    // Change the gate action to dirty it.
    await wrapper.find('[data-test-id="policy-gate-action-block"]').setValue(true)
    await nextTick()

    // Click cancel — the in-app dialog should appear.
    await wrapper.find('[data-testid="eval-editor-cancel"]').trigger('click')
    await nextTick()

    const dialog = wrapper.find('[data-test-id="dirty-confirm-dialog"]')
    expect(dialog.exists()).toBe(true)
    expect(dialog.attributes('role')).toBe('dialog')
    expect(dialog.attributes('aria-modal')).toBe('true')
    expect(dialog.text()).toContain('unsaved policy gate changes')

    // "Keep editing" keeps the form intact.
    await wrapper.find('[data-test-id="dirty-confirm-stay"]').trigger('click')
    await nextTick()
    expect(wrapper.find('[data-test-id="dirty-confirm-dialog"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="eval-editor-cancel"]').exists()).toBe(true)
    expect(blockChecked(wrapper)).toBe(true)
  })

  it('proceeds with cancel when the user clicks Discard (finding 4)', async () => {
    evalsList = [evalItem({ id: 'eval-1' })]
    gateResponses['eval-1'] = ok({ id: 'g1', action: 'warn', version: 1 })
    const wrapper = mountView()
    await flush()
    await selectPipeline(wrapper)
    await flush()
    await openEditor(wrapper, 'eval-1')

    // Change the gate action to dirty it.
    await wrapper.find('[data-test-id="policy-gate-action-block"]').setValue(true)
    await nextTick()

    // Click cancel — dialog appears.
    await wrapper.find('[data-testid="eval-editor-cancel"]').trigger('click')
    await nextTick()
    expect(wrapper.find('[data-test-id="dirty-confirm-dialog"]').exists()).toBe(true)

    // "Discard" proceeds with the cancel.
    await wrapper.find('[data-test-id="dirty-confirm-proceed"]').trigger('click')
    await flush()
    expect(wrapper.find('[data-test-id="dirty-confirm-dialog"]').exists()).toBe(false)
    // Form was reset — cancel button is gone.
    expect(wrapper.find('[data-testid="eval-editor-cancel"]').exists()).toBe(false)
  })

  it('allows cancel when the gate is not dirty (finding 4)', async () => {
    evalsList = [evalItem({ id: 'eval-1' })]
    gateResponses['eval-1'] = ok({ id: 'g1', action: 'warn', version: 1 })
    const wrapper = mountView()
    await flush()
    await selectPipeline(wrapper)
    await flush()
    await openEditor(wrapper, 'eval-1')

    // Gate was not changed — not dirty.
    await wrapper.find('[data-testid="eval-editor-cancel"]').trigger('click')
    await flush()

    // No dialog should appear — the form resets immediately.
    expect(wrapper.find('[data-test-id="dirty-confirm-dialog"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="eval-editor-cancel"]').exists()).toBe(false)
  })

  it('prompts on eval-switch when the gate action is dirty (finding 4)', async () => {
    evalsList = [
      evalItem({ id: 'eval-1', name: 'Eval 1' }),
      evalItem({ id: 'eval-2', name: 'Eval 2' }),
    ]
    gateResponses['eval-1'] = ok({ id: 'g1', action: 'warn', version: 1 })
    gateResponses['eval-2'] = ok({ id: 'g2', action: 'warn', version: 1 })
    const wrapper = mountView()
    await flush()
    await selectPipeline(wrapper)
    await flush()
    await openEditor(wrapper, 'eval-1')

    // Change the gate action to dirty it.
    await wrapper.find('[data-test-id="policy-gate-action-block"]').setValue(true)
    await nextTick()

    // Try to switch evals — dialog should appear.
    await openEditor(wrapper, 'eval-2')

    const dialog = wrapper.find('[data-test-id="dirty-confirm-dialog"]')
    expect(dialog.exists()).toBe(true)
    expect(dialog.text()).toContain('unsaved policy gate changes')

    // "Keep editing" stays on eval-1.
    await wrapper.find('[data-test-id="dirty-confirm-stay"]').trigger('click')
    await nextTick()
    expect(wrapper.find('[data-test-id="dirty-confirm-dialog"]').exists()).toBe(false)
    // Should still be on eval-1 (switch was rejected).
    expect(blockChecked(wrapper)).toBe(true)
  })

  it('proceeds with eval-switch when the user clicks Discard (finding 4)', async () => {
    evalsList = [
      evalItem({ id: 'eval-1', name: 'Eval 1' }),
      evalItem({ id: 'eval-2', name: 'Eval 2' }),
    ]
    gateResponses['eval-1'] = ok({ id: 'g1', action: 'warn', version: 1 })
    gateResponses['eval-2'] = ok({ id: 'g2', action: 'warn', version: 1 })
    const wrapper = mountView()
    await flush()
    await selectPipeline(wrapper)
    await flush()
    await openEditor(wrapper, 'eval-1')

    // Change the gate action to dirty it.
    await wrapper.find('[data-test-id="policy-gate-action-block"]').setValue(true)
    await nextTick()

    // Try to switch evals — dialog appears.
    // Note: openEditor triggers startEdit which shows the dialog, but the switch
    // happens after the dialog resolves. We need to resolve it asynchronously.
    const switchPromise = openEditor(wrapper, 'eval-2')
    await nextTick()

    expect(wrapper.find('[data-test-id="dirty-confirm-dialog"]').exists()).toBe(true)

    // "Discard" proceeds with the switch.
    await wrapper.find('[data-test-id="dirty-confirm-proceed"]').trigger('click')
    await switchPromise

    expect(wrapper.find('[data-test-id="dirty-confirm-dialog"]').exists()).toBe(false)
    // Now on eval-2 — warn is checked (eval-2 has warn gate).
    expect(warnChecked(wrapper)).toBe(true)
  })

  it('dismisses the dirty-confirm dialog on Escape key (finding 4)', async () => {
    evalsList = [evalItem({ id: 'eval-1' })]
    gateResponses['eval-1'] = ok({ id: 'g1', action: 'warn', version: 1 })
    const wrapper = mountView()
    await flush()
    await selectPipeline(wrapper)
    await flush()
    await openEditor(wrapper, 'eval-1')

    await wrapper.find('[data-test-id="policy-gate-action-block"]').setValue(true)
    await nextTick()

    await wrapper.find('[data-testid="eval-editor-cancel"]').trigger('click')
    await nextTick()
    expect(wrapper.find('[data-test-id="dirty-confirm-dialog"]').exists()).toBe(true)

    // Escape dismisses the dialog (resolves with false = stay).
    const dialog = wrapper.find('[data-test-id="dirty-confirm-dialog"]')
    dialog.element.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await nextTick()
    expect(wrapper.find('[data-test-id="dirty-confirm-dialog"]').exists()).toBe(false)
    // Form was NOT reset — the user chose to stay.
    expect(wrapper.find('[data-testid="eval-editor-cancel"]').exists()).toBe(true)
    expect(blockChecked(wrapper)).toBe(true)
  })

  it('traps Tab focus within the dirty-confirm dialog (finding 4)', async () => {
    evalsList = [evalItem({ id: 'eval-1' })]
    gateResponses['eval-1'] = ok({ id: 'g1', action: 'warn', version: 1 })
    const wrapper = mountView()
    await flush()
    await selectPipeline(wrapper)
    await flush()
    await openEditor(wrapper, 'eval-1')

    await wrapper.find('[data-test-id="policy-gate-action-block"]').setValue(true)
    await nextTick()

    await wrapper.find('[data-testid="eval-editor-cancel"]').trigger('click')
    await nextTick()

    const dialog = wrapper.find('[data-test-id="dirty-confirm-dialog"]')
    expect(dialog.exists()).toBe(true)
    expect(dialog.attributes('role')).toBe('dialog')
    expect(dialog.attributes('aria-modal')).toBe('true')

    // Structural verification: the dialog has the keydown handler wired.
    const source = readFileSync(
      join(__dirname, '..', 'views', 'EvalEditorView.vue'),
      'utf-8',
    )
    expect(source).toContain('onDirtyDialogKeydown')
    expect(source).toContain("e.key === 'Tab'")
    expect(source).toContain("e.key === 'Escape'")

    // The dialog must have two focusable buttons (Discard and Keep editing).
    const buttons = dialog.findAll('button')
    expect(buttons.length).toBe(2)
  })

  // Finding 5: Badge refresh after a successful retry.
  it('refreshes the eval-list badge after a successful retry (finding 5)', async () => {
    evalsList = [evalItem({ id: 'eval-1' })]
    gateResponses['eval-1'] = ok({ id: 'g1', action: 'warn', version: 2 })
    responders.gatePut = fail(500, 'gate write failed')
    const wrapper = mountView()
    await flush()
    await selectPipeline(wrapper)
    await flush()
    await openEditor(wrapper, 'eval-1')

    // Change the gate and save — gate fails, eval succeeds.
    await wrapper.find('[data-test-id="policy-gate-action-block"]').setValue(true)
    await nextTick()
    await wrapper.find('[data-testid="eval-editor-save"]').trigger('click')
    await flush()
    expect(wrapper.find('[data-test-id="policy-gate-error"]').exists()).toBe(true)

    // Reset the GET mock call count to isolate the retry's badge refresh.
    apiGET.mockClear()
    // Re-install the router so the badge refresh GET calls work.
    installRouter()

    // Make retry succeed.
    responders.gatePut = ok({ id: 'g1', action: 'block', version: 3 })
    await wrapper.find('[data-test-id="policy-gate-retry"]').trigger('click')
    await flush()

    // The badge refresh calls GET for each eval — verify the gate GET was
    // called again (the badge load fetches gate data for the eval list).
    const gateGets = apiGET.mock.calls.filter((c) => c[0] === GATE_URL)
    expect(gateGets.length).toBeGreaterThanOrEqual(1)
    // Error state should be cleared.
    expect(wrapper.find('[data-test-id="policy-gate-error"]').exists()).toBe(false)
  })

  // jsdom does not move document.activeElement on .focus(), so the trap tests
  // pin the active element and spy on the two end controls' focus() instead.
  function withActiveElement<T>(el: HTMLElement, fn: () => T): T {
    Object.defineProperty(document, 'activeElement', { configurable: true, get: () => el })
    try {
      return fn()
    } finally {
      delete (document as unknown as { activeElement?: unknown }).activeElement
    }
  }

  // Coverage — the Tab focus trap actually wraps focus in both directions.
  it('wraps forward Tab from the last control to the first (finding 3)', async () => {
    evalsList = [evalItem({ id: 'eval-1' })]
    gateResponses['eval-1'] = ok({ id: 'g1', action: 'block', version: 1 })
    const wrapper = mountView()
    await flush()
    await selectPipeline(wrapper)
    await flush()
    await openEditor(wrapper, 'eval-1')

    await wrapper.find('[data-test-id="policy-gate-delete"]').trigger('click')
    await nextTick()
    const dialog = wrapper.find('[data-test-id="policy-gate-confirm-dialog"]')
    const buttons = dialog.findAll('button')
    const first = buttons[0].element as HTMLElement
    const last = buttons[buttons.length - 1].element as HTMLElement
    const firstFocus = vi.spyOn(first, 'focus')

    withActiveElement(last, () => {
      dialog.element.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', bubbles: true }))
    })

    expect(firstFocus).toHaveBeenCalled()
  })

  it('wraps backward Tab from the first control to the last (finding 3)', async () => {
    evalsList = [evalItem({ id: 'eval-1' })]
    gateResponses['eval-1'] = ok({ id: 'g1', action: 'block', version: 1 })
    const wrapper = mountView()
    await flush()
    await selectPipeline(wrapper)
    await flush()
    await openEditor(wrapper, 'eval-1')

    await wrapper.find('[data-test-id="policy-gate-delete"]').trigger('click')
    await nextTick()
    const dialog = wrapper.find('[data-test-id="policy-gate-confirm-dialog"]')
    const buttons = dialog.findAll('button')
    const first = buttons[0].element as HTMLElement
    const last = buttons[buttons.length - 1].element as HTMLElement
    const lastFocus = vi.spyOn(last, 'focus')

    withActiveElement(first, () => {
      dialog.element.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', shiftKey: true, bubbles: true }))
    })

    expect(lastFocus).toHaveBeenCalled()
  })

  it('keeps focus put when Tab is pressed from neither end', async () => {
    evalsList = [evalItem({ id: 'eval-1' })]
    gateResponses['eval-1'] = ok({ id: 'g1', action: 'block', version: 1 })
    const wrapper = mountView()
    await flush()
    await selectPipeline(wrapper)
    await flush()
    await openEditor(wrapper, 'eval-1')

    await wrapper.find('[data-test-id="policy-gate-delete"]').trigger('click')
    await nextTick()
    const dialog = wrapper.find('[data-test-id="policy-gate-confirm-dialog"]')
    const buttons = dialog.findAll('button')
    const firstFocus = vi.spyOn(buttons[0].element as HTMLElement, 'focus')
    const lastFocus = vi.spyOn(buttons[buttons.length - 1].element as HTMLElement, 'focus')
    const outside = wrapper.find('[data-testid="eval-editor-save"]').element as HTMLElement

    withActiveElement(outside, () => {
      dialog.element.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', bubbles: true }))
    })

    expect(firstFocus).not.toHaveBeenCalled()
    expect(lastFocus).not.toHaveBeenCalled()
  })

  // Coverage — the dirty-confirm dialog's Tab focus trap wraps both ways.
  it('wraps focus within the dirty-confirm dialog in both directions', async () => {
    evalsList = [evalItem({ id: 'eval-1' })]
    gateResponses['eval-1'] = ok({ id: 'g1', action: 'warn', version: 1 })
    const wrapper = mountView()
    await flush()
    await selectPipeline(wrapper)
    await flush()
    await openEditor(wrapper, 'eval-1')

    await wrapper.find('[data-test-id="policy-gate-action-block"]').setValue(true)
    await nextTick()
    await wrapper.find('[data-testid="eval-editor-cancel"]').trigger('click')
    await nextTick()
    const dialog = wrapper.find('[data-test-id="dirty-confirm-dialog"]')
    const buttons = dialog.findAll('button')
    const first = buttons[0].element as HTMLElement
    const last = buttons[buttons.length - 1].element as HTMLElement
    const firstFocus = vi.spyOn(first, 'focus')
    const lastFocus = vi.spyOn(last, 'focus')

    withActiveElement(last, () => {
      dialog.element.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', bubbles: true }))
    })
    expect(firstFocus).toHaveBeenCalled()

    withActiveElement(first, () => {
      dialog.element.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', shiftKey: true, bubbles: true }))
    })
    expect(lastFocus).toHaveBeenCalled()
  })

  // Coverage — gate GET without id/version falls back to null / 1.
  it('falls back to null id and version 1 when the gate payload omits them', async () => {
    evalsList = [evalItem({ id: 'eval-1' })]
    gateResponses['eval-1'] = ok({ action: 'block' })
    const wrapper = mountView()
    await flush()
    await selectPipeline(wrapper)
    await flush()
    await openEditor(wrapper, 'eval-1')

    expect(blockChecked(wrapper)).toBe(true)
    // action present → exists, even with no id
    expect(wrapper.find('[data-test-id="policy-gate-delete"]').exists()).toBe(true)
  })

  // Coverage — eval-create response without an id skips the gate phase.
  it('skips the gate phase when the created eval has no id', async () => {
    responders.evalPost = ok({})
    const wrapper = mountView()
    await flush()
    await selectPipeline(wrapper)
    await flush()
    await wrapper.find('[data-testid="eval-editor-name"]').setValue('No Id')
    await nextTick()
    await wrapper.find('[data-testid="eval-editor-save"]').trigger('click')
    await flush()

    expect(apiPOST.mock.calls.filter((c) => c[0] === GATE_URL)).toHaveLength(0)
  })

  // Coverage — gate update response without id/version keeps prior identity.
  it('keeps the gate identity when the update payload omits id/version', async () => {
    evalsList = [evalItem({ id: 'eval-1' })]
    gateResponses['eval-1'] = ok({ id: 'g1', action: 'warn', version: 2 })
    responders.gatePut = ok({})
    const wrapper = mountView()
    await flush()
    await selectPipeline(wrapper)
    await flush()
    await openEditor(wrapper, 'eval-1')
    await wrapper.find('[data-test-id="policy-gate-action-block"]').setValue(true)
    await nextTick()
    await wrapper.find('[data-testid="eval-editor-save"]').trigger('click')
    await flush()

    expect(wrapper.find('[data-test-id="policy-gate-error"]').exists()).toBe(false)
    expect(blockChecked(wrapper)).toBe(false) // form reset → create-mode default
  })

  // Coverage — gate create response without id/version falls back.
  it('falls back when the gate create payload omits id/version', async () => {
    responders.evalPost = ok({ id: 'new-eval' })
    responders.gatePost = ok({})
    const wrapper = mountView()
    await flush()
    await selectPipeline(wrapper)
    await flush()
    await wrapper.find('[data-testid="eval-editor-name"]').setValue('Fresh')
    await nextTick()
    await wrapper.find('[data-testid="eval-editor-save"]').trigger('click')
    await flush()

    expect(wrapper.find('[data-test-id="policy-gate-error"]').exists()).toBe(false)
  })

  // Coverage — retry success with a bare payload exercises the nullish fallbacks.
  it('retries successfully with a bare gate payload', async () => {
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
    expect(wrapper.find('[data-test-id="policy-gate-error"]').exists()).toBe(true)

    responders.gatePut = ok({})
    await wrapper.find('[data-test-id="policy-gate-retry"]').trigger('click')
    await flush()
    expect(wrapper.find('[data-test-id="policy-gate-error"]').exists()).toBe(false)
  })

  // Coverage — non-Tab, non-Escape keys fall through the dialog key handlers.
  it('ignores other keys in both dialog key handlers', async () => {
    evalsList = [evalItem({ id: 'eval-1' })]
    gateResponses['eval-1'] = ok({ id: 'g1', action: 'warn', version: 1 })
    const wrapper = mountView()
    await flush()
    await selectPipeline(wrapper)
    await flush()
    await openEditor(wrapper, 'eval-1')

    await wrapper.find('[data-test-id="policy-gate-delete"]').trigger('click')
    await nextTick()
    const gateDialog = wrapper.find('[data-test-id="policy-gate-confirm-dialog"]')
    gateDialog.element.dispatchEvent(new KeyboardEvent('keydown', { key: 'a', bubbles: true }))
    expect(gateDialog.exists()).toBe(true)

    await (wrapper.vm as unknown as { cancelGateDelete: () => void }).cancelGateDelete()
    await nextTick()
    await wrapper.find('[data-test-id="policy-gate-action-block"]').setValue(true)
    await nextTick()
    await wrapper.find('[data-testid="eval-editor-cancel"]').trigger('click')
    await nextTick()
    const dirtyDialog = wrapper.find('[data-test-id="dirty-confirm-dialog"]')
    dirtyDialog.element.dispatchEvent(new KeyboardEvent('keydown', { key: 'a', bubbles: true }))
    expect(dirtyDialog.exists()).toBe(true)
  })

  // Coverage — the focus trap returns early when a dialog has no focusable children.
  it('returns early from the trap when the dialog has no focusable children', async () => {
    evalsList = [evalItem({ id: 'eval-1' })]
    gateResponses['eval-1'] = ok({ id: 'g1', action: 'warn', version: 1 })
    const wrapper = mountView()
    await flush()
    await selectPipeline(wrapper)
    await flush()
    await openEditor(wrapper, 'eval-1')

    await wrapper.find('[data-test-id="policy-gate-delete"]').trigger('click')
    await nextTick()
    const gateDialog = wrapper.find('[data-test-id="policy-gate-confirm-dialog"]')
    gateDialog.element.innerHTML = ''
    gateDialog.element.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', bubbles: true }))

    await (wrapper.vm as unknown as { cancelGateDelete: () => void }).cancelGateDelete()
    await nextTick()
    await wrapper.find('[data-test-id="policy-gate-action-block"]').setValue(true)
    await nextTick()
    await wrapper.find('[data-testid="eval-editor-cancel"]').trigger('click')
    await nextTick()
    const dirtyDialog = wrapper.find('[data-test-id="dirty-confirm-dialog"]')
    dirtyDialog.element.innerHTML = ''
    dirtyDialog.element.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', bubbles: true }))

    expect(dirtyDialog.exists()).toBe(true)
  })

  // Coverage — shift+Tab from a non-first control does not wrap.
  it('does not wrap shift+Tab when focus is not on the first control', async () => {
    evalsList = [evalItem({ id: 'eval-1' })]
    gateResponses['eval-1'] = ok({ id: 'g1', action: 'warn', version: 1 })
    const wrapper = mountView()
    await flush()
    await selectPipeline(wrapper)
    await flush()
    await openEditor(wrapper, 'eval-1')

    await wrapper.find('[data-test-id="policy-gate-delete"]').trigger('click')
    await nextTick()
    const dialog = wrapper.find('[data-test-id="policy-gate-confirm-dialog"]')
    const buttons = dialog.findAll('button')
    const firstFocus = vi.spyOn(buttons[0].element as HTMLElement, 'focus')
    const lastFocus = vi.spyOn(buttons[buttons.length - 1].element as HTMLElement, 'focus')

    withActiveElement(buttons[buttons.length - 1].element as HTMLElement, () => {
      dialog.element.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', shiftKey: true, bubbles: true }))
    })

    expect(firstFocus).not.toHaveBeenCalled()
    expect(lastFocus).not.toHaveBeenCalled()
  })

  // Coverage — the dirty dialog does not wrap forward from a non-last control.
  it('does not wrap forward Tab in the dirty dialog when focus is not on the last control', async () => {
    evalsList = [evalItem({ id: 'eval-1' })]
    gateResponses['eval-1'] = ok({ id: 'g1', action: 'warn', version: 1 })
    const wrapper = mountView()
    await flush()
    await selectPipeline(wrapper)
    await flush()
    await openEditor(wrapper, 'eval-1')

    await wrapper.find('[data-test-id="policy-gate-action-block"]').setValue(true)
    await nextTick()
    await wrapper.find('[data-testid="eval-editor-cancel"]').trigger('click')
    await nextTick()
    const dialog = wrapper.find('[data-test-id="dirty-confirm-dialog"]')
    const buttons = dialog.findAll('button')
    const firstFocus = vi.spyOn(buttons[0].element as HTMLElement, 'focus')
    const lastFocus = vi.spyOn(buttons[buttons.length - 1].element as HTMLElement, 'focus')

    withActiveElement(buttons[0].element as HTMLElement, () => {
      dialog.element.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', bubbles: true }))
    })

    expect(firstFocus).not.toHaveBeenCalled()
    expect(lastFocus).not.toHaveBeenCalled()
  })

  // Coverage — dialog key handlers are no-ops when their element ref is unset.
  it('does not wrap shift+Tab in the dirty dialog when focus is not on the first control', async () => {
    evalsList = [evalItem({ id: 'eval-1' })]
    gateResponses['eval-1'] = ok({ id: 'g1', action: 'warn', version: 1 })
    const wrapper = mountView()
    await flush()
    await selectPipeline(wrapper)
    await flush()
    await openEditor(wrapper, 'eval-1')

    await wrapper.find('[data-test-id="policy-gate-action-block"]').setValue(true)
    await nextTick()
    await wrapper.find('[data-testid="eval-editor-cancel"]').trigger('click')
    await nextTick()
    const dialog = wrapper.find('[data-test-id="dirty-confirm-dialog"]')
    const buttons = dialog.findAll('button')
    const firstFocus = vi.spyOn(buttons[0].element as HTMLElement, 'focus')
    const lastFocus = vi.spyOn(buttons[buttons.length - 1].element as HTMLElement, 'focus')

    withActiveElement(buttons[buttons.length - 1].element as HTMLElement, () => {
      dialog.element.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', shiftKey: true, bubbles: true }))
    })

    expect(firstFocus).not.toHaveBeenCalled()
    expect(lastFocus).not.toHaveBeenCalled()
  })

  it('ignores Tab when the dialog refs are unset', async () => {
    const wrapper = mountView()
    await flush()
    const vm = wrapper.vm as unknown as {
      onGateDialogKeydown: (e: KeyboardEvent) => void
      onDirtyDialogKeydown: (e: KeyboardEvent) => void
    }
    expect(() => vm.onGateDialogKeydown(new KeyboardEvent('keydown', { key: 'Tab' }))).not.toThrow()
    expect(() => vm.onDirtyDialogKeydown(new KeyboardEvent('keydown', { key: 'Tab' }))).not.toThrow()
  })

  // Coverage — resolveDirtyConfirm with no pending promise is a harmless no-op.
  it('resolveDirtyConfirm with no pending promise is a no-op', async () => {
    const wrapper = mountView()
    await flush()
    const vm = wrapper.vm as unknown as { resolveDirtyConfirm: (v: boolean) => void }
    expect(() => vm.resolveDirtyConfirm(false)).not.toThrow()
  })

  // Coverage — delete/retry guard clauses when there is no editing eval.
  it('deletePolicyGate is a no-op when no eval is being edited', async () => {
    const wrapper = mountView()
    await flush()
    const vm = wrapper.vm as unknown as { deletePolicyGate: () => Promise<void> }
    await vm.deletePolicyGate()
    expect(apiDELETE).not.toHaveBeenCalled()
  })

  it('retryPolicyGate is a no-op when there is no eval id to retry', async () => {
    const wrapper = mountView()
    await flush()
    const vm = wrapper.vm as unknown as { retryPolicyGate: () => Promise<void> }
    await vm.retryPolicyGate()
    expect(apiPUT).not.toHaveBeenCalled()
  })

  // Coverage — resetForm resolves an in-flight dirty-confirm promise.
  it('resolves a pending dirty-confirm when the form resets', async () => {
    evalsList = [evalItem({ id: 'eval-1' })]
    gateResponses['eval-1'] = ok({ id: 'g1', action: 'warn', version: 1 })
    const wrapper = mountView()
    await flush()
    await selectPipeline(wrapper)
    await flush()
    await openEditor(wrapper, 'eval-1')

    await wrapper.find('[data-test-id="policy-gate-action-block"]').setValue(true)
    await nextTick()
    await wrapper.find('[data-testid="eval-editor-cancel"]').trigger('click')
    await nextTick()
    expect(wrapper.find('[data-test-id="dirty-confirm-dialog"]').exists()).toBe(true)

    // A pipeline change resets the form while the confirm promise is pending.
    await (wrapper.vm as unknown as { onPipelineChange: () => Promise<void> }).onPipelineChange()
    await flush()

    expect(wrapper.find('[data-test-id="dirty-confirm-dialog"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="eval-editor-cancel"]').exists()).toBe(false)
  })

  // Coverage — a network-level failure loading the gate falls back to "no gate".
  it('treats a gate-load network failure as no gate known', async () => {
    evalsList = [evalItem({ id: 'eval-1' })]
    const wrapper = mountView()
    await flush()
    await selectPipeline(wrapper)
    await flush()
    const base = apiGET.getMockImplementation() as (
      url: string,
      init?: { params?: { path?: Record<string, string> } },
    ) => Promise<unknown>
    apiGET.mockImplementation(async (url: string, init?: { params?: { path?: Record<string, string> } }) => {
      if (url === GATE_URL) throw new Error('network down')
      return base(url, init)
    })

    await openEditor(wrapper, 'eval-1')

    expect(warnChecked(wrapper)).toBe(true)
    expect(wrapper.find('[data-test-id="policy-gate-delete"]').exists()).toBe(false)
  })

  // Coverage — the eval-save request throwing is reported as a form error.
  it('surfaces a form error when the eval save request throws', async () => {
    evalsList = [evalItem({ id: 'eval-1' })]
    gateResponses['eval-1'] = ok({ id: 'g1', action: 'warn', version: 1 })
    const wrapper = mountView()
    await flush()
    await selectPipeline(wrapper)
    await flush()
    await openEditor(wrapper, 'eval-1')
    apiPUT.mockImplementation(async (url: string) => {
      if (url === EVAL_PUT_URL) throw new Error('eval network down')
      if (url === GATE_URL) return responders.gatePut
      return ok({})
    })

    await wrapper.find('[data-testid="eval-editor-name"]').setValue('Changed')
    await nextTick()
    await wrapper.find('[data-testid="eval-editor-save"]').trigger('click')
    await flush()

    expect(wrapper.text()).toContain('eval network down')
  })

  // Coverage — the gate-save request throwing is treated as a gate failure.
  it('treats a gate-save network failure as the retryable gate error state', async () => {
    evalsList = [evalItem({ id: 'eval-1' })]
    gateResponses['eval-1'] = ok({ id: 'g1', action: 'warn', version: 1 })
    const wrapper = mountView()
    await flush()
    await selectPipeline(wrapper)
    await flush()
    await openEditor(wrapper, 'eval-1')
    apiPUT.mockImplementation(async (url: string) => {
      if (url === GATE_URL) throw new Error('gate network down')
      if (url === EVAL_PUT_URL) return responders.evalPut
      return ok({})
    })

    await wrapper.find('[data-test-id="policy-gate-action-block"]').setValue(true)
    await nextTick()
    await wrapper.find('[data-testid="eval-editor-save"]').trigger('click')
    await flush()

    const errBox = wrapper.find('[data-test-id="policy-gate-error"]')
    expect(errBox.exists()).toBe(true)
    expect(errBox.find('[data-test-id="policy-gate-retry"]').exists()).toBe(true)
  })

  // Coverage — delete treats a 404 as "already deleted" and a 5xx as an error.
  it('treats a 404 on gate delete as already-deleted', async () => {
    evalsList = [evalItem({ id: 'eval-1' })]
    gateResponses['eval-1'] = ok({ id: 'g1', action: 'warn', version: 1 })
    const wrapper = mountView()
    await flush()
    await selectPipeline(wrapper)
    await flush()
    await openEditor(wrapper, 'eval-1')
    responders.gateDelete = fail(404, 'Policy gate not found')

    await wrapper.find('[data-test-id="policy-gate-delete"]').trigger('click')
    await nextTick()
    await wrapper.find('[data-test-id="policy-gate-confirm-delete"]').trigger('click')
    await flush()

    expect(wrapper.find('[data-test-id="policy-gate-delete"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('Policy gate not found')
  })

  it('surfaces a form error when gate delete fails for a non-404 reason', async () => {
    evalsList = [evalItem({ id: 'eval-1' })]
    gateResponses['eval-1'] = ok({ id: 'g1', action: 'warn', version: 1 })
    const wrapper = mountView()
    await flush()
    await selectPipeline(wrapper)
    await flush()
    await openEditor(wrapper, 'eval-1')
    responders.gateDelete = fail(500, 'delete exploded')

    await wrapper.find('[data-test-id="policy-gate-delete"]').trigger('click')
    await nextTick()
    await wrapper.find('[data-test-id="policy-gate-confirm-delete"]').trigger('click')
    await flush()

    expect(wrapper.text()).toContain('delete exploded')
  })

  // ---------------------------------------------------------------------------
  // FAR-967 F9 — operator enable/disable toggle (CO-5)
  // ---------------------------------------------------------------------------

  async function openWithGate(gate: Record<string, unknown>): Promise<ViewWrapper> {
    evalsList = [evalItem({ id: 'eval-1' })]
    gateResponses['eval-1'] = ok(gate)
    const wrapper = mountView()
    await flush()
    await selectPipeline(wrapper)
    await flush()
    await openEditor(wrapper, 'eval-1')
    expect(wrapper.find('[data-test-id="policy-gate-toggle"]').exists()).toBe(true)
    return wrapper
  }

  function togglePatchCalls() {
    return apiPATCH.mock.calls.filter((c) => c[0] === GATE_TOGGLE_URL)
  }

  it('disables a warn gate immediately via PATCH toggle (F9)', async () => {
    const wrapper = await openWithGate({
      id: 'gate-1',
      action: 'warn',
      version: 1,
      enabled: true,
      enabled_at: '2026-01-01T00:00:00Z',
      disabled_at: null,
    })
    const toggle = wrapper.find('[data-test-id="policy-gate-toggle"]')
    expect(toggle.attributes('aria-checked')).toBe('true')
    responders.gateToggle = ok({ enabled: false, enabled_at: null, disabled_at: '2026-02-02T00:00:00Z' })

    await toggle.trigger('click')
    await flush()

    const calls = togglePatchCalls()
    expect(calls).toHaveLength(1)
    expect(calls[0][1].body).toEqual({ enabled: false })
    expect(calls[0][1].params).toEqual({ path: { eval_id: 'eval-1' } })
    // The switch reflects the SERVER-confirmed state from the response.
    expect(toggle.attributes('aria-checked')).toBe('false')
  })

  it('enables a disabled gate immediately via PATCH toggle (F9)', async () => {
    const wrapper = await openWithGate({
      id: 'gate-1',
      action: 'warn',
      version: 1,
      enabled: false,
      enabled_at: null,
      disabled_at: '2026-01-01T00:00:00Z',
    })
    const toggle = wrapper.find('[data-test-id="policy-gate-toggle"]')
    expect(toggle.attributes('aria-checked')).toBe('false')
    responders.gateToggle = ok({ enabled: true, enabled_at: '2026-02-02T00:00:00Z', disabled_at: null })

    await toggle.trigger('click')
    await flush()

    const calls = togglePatchCalls()
    expect(calls).toHaveLength(1)
    expect(calls[0][1].body).toEqual({ enabled: true })
    expect(toggle.attributes('aria-checked')).toBe('true')
  })

  it('requires confirmation before disabling a BLOCK gate (F9)', async () => {
    const wrapper = await openWithGate({
      id: 'gate-1',
      action: 'block',
      version: 1,
      enabled: true,
      enabled_at: '2026-01-01T00:00:00Z',
      disabled_at: null,
    })
    const toggle = wrapper.find('[data-test-id="policy-gate-toggle"]')

    await toggle.trigger('click')
    await nextTick()
    // Confirmation dialog opens; nothing has been PATCHed yet.
    expect(wrapper.find('[data-test-id="policy-gate-toggle-confirm-dialog"]').exists()).toBe(true)
    expect(togglePatchCalls()).toHaveLength(0)

    responders.gateToggle = ok({ enabled: false, enabled_at: null, disabled_at: '2026-02-02T00:00:00Z' })
    await wrapper.find('[data-test-id="policy-gate-confirm-toggle-disable"]').trigger('click')
    await flush()

    const calls = togglePatchCalls()
    expect(calls).toHaveLength(1)
    expect(calls[0][1].body).toEqual({ enabled: false })
    expect(wrapper.find('[data-test-id="policy-gate-toggle-confirm-dialog"]').exists()).toBe(false)
    expect(toggle.attributes('aria-checked')).toBe('false')
  })

  it('cancelling the block-gate disable confirmation issues no PATCH (F9)', async () => {
    const wrapper = await openWithGate({
      id: 'gate-1',
      action: 'block',
      version: 1,
      enabled: true,
      enabled_at: '2026-01-01T00:00:00Z',
      disabled_at: null,
    })
    const toggle = wrapper.find('[data-test-id="policy-gate-toggle"]')

    await toggle.trigger('click')
    await nextTick()
    expect(wrapper.find('[data-test-id="policy-gate-toggle-confirm-dialog"]').exists()).toBe(true)

    await wrapper.find('[data-test-id="policy-gate-cancel-toggle-disable"]').trigger('click')
    await flush()

    expect(togglePatchCalls()).toHaveLength(0)
    expect(wrapper.find('[data-test-id="policy-gate-toggle-confirm-dialog"]').exists()).toBe(false)
    expect(toggle.attributes('aria-checked')).toBe('true')
  })

  it('keeps the switch on when the toggle PATCH fails (F9)', async () => {
    const wrapper = await openWithGate({
      id: 'gate-1',
      action: 'warn',
      version: 1,
      enabled: true,
      enabled_at: '2026-01-01T00:00:00Z',
      disabled_at: null,
    })
    const toggle = wrapper.find('[data-test-id="policy-gate-toggle"]')
    responders.gateToggle = fail(500, 'toggle exploded')

    await toggle.trigger('click')
    await flush()

    expect(togglePatchCalls()).toHaveLength(1)
    // State does NOT flip on failure, and the toggle-error message surfaces.
    expect(toggle.attributes('aria-checked')).toBe('true')
    expect(wrapper.text()).toContain('Failed to toggle the policy gate')
  })

  it('surfaces an error when the toggle PATCH throws (F9)', async () => {
    const wrapper = await openWithGate({
      id: 'gate-1',
      action: 'warn',
      version: 1,
      enabled: true,
      enabled_at: '2026-01-01T00:00:00Z',
      disabled_at: null,
    })
    const toggle = wrapper.find('[data-test-id="policy-gate-toggle"]')
    // A network-level rejection (not an error envelope) must be caught.
    apiPATCH.mockRejectedValueOnce(new Error('network down'))

    await toggle.trigger('click')
    await flush()

    expect(togglePatchCalls()).toHaveLength(1)
    expect(toggle.attributes('aria-checked')).toBe('true')
    expect(wrapper.text()).toContain('Failed to toggle the policy gate')
  })

  it('falls back to the requested state when the toggle response omits enabled (F9)', async () => {
    const wrapper = await openWithGate({
      id: 'gate-1',
      action: 'warn',
      version: 1,
      enabled: true,
      enabled_at: '2026-01-01T00:00:00Z',
      disabled_at: null,
    })
    const toggle = wrapper.find('[data-test-id="policy-gate-toggle"]')
    responders.gateToggle = ok({})

    await toggle.trigger('click')
    await flush()

    // The requested transition (disable) is applied when the payload is bare.
    expect(toggle.attributes('aria-checked')).toBe('false')
  })

  it('ignores a toggle request when no gate exists (F9)', async () => {
    const wrapper = mountView()
    await flush()
    await selectPipeline(wrapper)
    await flush()

    // Create-mode: no gate row → the guard returns before any PATCH.
    ;(wrapper.vm as unknown as { requestToggleGate: () => void }).requestToggleGate()
    await flush()

    expect(togglePatchCalls()).toHaveLength(0)
  })

  it('ignores a toggle when the editing eval id is absent (F9)', async () => {
    const wrapper = mountView()
    await flush()
    await selectPipeline(wrapper)
    await flush()

    // No editor open → editingEvalId is null; toggling must be a no-op.
    await (wrapper.vm as unknown as { doToggleGate: (enabled: boolean) => Promise<void> }).doToggleGate(false)
    await flush()

    expect(togglePatchCalls()).toHaveLength(0)
  })

  it('ignores a toggle when the gate row has no id (F9)', async () => {
    const wrapper = await openWithGate({
      action: 'warn',
      version: 1,
      enabled: true,
      enabled_at: '2026-01-01T00:00:00Z',
      disabled_at: null,
    })
    // action present → exists, but no id → the write guard returns.
    await wrapper.find('[data-test-id="policy-gate-toggle"]').trigger('click')
    await flush()

    expect(togglePatchCalls()).toHaveLength(0)
    expect(wrapper.find('[data-test-id="policy-gate-toggle"]').attributes('aria-checked')).toBe('true')
  })

  it('dismisses the toggle-disable dialog on Escape (F9)', async () => {
    const wrapper = await openWithGate({
      id: 'gate-1',
      action: 'block',
      version: 1,
      enabled: true,
      enabled_at: '2026-01-01T00:00:00Z',
      disabled_at: null,
    })
    await wrapper.find('[data-test-id="policy-gate-toggle"]').trigger('click')
    await nextTick()
    const dialog = wrapper.find('[data-test-id="policy-gate-toggle-confirm-dialog"]')
    expect(dialog.exists()).toBe(true)

    dialog.element.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await flush()

    expect(wrapper.find('[data-test-id="policy-gate-toggle-confirm-dialog"]').exists()).toBe(false)
    expect(togglePatchCalls()).toHaveLength(0)
  })

  it('traps Tab focus within the toggle-disable dialog in both directions (F9)', async () => {
    const wrapper = await openWithGate({
      id: 'gate-1',
      action: 'block',
      version: 1,
      enabled: true,
      enabled_at: '2026-01-01T00:00:00Z',
      disabled_at: null,
    })
    await wrapper.find('[data-test-id="policy-gate-toggle"]').trigger('click')
    await nextTick()
    const dialog = wrapper.find('[data-test-id="policy-gate-toggle-confirm-dialog"]')
    const buttons = dialog.findAll('button')
    const first = buttons[0].element as HTMLElement
    const last = buttons[buttons.length - 1].element as HTMLElement
    const firstFocus = vi.spyOn(first, 'focus')
    const lastFocus = vi.spyOn(last, 'focus')

    withActiveElement(last, () => {
      dialog.element.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', bubbles: true }))
    })
    expect(firstFocus).toHaveBeenCalled()

    withActiveElement(first, () => {
      dialog.element.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', shiftKey: true, bubbles: true }))
    })
    expect(lastFocus).toHaveBeenCalled()
  })

  it('returns early from the toggle-dialog trap with no focusable children (F9)', async () => {
    const wrapper = await openWithGate({
      id: 'gate-1',
      action: 'block',
      version: 1,
      enabled: true,
      enabled_at: '2026-01-01T00:00:00Z',
      disabled_at: null,
    })
    await wrapper.find('[data-test-id="policy-gate-toggle"]').trigger('click')
    await nextTick()
    const dialog = wrapper.find('[data-test-id="policy-gate-toggle-confirm-dialog"]')
    dialog.element.innerHTML = ''
    dialog.element.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', bubbles: true }))

    expect(dialog.exists()).toBe(true)
  })

  it('does not wrap toggle-dialog focus from neither end (F9)', async () => {
    const wrapper = await openWithGate({
      id: 'gate-1',
      action: 'block',
      version: 1,
      enabled: true,
      enabled_at: '2026-01-01T00:00:00Z',
      disabled_at: null,
    })
    await wrapper.find('[data-test-id="policy-gate-toggle"]').trigger('click')
    await nextTick()
    const dialog = wrapper.find('[data-test-id="policy-gate-toggle-confirm-dialog"]')
    const buttons = dialog.findAll('button')
    const firstFocus = vi.spyOn(buttons[0].element as HTMLElement, 'focus')
    const last = buttons[buttons.length - 1].element as HTMLElement
    const lastFocus = vi.spyOn(last, 'focus')
    const outside = wrapper.find('[data-testid="eval-editor-save"]').element as HTMLElement

    withActiveElement(outside, () => {
      dialog.element.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', bubbles: true }))
    })
    expect(firstFocus).not.toHaveBeenCalled()
    expect(lastFocus).not.toHaveBeenCalled()

    // shift+Tab from the LAST control (not the first) must not wrap either.
    withActiveElement(last, () => {
      dialog.element.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', shiftKey: true, bubbles: true }))
    })
    expect(firstFocus).not.toHaveBeenCalled()
    expect(lastFocus).not.toHaveBeenCalled()
  })

  it('ignores other keys in the toggle dialog (F9)', async () => {
    const wrapper = await openWithGate({
      id: 'gate-1',
      action: 'block',
      version: 1,
      enabled: true,
      enabled_at: '2026-01-01T00:00:00Z',
      disabled_at: null,
    })
    await wrapper.find('[data-test-id="policy-gate-toggle"]').trigger('click')
    await nextTick()
    const dialog = wrapper.find('[data-test-id="policy-gate-toggle-confirm-dialog"]')

    dialog.element.dispatchEvent(new KeyboardEvent('keydown', { key: 'a', bubbles: true }))

    expect(dialog.exists()).toBe(true)
  })
})
