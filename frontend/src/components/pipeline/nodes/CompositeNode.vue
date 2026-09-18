<script setup lang="ts">
import { computed } from "vue";
import { useNode } from "@vue-flow/core";
import { useCompositeStore } from "../../../stores/compositeStore";

const { id, node } = useNode<{
  label?: string;
  compositeRef?: string;
  compositeParameterValues?: Record<string, unknown>;
  portCount?: number;
  totalPorts?: number;
}>();

const emit = defineEmits<{
  (e: "expand", nodeId: string): void;
}>();

const compositeStore = useCompositeStore();

const compositeName = computed(() => {
  if (!node.data.compositeRef) return "";
  const c = compositeStore.getCompositeById(node.data.compositeRef);
  return c?.name ?? node.data.compositeRef ?? "";
});

const portIndicator = computed(() => {
  const set = Object.keys(
    node.data.compositeParameterValues ?? {},
  ).length;
  const total = node.data.totalPorts ?? node.data.portCount ?? 0;
  return `${set}/${total} ports set`;
});
</script>

<template>
  <div
    class="relative rounded-lg border-2 border-indigo-500/60 bg-indigo-500/10 px-4 py-3 shadow-sm"
  >
    <span
      class="absolute -top-2 left-2 rounded-full border border-indigo-500/40 bg-indigo-500/20 px-2 py-0.5 text-[10px] font-medium text-indigo-300"
    >
      Composite
    </span>
    <div class="mt-1 text-sm font-semibold text-indigo-100">
      {{ compositeName || node.data.label || "Composite" }}
    </div>
    <div class="mt-0.5 flex items-center gap-2">
      <span class="text-[10px] text-indigo-300/70">{{ portIndicator }}</span>
    </div>
    <button type="button"
      class="mt-1 inline-flex items-center gap-1 rounded bg-indigo-500/20 px-2 py-0.5 text-[10px] text-indigo-300 hover:bg-indigo-500/30"
      @click.stop="emit('expand', id)"
    >
      <svg
        xmlns="http://www.w3.org/2000/svg"
        width="10"
        height="10"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
        stroke-linecap="round"
        stroke-linejoin="round"
      >
        <polyline points="15 3 21 3 21 9" />
        <polyline points="9 21 3 21 3 15" />
        <line x1="21" y1="3" x2="14" y2="10" />
        <line x1="3" y1="21" x2="10" y2="14" />
      </svg>
      Expand
    </button>
  </div>
</template>
