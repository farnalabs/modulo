<script setup lang="ts">
import { onMounted } from "vue";
import { useCompositeStore } from "../../../stores/compositeStore";
import Card from "../../ui/card/Card.vue";
import CardContent from "../../ui/card/CardContent.vue";
import CardHeader from "../../ui/card/CardHeader.vue";
import CardTitle from "../../ui/card/CardTitle.vue";
import CardDescription from "../../ui/card/CardDescription.vue";

const emit = defineEmits<{
  (e: "add", compositeId: string): void;
}>();

const compositeStore = useCompositeStore();

onMounted(() => {
  compositeStore.loadComposites();
});
</script>

<template>
  <div class="space-y-3">
    <div class="flex items-center justify-between">
      <h4 class="text-sm font-medium">{{ $t('components.pipeline.composite.CompositeLibraryPicker.composite_library') }}</h4>
      <span
        v-if="compositeStore.composites.length"
        class="text-xs text-muted-foreground"
      >
        {{ compositeStore.composites.length }} available
      </span>
    </div>

    <div
      v-if="compositeStore.loading"
      class="py-8 text-center text-sm text-muted-foreground"
    >
      <div
        class="mx-auto h-5 w-5 animate-spin rounded-full border-2 border-indigo-500 border-t-transparent"
      />
      <p class="mt-2">{{ $t('components.pipeline.composite.CompositeLibraryPicker.loading_composites') }}</p>
    </div>

    <div
      v-else-if="compositeStore.error"
      class="rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive"
    >
      {{ compositeStore.error }}
    </div>

    <div
      v-else-if="compositeStore.composites.length === 0"
      class="rounded-lg border border-dashed border-muted-foreground/30 p-6 text-center text-sm text-muted-foreground"
    >
      <p>{{ $t('components.pipeline.composite.CompositeLibraryPicker.no_composites_in_library_yet') }}</p>
    </div>

    <div v-else class="grid grid-cols-1 gap-3">
      <Card
        v-for="composite in compositeStore.composites"
        :key="composite.id"
        size="sm"
      >
        <CardHeader>
          <div class="flex items-center justify-between">
            <CardTitle class="text-sm">{{ composite.name }}</CardTitle>
            <span
              class="rounded border border-indigo-500/30 bg-indigo-500/10 px-1.5 py-0.5 text-[10px] text-indigo-300"
            >
              v{{ composite.version }}
            </span>
          </div>
          <CardDescription v-if="composite.description" class="text-xs">
            {{ composite.description }}
          </CardDescription>
        </CardHeader>
        <CardContent class="flex items-center justify-between">
          <span class="text-xs text-muted-foreground">
            {{ composite.parameter_ports_json.length }}
            {{ composite.parameter_ports_json.length === 1 ? "port" : "ports" }}
          </span>
          <button
            class="rounded-lg bg-indigo-600 px-3 py-1 text-xs font-medium text-white hover:bg-indigo-500"
            @click="emit('add', composite.id)"
          >
            Add to pipeline
          </button>
        </CardContent>
      </Card>
    </div>
  </div>
</template>
