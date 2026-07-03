<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { useCompositeStore } from '../../../stores/compositeStore'
import { useApi } from '../../../composables/useApi'
import Tabs from '../../ui/tabs/Tabs.vue'
import TabsList from '../../ui/tabs/TabsList.vue'
import TabsTrigger from '../../ui/tabs/TabsTrigger.vue'
import TabsContent from '../../ui/tabs/TabsContent.vue'
import FieldMappingPair from './FieldMappingPair.vue'
import type { SchemaField } from '../../../types/pipeline'

const props = defineProps<{
  compositeRef: string | null
  inputMapping: Record<string, string>
  outputMapping: Record<string, string>
  precedingNodeSchemaId: string | null
  downstreamNodeSchemaId: string | null
}>()

const emit = defineEmits<{
  (e: 'update:inputMapping', mapping: Record<string, string>): void
  (e: 'update:outputMapping', mapping: Record<string, string>): void
}>()

const compositeStore = useCompositeStore()
const { get } = useApi()
const activeTab = ref('input')

const composite = computed(() => {
  if (!props.compositeRef) return null
  return compositeStore.getCompositeById(props.compositeRef) ?? null
})

const inputSchemaId = computed(() => composite.value?.input_schema_id ?? null)
const outputSchemaId = computed(() => composite.value?.output_schema_id ?? null)

const sourceFields = ref<SchemaField[]>([])
const targetFields = ref<SchemaField[]>([])
const outputSourceFields = ref<SchemaField[]>([])
const outputTargetFields = ref<SchemaField[]>([])
const loading = ref(false)
const jsonPreview = ref('')

async function loadSchemaFields(schemaId: string): Promise<SchemaField[]> {
  try {
    const data = await get<{ fields: SchemaField[] }>(`/api/v1/schemas/${schemaId}/fields`)
    return data.fields ?? []
  } catch {
    return []
  }
}

async function loadSchemas() {
  loading.value = true
  try {
    if (props.precedingNodeSchemaId) {
      sourceFields.value = await loadSchemaFields(props.precedingNodeSchemaId)
    } else {
      sourceFields.value = []
    }
    if (inputSchemaId.value) {
      targetFields.value = await loadSchemaFields(inputSchemaId.value)
    } else {
      targetFields.value = []
    }
    if (outputSchemaId.value) {
      outputSourceFields.value = await loadSchemaFields(outputSchemaId.value)
    } else {
      outputSourceFields.value = []
    }
    if (props.downstreamNodeSchemaId) {
      outputTargetFields.value = await loadSchemaFields(props.downstreamNodeSchemaId)
    } else {
      outputTargetFields.value = []
    }
  } finally {
    loading.value = false
  }
}

watch(
  () => [props.compositeRef, props.precedingNodeSchemaId, props.downstreamNodeSchemaId],
  () => { loadSchemas() },
  { immediate: true },
)

watch(
  [() => props.inputMapping, () => props.outputMapping],
  () => {
    jsonPreview.value = JSON.stringify(
      {
        input_mapping: props.inputMapping,
        output_mapping: props.outputMapping,
      },
      null,
      2,
    )
  },
  { immediate: true },
)
</script>

<template>
  <div class="space-y-3">
    <div v-if="!composite" class="py-4 text-center text-sm text-muted-foreground">
      No composite selected.
    </div>

    <template v-else>
      <div class="flex items-center gap-2">
        <h4 class="text-sm font-medium">Schema Mapping</h4>
        <span
          v-if="inputSchemaId || outputSchemaId"
          class="rounded border border-indigo-500/30 bg-indigo-500/10 px-1.5 py-0.5 text-[10px] text-indigo-300"
        >
          {{ Object.keys(props.inputMapping).length + Object.keys(props.outputMapping).length }} mapped
        </span>
      </div>

      <Tabs v-model:default-value="activeTab" class="w-full">
        <TabsList class="w-full">
          <TabsTrigger value="input">
            Input Mapping
          </TabsTrigger>
          <TabsTrigger value="output">
            Output Mapping
          </TabsTrigger>
        </TabsList>

        <TabsContent value="input" class="mt-3">
          <div v-if="loading" class="py-4 text-center text-xs text-muted-foreground">
            Loading schemas...
          </div>
          <div v-else-if="sourceFields.length === 0 && targetFields.length === 0" class="py-4 text-center text-sm text-muted-foreground">
            No schemas available for input mapping.
          </div>
          <div v-else>
            <div class="mb-2 flex items-center gap-3 text-xs text-muted-foreground">
              <span v-if="sourceFields.length > 0">
                Source: {{ sourceFields.length }} field{{ sourceFields.length !== 1 ? 's' : '' }}
              </span>
              <span v-else class="text-amber-400">No preceding node schema</span>
              <span v-if="targetFields.length > 0">
                Target: {{ targetFields.length }} field{{ targetFields.length !== 1 ? 's' : '' }}
              </span>
              <span v-else class="text-amber-400">No composite input schema</span>
            </div>
            <FieldMappingPair
              :source-fields="sourceFields"
              :target-fields="targetFields"
              :mappings="props.inputMapping"
              direction="input"
              @update:mappings="emit('update:inputMapping', $event)"
            />
          </div>
        </TabsContent>

        <TabsContent value="output" class="mt-3">
          <div v-if="loading" class="py-4 text-center text-xs text-muted-foreground">
            Loading schemas...
          </div>
          <div v-else-if="outputSourceFields.length === 0 && outputTargetFields.length === 0" class="py-4 text-center text-sm text-muted-foreground">
            No schemas available for output mapping.
          </div>
          <div v-else>
            <div class="mb-2 flex items-center gap-3 text-xs text-muted-foreground">
              <span v-if="outputSourceFields.length > 0">
                Source: {{ outputSourceFields.length }} field{{ outputSourceFields.length !== 1 ? 's' : '' }}
              </span>
              <span v-else class="text-amber-400">No composite output schema</span>
              <span v-if="outputTargetFields.length > 0">
                Target: {{ outputTargetFields.length }} field{{ outputTargetFields.length !== 1 ? 's' : '' }}
              </span>
              <span v-else class="text-amber-400">No downstream schema</span>
            </div>
            <FieldMappingPair
              :source-fields="outputSourceFields"
              :target-fields="outputTargetFields"
              :mappings="props.outputMapping"
              direction="output"
              @update:mappings="emit('update:outputMapping', $event)"
            />
          </div>
        </TabsContent>
      </Tabs>

      <div class="mt-3">
        <details class="group">
          <summary class="cursor-pointer text-xs text-muted-foreground hover:text-foreground">
            JSON preview
          </summary>
          <pre class="mt-1 max-h-32 overflow-auto rounded-md border border-border/50 bg-muted/20 p-2 text-[10px] text-muted-foreground">{{ jsonPreview }}</pre>
        </details>
      </div>
    </template>
  </div>
</template>
