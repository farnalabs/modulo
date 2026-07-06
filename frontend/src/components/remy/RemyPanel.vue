<template>
  <div
    v-if="store.panelState !== 'closed'"
    class="remy-panel"
    :class="panelClasses"
    :style="panelStyle"
  >
    <div class="remy-titlebar" @mousedown="startDrag">
      <div class="flex items-center gap-2 flex-1 min-w-0">
        <span class="text-sm font-semibold truncate">
          <template v-if="store.activeSession && store.activeSession.name">
            {{ store.activeSession.name }}
          </template>
          <template v-else-if="store.activeSession">
            Session {{ store.activeSession.session_number ? '#' + store.activeSession.session_number : shortId(store.activeSession.id) }}
          </template>
          <template v-else>Remy</template>
        </span>
        <span v-if="store.isStreaming" class="remy-pulse-dot" />
      </div>
      <div class="flex items-center gap-1">
        <button
          v-if="store.activeSessionId"
          class="remy-titlebar-btn"
          @click="store.resetSessionPermissions()"
          title="Reset Permissions"
        >
          <svg
            xmlns="http://www.w3.org/2000/svg"
            width="14"
            height="14"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
          >
            <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
          </svg>
        </button>
        <button
          v-if="store.panelState === 'floating'"
          class="remy-titlebar-btn"
          @click="store.setPanelState('docked')"
          title="Dock"
        >
          <svg
            xmlns="http://www.w3.org/2000/svg"
            width="14"
            height="14"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
          >
            <rect x="3" y="3" width="18" height="18" rx="2" />
            <line x1="9" y1="3" x2="9" y2="21" />
          </svg>
        </button>
        <button
          v-if="store.panelState !== 'maximised'"
          class="remy-titlebar-btn"
          @click="store.setPanelState('maximised')"
          title="Maximise"
        >
          <svg
            xmlns="http://www.w3.org/2000/svg"
            width="14"
            height="14"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
          >
            <rect x="3" y="3" width="18" height="18" rx="2" />
          </svg>
        </button>
        <button
          v-else
          class="remy-titlebar-btn"
          @click="store.setPanelState('docked')"
          title="Minimise"
        >
          <svg
            xmlns="http://www.w3.org/2000/svg"
            width="14"
            height="14"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
          >
            <rect x="3" y="3" width="18" height="18" rx="2" />
            <line x1="3" y1="12" x2="21" y2="12" />
          </svg>
        </button>
        <button
          class="remy-titlebar-btn"
          @click="store.setPanelState('closed')"
          title="Close"
        >
          <svg
            xmlns="http://www.w3.org/2000/svg"
            width="14"
            height="14"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
          >
            <line x1="18" y1="6" x2="6" y2="18" />
            <line x1="6" y1="6" x2="18" y2="18" />
          </svg>
        </button>
      </div>
    </div>

    <div
      v-if="store.error"
      class="flex items-center justify-between px-3 py-2 text-sm text-destructive bg-destructive/5 border-b"
    >
      <span>{{ store.error }}</span>
      <button
        class="text-destructive hover:brightness-110 shrink-0 ml-2"
        @click="store.error = null"
      >
        &times;
      </button>
    </div>
    <div class="remy-body">
      <div class="remy-sidebar" :class="{ open: showSidebar }">
        <RemySessionDrawer
          @close="showSidebar = false"
          @select-session="showSidebar = false"
        />
      </div>
      <div class="remy-main">
          <div class="remy-chat-tabs flex items-center border-b px-2">
            <button
              class="remy-tab"
              :class="{ active: activeTab === 'chat' }"
              @click="activeTab = 'chat'"
            >
              Chat
            </button>
            <button
              class="remy-tab"
              :class="{ active: activeTab === 'skills' }"
              @click="activeTab = 'skills'"
            >
              Skills
            </button>
            <button
              class="remy-tab"
              :class="{ active: activeTab === 'sessions' }"
              @click="activeTab = 'sessions'"
            >
              Sessions
            </button>
            <button
              class="remy-tab"
              :class="{ active: activeTab === 'sources' }"
              @click="activeTab = 'sources'"
            >
              Sources
            </button>
          </div>
          <RemyChat v-show="activeTab === 'chat'" ref="chatRef" />
          <RemySkillManager v-if="activeTab === 'skills'" />
          <div
            v-show="activeTab === 'sessions'"
            class="flex-1 overflow-auto p-2"
          >
            <RemySessionDrawer @select-session="activeTab = 'chat'" />
          </div>
          <RemyContextSources v-show="activeTab === 'sources'" />
      </div>
    </div>

    <div
      v-if="store.panelState === 'floating' || store.panelState === 'docked'"
      class="remy-resize-handle"
      @mousedown="startResize"
    />
  </div>

  <button
    v-else
    class="remy-floating-btn"
    @click="store.setPanelState('floating')"
    title="Open Remy"
  >
    <svg
      xmlns="http://www.w3.org/2000/svg"
      width="20"
      height="20"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="2"
    >
      <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
    </svg>
  </button>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted } from "vue";
import { shortId } from "@/utils/format";
import { useRemyStore } from "@/composables/useRemyStore";
import { useRemyContext } from "@/composables/useRemyContext";
import { Button } from "@/components/ui/button";
import RemyChat from "./RemyChat.vue";
import RemySessionDrawer from "./RemySessionDrawer.vue";
import RemySkillManager from "./RemySkillManager.vue";
import RemyContextSources from "./RemyContextSources.vue";

const store = useRemyStore();
const { pageContext } = useRemyContext();
watch(
  pageContext,
  (ctx) => {
    store.setPageContext(ctx);
  },
  { immediate: true },
);
const chatRef = ref<InstanceType<typeof RemyChat> | null>(null);
const showSidebar = ref(false);
const activeTab = ref<"chat" | "skills" | "sessions" | "sources">("chat");

const panelClasses = computed(() => ({
  "remy-floating": store.panelState === "floating",
  "remy-docked": store.panelState === "docked",
  "remy-maximised": store.panelState === "maximised",
}));

const panelStyle = computed(() => {
  if (store.panelState === "maximised") return {};
  if (store.panelState === "docked") {
    return { width: "400px", height: "100vh" };
  }
  return {
    left: `${store.panelPosition.x}px`,
    top: `${store.panelPosition.y}px`,
    width: `${store.panelSize.width}px`,
    height: `${store.panelSize.height}px`,
  };
});

const dragging = ref(false);
const dragStart = ref({ x: 0, y: 0, posX: 0, posY: 0 });
const resizing = ref(false);
const resizeStart = ref({ x: 0, y: 0, w: 0, h: 0 });

function startDrag(e: MouseEvent) {
  if (store.panelState !== "floating") return;
  dragging.value = true;
  dragStart.value = {
    x: e.clientX,
    y: e.clientY,
    posX: store.panelPosition.x,
    posY: store.panelPosition.y,
  };
  document.addEventListener("mousemove", onDrag);
  document.addEventListener("mouseup", stopDrag);
}

function onDrag(e: MouseEvent) {
  if (!dragging.value) return;
  store.updatePosition({
    x: Math.max(8, Math.min(dragStart.value.posX + (e.clientX - dragStart.value.x), window.innerWidth - 340)),
    y: Math.max(8, Math.min(dragStart.value.posY + (e.clientY - dragStart.value.y), window.innerHeight - 100)),
  });
}

function stopDrag() {
  dragging.value = false;
  document.removeEventListener("mousemove", onDrag);
  document.removeEventListener("mouseup", stopDrag);
}

function startResize(e: MouseEvent) {
  resizing.value = true;
  resizeStart.value = {
    x: e.clientX,
    y: e.clientY,
    w: store.panelSize.width,
    h: store.panelSize.height,
  };
  document.addEventListener("mousemove", onResize);
  document.addEventListener("mouseup", stopResize);
}

function onResize(e: MouseEvent) {
  if (!resizing.value) return;
  store.updateSize({
    width: Math.min(Math.max(320, resizeStart.value.w + (e.clientX - resizeStart.value.x)), window.innerWidth - 16),
    height: Math.min(Math.max(400, resizeStart.value.h + (e.clientY - resizeStart.value.y)), window.innerHeight - 40),
  });
}

function stopResize() {
  resizing.value = false;
  document.removeEventListener("mousemove", onResize);
  document.removeEventListener("mouseup", stopResize);
}

async function handleNewSession() {
  try {
    const session = await store.createSession();
    if (session) {
      store.setPanelState("floating");
      activeTab.value = "chat";
    }
  } catch (e) {
    console.error("Failed to create session:", e);
  }
}

onMounted(async () => {
  await store.fetchSessions();
  if (!store.activeSessionId) {
    await handleNewSession();
  }
});

onUnmounted(() => {
  document.removeEventListener("mousemove", onDrag);
  document.removeEventListener("mouseup", stopDrag);
  document.removeEventListener("mousemove", onResize);
  document.removeEventListener("mouseup", stopResize);
});
</script>

<style scoped>
.remy-panel {
  @apply fixed z-50 flex flex-col border rounded-lg shadow-2xl overflow-hidden;
  background-color: hsl(var(--background));
  border-color: hsl(var(--border));
  transition:
    width 150ms ease,
    height 150ms ease,
    left 150ms ease,
    top 150ms ease;
}
.remy-floating {
  border-radius: var(--radius-lg);
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.3);
}
.remy-docked {
  top: 0;
  right: 0;
  border-radius: 0;
  border-top: none;
  border-bottom: none;
  border-right: none;
}
.remy-maximised {
  inset: 0;
  border-radius: 0;
}
.remy-titlebar {
  @apply flex items-center px-3 py-2 border-b select-none cursor-grab;
  background-color: hsl(var(--card));
  border-color: hsl(var(--border));
  min-height: 40px;
}
.remy-titlebar:active {
  cursor: grabbing;
}
.remy-titlebar-btn {
  @apply flex items-center justify-center rounded p-1 transition-colors;
  color: hsl(var(--muted-foreground));
}
.remy-titlebar-btn:hover {
  background-color: hsl(var(--accent));
  color: hsl(var(--foreground));
}
.remy-pulse-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background-color: hsl(var(--primary));
  animation: pulse-dot 1.5s ease-in-out infinite;
}
@keyframes pulse-dot {
  0%,
  100% {
    opacity: 1;
    transform: scale(1);
  }
  50% {
    opacity: 0.5;
    transform: scale(0.8);
  }
}
.remy-body {
  @apply flex flex-1 overflow-hidden;
}
.remy-sidebar {
  @apply w-64 border-r overflow-auto shrink-0 transition-transform;
  background-color: hsl(var(--card));
  border-color: hsl(var(--border));
}
.remy-sidebar:not(.open) {
  display: none;
}
.remy-main {
  @apply flex flex-col flex-1 overflow-hidden;
}
.remy-chat-tabs {
  background-color: hsl(var(--card));
}
.remy-tab {
  @apply px-3 py-2 text-xs font-medium transition-colors border-b-2 border-transparent;
  color: hsl(var(--muted-foreground));
}
.remy-tab:hover {
  color: hsl(var(--foreground));
}
.remy-tab.active {
  color: hsl(var(--primary));
  border-bottom-color: hsl(var(--primary));
}
.remy-floating-btn {
  @apply fixed bottom-6 right-6 z-50 flex items-center justify-center rounded-full shadow-lg transition-all;
  width: 48px;
  height: 48px;
  background-color: hsl(var(--primary));
  color: hsl(var(--primary-foreground));
  border: 1px solid hsla(var(--primary) / 0.3);
}
.remy-floating-btn:hover {
  transform: scale(1.05);
  box-shadow: 0 4px 20px hsla(var(--primary) / 0.3);
}
.remy-resize-handle {
  position: absolute;
  bottom: 0;
  right: 0;
  width: 12px;
  height: 12px;
  cursor: nwse-resize;
  background: linear-gradient(135deg, transparent 50%, hsl(var(--border)) 50%);
}
</style>
