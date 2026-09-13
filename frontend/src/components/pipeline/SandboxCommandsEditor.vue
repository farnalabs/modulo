<template>
  <div class="space-y-3" data-testid="pipeline-editor-node-commands-editor">
    <!-- Commands list — one row per command -->
    <div>
      <div class="flex items-center justify-between gap-2">
        <span class="block text-xs font-medium">{{ $t('views.PipelineEditorView.commands') }}</span>
        <button
          type="button"
          class="shrink-0 rounded border border-input bg-background px-2 py-0.5 text-[11px] hover:bg-accent"
          :aria-label="$t('views.PipelineEditorView.commands_add')"
          data-testid="pipeline-editor-node-command-add"
          @click="addRow"
        >{{ $t('views.PipelineEditorView.commands_add') }}</button>
      </div>
      <p class="mt-0.5 text-[11px] text-muted-foreground">{{ $t('views.PipelineEditorView.commands_list_hint') }}</p>
      <p
        v-if="rows.length === 0"
        class="mt-1 text-[11px] italic text-muted-foreground"
        data-testid="pipeline-editor-node-command-empty"
      >{{ $t('views.PipelineEditorView.commands_list_empty') }}</p>
      <ol v-else class="mt-1 space-y-1">
        <li v-for="(row, idx) in rows" :key="idx" class="flex items-center gap-1">
          <span class="w-4 shrink-0 text-right text-[10px] text-muted-foreground">{{ idx + 1 }}</span>
          <input
            :value="row"
            type="text"
            class="min-w-0 flex-1 rounded border border-input bg-background px-2 py-1 font-mono text-xs"
            :aria-label="$t('views.PipelineEditorView.commands_row_label', { n: idx + 1 })"
            :data-testid="`pipeline-editor-node-command-row-${idx}`"
            @input="onRowInput(idx, $event)"
          />
          <span class="flex shrink-0 items-center gap-0.5">
            <button
              type="button"
              class="rounded px-1 py-0.5 text-[10px] text-muted-foreground hover:bg-accent hover:text-foreground disabled:cursor-not-allowed disabled:opacity-40"
              :disabled="idx === 0"
              :aria-label="$t('views.PipelineEditorView.commands_row_move_up', { n: idx + 1 })"
              :data-testid="`pipeline-editor-node-command-up-${idx}`"
              @click="moveRow(idx, -1)"
            >
              <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m18 15-6-6-6 6"/></svg>
            </button>
            <button
              type="button"
              class="rounded px-1 py-0.5 text-[10px] text-muted-foreground hover:bg-accent hover:text-foreground disabled:cursor-not-allowed disabled:opacity-40"
              :disabled="idx === rows.length - 1"
              :aria-label="$t('views.PipelineEditorView.commands_row_move_down', { n: idx + 1 })"
              :data-testid="`pipeline-editor-node-command-down-${idx}`"
              @click="moveRow(idx, 1)"
            >
              <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg>
            </button>
            <button
              type="button"
              class="rounded px-1 py-0.5 text-[10px] text-muted-foreground hover:bg-accent hover:text-destructive"
              :aria-label="$t('views.PipelineEditorView.commands_row_remove', { n: idx + 1 })"
              :data-testid="`pipeline-editor-node-command-remove-${idx}`"
              @click="removeRow(idx)"
            >&times;</button>
          </span>
        </li>
      </ol>

      <!-- Join operator + effective-command preview -->
      <div class="mt-2 space-y-1">
        <div>
          <label for="pipeline-editor-node-command-joiner" class="block text-xs font-medium">{{ $t('views.PipelineEditorView.commands_join_operator') }}</label>
          <input
            id="pipeline-editor-node-command-joiner"
            :value="joinerModel"
            type="text"
            class="mt-1 w-full rounded-lg border border-input bg-background px-2 py-1 font-mono text-xs"
            :placeholder="$t('views.PipelineEditorView.commands_join_operator_placeholder')"
            :aria-label="$t('views.PipelineEditorView.commands_join_operator')"
            data-testid="pipeline-editor-node-command-joiner"
            @input="onJoinerInput"
          />
          <p class="mt-0.5 text-[11px] text-muted-foreground">{{ $t('views.PipelineEditorView.commands_join_operator_hint') }}</p>
        </div>
        <div v-if="effectiveCommand" data-testid="pipeline-editor-node-command-preview">
          <span class="block text-xs font-medium">{{ $t('views.PipelineEditorView.commands_effective_preview') }}</span>
          <p class="mt-0.5 break-all font-mono text-[11px] text-muted-foreground">{{ effectiveCommand }}</p>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'

const DEFAULT_JOINER = ' && '

const props = defineProps<{
  commands?: string[] | null
  joiner?: string | null
}>()

const emit = defineEmits<{
  (e: 'update:commands', value: string[]): void
  (e: 'update:joiner', value: string): void
}>()

// Local row buffer so in-progress (possibly empty) rows survive prop
// round-trips; the save path filters empty rows, this editor never does.
const rows = ref<string[]>([])

watch(
  () => props.commands,
  (cmds) => {
    const next = Array.isArray(cmds) ? [...cmds] : []
    if (JSON.stringify(next) !== JSON.stringify(rows.value)) {
      rows.value = next
    }
  },
  { immediate: true },
)

const joinerModel = computed(() => props.joiner ?? '')

function emitCommands() {
  emit('update:commands', [...rows.value])
}

function addRow() {
  rows.value.push('')
  emitCommands()
}

function removeRow(idx: number) {
  rows.value.splice(idx, 1)
  emitCommands()
}

function moveRow(idx: number, direction: -1 | 1) {
  const target = idx + direction
  if (target < 0 || target >= rows.value.length) return
  const next = [...rows.value]
  ;[next[idx], next[target]] = [next[target], next[idx]]
  rows.value = next
  emitCommands()
}

function onRowInput(idx: number, event: Event) {
  rows.value[idx] = (event.target as HTMLInputElement).value
  emitCommands()
}

function onJoinerInput(event: Event) {
  emit('update:joiner', (event.target as HTMLInputElement).value)
}

// Read-only preview of what the pipeline will actually run (the runtime joins
// the list with the joiner; empty joiner falls back to the default).
const effectiveCommand = computed(() => {
  const cmds = rows.value.filter((c) => c.trim() !== '')
  if (cmds.length === 0) return ''
  const joiner = joinerModel.value.length > 0 ? joinerModel.value : DEFAULT_JOINER
  return cmds.join(joiner)
})
</script>
