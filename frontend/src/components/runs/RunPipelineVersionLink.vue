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
  </router-link>
  <!-- No editor to open: a deleted pipeline (pipeline_name NULL) or a run with
       no pipeline id renders the caller's text as plain content — never a dead
       link. -->
  <span v-else>
    <slot>{{ pipelineName }}</slot>
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
}>()

const { t } = useI18n()

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
