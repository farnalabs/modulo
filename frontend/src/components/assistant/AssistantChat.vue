<template>
  <div class="assistant-chat flex flex-col flex-1 overflow-hidden">
    <div
      ref="scrollRef"
      class="assistant-messages flex-1 overflow-y-auto p-3 space-y-3"
    >
      <div
        v-if="store.activeSessionId && store.messages.length === 0 && !store.isStreaming"
        class="assistant-msg assistant"
      >
        <div class="assistant-msg-avatar">
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
        <div class="assistant-msg-content">
          <div class="assistant-markdown">
            <p class="assistant-p">{{ $t('components.assistant.AssistantChat.intro_text') }}</p>
          </div>
        </div>
      </div>
      <div
        v-for="msg in store.messages"
        :key="msg.id"
      >
        <div
          v-if="msg.role === 'summary'"
          class="assistant-turn-separator"
        >
          <div class="assistant-turn-line" />
          <span class="assistant-turn-label">{{ msg.content }}</span>
          <div class="assistant-turn-line" />
        </div>
        <section
          v-else-if="isAnalyticsChartMessage(msg)"
          class="assistant-analytics-card"
          :aria-label="$t('components.assistant.AssistantChat.analytics_chart_title')"
          data-testid="assistant-analytics-card"
        >
          <div class="assistant-analytics-header">
            <span class="assistant-analytics-title">{{ $t('components.assistant.AssistantChat.analytics_chart_title') }}</span>
            <fieldset
              class="assistant-analytics-measures"
              :aria-label="$t('components.assistant.AssistantChat.analytics_measure_label')"
            >
              <button
                v-for="m in analyticsMeasures"
                :key="m.value"
                type="button"
                class="assistant-measure-btn"
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
            class="assistant-analytics-link"
            :href="analyticsDeepLinkFor(msg)"
            @click.prevent="navigateToAnalytics(analyticsDeepLinkFor(msg))"
          >
            {{ $t('components.assistant.AssistantChat.view_full_analytics') }} <span aria-hidden="true">→</span>
          </a>
        </section>
        <div
          v-else-if="msg.role === 'tool_result' && msg.tool_results_json"
          class="assistant-tool-card"
        >
          <button type="button" class="assistant-tool-header" @click="toggleToolExpand(msg.id)">
            <span class="assistant-tool-name">?? Tool Called: {{ (msg.tool_results_json as ToolResult).tool_name }}</span>
            <span class="tool-badge" :class="(msg.tool_results_json as ToolResult).success ? 'success' : 'failed'">
              {{ (msg.tool_results_json as ToolResult).success ? 'Completed' : 'Failed' }}
            </span>
            <span class="tool-chevron" :class="{ expanded: expandedTools.has(msg.id) }">?</span>
          </button>
          <div v-if="expandedTools.has(msg.id)" class="assistant-tool-details">
            <pre>{{ formatToolDetails(msg.tool_results_json as ToolResult) }}</pre>
          </div>
        </div>
        <div
          v-else
          class="assistant-msg"
          :class="msg.role"
        >
          <div class="assistant-msg-avatar">
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
          <div class="assistant-msg-content">
            <div
              v-if="msg.role === 'assistant'"
              class="assistant-markdown"
              v-html="renderMarkdown(msg.content ?? '')"
            />
            <div v-else class="assistant-plaintext">{{ msg.content }}</div>
            <div
              v-if="msg.role === 'assistant' && msg.content"
              class="assistant-msg-actions"
            >
              <button
                type="button"
                class="assistant-copy-btn"
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
      <div v-if="store.isStreaming" class="assistant-msg assistant">
        <div class="assistant-msg-avatar">
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
        <div class="assistant-msg-content">
          <div class="assistant-streaming-indicator">
            <span class="streaming-dot" />
            <span class="streaming-dot" />
            <span class="streaming-dot" />
          </div>
        </div>
      </div>

      <div v-if="!assistantOnly && uiDrivingEnabled && store.pendingPermission" class="assistant-permission-card">
        <div class="assistant-permission-header">
          <ShieldAlertIcon class="h-4 w-4" />
          <span>{{ $t('components.assistant.AssistantChat.permission_request') }}</span>
        </div>
        <div class="assistant-permission-tools">
          <div
            v-for="tool in store.pendingPermission.tools"
            :key="tool.name"
            class="assistant-permission-tool"
            :class="{ 'assistant-permission-tool-nogo': tool.nogo }"
          >
            <div class="flex items-center gap-2 min-w-0">
              <span class="font-mono text-xs truncate">{{ tool.name }}</span>
              <span v-if="tool.nogo" class="assistant-nogo-badge">?? Destructive Page</span>
            </div>
            <span class="text-xs text-muted-foreground">{{ describeArgs(tool) }}</span>
          </div>
        </div>
        <div class="assistant-permission-actions">
          <Button severity="secondary" outlined size="small" :disabled="nogoCountdown > 0" class="relative" @click="store.approvePermission(store.pendingPermission.request_id, 'reject')">Deny{{ nogoCountdown > 0 ? ` (${nogoCountdown}s)` : '' }}</Button>
          <Button severity="secondary" size="small" :disabled="nogoCountdown > 0" @click="store.approvePermission(store.pendingPermission.request_id, 'approve')">Allow Once{{ nogoCountdown > 0 ? ` (${nogoCountdown}s)` : '' }}</Button>
          <Button size="small" :disabled="nogoCountdown > 0" @click="store.approvePermission(store.pendingPermission.request_id, 'approve_for_session')">Allow for Session{{ nogoCountdown > 0 ? ` (${nogoCountdown}s)` : '' }}</Button>
        </div>
      </div>

      <div v-if="!assistantOnly && uiDrivingEnabled && store.isExecutingUi" class="assistant-executing-indicator">
        <LoaderIcon class="h-3 w-3 animate-spin" />
        <span>{{ store.isPaused ? 'Assistant is paused. Resume or stop?' : 'Assistant is performing actions in the browser...' }}</span>
        <div class="flex gap-2">
          <Button v-if="!store.isPaused" severity="secondary" size="small" @click="pauseAssistant">? Pause</Button>
          <Button v-if="store.isPaused" severity="secondary" size="small" @click="resumeAssistant">? Resume</Button>
          <Button severity="danger" size="small" @click="abortUiCommands">{{ store.isPaused ? '? Stop' : 'Stop' }}</Button>
        </div>
      </div>
    </div>

    <div class="assistant-input-area border-t p-3 relative">
      <div
        v-if="showSlashMenu"
        class="assistant-slash-menu"
      >
        <button type="button"
          v-for="(cmd, idx) in filteredSlashCommands"
          :key="cmd.command"
          class="assistant-slash-item"
          :class="{ active: slashHighlightIdx === idx }"
          @click="executeSlashCommand(cmd)"
          @mouseenter="slashHighlightIdx = idx"
          @focus="slashHighlightIdx = idx"
        >
          <span class="assistant-slash-command">{{ cmd.command }}</span>
          <span class="assistant-slash-desc">{{ cmd.description }}</span>
        </button>
        <div v-if="filteredSlashCommands.length === 0" class="assistant-slash-empty">
          {{ $t('components.assistant.AssistantChat.no_slash_commands') }}
        </div>
      </div>
      <div class="flex gap-2">
        <div class="assistant-input-wrapper flex-1">
          <div
            class="assistant-input-highlight"
            aria-hidden="true"
            v-html="styledInput"
          />
          <textarea
            ref="textareaRef"
            v-model="inputText"
            class="assistant-input"
            rows="1"
            aria-label="Chat input"
            @keydown="onInputKeydown"
            @input="onInput"
            @scroll="syncHighlightScroll"
            :disabled="store.isStreaming || store.isExecutingUi"
          />
        </div>
        <Button :disabled="!inputText.trim() || store.isStreaming || store.isExecutingUi" @click="handleSend" :aria-label="$t('components.assistant.send_message')">
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
        class="assistant-delete-confirm"
      >
        <p class="text-sm font-medium">{{ $t('components.assistant.AssistantChat.delete_confirm') }}</p>
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
import { useAssistantStore } from "@/composables/useAssistantStore";
import { usePlanStore } from "@/stores/planStore";
import { useAssistantStream } from "@/composables/useAssistantStream";
import { abortUiCommands } from "@/composables/useUiCommandExecutor";
import Button from 'primevue/button'
import { getAccessToken } from "@/lib/api/client";
import { ShieldAlertIcon, LoaderIcon } from "@lucide/vue";
import AnalyticsChart from "../analytics/AnalyticsChart.vue";
import { MEASURES, type AnalyticsBucket, type AnalyticsMeasure } from "../../stores/analytics";
import type { ChatMessage, ToolResult } from "@/types/assistant";

const store = useAssistantStore();
const planStore = usePlanStore();
const router = useRouter();
const props = defineProps<{ assistantOnly?: boolean }>();
const { connectStream, disconnectStream } = useAssistantStream();
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
    description: 'Close Assistant panel',
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
  // Match on the command token only, so typed arguments ("/rename Foo")
  // keep the matching command visible in the menu.
  const partial = text.slice(1).split(' ')[0].toLowerCase()
  if (!partial) return slashCommands
  return slashCommands.filter(c => c.command.slice(1).toLowerCase().startsWith(partial))
})

function onInput() {
  resizeInput()
  if (inputText.value.startsWith('/')) {
    showSlashMenu.value = true
    slashHighlightIdx.value = 0
  } else {
    showSlashMenu.value = false
  }
}

// Returns the slash command when the full input is that command plus
// arguments (e.g. "/rename New Name"), null for bare/partial commands.
function slashCommandWithArgs(text: string): SlashCommand | null {
  if (!text.startsWith('/')) return null
  const name = text.split(' ')[0]
  const cmd = slashCommands.find(c => c.command === name)
  return cmd && text.length > cmd.command.length ? cmd : null
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
      // A command with typed arguments is complete — execute it with the
      // full input so commands like "/rename Foo" receive their arguments.
      const withArgs = slashCommandWithArgs(inputText.value)
      if (withArgs) {
        executeSlashCommand(withArgs)
        return
      }
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
  // assistant-only mode: never auto-create a new session when none remain — the
  // tabs store reconciles the empty state.
  if (props.assistantOnly) return
  const sessions = store.sortedSessions
  if (sessions.length > 0) {
    await store.loadSession(sessions[0].id)
  } else {
    await store.createSession()
  }
}

const uiDrivingEnabled = computed(() => planStore.featureEnabled('assistant_ui_driving'))

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

async function pauseAssistant() {
  await store.pauseAssistant()
}

async function resumeAssistant() {
  await store.resumeAssistant()
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
      connectStream(store.activeSessionId, { excludeUiTools: !!props.assistantOnly });
    } catch (e) {
      console.error("Failed to start Assistant stream:", e);
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
  const hl = document.querySelector('.assistant-input-highlight') as HTMLElement | null
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
    codeBlocks.push(`<pre${langAttr}><code class="assistant-code-block">${code}</code></pre>`);
    return placeholder;
  });

  html = html.replace(/`([^`]+)`/g, '<code class="assistant-inline-code">$1</code>');

  html = html.replace(/### (.+)/g, '<h4 class="assistant-h3">$1</h4>');
  html = html.replace(/## (.+)/g, '<h3 class="assistant-h2">$1</h3>');
  html = html.replace(/# (.+)/g, '<h2 class="assistant-h1">$1</h2>');

  html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/\*([^*]+)\*/g, "<em>$1</em>");

  html = html.replace(/^- (.+)/gm, '<li class="assistant-li">$1</li>');
  html = html.replace(
    /(<li[\s\S]*?<\/li>\n?)+/g,
    '<ul class="assistant-ul">$&</ul>',
  );

  html = html.replace(/\n\n/g, '</p><p class="assistant-p">');
  html = html.replace(/\n/g, "<br/>");

  if (!html.trim().startsWith('<')) {
    html = '<p class="assistant-p">' + html + "</p>";
  }

  html = html.replace(/%%CODE_BLOCK_(\d+)%%/g, (_, i) => codeBlocks[Number(i)] ?? "");

  return html;
}
</script>

<style scoped>
@reference "../../style.css";
.assistant-messages {
  scroll-behavior: smooth;
}
.assistant-msg {
  @apply flex gap-2 text-sm;
}
.assistant-msg.user {
  @apply flex-row-reverse;
}
.assistant-msg-avatar {
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
.assistant-msg-content {
  @apply max-w-[80%] space-y-1;
}
.assistant-msg.user .assistant-msg-content {
  @apply items-end;
}
.assistant-plaintext {
  @apply rounded-xl px-3 py-2;
  background-color: hsl(var(--primary));
  color: hsl(var(--primary-foreground));
}
.assistant-markdown {
  @apply rounded-xl px-3 py-2 leading-relaxed;
  background-color: hsl(var(--muted));
  color: hsl(var(--foreground));
}
.assistant-msg-actions {
  @apply flex justify-end pt-1;
}
.assistant-copy-btn {
  @apply flex items-center justify-center rounded p-1 transition-colors;
  color: hsl(var(--muted-foreground));
}
.assistant-copy-btn:hover {
  color: hsl(var(--foreground));
  background-color: hsl(var(--accent));
}
.assistant-streaming-indicator {
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
.assistant-input-area {
  background-color: hsl(var(--card));
  border-color: hsl(var(--border));
}
.assistant-input-wrapper {
  position: relative;
  min-height: 38px;
}

.assistant-input-highlight {
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

.assistant-input {
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

.assistant-input::placeholder {
  color: hsl(var(--muted-foreground));
}

.assistant-input:focus {
  border-color: hsl(var(--ring));
  box-shadow: 0 0 0 1px hsla(var(--ring) / 0.3);
}
.assistant-input:disabled {
  opacity: 0.5;
}
.assistant-turn-separator {
  @apply flex items-center gap-3 px-2 py-2;
}
.assistant-turn-line {
  @apply flex-1 h-px;
  background-color: hsl(var(--border));
}
.assistant-turn-label {
  @apply text-xs font-medium shrink-0;
  color: hsl(var(--muted-foreground));
}
.assistant-permission-card {
  @apply rounded-lg border p-3 space-y-3 text-sm;
  background-color: hsl(var(--card));
  border-color: hsl(var(--border));
}
.assistant-permission-header {
  @apply flex items-center gap-2 font-medium;
  color: hsl(var(--warning));
}
.assistant-permission-tools {
  @apply space-y-1;
}
.assistant-permission-tool {
  @apply flex items-center gap-2 rounded-md px-2 py-1;
  background-color: hsl(var(--muted));
}
.assistant-permission-tool-nogo {
  border: 1px solid hsl(0 72% 51% / 0.3);
  background-color: hsl(0 72% 51% / 0.05);
}
.assistant-nogo-badge {
  @apply text-[10px] font-semibold px-1.5 py-0.5 rounded;
  background-color: hsl(0 72% 51% / 0.15);
  color: hsl(0 72% 72%);
}
.light .assistant-nogo-badge {
  color: hsl(0 72% 40%);
}
.assistant-permission-actions {
  @apply flex items-center gap-2;
}
.assistant-executing-indicator {
  @apply flex items-center gap-2 rounded-lg border px-3 py-2 text-sm;
  background-color: hsl(var(--muted));
  border-color: hsl(var(--border));
}
.assistant-tool-card {
  @apply rounded-lg border text-sm overflow-hidden;
  background-color: hsl(var(--card));
  border-color: hsl(var(--border));
}
.assistant-analytics-card {
  @apply rounded-lg border p-3 space-y-2 text-sm;
  background-color: hsl(var(--card));
  border-color: hsl(var(--border));
}
.assistant-analytics-header {
  @apply flex flex-wrap items-center justify-between gap-2;
}
.assistant-analytics-title {
  @apply text-sm font-semibold;
  color: hsl(var(--foreground));
}
.assistant-analytics-measures {
  @apply flex items-center gap-1 flex-wrap;
  border: 0;
  margin: 0;
  padding: 0;
}
.assistant-measure-btn {
  @apply rounded px-1.5 py-0.5 text-[11px] font-medium transition-colors;
  color: hsl(var(--muted-foreground));
}
.assistant-measure-btn:hover {
  color: hsl(var(--foreground));
  background-color: hsl(var(--accent));
}
.assistant-measure-btn.active {
  color: hsl(var(--primary-foreground));
  background-color: hsl(var(--primary));
}
.assistant-analytics-link {
  @apply inline-flex items-center gap-1 text-xs font-medium hover:opacity-80;
  color: hsl(var(--primary));
}
.assistant-analytics-link:focus-visible {
  outline: 2px solid hsl(var(--ring));
  outline-offset: 2px;
}
.assistant-tool-header {
  @apply flex items-center gap-2 w-full px-3 py-2 text-left;
  background-color: hsl(var(--muted));
  color: hsl(var(--foreground));
}
.assistant-tool-header:hover {
  background-color: hsl(var(--accent));
}
.assistant-tool-name {
  @apply flex-1 font-medium;
}
.tool-badge {
  @apply text-xs font-medium px-2 py-0.5 rounded-full;
}
.tool-badge.success {
  background-color: hsl(142 76% 36% / 0.15);
  color: hsl(142 76% 52%);
}
.tool-badge.failed {
  background-color: hsl(0 72% 51% / 0.15);
  color: hsl(0 72% 72%);
}
.light .tool-badge.success {
  color: hsl(142 76% 26%);
}
.light .tool-badge.failed {
  color: hsl(0 72% 40%);
}
.tool-chevron {
  @apply text-xs transition-transform duration-200;
  color: hsl(var(--muted-foreground));
}
.tool-chevron.expanded {
  transform: rotate(180deg);
}
.assistant-tool-details {
  @apply border-t px-3 py-2;
  border-color: hsl(var(--border));
}
.assistant-tool-details pre {
  @apply text-xs leading-relaxed whitespace-pre-wrap;
  color: hsl(var(--muted-foreground));
}
.assistant-slash-menu {
  @apply absolute bottom-full left-3 right-3 mb-1 rounded-lg border shadow-lg overflow-hidden z-50;
  background-color: hsl(var(--popover));
  border-color: hsl(var(--border));
  max-height: 240px;
  overflow-y: auto;
}
.assistant-slash-item {
  @apply flex items-center gap-3 w-full px-3 py-2 text-left text-sm transition-colors;
  color: hsl(var(--popover-foreground));
  border-left: 2px solid transparent;
}
.assistant-slash-item:hover,
.assistant-slash-item.active {
  background-color: hsl(var(--accent));
}
.assistant-slash-item.active {
  border-left: 2px solid hsl(var(--primary));
}
.assistant-slash-item.active .assistant-slash-command {
  color: hsl(var(--primary));
}
.assistant-slash-item.active .assistant-slash-desc {
  color: hsl(var(--foreground));
}

.assistant-slash-command {
  @apply font-mono font-medium shrink-0;
  color: hsl(var(--primary));
}
.assistant-slash-desc {
  @apply text-xs truncate;
  color: hsl(var(--muted-foreground));
}
.assistant-slash-empty {
  @apply px-3 py-2 text-sm;
  color: hsl(var(--muted-foreground));
}
.assistant-delete-confirm {
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
