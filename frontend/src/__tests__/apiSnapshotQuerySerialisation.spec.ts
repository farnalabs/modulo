import createClient from 'openapi-fetch'
import { describe, it, expect } from 'vitest'
import type { paths } from '../lib/api/schema'

// FAR-1895 (finding G): the editor's version-chip / timeline tests assert the
// QUERY OBJECT the component passes to the typed client. That is not the wire
// form — a change to how the client serialises an array (`run` as a repeated
// param vs `exclude_created_kind[]=run` vs a comma-joined value) would make the
// request 422 with no test failing. This spec drives the REAL openapi-fetch
// client (the same one `lib/api/client.ts` creates) with a capturing fetch and
// asserts the actual query string.
describe('snapshot list query serialisation (FAR-1895)', () => {
  it('serialises exclude_created_kind as repeated params, not a bracketed/joined value', async () => {
    const urls: string[] = []
    const client = createClient<paths>({
      baseUrl: 'https://example.test',
      fetch: (input: Request) => {
        urls.push(input.url)
        return Promise.resolve(
          new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } }),
        )
      },
    })

    await client.GET('/api/v1/pipelines/{pipeline_id}/snapshots', {
      params: {
        path: { pipeline_id: 'p-1' },
        query: { page_size: 2, exclude_created_kind: ['run'], exclude_drafts: true },
      },
    })

    expect(urls).toHaveLength(1)
    const url = new URL(urls[0])
    // The backend binds `exclude_created_kind` as a repeated query param.
    expect(url.searchParams.getAll('exclude_created_kind')).toEqual(['run'])
    expect(url.search).not.toContain('exclude_created_kind%5B%5D') // no [] suffix
    expect(url.searchParams.get('exclude_drafts')).toBe('true')
    expect(url.searchParams.get('page_size')).toBe('2')
  })
})
