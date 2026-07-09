<script setup lang="ts">
import type { HTMLAttributes } from "vue";
import { cn } from "@/lib/utils";

const props = withDefaults(
  defineProps<{
    class?: HTMLAttributes["class"];
    size?: "default" | "sm";
  }>(),
  {
    size: "default",
  },
);
</script>

<template>
  <div
    data-slot="card"
    :data-size="size"
    :class="
      cn(
        'bg-card text-card-foreground gap-4 overflow-hidden p-5 text-sm border border-border rounded-[var(--radius-lg)] has-data-[slot=card-footer]:pb-0 has-[>img:first-child]:pt-0 data-[size=sm]:gap-3 data-[size=sm]:p-3 data-[size=sm]:has-data-[slot=card-footer]:pb-0 *:[img:first-child]:rounded-t-[var(--radius-lg)] *:[img:last-child]:rounded-b-[var(--radius-lg)] group/card relative flex flex-col',
        props.class,
      )
    "
  >
    <slot />
  </div>
</template>
