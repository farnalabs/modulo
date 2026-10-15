<template>
  <!-- FAR-1812: version indicator chip. Shows `v{n}` only (no content hash —
       a hash would need a new DB column plus a backfill for no user-visible
       gain, ADR 061). Opens on hover AND on keyboard focus so the metadata is
       reachable without a pointer; the popover is a child of this wrapper so
       moving into it never closes the panel (mouseleave/focusout only fire
       when the pointer/focus leaves the whole wrapper). The wrapper itself
       is not interactive — its inner button is — so the container-level
       hover/focus handlers are exempted the same way the editor's modal
       backdrop is. -->
  <!-- eslint-disable-next-line vuejs-accessibility/no-static-element-interactions -->
  <div
    ref="wrapperRef"
    class="relative"
    @mouseenter="show"
    @mouseleave="hide"
    @focusin="show"
    @focusout="onFocusOut"
    @keydown="onKeydown"
  >
    <button
      type="button"
      :class="chipClass"
      :aria-expanded="open"
      aria-haspopup="dialog"
      :aria-label="$t('components.PipelineVersionChip.chip_aria', { version: snapshot.snapshot_version })"
      data-testid="pipeline-editor-version-chip"
      @click="show"
    >
      v{{ snapshot.snapshot_version }}
    </button>
    <div
      v-if="open"
      role="dialog"
      :aria-label="$t('components.PipelineVersionChip.popover_aria', { version: snapshot.snapshot_version })"
      class="absolute left-0 top-full z-30 mt-1 w-64 rounded-lg border bg-card p-3 text-xs shadow-lg"
      data-testid="pipeline-editor-version-chip-popover"
    >
      <dl class="space-y-1">
        <div class="flex items-baseline justify-between gap-2">
          <dt class="text-muted-foreground">{{ $t('components.PipelineVersionChip.created_label') }}</dt>
          <dd class="text-right font-medium">{{ createdLabel }}</dd>
        </div>
        <div class="flex items-baseline justify-between gap-2">
          <dt class="text-muted-foreground">{{ $t('components.PipelineVersionChip.kind_label') }}</dt>
          <dd class="text-right font-medium" data-testid="pipeline-editor-version-chip-kind">{{ kindLabel }}</dd>
        </div>
        <div class="flex items-baseline justify-between gap-2">
          <dt class="text-muted-foreground">{{ $t('components.PipelineVersionChip.author_label') }}</dt>
          <dd class="text-right font-medium" data-testid="pipeline-editor-version-chip-author">{{ authorLabel }}</dd>
        </div>
        <div class="flex items-baseline justify-between gap-2">
          <dt class="text-muted-foreground">{{ $t('components.PipelineVersionChip.tag_label') }}</dt>
          <dd class="break-all text-right font-medium">{{ snapshot.tag || $t('components.PipelineVersionChip.no_value') }}</dd>
        </div>
        <div class="flex items-baseline justify-between gap-2">
          <dt class="text-muted-foreground">{{ $t('components.PipelineVersionChip.notes_label') }}</dt>
          <dd class="break-all text-right font-medium">{{ snapshot.notes || $t('components.PipelineVersionChip.no_value') }}</dd>
        </div>
      </dl>
      <button
        type="button"
        class="mt-2 w-full rounded-md border border-input bg-background px-2 py-1 text-left text-xs font-medium hover:bg-accent"
        data-testid="pipeline-editor-version-chip-open-timeline"
        @click="emit('open-timeline')"
      >
        {{ $t('components.PipelineVersionChip.open_timeline') }}
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { formatDateShort } from '../../lib/formatDate'
import { shortId } from '../../utils/format'

/** Shape the chip consumes — structurally satisfied by SnapshotDetailResponse. */
interface VersionChipSnapshot {
  snapshot_version: number
  created_at: string | null
  created_kind: string
  created_by?: string | null
  tag: string | null
  notes: string | null
}

const props = defineProps<{
  snapshot: VersionChipSnapshot
  /** 'viewing' tints the chip amber (a historical version is on screen). */
  mode: 'latest' | 'viewing'
}>()

const emit = defineEmits<{
  'open-timeline': []
}>()

const { t } = useI18n()

const open = ref(false)
const wrapperRef = ref<HTMLElement | null>(null)

const chipClass = computed(() =>
  props.mode === 'viewing'
    ? 'inline-flex h-6 shrink-0 items-center rounded border border-warning/40 bg-warning/10 px-1.5 font-mono text-[11px] font-medium text-warning-text'
    : 'inline-flex h-6 shrink-0 items-center rounded border border-input bg-background px-1.5 font-mono text-[11px] font-medium hover:bg-accent',
)

const createdLabel = computed(() => formatDateShort(props.snapshot.created_at))

// created_kind is a closed server vocabulary (initial/edit/rollback/run) with
// a known evolution ahead of it, so an unknown future value falls back to the
// raw value (STATE-6) instead of rendering the i18n key or an empty cell.
const CREATED_KIND_KEYS: Record<string, string> = {
  initial: 'components.PipelineVersionChip.kind_initial',
  edit: 'components.PipelineVersionChip.kind_edit',
  rollback: 'components.PipelineVersionChip.kind_rollback',
  run: 'components.PipelineVersionChip.kind_run',
}

const kindLabel = computed(() => {
  const key = CREATED_KIND_KEYS[props.snapshot.created_kind]
  if (key) return t(key)
  return props.snapshot.created_kind || t('components.PipelineVersionChip.no_value')
})

// The snapshot API exposes the author as an account id; there is no general
// member-directory endpoint every viewer may call, so the id renders short
// (same precedent as the lifecycle-map version picker) rather than a raw UUID.
const authorLabel = computed(() => {
  const createdBy = props.snapshot.created_by
  return createdBy ? shortId(createdBy) : t('components.PipelineVersionChip.no_value')
})

function show(): void {
  open.value = true
}

function hide(): void {
  open.value = false
}

function onFocusOut(event: FocusEvent): void {
  const next = event.relatedTarget as Node | null
  // Keep the popover open while focus moves between the chip and its own
  // controls (clicking "Open in timeline" must not unmount it first).
  if (next && wrapperRef.value?.contains(next)) return
  open.value = false
}

function onKeydown(event: KeyboardEvent): void {
  // Container-level handler: never swallow keys typed into form controls
  // (there are none inside this popover today, but the guard keeps the rule
  // structural if one is ever added).
  const target = event.target as HTMLElement | null
  if (target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA')) return
  if (event.key === 'Escape' && open.value) {
    event.preventDefault()
    open.value = false
  }
}
</script>
