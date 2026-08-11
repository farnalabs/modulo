import { describe, it, expect, vi, beforeEach } from 'vitest'
import { reactive } from 'vue'
import { useRoute } from 'vue-router'
import type { RouteLocationNormalizedLoaded } from 'vue-router'

vi.mock('vue-router', async (importOriginal) => {
  const actual = await importOriginal<typeof import('vue-router')>()
  return {
    ...actual,
    useRoute: vi.fn(),
  }
})

let route: ReturnType<typeof reactive>

function setRoute(patch: Partial<RouteLocationNormalizedLoaded>) {
  Object.assign(route, patch)
  vi.mocked(useRoute).mockReturnValue(route as unknown as RouteLocationNormalizedLoaded)
}

beforeEach(() => {
  vi.resetModules()
  route = reactive<Partial<RouteLocationNormalizedLoaded>>({
    name: null,
    params: {},
    path: '/',
  } as Partial<RouteLocationNormalizedLoaded>)
  vi.mocked(useRoute).mockReturnValue(route as unknown as RouteLocationNormalizedLoaded)
})

async function setupContext() {
  const { useRemyContext } = await import('../../composables/useRemyContext')
  return useRemyContext()
}

describe('useRemyContext', () => {
  it('starts with an empty page context on a bare route', async () => {
    const ctx = await setupContext()
    expect(ctx.pageContext.value).toEqual({
      route: '/',
      params: {},
      entities: [],
    })
  })

  it('captures the route name, params and derived entities', async () => {
    setRoute({
      name: 'run-detail',
      params: { id: 'run-123', teamId: 'team-7' },
      path: '/runs/run-123',
    })

    const ctx = await setupContext()
    expect(ctx.pageContext.value.route).toBe('run-detail')
    expect(ctx.pageContext.value.params).toEqual({ id: 'run-123', teamId: 'team-7' })
    expect(ctx.pageContext.value.entities).toContain('run:run-123')
    expect(ctx.pageContext.value.entities).toContain('team:team-7')
  })

  it('derives pipeline entities from the pipelineId param', async () => {
    setRoute({
      name: 'pipeline-editor',
      params: { pipelineId: 'pipe-1' },
      path: '/pipelines/pipe-1',
    })

    const ctx = await setupContext()
    expect(ctx.pageContext.value.entities).toContain('pipeline:pipe-1')
  })

  it('omits entity prefixes for params it does not know', async () => {
    setRoute({
      name: 'settings',
      params: { orgId: 'org-1', foo: 'bar' },
      path: '/settings',
    })

    const ctx = await setupContext()
    expect(ctx.pageContext.value.entities).toEqual([])
    expect(ctx.pageContext.value.params).toEqual({ orgId: 'org-1', foo: 'bar' })
  })

  it('flattens array params to their first element', async () => {
    setRoute({
      name: 'run-detail',
      params: { id: ['run-1', 'run-2'], teamId: [] as never },
      path: '/runs/run-1',
    })

    const ctx = await setupContext()
    expect(ctx.pageContext.value.params).toEqual({ id: 'run-1', teamId: '' })
    expect(ctx.pageContext.value.entities).toEqual(['run:run-1'])
  })

  it('coerces non-string params to empty strings', async () => {
    setRoute({
      name: 'run-detail',
      params: { id: 42 as never, teamId: null as never },
      path: '/runs/42',
    })

    const ctx = await setupContext()
    expect(ctx.pageContext.value.params).toEqual({ id: '', teamId: '' })
    expect(ctx.pageContext.value.entities).toEqual([])
  })

  it('falls back to the route path when the route name is null', async () => {
    setRoute({
      name: null,
      params: { id: 'run-9' },
      path: '/runs/run-9',
    })

    const ctx = await setupContext()
    expect(ctx.pageContext.value.route).toBe('/runs/run-9')
    expect(ctx.pageContext.value.entities).toEqual(['run:run-9'])
  })

  it('recomputes the context when the route changes', async () => {
    setRoute({
      name: 'run-detail',
      params: { id: 'run-1' },
      path: '/runs/run-1',
    })
    const ctx = await setupContext()
    expect(ctx.pageContext.value.entities).toEqual(['run:run-1'])

    setRoute({
      name: 'pipeline-editor',
      params: { pipelineId: 'pipe-2' },
      path: '/pipelines/pipe-2',
    })

    await vi.waitFor(() => {
      expect(ctx.pageContext.value.route).toBe('pipeline-editor')
      expect(ctx.pageContext.value.entities).toEqual(['pipeline:pipe-2'])
    })
  })
})
