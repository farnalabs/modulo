/**
 * Static structural parity test: every team-tier feature with a backend
 * `require_feature` gate should ALSO have a frontend `FeatureGate` /
 * `featureEnabled` reference, and vice-versa.
 *
 * Reads the backend route files and frontend view files as plain text (no
 * module import / no component mount) and compares the feature-name sets.
 *
 * Known acceptable exceptions — backend-only features with no UI surface:
 *   - scim            : SCIM provisioning is API-only (no UI)
 *   - external_secrets: configured via connector/API, no dedicated UI page yet
 */
import { describe, it, expect } from 'vitest'
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, resolve } from 'node:path'

const REPO_ROOT = resolve(__dirname, '../../../')
const BACKEND_ROUTES = join(REPO_ROOT, 'backend', 'src', 'modulo', 'api', 'routes')
const FRONTEND_SRC = resolve(__dirname, '../')
const BACKEND_FEATURE_FLAGS = join(REPO_ROOT, 'backend', 'src', 'modulo', 'core', 'feature_flags.py')

/**
 * Known acceptable exceptions — backend-only features with no UI surface:
 *   - scim            : SCIM provisioning is API-only (no UI)
 *   - external_secrets: configured via connector/API, no dedicated UI page yet
 */
const BACKEND_ONLY_EXCEPTIONS = new Set(['scim', 'external_secrets'])

/**
 * Known acceptable exceptions — frontend-gated team features with no backend
 * route gate yet:
 *   - pipeline_delete: UI hard-delete button is featureEnabled-gated but the
 *     DELETE /api/v1/pipelines route has no require_feature("pipeline_delete")
 *     yet. Tracked as a known unenforced team flag in the backend parity test
 *     (test_team_feature_parity.py KNOWN_UNENFORCED_TEAM_FLAGS).
 */
const FRONTEND_ONLY_EXCEPTIONS = new Set(['pipeline_delete'])

/**
 * Backend-gated features whose frontend gate uses the manifest route-guard
 * mechanism (required_tier / required_permissions in manifest.yaml) rather
 * than a FeatureGate component or planStore.featureEnabled() call:
 *   - analytics_page: the /analytics route is gated by
 *     required_permissions: [analytics.query] in frontend/src/manifest.yaml.
 */
const MANIFEST_GATED_EXCEPTIONS = new Set(['analytics_page'])

function collectTeamFeatureNames(): Set<string> {
  const names = new Set<string>()
  const text = readFileSync(BACKEND_FEATURE_FLAGS, 'utf-8')
  // Split on each FeatureFlag declaration so name + tier resolve within the
  // same flag (a lazy cross-flag regex would misattribute a later tier="team").
  for (const chunk of text.split('FeatureFlag(')) {
    const nameMatch = chunk.match(/name="([a-z_]+)"/)
    if (nameMatch && /tier="team"/.test(chunk)) {
      names.add(nameMatch[1])
    }
  }
  return names
}

function collectBackendTeamFeatures(): Set<string> {
  const features = new Set<string>()
  if (!statSync(BACKEND_ROUTES, { throwIfNoEntry: false })) return features
  for (const file of readdirSync(BACKEND_ROUTES)) {
    if (!file.endsWith('.py')) continue
    const text = readFileSync(join(BACKEND_ROUTES, file), 'utf-8')
    for (const match of text.matchAll(/require_feature\("([a-z_]+)"\)/g)) {
      features.add(match[1])
    }
  }
  return features
}

function collectFrontendFeatureRefs(): Set<string> {
  const refs = new Set<string>()
  const walk = (dir: string) => {
    for (const entry of readdirSync(dir)) {
      const full = join(dir, entry)
      if (statSync(full).isDirectory()) {
        walk(full)
      } else if (entry.endsWith('.vue') || entry.endsWith('.ts')) {
        const text = readFileSync(full, 'utf-8')
        for (const match of text.matchAll(/feature-name="([a-z_]+)"/g)) refs.add(match[1])
        for (const match of text.matchAll(/featureEnabled\('([a-z_]+)'\)/g)) refs.add(match[1])
        for (const match of text.matchAll(/featureEnabled\("([a-z_]+)"\)/g)) refs.add(match[1])
      }
    }
  }
  walk(FRONTEND_SRC)
  return refs
}

describe('team feature backend <-> frontend parity', () => {
  const backendFeatures = collectBackendTeamFeatures()
  const frontendRefs = collectFrontendFeatureRefs()
  const teamFeatures = collectTeamFeatureNames()

  it('every backend-gated feature has a frontend FeatureGate reference', () => {
    const missing = [...backendFeatures].filter(
      (f) => !frontendRefs.has(f) && !BACKEND_ONLY_EXCEPTIONS.has(f) && !MANIFEST_GATED_EXCEPTIONS.has(f),
    )
    expect(missing).toEqual([])
  })

  it('every team-tier frontend-gated feature is enforced on the backend', () => {
    const teamFrontendGated = [...frontendRefs].filter((f) => teamFeatures.has(f))
    const missing = teamFrontendGated.filter((f) => !backendFeatures.has(f) && !FRONTEND_ONLY_EXCEPTIONS.has(f))
    expect(missing).toEqual([])
  })
})
