<template>
  <div class="remy-chat flex flex-col flex-1 overflow-hidden">
    <div
      ref="scrollRef"
      class="remy-messages flex-1 overflow-y-auto p-3 space-y-3"
    >
      <div
        v-for="msg in store.messages"
        :key="msg.id"
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
    </div>

    <div class="remy-input-area border-t p-3">
      <div class="flex gap-2">
        <input
          v-model="inputText"
          class="remy-input flex-1"
          placeholder="Ask Remy..."
          @keydown.enter.prevent="handleSend"
          :disabled="store.isStreaming"
        />
        <Button
          :disabled="!inputText.trim() || store.isStreaming"
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
import Button from "@/components/ui/button/Button.vue";
import { getAccessToken } from "@/lib/api/client";

const store = useRemyStore();
const { connectStream } = useRemyStream();
const scrollRef = ref<HTMLDivElement | null>(null);
const inputText = ref("");

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

function scrollToBottom() {
  nextTick(() => {
    if (scrollRef.value) {
      scrollRef.value.scrollTop = scrollRef.value.scrollHeight;
    }
  });
}

watch(() => [store.messages.length, store.isStreaming], scrollToBottom);

async function handleSend() {
  const text = inputText.value.trim();
  if (!text || store.isStreaming) return;
  inputText.value = "";
  await store.sendMessage(text);
  if (store.activeSessionId) {
    connectStream(store.activeSessionId);
  }
}

function copyMessage(text: string) {
  navigator.clipboard.writeText(text).catch(() => {});
}

function escapeHtml(text: string): string {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

function renderMarkdown(text: string): string {
  if (!text) return "";
  let html = escapeHtml(text);

  html = html.replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) => {
    const langAttr = lang ? ` data-lang="${escapeHtml(lang)}"` : "";
    return `<pre${langAttr}><code class="remy-code-block">${code}</code></pre>`;
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
  @apply rounded-lg px-3 py-2 text-sm outline-none;
  background-color: hsl(var(--background));
  border: 1px solid hsl(var(--input));
  color: hsl(var(--foreground));
}
.remy-input:focus {
  border-color: hsl(var(--ring));
  box-shadow: 0 0 0 1px hsla(var(--ring) / 0.3);
}
.remy-input:disabled {
  opacity: 0.5;
}
</style>
