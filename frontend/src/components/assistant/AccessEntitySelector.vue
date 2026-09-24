<template>
  <div class="space-y-3">
    <div class="relative">
      <input :aria-label="placeholder"
        ref="inputRef"
        v-model="query"
        type="text"
        class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        :placeholder="placeholder"
        :data-testid="testId"
        @focus="isOpen = true"
        @blur="onBlur"
        @keydown.escape="isOpen = false"
        @keydown.enter.prevent="selectFirst"
      />
      <div
        v-if="isOpen && filteredEntities.length > 0"
        class="absolute z-20 mt-1 max-h-48 w-full overflow-y-auto rounded-lg border bg-popover text-popover-foreground shadow-lg"
      >
        <button
          v-for="entity in filteredEntities"
          :key="entity.id"
          type="button"
          class="flex w-full items-center gap-2 px-3 py-2 text-left text-sm hover:bg-accent transition-colors"
          @mousedown.prevent="select(entity)"
        >
          <span class="font-medium">{{ displayLabel(entity) }}</span>
          <span class="text-muted-foreground">{{ displayDescription(entity) }}</span>
        </button>
      </div>
      <p
        v-else-if="isOpen && query.trim().length > 0 && filteredEntities.length === 0"
        class="absolute z-20 mt-1 w-full rounded-lg border bg-popover px-3 py-2 text-sm text-muted-foreground shadow-lg"
      >
        {{ noResultsText }}
      </p>
    </div>

    <div v-if="selectedEntities.length === 0" class="rounded-lg border border-dashed p-4 text-center text-sm text-muted-foreground">
      {{ emptyText }}
    </div>
    <div v-else class="overflow-hidden rounded-lg border">
      <table class="w-full text-left text-sm">
        <thead>
          <tr>
            <th scope="col" class="sr-only">{{ labelField }}</th>
            <th v-if="descriptionField" scope="col" class="sr-only">{{ descriptionField }}</th>
            <th scope="col" class="sr-only">{{ $t('common.remove') }}</th>
          </tr>
        </thead>
        <tbody class="divide-y">
          <tr
            v-for="entity in selectedEntities"
            :key="entity.id"
            class="hover:bg-muted/30 transition-colors"
          >
            <td class="px-3 py-2 font-medium">{{ displayLabel(entity) }}</td>
            <td v-if="descriptionField" class="px-3 py-2 text-muted-foreground">
              {{ displayDescription(entity) }}
            </td>
            <td class="px-3 py-2 text-right">
              <button
                type="button"
                class="rounded p-1 text-muted-foreground hover:text-destructive transition-colors"
                :aria-label="`Remove ${displayLabel(entity)}`"
                :title="`Remove ${displayLabel(entity)}`"
                @click="remove(entity.id)"
              >
                <svg class="h-4 w-4" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <path d="M18 6 6 18" /><path d="m6 6 12 12" />
                </svg>
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onUnmounted } from 'vue'

const props = withDefaults(defineProps<{
  modelValue: string[]
  entities?: Array<Record<string, any>>
  labelField: string
  descriptionField?: string
  placeholder?: string
  noResultsText?: string
  emptyText?: string
  testId?: string
}>(), {
  entities: () => [],
  descriptionField: '',
  placeholder: 'Search...',
  noResultsText: 'No results found',
  emptyText: 'No items selected',
  testId: '',
})

const emit = defineEmits<{
  (e: 'update:modelValue', value: string[]): void
}>()

const query = ref('')
const isOpen = ref(false)
const inputRef = ref<HTMLInputElement | null>(null)

const selectedIds = computed({
  get: () => props.modelValue,
  set: (val) => emit('update:modelValue', val),
})

const selectedSet = computed(() => new Set(selectedIds.value))

const availableEntities = computed(() =>
  props.entities.filter(e => !selectedSet.value.has(e.id))
)

const filteredEntities = computed(() => {
  const q = query.value.trim().toLowerCase()
  if (!q) return availableEntities.value
  return availableEntities.value.filter(e => {
    const label = (e[props.labelField] || '').toLowerCase()
    const desc = props.descriptionField ? (e[props.descriptionField] || '').toLowerCase() : ''
    return label.includes(q) || desc.includes(q)
  })
})

const selectedEntities = computed(() =>
  props.entities.filter(e => selectedSet.value.has(e.id))
)

function displayLabel(entity: Record<string, any>): string {
  return entity[props.labelField] || entity.id
}

function displayDescription(entity: Record<string, any>): string {
  return props.descriptionField && entity[props.descriptionField]
    ? `(${entity[props.descriptionField]})`
    : ''
}

function select(entity: Record<string, any>) {
  if (selectedSet.value.has(entity.id)) return
  selectedIds.value = [...selectedIds.value, entity.id]
  query.value = ''
  inputRef.value?.focus()
}

function selectFirst() {
  if (filteredEntities.value.length > 0) {
    select(filteredEntities.value[0])
  }
}

function remove(id: string) {
  selectedIds.value = selectedIds.value.filter(sid => sid !== id)
}

let blurTimer: ReturnType<typeof setTimeout> | null = null

function onBlur() {
  if (blurTimer) clearTimeout(blurTimer)
  blurTimer = setTimeout(() => { isOpen.value = false }, 180)
}

onUnmounted(() => {
  if (blurTimer) clearTimeout(blurTimer)
})
</script>
