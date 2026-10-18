/**
 * FAR-53 (Cmd+K Phase 2): user-data result providers for the command palette.
 *
 * Phase 1 indexed navigation only (the nav-item list in `CommandPalette.vue`).
 * This module adds the user-data sources the palette searches alongside nav
 * items: pipelines, runs, schemas, connectors and a "recently visited" source.
 *
 * Authz/tenancy: every provider queries the SAME endpoints the owning views
 * call (`/api/v1/pipelines`, `/api/v1/runs`, `/api/v1/schemas`,
 * `/api/v1/connectors`) through the shared `api` client, so org/team scoping
 * and permission checks are enforced server-side exactly as they are
 * everywhere else in the app. A provider whose endpoint denies the caller
 * (401/403) is disabled for the session instead of being retried on every
 * keystroke; any other failure rejects so the palette can degrade gracefully.
 */
import { api } from '../api/client'
import { fetchRuns, type RunListItem } from '../api/runs'
import { formatApiError } from '../api/formatError'
import { getRecentVisits } from './recents'

export type PaletteItemType = 'nav' | 'recent' | 'pipeline' | 'run' | 'schema' | 'connector'

export interface PaletteItem {
  /** Stable unique key — used for `:key`, de-duplication and `aria-activedescendant`. */
  id: string
  label: string
  subtitle?: string | null
  icon: string
  /** Router path to navigate to when the item is chosen. */
  path: string
  type: PaletteItemType
  /** i18n key for the section label rendered on the right of the row. */
  sectionKey?: string
  /** Legacy already-localised section label (nav items from the manifest). */
  section?: string
}

export interface PaletteProvider {
  id: string
  icon: string
  sectionKey: string
  /**
   * Minimum trimmed query length before this provider is consulted. Recents
   * surface on an empty query (0); remote sources wait for a real query so an
   * empty palette stays cheap (recents + nav only).
   */
  minQueryLength: number
  search: (query: string) => Promise<PaletteItem[]>
  /** Clears the provider's cache — test isolation only. */
  reset?: () => void
}

const LIST_PAGE_SIZE = 100
const RUN_LIMIT = 20
/**
 * Short TTL on the cached list payloads. A list search is a client-side filter
 * over one fetched page, so re-fetching on every debounced keystroke would be a
 * request storm; 30s keeps a typing session to a single request per source
 * while still picking up newly created entities shortly after they exist.
 */
const LIST_CACHE_TTL_MS = 30_000

interface ListPage<T> {
  items?: T[]
}

interface ListProviderConfig<T> {
  id: string
  icon: string
  sectionKey: string
  load: () => Promise<{ data?: ListPage<T>; error?: unknown; response?: Response }>
  map: (row: T) => PaletteItem | null
  /** Query-independent predicate — case-insensitive match over label + subtitle. */
  matches?: (item: PaletteItem, query: string) => boolean
}

function defaultMatches(item: PaletteItem, query: string): boolean {
  const haystack = `${item.label} ${item.subtitle ?? ''}`.toLowerCase()
  return haystack.includes(query.toLowerCase())
}

/**
 * A provider that fetches one page of a list endpoint and filters it
 * client-side (neither pipelines, schemas nor connectors expose a server-side
 * text filter).
 */
function createListProvider<T>(config: ListProviderConfig<T>): PaletteProvider {
  let cache: PaletteItem[] | null = null
  let cachedAt = 0
  let denied = false
  const matches = config.matches ?? defaultMatches

  return {
    id: config.id,
    icon: config.icon,
    sectionKey: config.sectionKey,
    minQueryLength: 2,
    async search(query: string): Promise<PaletteItem[]> {
      // A 401/403 means the caller has no permission for this source (e.g. a
      // viewer on /api/v1/connectors). Disable it rather than re-requesting on
      // every keystroke — the palette stays usable for the sources they CAN see.
      if (denied) return []
      const now = Date.now()
      if (cache === null || now - cachedAt > LIST_CACHE_TTL_MS) {
        const { data, error, response } = await config.load()
        if (response && (response.status === 401 || response.status === 403)) {
          denied = true
          return []
        }
        if (error) throw new Error(formatApiError(error))
        cache = (data?.items ?? [])
          .map(config.map)
          .filter((item): item is PaletteItem => item !== null)
        cachedAt = now
      }
      return cache.filter((item) => matches(item, query))
    },
    reset() {
      cache = null
      cachedAt = 0
      denied = false
    },
  }
}

export const pipelinesProvider = createListProvider<{ id: string; name: string; description?: string | null }>({
  id: 'pipelines',
  icon: 'GitFork',
  sectionKey: 'components.CommandPalette.section_pipelines',
  load: () => api.GET('/api/v1/pipelines', { params: { query: { page_size: LIST_PAGE_SIZE } } }),
  map: (row) => ({
    id: `pipeline:${row.id}`,
    label: row.name,
    subtitle: row.description ?? null,
    icon: 'GitFork',
    path: `/pipelines/${row.id}/editor`,
    type: 'pipeline',
    sectionKey: 'components.CommandPalette.section_pipelines',
  }),
})

export const schemasProvider = createListProvider<{ id: string; name: string; description?: string | null }>({
  id: 'schemas',
  icon: 'Database',
  sectionKey: 'components.CommandPalette.section_schemas',
  load: () => api.GET('/api/v1/schemas', { params: { query: { page_size: LIST_PAGE_SIZE } } }),
  map: (row) => ({
    id: `schema:${row.id}`,
    label: row.name,
    subtitle: row.description ?? null,
    icon: 'Database',
    path: `/schemas/editor/${row.id}`,
    type: 'schema',
    sectionKey: 'components.CommandPalette.section_schemas',
  }),
})

export const connectorsProvider = createListProvider<{ id: string; name: string; connector_type_id?: string }>({
  id: 'connectors',
  icon: 'Plug',
  sectionKey: 'components.CommandPalette.section_connectors',
  load: () => api.GET('/api/v1/connectors', { params: { query: { page_size: LIST_PAGE_SIZE } } }),
  map: (row) => ({
    id: `connector:${row.id}`,
    label: row.name,
    subtitle: row.connector_type_id ?? null,
    icon: 'Plug',
    // Connectors have no per-id route — the list page is the destination
    // (mirrors the admin global-search library results behaviour).
    path: '/admin/connectors',
    type: 'connector',
    sectionKey: 'components.CommandPalette.section_connectors',
  }),
})

/** Runs expose a server-side `search` filter, so this provider is not cached. */
function runToItem(row: RunListItem): PaletteItem {
  const label = row.run_number !== null && row.run_number !== undefined
    ? `#${row.run_number}`
    : `#${row.run_id.slice(0, 8)}`
  return {
    id: `run:${row.run_id}`,
    label,
    subtitle: row.pipeline_name ?? null,
    icon: 'CirclePlay',
    path: `/runs/${row.run_id}`,
    type: 'run',
    sectionKey: 'components.CommandPalette.section_runs',
  }
}

export const runsProvider: PaletteProvider = {
  id: 'runs',
  icon: 'CirclePlay',
  sectionKey: 'components.CommandPalette.section_runs',
  minQueryLength: 2,
  async search(query: string): Promise<PaletteItem[]> {
    const result = await fetchRuns({ search: query, page_size: RUN_LIMIT })
    return result.items.map(runToItem)
  },
}

/** Recents surface on an empty query (and narrow as the user types). */
export const recentsProvider: PaletteProvider = {
  id: 'recents',
  icon: 'History',
  sectionKey: 'components.CommandPalette.section_recent',
  minQueryLength: 0,
  async search(query: string): Promise<PaletteItem[]> {
    const needle = query.trim().toLowerCase()
    return getRecentVisits()
      .filter((visit) => (
        !needle
        || visit.label.toLowerCase().includes(needle)
        || visit.path.toLowerCase().includes(needle)
      ))
      .map((visit) => ({
        id: `recent:${visit.path}`,
        label: visit.label,
        subtitle: visit.path,
        icon: visit.icon,
        path: visit.path,
        type: 'recent',
        sectionKey: 'components.CommandPalette.section_recent',
      }))
  },
}

/** Recents first (jump-back is the common case), then the remote sources. */
export const PALETTE_PROVIDERS: PaletteProvider[] = [
  recentsProvider,
  pipelinesProvider,
  runsProvider,
  schemasProvider,
  connectorsProvider,
]

/** Clear every provider's cache — test isolation only. */
export function resetPaletteProviderCaches(): void {
  for (const provider of PALETTE_PROVIDERS) provider.reset?.()
}
