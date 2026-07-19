<script setup lang="ts">
import { ref, computed } from 'vue'
import type { SchemaField } from '../../../types/pipeline'
import { Button } from '../../ui/button'
import { Select, SelectTrigger, SelectContent, SelectItem, SelectValue } from '../../ui/select'

const props = defineProps<{
  sourceFields: SchemaField[]
  targetFields: SchemaField[]
  mappings: Record<string, string>
  direction: 'input' | 'output'
}>()

const emit = defineEmits<{
  (e: 'update:mappings', mappings: Record<string, string>): void
}>()

const showPicker = ref(false)
const selectedSource = ref('')
const selectedTarget = ref('')

const isPassthrough = computed(() => {
  if (props.sourceFields.length !== props.targetFields.length) return false
  return props.sourceFields.every((sf, i) => {
    const tf = props.targetFields[i]
    return tf && sf.name === tf.name && sf.type === tf.type
  })
})

const unmappedSource = computed(() => {
  const mappedKeys = new Set(Object.keys(props.mappings))
  return props.sourceFields.filter(f => !mappedKeys.has(f.name))
})

const unmappedTarget = computed(() => {
  const mappedKeys = new Set(Object.values(props.mappings))
  return props.targetFields.filter(f => !mappedKeys.has(f.name))
})

const pickerSourceFields = computed(() => {
  return props.sourceFields.filter(f => !Object.keys(props.mappings).includes(f.name))
})

const pickerTargetFields = computed(() => {
  return props.targetFields.filter(f => !Object.values(props.mappings).includes(f.name))
})

function autoMap() {
  const auto: Record<string, string> = {}
  for (const sf of props.sourceFields) {
    const match = props.targetFields.find(tf => tf.name === sf.name && tf.type === sf.type)
    if (match) {
      auto[sf.name] = match.name
    }
  }
  emit('update:mappings', { ...props.mappings, ...auto })
}

function clearMapping() {
  emit('update:mappings', {})
}

function addMapping() {
  if (selectedSource.value && selectedTarget.value) {
    emit('update:mappings', {
      ...props.mappings,
      [selectedSource.value]: selectedTarget.value,
    })
    selectedSource.value = ''
    selectedTarget.value = ''
    showPicker.value = false
  }
}

function removeMapping(sourceKey: string) {
  const updated = { ...props.mappings }
  delete updated[sourceKey]
  emit('update:mappings', updated)
}
</script>

<template>
  <div class="space-y-3">
    <div v-if="isPassthrough" class="flex items-center gap-2 rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-3 py-2">
      <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="text-emerald-400">
        <polyline points="22 12 18 12 15 21 9 3 6 12 2 12" />
      </svg>
      <span class="text-xs font-medium text-emerald-400">Passthrough</span>
      <span class="text-xs text-muted-foreground">Schemas are identical — no mapping needed</span>
    </div>

    <div v-if="!isPassthrough" class="space-y-2">
      <div class="flex items-center justify-between">
        <span class="text-xs font-medium text-muted-foreground">
          {{ Object.keys(mappings).length }} field{{ Object.keys(mappings).length !== 1 ? 's' : '' }} mapped
        </span>
        <div class="flex gap-1">
          <Button variant="outline" size="sm" @click="autoMap">
            Auto-map
          </Button>
          <Button v-if="Object.keys(mappings).length > 0" variant="outline" size="sm" @click="clearMapping">
            Clear
          </Button>
        </div>
      </div>

      <div v-if="Object.keys(mappings).length > 0" class="space-y-1">
        <div
          v-for="(target, source) in mappings"
          :key="source"
          class="flex items-center gap-2 rounded-md border border-border/50 bg-muted/30 px-3 py-1.5 text-sm"
        >
          <span class="min-w-0 flex-1 truncate font-mono text-xs text-foreground">{{ source }}</span>
          <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="shrink-0 text-muted-foreground">
            <path d="M5 12h14" />
            <path d="m12 5 7 7-7 7" />
          </svg>
          <span class="min-w-0 flex-1 truncate font-mono text-xs text-foreground">{{ target }}</span>
          <button
            class="ml-1 shrink-0 rounded p-0.5 text-muted-foreground hover:text-destructive"
            @click="removeMapping(source)"
          >
            <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M18 6 6 18" />
              <path d="m6 6 12 12" />
            </svg>
          </button>
        </div>
      </div>

      <div v-if="unmappedSource.length > 0 || unmappedTarget.length > 0" class="mt-2">
        <div v-if="!showPicker" class="flex items-center gap-2">
          <span class="text-xs text-muted-foreground">
            {{ unmappedSource.length }} unmapped source,
            {{ unmappedTarget.length }} unmapped target
          </span>
          <Button variant="secondary" size="sm" @click="showPicker = true">
            Add mapping
          </Button>
        </div>

        <div v-else class="mt-2 space-y-2 rounded-md border border-border/50 bg-muted/20 p-3">
          <div class="grid grid-cols-2 gap-3">
            <div>
              <label for="fieldmappingpair-field-2" class="mb-1 block text-xs font-medium text-muted-foreground">Source field</label>
              <Select v-model="selectedSource">
                <SelectTrigger class="w-full rounded-md border border-border bg-background px-2 py-1 text-xs" aria-label="Source field">
                  <SelectValue placeholder="Select source" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem v-for="f in pickerSourceFields" :key="f.name" :value="f.name">
                    {{ f.name }} ({{ f.type }})
                  </SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div>
              <label for="fieldmappingpair-field-1" class="mb-1 block text-xs font-medium text-muted-foreground">Target field</label>
              <Select v-model="selectedTarget">
                <SelectTrigger class="w-full rounded-md border border-border bg-background px-2 py-1 text-xs" aria-label="Target field">
                  <SelectValue placeholder="Select target" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem v-for="f in pickerTargetFields" :key="f.name" :value="f.name">
                    {{ f.name }} ({{ f.type }})
                  </SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>
          <div class="flex justify-end gap-1">
            <Button variant="outline" size="sm" @click="showPicker = false">
              Cancel
            </Button>
            <Button
              variant="default"
              size="sm"
              :disabled="!selectedSource || !selectedTarget"
              @click="addMapping"
            >
              Add
            </Button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
