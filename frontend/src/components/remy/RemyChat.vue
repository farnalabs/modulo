<template>
  <div class="remy-chat flex flex-col flex-1 overflow-hidden">
    <div
      ref="scrollRef"
      class="remy-messages flex-1 overflow-y-auto p-3 space-y-3"
    >
      <div
        v-if="store.activeSessionId && store.messages.length === 0 && !store.isStreaming"
        class="remy-msg assistant"
      >
        <div class="remy-msg-avatar">
          <div class="avatar-assistant">
            <svg
              xmlns="http://www.w3.org/2000/svg"
              width="14"
              height="14"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
            >
              <path
                d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2z"
              />
              <path d="M8 14s1.5 2 4 2 4-2 4-2" />
              <line x1="9" y1="9" x2="9.01" y2="9" />
              <line x1="15" y1="9" x2="15.01" y2="9" />
            </svg>
          </div>
        </div>
        <div class="remy-msg-content">
          <div class="remy-markdown">
            <p class="remy-p">{{ $t('components.remy.RemyChat.intro_text') }}</p>
          </div>
        </div>
      </div>
      <div
        v-for="msg in store.messages"
        :key="msg.id"
      >
        <div
          v-if="msg.role === 'summary'"
          class="remy-turn-separator"
        >
          <div class="remy-turn-line" />
          <span class="remy-turn-label">{{ msg.content }}</span>
          <div class="remy-turn-line" />
        </div>
        <section
          v-else-if="isAnalyticsChartMessage(msg)"
          class="remy-analytics-card"
          :aria-label="$t('components.remy.RemyChat.analytics_chart_title')"
          data-testid="remy-analytics-card"
        >
          <div class="remy-analytics-header">
            <span class="remy-analytics-title">{{ $t('components.remy.RemyChat.analytics_chart_title') }}</span>
            <fieldset
              class="remy-analytics-measures"
              :aria-label="$t('components.remy.RemyChat.analytics_measure_label')"
            >
              <button
                v-for="m in analyticsMeasures"
                :key="m.value"
                type="button"
                class="remy-measure-btn"
                :class="{ active: analyticsMeasureFor(msg) === m.value }"
                :aria-pressed="analyticsMeasureFor(msg) === m.value"
                @click="setAnalyticsMeasureFor(msg, m.value)"
              >
                {{ $t(m.labelKey) }}
              </button>
            </fieldset>
          </div>
          <AnalyticsChart
            :series="analyticsSeriesFor(msg)"
            :measure="analyticsMeasureFor(msg)"
            :group-by="analyticsGroupByFor(msg)"
          />
          <a
            v-if="analyticsDeepLinkFor(msg)"
            class="remy-analytics-link"
            :href="analyticsDeepLinkFor(msg)"
            @click.prevent="navigateToAnalytics(analyticsDeepLinkFor(msg))"
          >
            {{ $t('components.remy.RemyChat.view_full_analytics') }} <span aria-hidden="true">→</span>
          </a>
        </section>
        <div
          v-else-if="msg.role === 'tool_result' && msg.tool_results_json"
          class="remy-tool-card"
        >
          <button type="button" class="remy-tool-header" @click="toggleToolExpand(msg.id)">
            <span class="remy-tool-name">?? Tool Called: {{ (msg.tool_results_json as ToolResult).tool_name }}</span>
            <span class="tool-badge" :class="(msg.tool_results_json as ToolResult).success ? 'success' : 'failed'">
              {{ (msg.tool_results_json as ToolResult).success ? 'Completed' : 'Failed' }}
            </span>
            <span class="tool-chevron" :class="{ expanded: expandedTools.has(msg.id) }">?</span>
          </button>
          <div v-if="expandedTools.has(msg.id)" class="remy-tool-details">
            <pre>{{ formatToolDetails(msg.tool_results_json as ToolResult) }}</pre>
          </div>
        </div>
        <div
          v-else
          class="remy-msg"
          :class="msg.role"
        >
          <div class="remy-msg-avatar">
            <div v-if="msg.role === 'user'" class="avatar-user">
              {{ userInitial }}
            </div>
            <div v-else class="avatar-assistant">
              <svg
                xmlns="http://www.w3.org/2000/svg"
                width="14"
                height="14"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
              >
                <path
                  d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2z"
                />
                <path d="M8 14s1.5 2 4 2 4-2 4-2" />
                <line x1="9" y1="9" x2="9.01" y2="9" />
                <line x1="15" y1="9" x2="15.01" y2="9" />
              </svg>
            </div>
          </div>
          <div class="remy-msg-content">
            <div
              v-if="msg.role === 'assistant'"
              class="remy-markdown"
              v-html="renderMarkdown(msg.content ?? '')"
            />
            <div v-else class="remy-plaintext">{{ msg.content }}</div>
            <div
              v-if="msg.role === 'assistant' && msg.content"
              class="remy-msg-actions"
            >
              <button
                type="button"
                class="remy-copy-btn"
                @click="copyMessage(msg.content ?? '')"
                title="Copy"
                :aria-label="'Copy message'"
              >
                <svg
                  xmlns="http://www.w3.org/2000/svg"
                  width="12"
                  height="12"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  stroke-width="2"
                >
                  <rect x="9" y="9" width="13" height="13" rx="2" />
                  <path
                    d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"
                  />
                </svg>
              </button>
            </div>
          </div>
        </div>
      </div>
      <div v-if="store.isStreaming" class="remy-msg assistant">
        <div class="remy-msg-avatar">
          <div class="avatar-assistant">
            <svg
              xmlns="http://www.w3.org/2000/svg"
              width="14"
              height="14"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
            >
              <path
                d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2z"
              />
              <path d="M8 14s1.5 2 4 2 4-2 4-2" />
              <line x1="9" y1="9" x2="9.01" y2="9" />
              <line x1="15" y1="9" x2="15.01" y2="9" />
            </svg>
          </div>
        </div>
        <div class="remy-msg-content">
          <div class="remy-streaming-indicator">
            <span class="streaming-dot" />
            <span class="streaming-dot" />
            <span class="streaming-dot" />
          </div>
        </div>
      </div>

      <div v-if="!remyOnly && uiDrivingEnabled && store.pendingPermission" class="remy-permission-card">
        <div class="remy-permission-header">
          <ShieldAlertIcon class="h-4 w-4" />
          <span>{{ $t('components.remy.RemyChat.permission_request') }}</span>
        </div>
        <div class="remy-permission-tools">
          <div
            v-for="tool in store.pendingPermission.tools"
            :key="tool.name"
            class="remy-permission-tool"
            :class="{ 'remy-permission-tool-nogo': tool.nogo }"
          >
            <div class="flex items-center gap-2 min-w-0">
              <span class="font-mono text-xs truncate">{{ tool.name }}</span>
              <span v-if="tool.nogo" class="remy-nogo-badge">?? Destructive Page</span>
            </div>
            <span class="text-xs text-muted-foreground">{{ describeArgs(tool) }}</span>
          </div>
        </div>
        <div class="remy-permission-actions">
          <Button severity="secondary" outlined size="small" :disabled="nogoCountdown > 0" class="relative" @click="store.approvePermission(store.pendingPermission.request_id, 'reject')">Deny{{ nogoCountdown > 0 ? ` (${nogoCountdown}s)` : '' }}</Button>
          <Button severity="secondary" size="small" :disabled="nogoCountdown > 0" @click="store.approvePermission(store.pendingPermission.request_id, 'approve')">Allow Once{{ nogoCountdown > 0 ? ` (${nogoCountdown}s)` : '' }}</Button>
          <Button size="small" :disabled="nogoCountdown > 0" @click="store.approvePermission(store.pendingPermission.request_id, 'approve_for_session')">Allow for Session{{ nogoCountdown > 0 ? ` (${nogoCountdown}s)` : '' }}</Button>
        </div>
      </div>

      <div v-if="!remyOnly && uiDrivingEnabled && store.isExecutingUi" class="remy-executing-indicator">
        <LoaderIcon class="h-3 w-3 animate-spin" />
        <span>{{ store.isPaused ? 'Remy is paused. Resume or stop?' : 'Remy is performing actions in the browser...' }}</span>
        <div class="flex gap-2">
          <Button v-if="!store.isPaused" severity="secondary" size="small" @click="pauseRemy">? Pause</Button>
          <Button v-if="store.isPaused" severity="secondary" size="small" @click="resumeRemy">? Resume</Button>
          <Button severity="danger" size="small" @click="abortUiCommands">{{ store.isPaused ? '? Stop' : 'Stop' }}</Button>
        </div>
      </div>
    </div>

    <div class="remy-input-area border-t p-3 relative">
      <div
        v-if="showSlashMenu"
        class="remy-slash-menu"
      >
        <button type="button"
          v-for="(cmd, idx) in filteredSlashCommands"
          :key="cmd.command"
          class="remy-slash-item"
          :class="{ active: slashHighlightIdx === idx }"
          @click="executeSlashCommand(cmd)"
          @mouseenter="slashHighlightIdx = idx"
          @focus="slashHighlightIdx = idx"
        >
          <span class="remy-slash-command">{{ cmd.command }}</span>
          <span class="remy-slash-desc">{{ cmd.description }}</span>
        </button>
        <div v-if="filteredSlashCommands.length === 0" class="remy-slash-empty">
          {{ $t('components.remy.RemyChat.no_slash_commands') }}
        </div>
      </div>
      <div class="flex gap-2">
        <div class="remy-input-wrapper flex-1">
          <div
            class="remy-input-highlight"
            aria-hidden="true"
            v-html="styledInput"
          />
          <textarea
            ref="textareaRef"
            v-model="inputText"
            class="remy-input"
            rows="1"
            aria-label="Chat input"
            @keydown="onInputKeydown"
            @input="onInput"
            @scroll="syncHighlightScroll"
            :disabled="store.isStreaming || store.isExecutingUi"
          />
        </div>
        <Button :disabled="!inputText.trim() || store.isStreaming || store.isExecutingUi" @click="handleSend" :aria-label="$t('components.remy.send_message')">
          <svg
            xmlns="http://www.w3.org/2000/svg"
            width="16"
            height="16"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
          >
            <line x1="22" y1="2" x2="11" y2="13" />
            <polygon points="22 2 15 22 11 13 2 9 22 2" />
          </svg>
        </Button>
      </div>
      <div
        v-if="showDeleteConfirm"
        class="remy-delete-confirm"
      >
        <p class="text-sm font-medium">{{ $t('components.remy.RemyChat.delete_confirm') }}</p>
        <div class="flex gap-2 mt-2">
          <Button severity="danger" size="small" @click="deleteCurrentSession">
            {{ $t('common.delete') }}
          </Button>
          <Button severity="secondary" outlined size="small" @click="showDeleteConfirm = false">
            {{ $t('common.cancel') }}
          </Button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, watch, nextTick, computed, reactive, onUnmounted } from "vue";
import { useRouter } from "vue-router";
import { useRemyStore } from "@/composables/useRemyStore";
import { usePlanStore } from "@/stores/planStore";
import { useRemyStream } from "@/composables/useRemyStream";
import { abortUiCommands } from "@/composables/useUiCommandExecutor";
import Button from 'primevue/button'
import { getAccessToken } from "@/lib/api/client";
import { ShieldAlertIcon, LoaderIcon } from "@lucide/vue";
import AnalyticsChart from "../analytics/AnalyticsChart.vue";
import { MEASURES, type AnalyticsBucket, type AnalyticsMeasure } from "../../stores/analytics";
import type { ChatMessage, ToolResult } from "@/types/remy";

const store = useRemyStore();
const planStore = usePlanStore();
const router = useRouter();
const props = defineProps<{ remyOnly?: boolean }>();
const { connectStream, disconnectStream } = useRemyStream();
const scrollRef = ref<HTMLDivElement | null>(null);
const inputText = ref("");
const textareaRef = ref<HTMLTextAreaElement | null>(null);

interface SlashCommand {
  command: string
  description: string
  action: () => void
}

const slashCommands: SlashCommand[] = [
  {
    command: '/rename',
    description: 'Rename current session',
    action: () => {
      const text = inputText.value
      const parts = text.split(' ')
      const newName = parts.slice(1).join(' ').trim()
      showSlashMenu.value = false
      if (newName && store.activeSessionId) {
        store.renameSession(store.activeSessionId, newName)
      } else {
        store.triggerRename()
      }
    },
  },
  {
    command: '/exit',
    description: 'Close Remy panel',
    action: () => {
      showSlashMenu.value = false
      store.setPanelState('closed')
    },
  },
  {
    command: '/help',
    description: 'Show available commands',
    action: () => {
      showSlashMenu.value = false
      const names = slashCommands.map(c => c.command).join(', ')
      store.appendSystemMessage(`Available commands: ${names}`)
    },
  },
  {
    command: '/clear',
    description: 'Clear current input',
    action: () => {
      inputText.value = ''
      showSlashMenu.value = false
    },
  },
  {
    command: '/new',
    description: 'Create a new session',
    action: async () => {
      showSlashMenu.value = false
      await store.createSession()
    },
  },
  {
    command: '/delete',
    description: 'Delete current session',
    action: () => {
      showSlashMenu.value = false
      showDeleteConfirm.value = true
    },
  },
]

const styledInput = computed(() => escapeHtml(inputText.value))

const showSlashMenu = ref(false)
const slashHighlightIdx = ref(0)
const showDeleteConfirm = ref(false)

const filteredSlashCommands = computed(() => {
  const text = inputText.value
  if (!text.startsWith('/')) return []
  const partial = text.slice(1).toLowerCase()
  if (!partial) return slashCommands
  return slashCommands.filter(c => c.command.slice(1).toLowerCase().startsWith(partial))
})

function onInput() {
  resizeInput()
  if (inputText.value.startsWith('/') && !inputText.value.includes(' ')) {
    showSlashMenu.value = true
    slashHighlightIdx.value = 0
  } else {
    showSlashMenu.value = false
  }
}

function executeSlashCommand(cmd: SlashCommand) {
  showSlashMenu.value = false
  cmd.action()
  inputText.value = ''
}

function onInputKeydown(e: KeyboardEvent) {
  if (showSlashMenu.value && filteredSlashCommands.value.length > 0) {
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      slashHighlightIdx.value = (slashHighlightIdx.value + 1) % filteredSlashCommands.value.length
      return
    }
    if (e.key === 'ArrowUp') {
      e.preventDefault()
      slashHighlightIdx.value = (slashHighlightIdx.value - 1 + filteredSlashCommands.value.length) % filteredSlashCommands.value.length
      return
    }
    if (e.key === 'Enter' || e.key === 'Tab') {
      e.preventDefault()
      const cmd = filteredSlashCommands.value[slashHighlightIdx.value]
      if (cmd.command === '/exit' || cmd.command === '/delete') {
        executeSlashCommand(cmd)
      } else {
        inputText.value = cmd.command + ' '
        showSlashMenu.value = false
        nextTick(() => {
          textareaRef.value?.focus()
        })
      }
      return
    }
    if (e.key === 'Escape') {
      e.preventDefault()
      showSlashMenu.value = false
      return
    }
  }

  if (e.key === 'Enter' && !e.shiftKey && !e.ctrlKey && !e.metaKey) {
    e.preventDefault()
    handleSend()
  }
}

async function deleteCurrentSession() {
  if (!store.activeSessionId) return
  showDeleteConfirm.value = false
  const id = store.activeSessionId
  await store.deleteSession(id)
  // remy-only mode: never auto-create a new session when none remain — the
  // tabs store reconciles the empty state.
  if (props.remyOnly) return
  const sessions = store.sortedSessions
  if (sessions.length > 0) {
    await store.loadSession(sessions[0].id)
  } else {
    await store.createSession()
  }
}

const uiDrivingEnabled = computed(() => planStore.featureEnabled('remy_ui_driving'))

const expandedTools = ref(new Set<string>())

function toggleToolExpand(id: string) {
  if (expandedTools.value.has(id)) {
    expandedTools.value.delete(id)
  } else {
    expandedTools.value.add(id)
  }
  expandedTools.value = new Set(expandedTools.value)
}

function formatToolDetails(tc: { tool_call_id: string; tool_name: string; success: boolean; result?: unknown; error?: string }): string {
  const lines: string[] = [`Tool: ${tc.tool_name}`, `ID: ${tc.tool_call_id}`, `Status: ${tc.success ? 'Completed' : 'Failed'}`, '']
  if (tc.result !== undefined) {
    const resultStr = typeof tc.result === 'object' ? JSON.stringify(tc.result, null, 2) : String(tc.result)
    lines.push('Result:', resultStr)
  }
  if (tc.error) {
    lines.push('Error:', tc.error)
  }
  return lines.join('\n')
}

interface AnalyticsToolResult {
  group_by?: string
  dimension?: string | null
  date_from?: string | null
  date_to?: string | null
  buckets?: Array<Record<string, unknown>>
  deep_link?: string
}

function isAnalyticsToolResult(result: unknown): result is AnalyticsToolResult {
  if (!result || typeof result !== 'object') return false
  const r = result as Record<string, unknown>
  return typeof r.group_by === 'string' && Array.isArray(r.buckets)
}

function isAnalyticsChartMessage(msg: ChatMessage): boolean {
  if (msg.role !== 'tool_result' || !msg.tool_results_json) return false
  const tr = msg.tool_results_json as ToolResult
  if (!tr.success || tr.tool_name !== 'query_analytics') return false
  return isAnalyticsToolResult(tr.result)
}

const analyticsMeasures = MEASURES
const analyticsMeasureByMsg = reactive(new Map<string, AnalyticsMeasure>())
function analyticsMeasureFor(msg: ChatMessage): AnalyticsMeasure {
  return analyticsMeasureByMsg.get(msg.id) ?? 'count'
}
function setAnalyticsMeasureFor(msg: ChatMessage, measure: AnalyticsMeasure): void {
  analyticsMeasureByMsg.set(msg.id, measure)
}
function analyticsSeriesFor(msg: ChatMessage): AnalyticsBucket[] {
  const result = (msg.tool_results_json as ToolResult | null)?.result
  if (!isAnalyticsToolResult(result)) return []
  return result.buckets as unknown as AnalyticsBucket[]
}
function analyticsGroupByFor(msg: ChatMessage): string {
  const result = (msg.tool_results_json as ToolResult | null)?.result
  if (!isAnalyticsToolResult(result)) return 'day'
  return result.group_by ?? 'day'
}
function analyticsDeepLinkFor(msg: ChatMessage): string | undefined {
  const result = (msg.tool_results_json as ToolResult | null)?.result
  if (!isAnalyticsToolResult(result) || !result.deep_link) return undefined
  return result.deep_link
}
function navigateToAnalytics(link: string | undefined): void {
  if (link) router.push(link)
}

const nogoCountdown = ref(0)
let nogoCountdownTimer: ReturnType<typeof setInterval> | null = null

function hasNogoTool(): boolean {
  return store.pendingPermission?.tools.some((t) => t.nogo) ?? false
}

function startNogoCountdown() {
  stopNogoCountdown()
  if (!hasNogoTool()) return
  nogoCountdown.value = 3
  nogoCountdownTimer = setInterval(() => {
    nogoCountdown.value--
    if (nogoCountdown.value <= 0) {
      stopNogoCountdown()
    }
  }, 1000)
}

function stopNogoCountdown() {
  if (nogoCountdownTimer) {
    clearInterval(nogoCountdownTimer)
    nogoCountdownTimer = null
  }
  nogoCountdown.value = 0
}

watch(() => store.pendingPermission, (val) => {
  if (val && hasNogoTool()) {
    startNogoCountdown()
  } else {
    stopNogoCountdown()
  }
}, { immediate: true })

async function pauseRemy() {
  await store.pauseRemy()
}

async function resumeRemy() {
  await store.resumeRemy()
}

onUnmounted(() => {
  stopNogoCountdown()
})

defineExpose({ disconnect: disconnectStream })

const userEmail = computed(() => {
  const token = getAccessToken();
  if (!token) return "";
  try {
    const payload = JSON.parse(atob(token.split(".")[1]));
    return payload.sub || "";
  } catch {
    return "";
  }
});

const userInitial = computed(() => {
  const email = userEmail.value;
  if (!email) return "?";
  return email.charAt(0).toUpperCase();
});

function friendlySelector(sel: string): string {
  const m = sel.match(/\[data-testid="([^"]+)"\]/)
  return m ? m[1].replaceAll('-', ' ') : sel
}

function describeArgs(tool: { name: string; args: Record<string, unknown> }): string {
  switch (tool.name) {
    case 'navigate':
      return `Navigate to ${tool.args.path}`
    case 'click':
      return `Click '${friendlySelector(tool.args.selector as string)}'`
    case 'fill':
      return `Type into ${friendlySelector(tool.args.selector as string)}: '${tool.args.value}'`
    case 'select':
      return `Select '${tool.args.value}' from ${friendlySelector(tool.args.selector as string)}`
    case 'extract':
      return `Read text from ${friendlySelector(tool.args.selector as string)}`
    case 'extract_all':
      return `Read text from all '${tool.args.selector}' elements`
    case 'get_page_interactables':
      return 'Discover all clickable elements on the page'
    case 'wait':
      return tool.args.selector ? `Wait for '${tool.args.selector}' to appear` : `Wait ${tool.args.ms ?? ''}ms`
    case 'go_back':
      return 'Go back to previous page'
    case 'get_url':
      return 'Get current page URL'
    case 'press':
      return `Press '${tool.args.key}' key`
    default:
      return ''
  }
}

function scrollToBottom() {
  nextTick(() => {
    if (scrollRef.value) {
      scrollRef.value.scrollTop = scrollRef.value.scrollHeight;
    }
  });
}

watch(() => [store.messages.length, store.isStreaming, store.isExecutingUi], scrollToBottom);

async function handleSend() {
  const text = inputText.value.trim();
  if (!text || store.isStreaming) return;
  inputText.value = "";
  resizeInput()
  await store.sendMessage(text);
  if (store.activeSessionId) {
    try {
      connectStream(store.activeSessionId, { excludeUiTools: !!props.remyOnly });
    } catch (e) {
      console.error("Failed to start Remy stream:", e);
    }
  }
}

function resizeInput() {
  nextTick(() => {
    const el = textareaRef.value
    if (el) {
      el.style.height = 'auto'
      el.style.height = Math.min(el.scrollHeight, 200) + 'px'
    }
  })
}

function syncHighlightScroll() {
  const el = textareaRef.value
  const hl = document.querySelector('.remy-input-highlight') as HTMLElement | null
  if (el && hl) {
    hl.scrollTop = el.scrollTop
    hl.scrollLeft = el.scrollLeft
  }
}

function copyMessage(text: string) {
  navigator.clipboard.writeText(text).catch(() => {});
}

function escapeHtml(text: string): string {
  return text.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
}

function renderMarkdown(text: string): string {
  if (!text) return "";
  let html = escapeHtml(text);

  const codeBlocks: string[] = [];
  const CB = "%%CODE_BLOCK_";
  html = html.replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) => {
    const langAttr = lang ? ` data-lang="${escapeHtml(lang)}"` : "";
    const idx = codeBlocks.length;
    const placeholder = `${CB}${idx}%%`;
    codeBlocks.push(`<pre${langAttr}><code class="remy-code-block">${code}</code></pre>`);
    return placeholder;
  });

  html = html.replace(/`([^`]+)`/g, '<code class="remy-inline-code">$1</code>');

  html = html.replace(/### (.+)/g, '<h4 class="remy-h3">$1</h4>');
  html = html.replace(/## (.+)/g, '<h3 class="remy-h2">$1</h3>');
  html = html.replace(/# (.+)/g, '<h2 class="remy-h1">$1</h2>');

  html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/\*([^*]+)\*/g, "<em>$1</em>");

  html = html.replace(/^- (.+)/gm, '<li class="remy-li">$1</li>');
  html = html.replace(
    /(<li[\s\S]*?<\/li>\n?)+/g,
    '<ul class="remy-ul">$&</ul>',
  );

  html = html.replace(/\n\n/g, '</p><p class="remy-p">');
  html = html.replace(/\n/g, "<br/>");

  if (!html.trim().startsWith('<')) {
    html = '<p class="remy-p">' + html + "</p>";
  }

  html = html.replace(/%%CODE_BLOCK_(\d+)%%/g, (_, i) => codeBlocks[Number(i)] ?? "");

  return html;
}
</script>

<style scoped>
.remy-messages {
  scroll-behavior: smooth;
}
.remy-msg {
  @apply flex gap-2 text-sm;
}
.remy-msg.user {
  @apply flex-row-reverse;
}
.remy-msg-avatar {
  @apply shrink-0;
}
.avatar-user {
  @apply flex items-center justify-center rounded-full text-xs font-bold;
  width: 24px;
  height: 24px;
  background-color: hsl(var(--primary));
  color: hsl(var(--primary-foreground));
}
.avatar-assistant {
  @apply flex items-center justify-center rounded-full;
  width: 24px;
  height: 24px;
  background-color: hsl(var(--muted));
  color: hsl(var(--muted-foreground));
}
.remy-msg-content {
  @apply max-w-[80%] space-y-1;
}
.remy-msg.user .remy-msg-content {
  @apply items-end;
}
.remy-plaintext {
  @apply rounded-xl px-3 py-2;
  background-color: hsl(var(--primary));
  color: hsl(var(--primary-foreground));
}
.remy-markdown {
  @apply rounded-xl px-3 py-2 leading-relaxed;
  background-color: hsl(var(--muted));
  color: hsl(var(--foreground));
}
.remy-msg-actions {
  @apply flex justify-end pt-1;
}
.remy-copy-btn {
  @apply flex items-center justify-center rounded p-1 transition-colors;
  color: hsl(var(--muted-foreground));
}
.remy-copy-btn:hover {
  color: hsl(var(--foreground));
  background-color: hsl(var(--accent));
}
.remy-streaming-indicator {
  @apply flex items-center gap-1 px-3 py-4;
}
.streaming-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background-color: hsl(var(--muted-foreground));
  animation: stream-bounce 1.4s ease-in-out infinite;
}
.streaming-dot:nth-child(2) {
  animation-delay: 0.2s;
}
.streaming-dot:nth-child(3) {
  animation-delay: 0.4s;
}
@keyframes stream-bounce {
  0%,
  80%,
  100% {
    transform: scale(0.6);
    opacity: 0.4;
  }
  40% {
    transform: scale(1);
    opacity: 1;
  }
}
.remy-input-area {
  background-color: hsl(var(--card));
  border-color: hsl(var(--border));
}
.remy-input-wrapper {
  position: relative;
  min-height: 38px;
}

.remy-input-highlight {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  padding: 8px 12px;
  font-size: 0.875rem;
  line-height: 1.4;
  white-space: pre-wrap;
  word-wrap: break-word;
  overflow: hidden;
  pointer-events: none;
  color: hsl(var(--foreground));
  border: 1px solid transparent;
  border-radius: var(--radius-lg, 0.5rem);
}

.remy-input {
  @apply rounded-lg px-3 py-2 text-sm outline-none resize-none;
  position: relative;
  background: transparent;
  border: 1px solid hsl(var(--input));
  color: transparent;
  caret-color: hsl(var(--foreground));
  min-height: 38px;
  line-height: 1.4;
  width: 100%;
}

.remy-input::placeholder {
  color: hsl(var(--muted-foreground));
}

.remy-input:focus {
  border-color: hsl(var(--ring));
  box-shadow: 0 0 0 1px hsla(var(--ring) / 0.3);
}
.remy-input:disabled {
  opacity: 0.5;
}
.remy-turn-separator {
  @apply flex items-center gap-3 px-2 py-2;
}
.remy-turn-line {
  @apply flex-1 h-px;
  background-color: hsl(var(--border));
}
.remy-turn-label {
  @apply text-xs font-medium shrink-0;
  color: hsl(var(--muted-foreground));
}
.remy-permission-card {
  @apply rounded-lg border p-3 space-y-3 text-sm;
  background-color: hsl(var(--card));
  border-color: hsl(var(--border));
}
.remy-permission-header {
  @apply flex items-center gap-2 font-medium;
  color: hsl(var(--warning));
}
.remy-permission-tools {
  @apply space-y-1;
}
.remy-permission-tool {
  @apply flex items-center gap-2 rounded-md px-2 py-1;
  background-color: hsl(var(--muted));
}
.remy-permission-tool-nogo {
  border: 1px solid hsl(0 72% 51% / 0.3);
  background-color: hsl(0 72% 51% / 0.05);
}
.remy-nogo-badge {
  @apply text-[10px] font-semibold px-1.5 py-0.5 rounded;
  background-color: hsl(0 72% 51% / 0.15);
  color: hsl(0 72% 51%);
}
.remy-permission-actions {
  @apply flex items-center gap-2;
}
.remy-executing-indicator {
  @apply flex items-center gap-2 rounded-lg border px-3 py-2 text-sm;
  background-color: hsl(var(--muted));
  border-color: hsl(var(--border));
}
.remy-tool-card {
  @apply rounded-lg border text-sm overflow-hidden;
  background-color: hsl(var(--card));
  border-color: hsl(var(--border));
}
.remy-analytics-card {
  @apply rounded-lg border p-3 space-y-2 text-sm;
  background-color: hsl(var(--card));
  border-color: hsl(var(--border));
}
.remy-analytics-header {
  @apply flex flex-wrap items-center justify-between gap-2;
}
.remy-analytics-title {
  @apply text-sm font-semibold;
  color: hsl(var(--foreground));
}
.remy-analytics-measures {
  @apply flex items-center gap-1 flex-wrap;
  border: 0;
  margin: 0;
  padding: 0;
}
.remy-measure-btn {
  @apply rounded px-1.5 py-0.5 text-[11px] font-medium transition-colors;
  color: hsl(var(--muted-foreground));
}
.remy-measure-btn:hover {
  color: hsl(var(--foreground));
  background-color: hsl(var(--accent));
}
.remy-measure-btn.active {
  color: hsl(var(--primary-foreground));
  background-color: hsl(var(--primary));
}
.remy-analytics-link {
  @apply inline-flex items-center gap-1 text-xs font-medium hover:opacity-80;
  color: hsl(var(--primary));
}
.remy-analytics-link:focus-visible {
  outline: 2px solid hsl(var(--ring));
  outline-offset: 2px;
}
.remy-tool-header {
  @apply flex items-center gap-2 w-full px-3 py-2 text-left cursor-pointer;
  background-color: hsl(var(--muted));
  color: hsl(var(--foreground));
}
.remy-tool-header:hover {
  background-color: hsl(var(--accent));
}
.remy-tool-name {
  @apply flex-1 font-medium;
}
.tool-badge {
  @apply text-xs font-medium px-2 py-0.5 rounded-full;
}
.tool-badge.success {
  background-color: hsl(142 76% 36% / 0.15);
  color: hsl(142 76% 36%);
}
.tool-badge.failed {
  background-color: hsl(0 72% 51% / 0.15);
  color: hsl(0 72% 51%);
}
.tool-chevron {
  @apply text-xs transition-transform duration-200;
  color: hsl(var(--muted-foreground));
}
.tool-chevron.expanded {
  transform: rotate(180deg);
}
.remy-tool-details {
  @apply border-t px-3 py-2;
  border-color: hsl(var(--border));
}
.remy-tool-details pre {
  @apply text-xs leading-relaxed whitespace-pre-wrap;
  color: hsl(var(--muted-foreground));
}
.remy-slash-menu {
  @apply absolute bottom-full left-3 right-3 mb-1 rounded-lg border shadow-lg overflow-hidden z-50;
  background-color: hsl(var(--popover));
  border-color: hsl(var(--border));
  max-height: 240px;
  overflow-y: auto;
}
.remy-slash-item {
  @apply flex items-center gap-3 w-full px-3 py-2 text-left text-sm transition-colors cursor-pointer;
  color: hsl(var(--popover-foreground));
  border-left: 2px solid transparent;
}
.remy-slash-item:hover,
.remy-slash-item.active {
  background-color: hsl(var(--accent));
}
.remy-slash-item.active {
  border-left: 2px solid hsl(var(--primary));
}
.remy-slash-item.active .remy-slash-command {
  color: hsl(var(--primary));
}
.remy-slash-item.active .remy-slash-desc {
  color: hsl(var(--foreground));
}

.remy-slash-command {
  @apply font-mono font-medium shrink-0;
  color: hsl(var(--primary));
}
.remy-slash-desc {
  @apply text-xs truncate;
  color: hsl(var(--muted-foreground));
}
.remy-slash-empty {
  @apply px-3 py-2 text-sm;
  color: hsl(var(--muted-foreground));
}
.remy-delete-confirm {
  @apply rounded-lg border p-3 mt-2;
  background-color: hsl(var(--card));
  border-color: hsl(var(--border));
}
@keyframes skill-shimmer {
  0%, 100% {
    color: #00FFD1;
    text-shadow: 0 0 4px rgba(0, 255, 209, 0.3);
  }
  33% {
    color: #4DFFCB;
    text-shadow: 0 0 6px rgba(77, 255, 203, 0.2);
  }
  66% {
    color: #00CCA8;
    text-shadow: 0 0 4px rgba(0, 204, 168, 0.3);
  }
}

.skill-inline {
  animation: skill-shimmer 3s ease-in-out infinite;
  font-weight: 600;
}
</style>
