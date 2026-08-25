<script setup lang="ts">
import { ref, computed } from "vue";
import { useRouter } from "vue-router";
import { useApi } from "../../../composables/useApi";
import type { ParameterPort } from "../../../types/pipeline";
import Button from 'primevue/button'
import { formatApiError } from "../../../lib/api/formatError";

const props = defineProps<{
  compositeId: string;
  ports: ParameterPort[];
}>();

const emit = defineEmits<{
  (e: "close"): void;
  (e: "published"): void;
}>();

const { patch, post } = useApi();
const router = useRouter();

const step = ref(1);
const loading = ref(false);
const error = ref<string | null>(null);
const success = ref(false);

const primaryLabel = computed(() => {
  if (loading.value) return "Processing...";
  return step.value < 4 ? "Next" : "Publish";
});

// Step 1: Name & Description
const name = ref("");
const description = ref("");

// Step 2: Review ports
// Step 3: Set version
const version = ref("1.0.0");

// Step 4: Confirm

const steps = computed(() => [
  { num: 1, label: "Name & Description", done: !!name.value.trim() },
  { num: 2, label: "Review Ports", done: false },
  { num: 3, label: "Version", done: !!version.value.trim() },
  { num: 4, label: "Confirm", done: false },
]);

const canProceed = computed(() => {
  switch (step.value) {
    case 1:
      return !!name.value.trim();
    case 2:
      return true;
    case 3:
      return !!version.value.trim();
    case 4:
      return true;
    default:
      return false;
  }
});

async function nextStep() {
  if (step.value === 1) {
    // Update the composite name and description
    loading.value = true;
    error.value = null;
    try {
      await patch(`/api/v1/composite-templates/${props.compositeId}`, {
        name: name.value.trim(),
        description: description.value.trim() || null,
      });
      step.value = 2;
    } catch (e) {
      error.value = formatApiError(e);
    } finally {
      loading.value = false;
    }
  } else if (step.value < 4) {
    step.value += 1;
  } else {
    loading.value = true;
    error.value = null;
    try {
      await post(`/api/v1/composite-templates/${props.compositeId}/publish`, {
        version: version.value.trim(),
      });
      success.value = true;
      emit("published");
    } catch (e) {
      error.value = formatApiError(e);
    } finally {
      loading.value = false;
    }
  }
}

function prevStep() {
  step.value = Math.max(1, step.value - 1);
}

function portRef(port: { name: string }) {
  return `{{parameter.${port.name}}}`;
}

function goToLibrary() {
  router.push({ name: "library" });
}
</script>

<template>
  <div role="button" tabindex="0" @keydown.enter="($event.currentTarget as HTMLElement).click()" @keydown.space.prevent="($event.currentTarget as HTMLElement).click()"
    class="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
    @click.self="emit('close')"
  >
    <div class="w-full max-w-lg rounded-lg border bg-card p-6 shadow-lg">
      <!-- Step indicator -->
      <div class="mb-6 flex items-center justify-between">
        <div v-for="s in steps" :key="s.num" class="flex items-center">
          <div
            class="flex h-7 w-7 items-center justify-center rounded-full text-xs font-medium"
            :class="
              step === s.num
                ? 'bg-indigo-600 text-white'
                : step > s.num
                  ? 'bg-green-600 text-white'
                  : 'bg-muted text-muted-foreground'
            "
          >
            {{ step > s.num ? "✓" : s.num }}
          </div>
          <span
            class="ml-2 text-xs"
            :class="
              step === s.num
                ? 'font-semibold text-foreground'
                : 'text-muted-foreground'
            "
          >
            {{ s.label }}
          </span>
          <div v-if="s.num < 4" class="mx-3 h-px w-8 bg-border" />
        </div>
      </div>

      <!-- Step 1: Name & Description -->
      <div v-if="step === 1" class="space-y-4">
        <p class="text-sm text-muted-foreground">
          Give your composite template a name and description.
        </p>
        <div>
          <label for="publishcompositeflow-field-3" class="mb-1 block text-sm font-medium">{{ $t('components.pipeline.composite.PortDefinitionPanel.name') }}</label>
          <input id="publishcompositeflow-field-3"
            v-model="name"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
            :placeholder="$t('components.pipeline.composite.PublishCompositeFlow.code_review_assistant')"
          />
        </div>
        <div>
          <label for="publishcompositeflow-field-2" class="mb-1 block text-sm font-medium">{{ $t('components.pipeline.composite.PublishCompositeFlow.description') }}</label>
          <textarea id="publishcompositeflow-field-2"
            v-model="description"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
            rows="3"
            :placeholder="$t('components.pipeline.composite.PublishCompositeFlow.a_reusable_composite_that_performs_code_review_across_multip')"
          />
        </div>
      </div>

      <!-- Step 2: Review Ports -->
      <div v-if="step === 2" class="space-y-3">
        <p class="text-sm text-muted-foreground">
          Review the parameter ports that pipeline authors can configure.
        </p>
        <div
          v-if="ports.length === 0"
          class="rounded-lg border border-dashed border-muted-foreground/30 p-6 text-center text-sm text-muted-foreground"
        >
          No parameter ports defined. Pipeline authors won't be able to
          configure any values.
        </div>
        <div
          v-for="port in ports"
          :key="port.id"
          class="rounded-lg border bg-muted/30 p-3"
        >
          <div class="flex items-center justify-between">
            <div>
              <span class="text-sm font-medium">{{ port.label }}</span>
              <span
                v-if="port.required"
                class="ml-2 rounded bg-destructive/10 px-1.5 py-0.5 text-[10px] text-destructive"
                >required</span
              >
            </div>
            <span
              class="rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground"
              >{{ port.type }}</span
            >
          </div>
          <p
            v-if="port.description"
            class="mt-0.5 text-xs text-muted-foreground"
          >
            {{ port.description }}
          </p>
          <code class="mt-1 block text-[10px] text-indigo-400">{{
            portRef(port)
          }}</code>
        </div>
      </div>

      <!-- Step 3: Version -->
      <div v-if="step === 3" class="space-y-4">
        <p class="text-sm text-muted-foreground">
          Set the version for this composite template. Semantic versioning
          recommended.
        </p>
        <div>
          <label for="publishcompositeflow-field-1" class="mb-1 block text-sm font-medium">{{ $t('components.pipeline.composite.PublishCompositeFlow.version') }}</label>
          <input id="publishcompositeflow-field-1"
            v-model="version"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm font-mono"
            placeholder="1.0.0"
          />
        </div>
      </div>

      <!-- Step 4: Confirm -->
      <div v-if="step === 4" class="space-y-4">
        <p class="text-sm text-muted-foreground">
          Ready to publish this composite template. Once published, it will be
          available in the library for reuse.
        </p>
        <div class="rounded-lg border bg-muted/30 p-4 space-y-2">
          <div class="flex justify-between text-sm">
            <span class="text-muted-foreground">{{ $t('components.pipeline.composite.PublishCompositeFlow.name_label') }}</span>
            <span class="font-medium">{{ name }}</span>
          </div>
          <div v-if="description" class="flex justify-between text-sm">
            <span class="text-muted-foreground">{{ $t('components.pipeline.composite.PublishCompositeFlow.description') }}</span>
            <span class="font-medium text-right max-w-[200px]">{{
              description
            }}</span>
          </div>
          <div class="flex justify-between text-sm">
            <span class="text-muted-foreground">{{ $t('components.pipeline.composite.PublishCompositeFlow.ports') }}</span>
            <span class="font-medium">{{ ports.length }}</span>
          </div>
          <div class="flex justify-between text-sm">
            <span class="text-muted-foreground">{{ $t('components.pipeline.composite.PublishCompositeFlow.version_label') }}</span>
            <span class="font-medium">{{ version }}</span>
          </div>
        </div>
      </div>

      <!-- Success -->
      <div v-if="success" class="space-y-4">
        <div
          class="flex items-center gap-3 rounded-lg border border-green-600/30 bg-green-600/10 p-4"
        >
          <span
            class="flex h-8 w-8 items-center justify-center rounded-full bg-green-600 text-lg font-bold text-white"
            >✓</span
          >
          <div>
            <p class="text-sm font-medium text-green-700 dark:text-green-300">
              Published!
            </p>
            <p class="text-xs text-muted-foreground">
              Version {{ version }} is now available in the library.
            </p>
          </div>
        </div>
        <div class="flex justify-end gap-2">
          <button type="button"
            class="rounded-lg border border-input bg-background px-4 py-2 text-sm hover:bg-accent"
            @click="emit('close')"
          >
            Stay Here
          </button>
          <Button @click="goToLibrary">
            Go to Library
          </Button>
        </div>
      </div>

      <!-- Error -->
      <div
        v-if="error && !success"
        class="mt-4 rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive"
      >
        {{ error }}
      </div>

      <!-- Navigation -->
      <div v-if="!success" class="mt-6 flex justify-between">
        <button type="button"
          v-if="step > 1"
          class="rounded-lg border border-input bg-background px-4 py-2 text-sm hover:bg-accent"
          @click="prevStep"
        >
          Back
        </button>
        <div v-else />
        <button type="button"
          :disabled="!canProceed || loading"
          class="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-500 disabled:opacity-50"
          @click="nextStep"
        >
          {{ primaryLabel }}
        </button>
      </div>
    </div>
  </div>
</template>
