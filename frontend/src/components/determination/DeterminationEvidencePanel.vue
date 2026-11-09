<template>
  <!--
    The drill-down is a native modal `<dialog>`: showModal() puts it in the top
    layer, makes the page behind it inert and contains focus, so the panel is
    keyboard-reachable without a hand-rolled focus trap. It is deliberately not
    teleported - a modal dialog is top-layer wherever it sits in the document,
    and keeping it in the panel's own tree means the trigger button is still a
    DOM sibling for focus restoration.
  -->
  <dialog
    v-if="element"
    ref="dialogRef"
    class="mx-auto mt-[8vh] w-full max-w-2xl rounded-lg border bg-background p-0 text-foreground shadow-xl backdrop:bg-black/50"
    :aria-labelledby="titleId"
    data-testid="determination-evidence-panel"
    @click="onBackdropClick"
    @keydown.escape.prevent="requestClose"
    @close="onDialogClose"
  >
    <header class="flex flex-wrap items-start justify-between gap-3 border-b p-4">
      <div class="space-y-1">
        <h2 :id="titleId" class="text-lg font-semibold">
          {{ $t('views.DeterminationView.evidence_panel_title') }}
        </h2>
        <p class="text-xs text-muted-foreground">{{ $t('views.DeterminationView.evidence_panel_hint') }}</p>
      </div>
      <button
        type="button"
        class="rounded-md border px-3 py-1.5 text-sm font-medium"
        data-testid="determination-evidence-panel-close"
        @click="requestClose"
      >
        {{ $t('views.DeterminationView.evidence_panel_close') }}
      </button>
    </header>

    <div class="max-h-[70vh] space-y-4 overflow-y-auto p-4">
      <div class="space-y-2" aria-live="polite">
        <div class="flex flex-wrap items-center gap-2">
          <span
            class="rounded-full px-2 py-0.5 text-xs font-medium"
            :class="elementKindBadgeClass(element.kind)"
            data-testid="determination-evidence-panel-kind"
          >
            {{ $t('views.DeterminationView.element_kind_' + element.kind) }}
          </span>
          <span
            class="rounded-full px-2 py-0.5 text-xs font-medium"
            :class="confidenceBadgeClass(element.confidence)"
            data-testid="determination-evidence-panel-confidence"
          >
            {{ $t('views.DeterminationView.confidence_' + element.confidence) }}
          </span>
        </div>
        <p class="text-sm font-medium" data-testid="determination-evidence-panel-statement">
          {{ element.statement }}
        </p>
        <p class="text-xs text-muted-foreground">
          {{ $t('views.DeterminationView.evidence_panel_confidence') }}:
          {{ $t('views.DeterminationView.confidence_' + element.confidence) }}
        </p>
      </div>

      <section class="space-y-1">
        <h3 class="text-sm font-medium">{{ $t('views.DeterminationView.evidence_panel_uncertainty') }}</h3>
        <p
          v-if="uncertainty.withheld"
          class="text-sm italic text-muted-foreground"
          data-testid="determination-evidence-panel-uncertainty-withheld"
        >
          {{ $t('views.DeterminationView.evidence_value_withheld') }}
        </p>
        <p
          v-else-if="uncertainty.text"
          class="text-sm text-muted-foreground"
          data-testid="determination-evidence-panel-uncertainty"
        >
          {{ uncertainty.text }}
        </p>
        <p
          v-else
          class="text-sm text-muted-foreground"
          data-testid="determination-evidence-panel-uncertainty-empty"
        >
          {{ $t('views.DeterminationView.evidence_panel_uncertainty_empty') }}
        </p>
      </section>

      <section class="space-y-1" aria-live="polite">
        <h3 class="text-sm font-medium">
          {{ $t('views.DeterminationView.evidence_panel_references') }}
          <span class="text-muted-foreground">
            ({{ $t('views.DeterminationView.evidence_panel_reference_count', { count: element.evidenceRefs.length }) }})
          </span>
        </h3>
        <DeterminationEvidenceRefs :references="element.evidenceRefs" />
      </section>
    </div>
  </dialog>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import DeterminationEvidenceRefs from './DeterminationEvidenceRefs.vue'
import { confidenceBadgeClass } from './confidencePresentation'
import {
  displayEvidenceText,
  elementKindBadgeClass,
  type EvidenceElementView,
} from './evidencePresentation'

const props = defineProps<{ element: EvidenceElementView | null }>()

const emit = defineEmits<{ (event: 'close'): void }>()

const titleId = 'determination-evidence-panel-title'
const dialogRef = ref<HTMLDialogElement | null>(null)

const uncertainty = computed(() => displayEvidenceText(props.element?.uncertainty ?? ''))

watch(
  () => props.element,
  async (element) => {
    await nextTick()
    const dialog = dialogRef.value
    if (element && dialog && !dialog.open) {
      dialog.showModal()
    } else if (!element && dialog?.open) {
      dialog.close()
    }
  },
  { immediate: true },
)

/**
 * Close the panel from a button, the backdrop or Escape. Emitting here keeps
 * the parent's selection in sync immediately; the browser's own asynchronous
 * `close` event then finds the element already cleared and stays quiet.
 */
function requestClose(): void {
  const dialog = dialogRef.value
  if (dialog?.open) dialog.close()
  emit('close')
}

/** The browser closed the dialog itself (e.g. a native cancel). */
function onDialogClose(): void {
  if (props.element) emit('close')
}

/** A click whose target is the dialog itself is a click on ::backdrop. */
function onBackdropClick(event: MouseEvent): void {
  if (event.target === event.currentTarget) requestClose()
}
</script>
