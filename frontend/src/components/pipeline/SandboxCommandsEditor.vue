<template>
  <div class="space-y-3" data-testid="pipeline-editor-node-commands-editor">
    <!-- Single (scalar) command — mutually exclusive with the list below -->
    <div>
      <label for="pipeline-editor-node-command-scalar" class="block text-xs font-medium">{{ $t('views.PipelineEditorView.commands_single') }}</label>
      <input
        id="pipeline-editor-node-command-scalar"
        :value="scalarModel"
        type="text"
        class="mt-1 w-full rounded-lg border border-input bg-background px-2 py-1 font-mono text-xs"
        :disabled="listActive"
        :placeholder="$t('views.PipelineEditorView.commands_single_placeholder')"
        :aria-label="$t('views.PipelineEditorView.commands_single')"
        data-testid="pipeline-editor-node-command-scalar"
        @input="onScalarInput"
      />
      <p class="mt-0.5 text-[11px] text-muted-foreground">
        {{ listActive ? $t('views.PipelineEditorView.commands_single_disabled_hint') : $t('views.PipelineEditorView.commands_single_hint') }}
      </p>
    </div>

    <!-- Commands list — one row per command -->
    <div :class="{ 'opacity-60': scalarActive }">
      <div class="flex items-center justify-between gap-2">
        <span class="block text-xs font-medium">{{ $t('views.PipelineEditorView.commands') }}</span>
        <button
          type="button"
          class="shrink-0 rounded border border-input bg-background px-2 py-0.5 text-[11px] hover:bg-accent disabled:cursor-not-allowed disabled:opacity-50"
          :disabled="scalarActive"
          :aria-label="$t('views.PipelineEditorView.commands_add')"
          data-testid="pipeline-editor-node-command-add"
          @click="addRow"
        >{{ $t('views.PipelineEditorView.commands_add') }}</button>
      </div>
      <p class="mt-0.5 text-[11px] text-muted-foreground">
        {{ scalarActive ? $t('views.PipelineEditorView.commands_list_disabled_hint') : $t('views.PipelineEditorView.commands_list_hint') }}
      </p>
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
            :disabled="scalarActive"
            :aria-label="$t('views.PipelineEditorView.commands_row_label', { n: idx + 1 })"
            :data-testid="`pipeline-editor-node-command-row-${idx}`"
            @input="onRowInput(idx, $event)"
          />
          <span class="flex shrink-0 items-center gap-0.5">
            <button
              type="button"
              class="rounded px-1 py-0.5 text-[10px] text-muted-foreground hover:bg-accent hover:text-foreground disabled:cursor-not-allowed disabled:opacity-40"
              :disabled="idx === 0 || scalarActive"
              :aria-label="$t('views.PipelineEditorView.commands_row_move_up', { n: idx + 1 })"
              :data-testid="`pipeline-editor-node-command-up-${idx}`"
              @click="moveRow(idx, -1)"
            >
              <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m18 15-6-6-6 6"/></svg>
            </button>
            <button
              type="button"
              class="rounded px-1 py-0.5 text-[10px] text-muted-foreground hover:bg-accent hover:text-foreground disabled:cursor-not-allowed disabled:opacity-40"
              :disabled="idx === rows.length - 1 || scalarActive"
              :aria-label="$t('views.PipelineEditorView.commands_row_move_down', { n: idx + 1 })"
              :data-testid="`pipeline-editor-node-command-down-${idx}`"
              @click="moveRow(idx, 1)"
            >
              <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg>
            </button>
            <button
              type="button"
              class="rounded px-1 py-0.5 text-[10px] text-muted-foreground hover:bg-accent hover:text-destructive disabled:cursor-not-allowed disabled:opacity-40"
              :disabled="scalarActive"
              :aria-label="$t('views.PipelineEditorView.commands_row_remove', { n: idx + 1 })"
              :data-testid="`pipeline-editor-node-command-remove-${idx}`"
              @click="removeRow(idx)"
            >&times;</button>
          </span>
        </li>
      </ol>

      <!-- Join operator + effective-command preview (only meaningful for a list) -->
      <div v-if="listActive" class="mt-2 space-y-1">
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
  scalarCommand?: string | null
  commands?: string[] | null
  joiner?: string | null
}>()

const emit = defineEmits<{
  (e: 'update:scalarCommand', value: string): void
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

const scalarModel = computed(() => props.scalarCommand ?? '')
const joinerModel = computed(() => props.joiner ?? '')

const listActive = computed(() => rows.value.some((c) => c.trim() !== ''))
const scalarActive = computed(() => scalarModel.value.trim() !== '')

// Backend semantics (routes/agents.py create/update XOR; the runtime resolves
// the list first): a non-empty list clears the scalar command.
watch(
  listActive,
  (active) => {
    if (active && scalarActive.value) {
      emit('update:scalarCommand', '')
    }
  },
  { immediate: true },
)

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

function onScalarInput(event: Event) {
  emit('update:scalarCommand', (event.target as HTMLInputElement).value)
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
