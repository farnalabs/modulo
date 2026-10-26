<template>
  <div class="page-wide space-y-6" data-testid="compliance-evidence-index">
    <PageHeader
      :title="$t('views.ComplianceEvidenceIndex.title')"
      :subtitle="$t('views.ComplianceEvidenceIndex.subtitle')"
    />

    <div
      class="rounded-lg border border-primary/30 bg-primary/5 p-4 text-sm"
      data-testid="compliance-evidence-index-scope-note"
    >
      {{ $t('views.ComplianceEvidenceIndex.scope_note') }}
    </div>

    <div
      v-if="loading"
      aria-live="polite"
      aria-busy="true"
      class="flex items-center gap-3 text-sm text-muted-foreground"
      data-testid="compliance-evidence-index-loading"
    >
      <LoadingSpinner />
      <span>{{ $t('views.ComplianceEvidenceIndex.loading') }}</span>
    </div>

    <div v-else-if="permissionDenied" aria-live="assertive" data-testid="compliance-evidence-index-permission-denied" class="rounded-lg border border-warning/50 bg-warning/10 p-4 text-sm">
      <p class="font-semibold">{{ $t('views.ComplianceEvidenceIndex.permission_denied_title') }}</p>
      <p class="mt-1">{{ $t('views.ComplianceEvidenceIndex.permission_denied_description') }}</p>
    </div>

    <div v-else-if="loadError" aria-live="assertive" data-testid="compliance-evidence-index-error">
      <ErrorAlert :message="loadError" :on-retry="load" />
    </div>

    <EmptyState
      v-else-if="!fetched || !indexData"
      :title="$t('views.ComplianceEvidenceIndex.empty_title')"
      :description="$t('views.ComplianceEvidenceIndex.empty_description')"
    />

    <template v-else>
      <div
        v-if="!indexData.complete || indexData.missing_artifact_types.length > 0"
        class="rounded-lg border border-warning/50 bg-warning/10 p-4 text-sm"
        aria-live="polite"
        data-testid="compliance-evidence-index-incomplete"
      >
        <p class="font-semibold">{{ $t('views.ComplianceEvidenceIndex.incomplete_title') }}</p>
        <p class="mt-1">{{ $t('views.ComplianceEvidenceIndex.incomplete_description') }}</p>
        <ul v-if="indexData.missing_artifact_types.length > 0" class="mt-2 list-inside list-disc">
          <li v-for="artifactType in indexData.missing_artifact_types" :key="artifactType">{{ artifactType }}</li>
        </ul>
      </div>

      <div
        v-if="lowerBoundArtifacts.length > 0"
        class="rounded-lg border border-warning/50 bg-warning/10 p-4 text-sm"
        aria-live="polite"
        data-testid="compliance-evidence-index-lower-bounds"
      >
        <p class="font-semibold">{{ $t('views.ComplianceEvidenceIndex.lower_bound_title') }}</p>
        <p class="mt-1">{{ $t('views.ComplianceEvidenceIndex.lower_bound_description') }}</p>
        <p class="mt-2 font-mono text-xs">{{ lowerBoundArtifacts.join(', ') }}</p>
      </div>

      <h2 id="evidence-index-pack-heading" class="sr-only">{{ $t('views.ComplianceEvidenceIndex.pack_heading') }}</h2>
      <dl class="grid gap-4 sm:grid-cols-2 lg:grid-cols-4" aria-labelledby="evidence-index-pack-heading">
        <div class="card p-4" data-testid="compliance-evidence-index-pack-version">
          <dt class="text-xs font-medium uppercase tracking-wide text-muted-foreground">{{ $t('views.ComplianceEvidenceIndex.pack_version') }}</dt>
          <dd class="mt-1 font-mono text-sm">{{ indexData.pack_version }}</dd>
        </div>
        <div class="card p-4" data-testid="compliance-evidence-index-pack-hash">
          <dt class="text-xs font-medium uppercase tracking-wide text-muted-foreground">{{ $t('views.ComplianceEvidenceIndex.pack_hash') }}</dt>
          <dd class="mt-1 break-all font-mono text-xs">{{ indexData.pack_content_hash }}</dd>
        </div>
        <div class="card p-4" data-testid="compliance-evidence-index-product-version">
          <dt class="text-xs font-medium uppercase tracking-wide text-muted-foreground">{{ $t('views.ComplianceEvidenceIndex.product_version') }}</dt>
          <dd class="mt-1 font-mono text-sm">{{ indexData.product_version }}</dd>
        </div>
        <div class="card p-4" data-testid="compliance-evidence-index-frameworks">
          <dt class="text-xs font-medium uppercase tracking-wide text-muted-foreground">{{ $t('views.ComplianceEvidenceIndex.frameworks') }}</dt>
          <dd class="mt-1 text-sm">{{ indexData.framework_ids.join(', ') }}</dd>
        </div>
        <div class="card p-4" data-testid="compliance-evidence-index-pack-published-at">
          <dt class="text-xs font-medium uppercase tracking-wide text-muted-foreground">{{ $t('views.ComplianceEvidenceIndex.pack_published_at') }}</dt>
          <dd class="mt-1 text-sm">{{ formatTimestamp(indexData.pack_published_at) }}</dd>
        </div>
        <div class="card p-4" data-testid="compliance-evidence-index-generated-at">
          <dt class="text-xs font-medium uppercase tracking-wide text-muted-foreground">{{ $t('views.ComplianceEvidenceIndex.generated_at') }}</dt>
          <dd class="mt-1 text-sm">{{ formatTimestamp(indexData.generated_at) }}</dd>
        </div>
      </dl>

      <section class="space-y-3" aria-labelledby="evidence-index-coverage-heading" data-testid="compliance-evidence-index-coverage">
        <h2 id="evidence-index-coverage-heading" class="text-lg font-semibold">{{ $t('views.ComplianceEvidenceIndex.coverage_heading') }}</h2>
        <div
          class="overflow-x-auto rounded-lg border bg-card shadow-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          role="region"
          aria-labelledby="evidence-index-coverage-heading"
          tabindex="0"
          data-testid="compliance-evidence-index-coverage-scroll"
        >
          <table class="w-full text-left text-sm">
            <thead>
              <tr class="border-b bg-muted/50">
                <th scope="col" class="px-3 py-2 font-medium">{{ $t('views.ComplianceEvidenceIndex.framework') }}</th>
                <th scope="col" class="px-3 py-2 font-medium">{{ $t('views.ComplianceEvidenceIndex.category') }}</th>
                <th scope="col" class="px-3 py-2 text-right font-medium">{{ $t('views.ComplianceEvidenceIndex.count') }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in coverageRows" :key="`${row.framework}-${row.category}`" class="border-b last:border-b-0">
                <td class="px-3 py-2 font-mono text-xs">{{ row.framework }}</td>
                <td class="px-3 py-2">{{ row.category }}</td>
                <td class="px-3 py-2 text-right tabular-nums">{{ row.count }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <section class="space-y-3" aria-labelledby="evidence-index-artifacts-heading">
        <div class="flex items-baseline justify-between gap-3">
          <h2 id="evidence-index-artifacts-heading" class="text-lg font-semibold">{{ $t('views.ComplianceEvidenceIndex.artifacts_heading') }}</h2>
          <p aria-live="polite" class="text-sm text-muted-foreground" data-testid="compliance-evidence-index-artifact-count">
            {{ $t('views.ComplianceEvidenceIndex.artifact_count', { count: indexData.artifacts.length }) }}
          </p>
        </div>
        <div
          class="overflow-x-auto rounded-lg border bg-card shadow-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          role="region"
          aria-labelledby="evidence-index-artifacts-heading"
          tabindex="0"
          data-testid="compliance-evidence-index-artifacts-scroll"
        >
          <table class="w-full min-w-[1050px] text-left text-sm">
            <thead>
              <tr class="border-b bg-muted/50">
                <th scope="col" class="px-3 py-2 font-medium">{{ $t('views.ComplianceEvidenceIndex.artifact_type') }}</th>
                <th scope="col" class="px-3 py-2 font-medium">{{ $t('views.ComplianceEvidenceIndex.status') }}</th>
                <th scope="col" class="px-3 py-2 font-medium">{{ $t('views.ComplianceEvidenceIndex.identifier_location') }}</th>
                <th scope="col" class="px-3 py-2 font-medium">{{ $t('views.ComplianceEvidenceIndex.provenance') }}</th>
                <th scope="col" class="px-3 py-2 font-medium">{{ $t('views.ComplianceEvidenceIndex.mapping_rows') }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="artifact in indexData.artifacts" :key="artifact.artifact_type" class="border-b align-top last:border-b-0">
                <td class="px-3 py-3">
                  <div class="font-mono text-xs">{{ artifact.artifact_type }}</div>
                  <div class="mt-1 max-w-xs text-xs text-muted-foreground">{{ artifact.description }}</div>
                </td>
                <td class="px-3 py-3">
                  <span
                    class="inline-flex rounded-full border px-2 py-0.5 text-xs font-medium"
                    :class="statusClass(artifact.status)"
                    :aria-label="$t('views.ComplianceEvidenceIndex.status_label', { status: statusLabel(artifact.status) })"
                  >
                    {{ statusLabel(artifact.status) }}
                  </span>
                  <p v-if="artifact.absence_reason" class="mt-1 max-w-xs text-xs text-destructive-text">{{ artifact.absence_reason }}</p>
                  <p v-if="artifact.record_count !== null" class="mt-1 text-xs text-muted-foreground">
                    {{ $t('views.ComplianceEvidenceIndex.record_count', { count: artifact.record_count }) }}
                  </p>
                </td>
                <td class="max-w-sm px-3 py-3">
                  <div class="break-words">{{ artifact.location }}</div>
                  <ul v-if="artifact.identifiers.length > 0" class="mt-1 space-y-1 text-xs text-muted-foreground">
                    <li v-for="identifier in identifierDisplays(artifact)" :key="identifier.raw">
                      <template v-if="identifier.label">
                        <span class="text-foreground" data-testid="compliance-evidence-index-identifier-label">{{ identifier.label }}</span>
                        <span class="ml-1 font-mono">{{ identifier.reference }}</span>
                        <router-link
                          v-if="identifier.href"
                          :to="identifier.href"
                          :aria-label="$t('views.ComplianceEvidenceIndex.snapshot_open_label', { version: identifier.version, reference: identifier.reference })"
                          class="ml-1 underline hover:text-foreground"
                          data-testid="compliance-evidence-index-snapshot-link"
                        >{{ $t('views.ComplianceEvidenceIndex.snapshot_open') }}</router-link>
                        <button
                          type="button"
                          :aria-label="identifierCopyLabel(identifier)"
                          aria-live="polite"
                          class="ml-1 underline hover:text-foreground"
                          data-testid="compliance-evidence-index-copy-identifier"
                          @click="copyIdentifier(identifier.raw)"
                        >{{ copiedIdentifier === identifier.raw ? $t('views.ComplianceEvidenceIndex.identifier_copied') : $t('views.ComplianceEvidenceIndex.identifier_copy') }}</button>
                        <details class="mt-1" data-testid="compliance-evidence-index-identifier-details">
                          <summary class="cursor-pointer">{{ $t('views.ComplianceEvidenceIndex.identifier_details') }}</summary>
                          <code class="mt-1 block select-all break-all font-mono">{{ identifier.raw }}</code>
                        </details>
                        <p
                          v-if="copyFailedIdentifier === identifier.raw"
                          class="mt-1 text-destructive-text"
                          role="alert"
                          data-testid="compliance-evidence-index-copy-error"
                        >{{ $t('views.ComplianceEvidenceIndex.identifier_copy_failed') }}</p>
                      </template>
                      <span v-else class="font-mono">{{ identifier.raw }}</span>
                    </li>
                  </ul>
                </td>
                <td class="px-3 py-3">{{ provenanceLabel(artifact.provenance) }}</td>
                <td class="px-3 py-3 font-mono text-xs">
                  <span v-if="artifact.mapping_row_ids.length > 0">{{ artifact.mapping_row_ids.join(', ') }}</span>
                  <span v-else class="text-muted-foreground">{{ $t('views.ComplianceEvidenceIndex.not_available') }}</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <section
        class="rounded-lg border border-warning/50 bg-warning/10 p-4 text-sm"
        aria-labelledby="evidence-index-customer-heading"
        data-testid="compliance-evidence-index-customer-responsibility"
      >
        <h2 id="evidence-index-customer-heading" class="font-semibold">{{ $t('views.ComplianceEvidenceIndex.customer_heading') }}</h2>
        <p class="mt-1">{{ $t('views.ComplianceEvidenceIndex.customer_description') }}</p>
      </section>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import PageHeader from '../components/shared/PageHeader.vue'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import EmptyState from '../components/shared/EmptyState.vue'
import { api } from '../lib/api/client'
import type { components } from '../lib/api/schema'
import { useDataFetch } from '../composables/useDataFetch'
import { shortId } from '../utils/format'

type EvidenceIndex = components['schemas']['EvidenceIndex']
type ArtifactEntry = components['schemas']['ArtifactEntry']

type CoverageRow = { framework: string; category: string; count: number }

type IdentifierDisplay = {
  raw: string
  label: string | null
  reference: string | null
  href: string | null
  version: number | null
}

/**
 * The run-snapshot artefact names each frozen snapshot as
 * `pipeline-snapshot:<uuid>@v<version>` (backend/compliance/evidence_index.py).
 * The UUID is unambiguous but unreadable, so the table renders a concise
 * version label plus the app-wide short id, and keeps the RAW identifier
 * reachable through the detail disclosure and the copy action. Anything that
 * does not match this shape is rendered verbatim - never partially parsed.
 */
const PIPELINE_SNAPSHOT_IDENTIFIER = /^pipeline-snapshot:([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})@v(\d+)$/i

const { t: $t } = useI18n()

const {
  data: indexData,
  loading,
  error: loadError,
  fetched,
  load,
} = useDataFetch(
  async () => {
    const response = await api.GET('/api/v1/compliance/evidence-index')
    return { data: response.data, error: response.error }
  },
  { initialValue: null as EvidenceIndex | null },
)

const permissionDenied = computed(() => {
  const error = loadError.value
  return typeof error === 'string' && /forbidden|permission|not allowed|access denied/i.test(error)
})

const coverageRows = computed<CoverageRow[]>(() =>
  Object.entries(indexData.value!.coverage).flatMap(([framework, categories]) =>
    Object.entries(categories).map(([category, count]) => ({ framework, category, count })),
  ),
)

const lowerBoundArtifacts = computed(() =>
  indexData.value!.artifacts
    .filter((artifact) => artifact.detail.human_only_counts_are_lower_bounds === true)
    .map((artifact) => artifact.artifact_type),
)

const copiedIdentifier = ref<string | null>(null)
const copyFailedIdentifier = ref<string | null>(null)
let copyTimeout: ReturnType<typeof setTimeout> | null = null

onBeforeUnmount(() => {
  clearTimeout(copyTimeout ?? undefined)
})

async function copyIdentifier(identifier: string): Promise<void> {
  copyFailedIdentifier.value = null
  try {
    await navigator.clipboard.writeText(identifier)
    copiedIdentifier.value = identifier
    clearTimeout(copyTimeout ?? undefined)
    copyTimeout = setTimeout(() => {
      copiedIdentifier.value = null
    }, 2000)
  } catch (error) {
    // `navigator.clipboard` is undefined outside a secure context (a plain
    // HTTP self-hosted instance), so a failed copy must be visible: the raw
    // identifier stays selectable in the detail disclosure.
    console.warn('Failed to copy evidence identifier', error)
    copiedIdentifier.value = null
    copyFailedIdentifier.value = identifier
  }
}

/**
 * The backend builds `identifiers` and `detail.sample_pipeline_ids` from the
 * SAME ordered rows, so index i in one names the pipeline of index i in the
 * other. When the detail is missing (or a future payload drops it) the label
 * still renders - only the navigation link is omitted rather than guessing.
 */
function samplePipelineIds(artifact: ArtifactEntry): (string | undefined)[] {
  const value = artifact.detail.sample_pipeline_ids
  // Map (not filter) so a non-string entry becomes undefined IN PLACE: the
  // pairing is positional, and compacting the array would shift every later
  // identifier onto the wrong pipeline (a misattribution, not a missing link).
  return Array.isArray(value) ? value.map((item) => (typeof item === 'string' ? item : undefined)) : []
}

function identifierDisplays(artifact: ArtifactEntry): IdentifierDisplay[] {
  const pipelineIds = samplePipelineIds(artifact)
  return artifact.identifiers.map((raw, index) => {
    const match = PIPELINE_SNAPSHOT_IDENTIFIER.exec(raw)
    if (!match) return { raw, label: null, reference: null, href: null, version: null }
    const snapshotId = match[1]
    const version = Number(match[2])
    const pipelineId = pipelineIds[index]
    return {
      raw,
      label: $t('views.ComplianceEvidenceIndex.snapshot_label', { version }),
      reference: shortId(snapshotId),
      href: pipelineId
        ? `/pipelines/${encodeURIComponent(pipelineId)}/editor?snapshot=${snapshotId}`
        : null,
      version,
    }
  })
}

function identifierCopyLabel(display: IdentifierDisplay): string {
  return display.raw === copiedIdentifier.value
    ? $t('views.ComplianceEvidenceIndex.identifier_copied_label', { reference: display.reference })
    : $t('views.ComplianceEvidenceIndex.identifier_copy_label', { reference: display.reference })
}

function statusClass(status: string): string {
  if (status === 'present') return 'border-success/40 bg-success/10 text-success-text'
  if (status === 'customer_supplied') return 'border-primary/40 bg-primary/10 text-primary'
  return 'border-warning/50 bg-warning/10 text-warning-text'
}

const statusTranslationKeys: Record<string, string> = {
  present: 'views.ComplianceEvidenceIndex.status_present',
  absent: 'views.ComplianceEvidenceIndex.status_absent',
  customer_supplied: 'views.ComplianceEvidenceIndex.status_customer_supplied',
}

const provenanceTranslationKeys: Record<string, string> = {
  observed: 'views.ComplianceEvidenceIndex.provenance_observed',
  customer_supplied: 'views.ComplianceEvidenceIndex.provenance_customer_supplied',
  derived: 'views.ComplianceEvidenceIndex.provenance_derived',
}

function localizedValue(value: string, translationKeys: Record<string, string>): string {
  const translationKey = translationKeys[value]
  return translationKey
    ? $t(translationKey)
    : $t('views.ComplianceEvidenceIndex.unknown_value', { value })
}

function statusLabel(status: string): string {
  return localizedValue(status, statusTranslationKeys)
}

function provenanceLabel(provenance: string): string {
  return localizedValue(provenance, provenanceTranslationKeys)
}

function formatTimestamp(value: string): string {
  const timestamp = new Date(value)
  if (Number.isNaN(timestamp.getTime())) return value
  return timestamp.toLocaleString()
}

void load()
</script>
