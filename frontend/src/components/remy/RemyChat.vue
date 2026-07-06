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
            <p class="remy-p">Hi! I'm Remy, your Modulo AI assistant. I can help you build pipelines, run evaluations, manage your workspace, and answer questions about your data. What would you like help with?</p>
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
        <div
          v-else-if="msg.role === 'tool_result' && msg.tool_results_json"
          class="remy-tool-card"
        >
          <button class="remy-tool-header" @click="toggleToolExpand(msg.id)">
            <span class="remy-tool-name">🛠 Tool Called: {{ (msg.tool_results_json as any).tool_name }}</span>
            <span class="tool-badge" :class="(msg.tool_results_json as any).success ? 'success' : 'failed'">
              {{ (msg.tool_results_json as any).success ? 'Completed' : 'Failed' }}
            </span>
            <span class="tool-chevron" :class="{ expanded: expandedTools.has(msg.id) }">▼</span>
          </button>
          <div v-if="expandedTools.has(msg.id)" class="remy-tool-details">
            <pre>{{ formatToolDetails(msg.tool_results_json as any) }}</pre>
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
                class="remy-copy-btn"
                @click="copyMessage(msg.content ?? '')"
                title="Copy"
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

      <div v-if="store.pendingPermission" class="remy-permission-card">
        <div class="remy-permission-header">
          <ShieldAlertIcon class="h-4 w-4" />
          <span>Remy wants to perform actions on your behalf</span>
        </div>
        <div class="remy-permission-tools">
          <div
            v-for="tool in store.pendingPermission.tools"
            :key="tool.name"
            class="remy-permission-tool"
          >
            <span class="font-mono text-xs">{{ tool.name }}</span>
            <span class="text-xs text-muted-foreground">{{ describeArgs(tool) }}</span>
          </div>
        </div>
        <div class="remy-permission-actions">
          <Button variant="outline" size="sm" @click="store.approvePermission(store.pendingPermission.request_id, 'reject')">Deny</Button>
          <Button variant="secondary" size="sm" @click="store.approvePermission(store.pendingPermission.request_id, 'approve')">Allow Once</Button>
          <Button size="sm" @click="store.approvePermission(store.pendingPermission.request_id, 'approve_for_session')">Allow for Session</Button>
        </div>
      </div>

      <div v-if="store.isExecutingUi" class="remy-executing-indicator">
        <LoaderIcon class="h-3 w-3 animate-spin" />
        <span>Remy is performing actions in the browser...</span>
        <Button variant="destructive" size="sm" @click="abortUiCommands">Stop</Button>
      </div>
    </div>

    <div class="remy-input-area border-t p-3">
      <div class="flex gap-2">
        <textarea
          v-model="inputText"
          class="remy-input flex-1"
          :placeholder="$t('components.remy.RemyChat.ask_remy')"
          rows="1"
          @keydown="onInputKeydown"
          @input="resizeInput"
          :disabled="store.isStreaming || store.isExecutingUi"
        />
        <Button
          :disabled="!inputText.trim() || store.isStreaming || store.isExecutingUi"
          @click="handleSend"
        >
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
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, watch, nextTick, computed } from "vue";
import { useRemyStore } from "@/composables/useRemyStore";
import { useRemyStream } from "@/composables/useRemyStream";
import { abortUiCommands } from "@/composables/useUiCommandExecutor";
import { Button } from "@/components/ui/button";
import { getAccessToken } from "@/lib/api/client";
import { ShieldAlertIcon, LoaderIcon } from "@lucide/vue";

const store = useRemyStore();
const { connectStream } = useRemyStream();
const scrollRef = ref<HTMLDivElement | null>(null);
const inputText = ref("");

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
  return m ? m[1].replace(/-/g, ' ') : sel
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
      connectStream(store.activeSessionId);
    } catch (e) {
      console.error("Failed to start Remy stream:", e);
    }
  }
}

function onInputKeydown(e: KeyboardEvent) {
  if (e.key === 'Enter' && !e.shiftKey && !e.ctrlKey && !e.metaKey) {
    e.preventDefault()
    handleSend()
  }
}

const inputRef = ref<HTMLTextAreaElement | null>(null)

function resizeInput() {
  nextTick(() => {
    const el = document.querySelector('.remy-input') as HTMLTextAreaElement | null
    if (el) {
      el.style.height = 'auto'
      el.style.height = Math.min(el.scrollHeight, 200) + 'px'
    }
  })
}

function copyMessage(text: string) {
  navigator.clipboard.writeText(text).catch((e) => {
    console.warn("Clipboard write failed:", e);
  });
}

function escapeHtml(text: string): string {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

function renderMarkdown(text: string): string {
  if (!text) return "";
  let html = escapeHtml(text);

  const codeBlocks: string[] = [];
  html = html.replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) => {
    const langAttr = lang ? ` data-lang="${escapeHtml(lang)}"` : "";
    const placeholder = `\x00CODE_BLOCK_${codeBlocks.length}\x00`;
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

  html = '<p class="remy-p">' + html + "</p>";

  html = html.replace(/\x00CODE_BLOCK_(\d+)\x00/g, (_, i) => codeBlocks[Number(i)] ?? "");

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
.remy-input {
  @apply rounded-lg px-3 py-2 text-sm outline-none resize-none;
  background-color: hsl(var(--background));
  border: 1px solid hsl(var(--input));
  color: hsl(var(--foreground));
  overflow-y: auto;
  min-height: 38px;
  max-height: 200px;
  line-height: 1.4;
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
</style>
