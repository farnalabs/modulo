<template>
  <!-- FAR-1813: the pipeline NAME on a run surface, linked to the pipeline
       VERSION the run executed (/pipelines/{id}/editor?snapshot={snapshot_id}).
       A real RouterLink — focusable, keyboard-reachable, no click handler on a
       non-interactive element. -->
  <router-link
    v-if="linkTo"
    :to="linkTo"
    :title="linkTitle"
    class="hover:underline"
  >
    <slot>{{ pipelineName }}</slot>
    <!-- FAR-1813: the `v{n}` version chip lives INSIDE the link so the whole
         thing is one tab stop with one accessible name ("my-pipeline v3"),
         never a focusable chip or orphan version text after the link. -->
    <span
      v-if="versionLabel"
      data-testid="run-pipeline-version-chip"
      :title="versionChipTitle ?? undefined"
      class="ml-1 inline-block rounded bg-muted px-1 align-middle font-mono text-[0.65em] leading-relaxed text-muted-foreground no-underline"
    >{{ versionLabel }}</span>
  </router-link>
  <!-- No editor to open: a deleted pipeline (pipeline_name NULL) or a run with
       no pipeline id renders the caller's text as plain content — never a dead
       link. The chip still renders here (non-interactive): the version
       describes the RUN, which stays honest even when the pipeline row is
       gone. -->
  <span v-else>
    <slot>{{ pipelineName }}</slot>
    <span
      v-if="versionLabel"
      data-testid="run-pipeline-version-chip"
      :title="versionChipTitle ?? undefined"
      class="ml-1 inline-block rounded bg-muted px-1 align-middle font-mono text-[0.65em] leading-relaxed text-muted-foreground no-underline"
    >{{ versionLabel }}</span>
  </span>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'

const props = defineProps<{
  /** The run's pipeline id. Missing => no link target exists. */
  pipelineId?: string | null
  /**
   * The run's pipeline name. NULL means the pipeline row is gone (deleted) —
   * its editor cannot be opened, so the component renders plain content
   * instead of linking into a dead end.
   */
  pipelineName?: string | null
  /**
   * The run's OWN snapshot_id (null for legacy/pre-snapshot runs). Always
   * taken from the run being rendered: a rerun reuses its source run's
   * snapshot, so the link names the version actually re-executed — never the
   * pipeline head, and never derived from any other run.
   */
  snapshotId?: string | null
  /**
   * The run's snapshot VERSION (RunResponse.snapshot_version). NULL for a
   * legacy/pre-snapshot run — no chip is rendered at all: absence is the
   * honest representation of a run with no recorded version, never an empty
   * chip shell or a placeholder like `v0`.
   */
  snapshotVersion?: number | null
}>()

const { t } = useI18n()

/**
 * The chip renders ONLY for a positive integer version. Everything else a
 * payload could carry — null, undefined, a boolean, a numeric string, NaN,
 * Infinity, a non-integer or 0/negative — resolves to null so no chip is
 * drawn: never `v0`, `vnull`, `vNaN` or `vundefined`.
 */
function coerceVersion(value: unknown): number | null {
  if (typeof value !== 'number') return null // bool, numeric string, null, undefined
  if (!Number.isFinite(value)) return null // NaN / ±Infinity
  if (!Number.isInteger(value)) return null // 3.5
  if (value < 1) return null // 0 or negative — there is no "version 0"
  return value
}

const version = computed<number | null>(() => coerceVersion(props.snapshotVersion))

const versionLabel = computed<string | null>(() =>
  version.value === null
    ? null
    : t('components.RunPipelineVersionLink.version_chip', { version: version.value }),
)

const versionChipTitle = computed<string | null>(() =>
  version.value === null
    ? null
    : t('components.RunPipelineVersionLink.version_chip_title', { version: version.value }),
)

const linkTo = computed<string | null>(() => {
  if (!props.pipelineId || !props.pipelineName) return null
  const editor = `/pipelines/${props.pipelineId}/editor`
  // Legacy run (snapshot_id NULL): no historic version exists, so the live
  // editor is the honest destination — the title says so rather than claiming
  // a version this run has no record of.
  return props.snapshotId ? `${editor}?snapshot=${props.snapshotId}` : editor
})

const linkTitle = computed<string>(() =>
  props.snapshotId
    ? t('components.RunPipelineVersionLink.version_title')
    : t('components.RunPipelineVersionLink.current_title'),
)
</script>
