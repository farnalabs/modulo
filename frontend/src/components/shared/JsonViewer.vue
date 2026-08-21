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
      <template v-if="isPlainString">
        <pre
          class="whitespace-pre-wrap break-words font-mono text-xs leading-relaxed text-foreground"
          :data-testid="plainStringTruncated ? 'json-viewer-string-truncated' : undefined"
        >{{ expandedPlainString ? rawString : plainStringPreview }}</pre>
        <div v-if="plainStringTruncated" class="mt-2 flex items-center gap-2">
          <span class="json-viewer-string-count" role="status">{{ t('components.JsonViewer.truncated_count', { count: formatCount(rawString.length) }) }}</span>
          <button
            v-if="!expandedPlainString"
            type="button"
            class="json-viewer-string-toggle"
            data-testid="json-viewer-string-expand"
            :aria-expanded="false"
            :aria-label="t('components.JsonViewer.expand_string')"
            @click="expandedPlainString = true"
          >
            {{ t('components.JsonViewer.expand_string') }}
          </button>
          <button
            v-else
            type="button"
            class="json-viewer-string-toggle"
            data-testid="json-viewer-string-collapse"
            :aria-expanded="true"
            :aria-label="t('components.JsonViewer.collapse_string')"
            @click="expandedPlainString = false"
          >
            {{ t('components.JsonViewer.collapse_string') }}
          </button>
        </div>
      </template>
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
        :render-node-value="renderNodeValue"
        :virtual="virtual"
        :height="height"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, h, ref, watch } from 'vue'
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
    stringTruncateLength?: number
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
    stringTruncateLength: 500,
  },
)

const { t } = useI18n()
const copied = ref(false)
const deepRef = ref(props.deep)

// Paths of long string nodes the user has expanded to their full value.
const expandedPaths = ref(new Set<string>())
// Whether a top-level plain string has been expanded.
const expandedPlainString = ref(false)

watch(
  () => props.deep,
  (value) => {
    deepRef.value = value
  },
)

const rawString = computed(() => (typeof props.data === 'string' ? props.data : ''))

const plainStringPreview = computed(() => {
  const value = rawString.value
  if (value.length <= props.stringTruncateLength) return value
  return value.slice(0, props.stringTruncateLength)
})

const plainStringTruncated = computed(() => rawString.value.length > props.stringTruncateLength)

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

function formatCount(length: number): string {
  return length.toLocaleString('en-US')
}

// Subset of the dep's `NodeDataType` that our value override relies on.
interface ViewerNode {
  content: string | number | boolean | null
  path: string
  type: string
}

/**
 * Override for vue-json-pretty's `renderNodeValue` extension point. Long
 * string values render truncated (first N chars + char count + expand
 * button) collapsed by default; expanding swaps in the full value as a
 * wrapping mono/log block. Non-string and short values fall through to the
 * dep's default rendering unchanged.
 */
function renderNodeValue({ node, defaultValue }: { node: ViewerNode; defaultValue: unknown }) {
  if (node.type !== 'content' || typeof node.content !== 'string') return defaultValue
  const full = node.content
  if (full.length <= props.stringTruncateLength) return defaultValue

  const path = node.path
  const expanded = expandedPaths.value.has(path)
  const toggle = (event: MouseEvent) => {
    event.stopPropagation()
    const next = new Set(expandedPaths.value)
    if (expanded) next.delete(path)
    else next.add(path)
    expandedPaths.value = next
  }

  if (expanded) {
    return h('span', { class: 'json-viewer-string-expanded', 'data-testid': 'json-viewer-string-expanded' }, [
      h('span', { class: 'json-viewer-string-expanded-text', role: 'status' }, full),
      h(
        'button',
        {
          type: 'button',
          class: 'json-viewer-string-toggle',
          'data-testid': 'json-viewer-string-collapse',
          'aria-expanded': 'true',
          'aria-label': t('components.JsonViewer.collapse_string'),
          onClick: toggle,
        },
        t('components.JsonViewer.collapse_string'),
      ),
    ])
  }

  return h('span', { class: 'json-viewer-string-truncated', 'data-testid': 'json-viewer-string-truncated' }, [
    h('span', { class: 'json-viewer-string-truncated-text' }, full.slice(0, props.stringTruncateLength)),
    h(
      'button',
      {
        type: 'button',
        class: 'json-viewer-string-toggle',
        'data-testid': 'json-viewer-string-expand',
        'aria-expanded': 'false',
        'aria-label': t('components.JsonViewer.expand_string'),
        onClick: toggle,
      },
      t('components.JsonViewer.expand_string'),
    ),
    h(
      'span',
      { class: 'json-viewer-string-count', role: 'status' },
      t('components.JsonViewer.truncated_count', { count: formatCount(full.length) }),
    ),
  ])
}
</script>
