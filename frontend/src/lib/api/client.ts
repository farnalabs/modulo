import createClient from 'openapi-fetch'
import type { paths } from './schema'

export const api = createClient<paths>({
  baseUrl: '/api/v1',
})

export type { paths, components } from './schema'
