<template>
  <div class="json-viewer" data-testid="json-viewer">
    <div v-if="showToolbar" class="mb-2 flex items-center gap-2">
      <button
        type="button"
        data-testid="json-viewer-copy"
        class="rounded-md border border-input bg-background px-2 py-1 text-xs font-medium text-muted-foreground hover:bg-accent hover:text-foreground transition-colors"
        :aria-label="t('components.JsonViewer.copy')"
        @click="copyValue"
      >
        {{ copied ? t('components.JsonViewer.copied') : t('components.JsonViewer.copy') }}
      </button>
      <button
        type="button"
        data-testid="json-viewer-expand-all"
        class="rounded-md border border-input bg-background px-2 py-1 text-xs font-medium text-muted-foreground hover:bg-accent hover:text-foreground transition-colors"
        :aria-label="t('components.JsonViewer.expand_all')"
        @click="deepRef = 999"
      >
        {{ t('components.JsonViewer.expand_all') }}
      </button>
      <button
        type="button"
        data-testid="json-viewer-collapse-all"
        class="rounded-md border border-input bg-background px-2 py-1 text-xs font-medium text-muted-foreground hover:bg-accent hover:text-foreground transition-colors"
        :aria-label="t('components.JsonViewer.collapse_all')"
        @click="deepRef = 1"
      >
        {{ t('components.JsonViewer.collapse_all') }}
      </button>
    </div>

    <div
      class="overflow-auto rounded-lg border border-border bg-background p-3"
      :style="scrollContainerStyle"
    >
      <pre v-if="isPlainString" class="whitespace-pre-wrap break-words font-mono text-xs leading-relaxed text-foreground">{{ rawString }}</pre>
      <VueJsonPretty
        v-else
        :data="parsedData"
        :deep="deepRef"
        :collapsed-node-length="collapsedNodeLength"
        :show-length="showLength"
        :show-line="true"
        :show-icon="showIcon"
        :show-double-quotes="false"
        :render-node-actions="renderNodeActions"
        :virtual="virtual"
        :height="height"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import VueJsonPretty from 'vue-json-pretty'
import 'vue-json-pretty/lib/styles.css'
import './json-viewer.css'

const props = withDefaults(
  defineProps<{
    data: unknown
    deep?: number
    collapsedNodeLength?: number
    showLength?: boolean
    maxHeight?: string
    showToolbar?: boolean
    virtual?: boolean
    height?: number
    showIcon?: boolean
    renderNodeActions?: boolean
  }>(),
  {
    deep: 2,
    collapsedNodeLength: 20,
    showLength: true,
    maxHeight: '24rem',
    showToolbar: true,
    virtual: false,
    height: 400,
    showIcon: false,
    renderNodeActions: false,
  },
)

const { t } = useI18n()
const copied = ref(false)
const deepRef = ref(props.deep)

watch(
  () => props.deep,
  (value) => {
    deepRef.value = value
  },
)

const rawString = computed(() => (typeof props.data === 'string' ? props.data : ''))

// Mirrors the dep's own `JSONDataType` (vue-json-pretty/types/utils) —
// not exported from the package root, so we declare it locally.
type JsonDataType = string | number | boolean | unknown[] | Record<string, unknown> | null

interface ParseResult {
  ok: boolean
  value: unknown
}

function tryParse(raw: string): ParseResult {
  try {
    return { ok: true, value: JSON.parse(raw) }
  } catch {
    return { ok: false, value: raw }
  }
}

const parsed = computed<ParseResult>(() => {
  if (typeof props.data !== 'string') return { ok: false, value: props.data }
  return tryParse(props.data)
})

const isPlainString = computed(() => typeof props.data === 'string' && !parsed.value.ok)
const parsedData = computed<JsonDataType>(() => (parsed.value.ok ? parsed.value.value : props.data) as JsonDataType)

const scrollContainerStyle = computed(() => {
  if (!props.maxHeight) return undefined
  return { maxHeight: props.maxHeight }
})

function copyValue() {
  const text = JSON.stringify(parsedData.value ?? rawString.value, null, 2)
  if (!text) return
  void navigator.clipboard
    .writeText(text)
    .then(() => {
      copied.value = true
      setTimeout(() => {
        copied.value = false
      }, 2000)
    })
    .catch(() => {
      copied.value = false
    })
}
</script>
