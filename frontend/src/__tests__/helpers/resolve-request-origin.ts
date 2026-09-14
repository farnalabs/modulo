import { vi } from 'vitest'

// openapi-fetch builds the client's relative request paths with baseUrl: '' and
// lets the platform resolve them against the document origin. undici's global
// Request (used under jsdom) throws on relative URLs, so this helper — which
// MUST be imported before lib/api/client in any test that exercises the
// generated client — wraps Request to resolve relative paths against a fixed
// origin and installs a shared, per-test-resettable fetch spy.
const RealRequest = globalThis.Request

vi.stubGlobal(
  'Request',
  class extends RealRequest {
    constructor(input: RequestInfo | URL, init?: RequestInit) {
      if (typeof input === 'string' && !/^https?:\/\//.test(input)) {
        input = new URL(input, 'http://localhost/').toString()
      }
      super(input as RequestInfo | URL, init)
    }
  },
)

// Single fetch spy captured by createClient() at client import time. Tests
// reset its implementation per case via fetchMock.mockImplementation(...).
export const fetchMock = vi.fn(async () => new Response(JSON.stringify({}), { status: 200 }))

vi.stubGlobal('fetch', fetchMock)
