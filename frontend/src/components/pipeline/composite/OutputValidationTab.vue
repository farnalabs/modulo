<script setup lang="ts">
import { ref, computed } from "vue";
import { Button } from "../../ui/button";
import { Input } from "../../ui/input";
import { Badge } from "../../ui/badge";

interface EvalConfig {
  id: string;
  name: string;
  type: "regex" | "json_schema" | "llm_judge";
  config: Record<string, unknown>;
  failure_behaviour: "retry" | "block" | "warn";
}

const props = defineProps<{
  evalDefinitions: EvalConfig[];
  maxValidationRetries: number;
}>();

const emit = defineEmits<{
  (e: "update:evalDefinitions", val: EvalConfig[]): void;
  (e: "update:maxValidationRetries", val: number): void;
}>();

const localRetries = ref(props.maxValidationRetries);

function addEval() {
  const newEval: EvalConfig = {
    id: crypto.randomUUID(),
    name: "",
    type: "regex",
    config: { field: "", pattern: "" },
    failure_behaviour: "retry",
  };
  emit("update:evalDefinitions", [...props.evalDefinitions, newEval]);
}

function removeEval(id: string) {
  emit(
    "update:evalDefinitions",
    props.evalDefinitions.filter((e) => e.id !== id),
  );
}

function updateEval(id: string, patch: Partial<EvalConfig>) {
  emit(
    "update:evalDefinitions",
    props.evalDefinitions.map((e) => (e.id === id ? { ...e, ...patch } : e)),
  );
}

function updateConfig(id: string, configPatch: Record<string, unknown>) {
  emit(
    "update:evalDefinitions",
    props.evalDefinitions.map((e) =>
      e.id === id ? { ...e, config: { ...e.config, ...configPatch } } : e,
    ),
  );
}

function updateRetries(e: Event) {
  const target = e.target as HTMLInputElement;
  const val = parseInt(target.value, 10);
  if (!isNaN(val)) {
    localRetries.value = val;
    emit("update:maxValidationRetries", val);
  }
}

const evalCount = computed(() => props.evalDefinitions.length);
</script>

<template>
  <div class="space-y-4">
    <div class="flex items-center justify-between">
      <label class="text-sm font-medium">{{ $t('components.pipeline.composite.OutputValidationTab.output_validation') }}</label>
      <Badge variant="outline" class="text-xs">
        {{ evalCount }} eval{{ evalCount === 1 ? "" : "s" }} configured
      </Badge>
    </div>

    <div
      v-if="evalCount === 0"
      class="rounded-lg border border-dashed border-muted-foreground/30 p-6 text-center text-sm text-muted-foreground"
    >
      No output validation evals configured.
    </div>

    <div
      v-for="(evalDef, idx) in evalDefinitions"
      :key="evalDef.id"
      class="rounded-lg border border-border bg-card p-4 space-y-3"
    >
      <div class="flex items-center justify-between">
        <span
          class="text-xs font-semibold uppercase tracking-wide text-muted-foreground"
        >
          Eval #{{ idx + 1 }}
        </span>
        <button
          class="text-xs text-destructive hover:underline"
          @click="removeEval(evalDef.id)"
        >
          Remove
        </button>
      </div>

      <div class="grid grid-cols-2 gap-3">
        <div class="space-y-1">
          <label class="text-xs text-muted-foreground">Name</label>
          <Input
            :model-value="evalDef.name"
            :placeholder="$t('components.pipeline.composite.OutputValidationTab.eval_name')"
            @update:model-value="
              (val: string | number) =>
                updateEval(evalDef.id, { name: String(val) })
            "
          />
        </div>
        <div class="space-y-1">
          <label class="text-xs text-muted-foreground">Type</label>
          <select
            :value="evalDef.type"
            class="bg-background border-input focus-visible:border-ring h-8 w-full rounded-lg border px-2.5 py-1 text-sm"
            @change="
              (e: Event) =>
                updateEval(evalDef.id, {
                  type: (e.target as HTMLSelectElement)
                    .value as EvalConfig['type'],
                })
            "
          >
            <option value="regex">Regex</option>
            <option value="json_schema">{{ $t('components.pipeline.composite.OutputValidationTab.json_schema') }}</option>
            <option value="llm_judge">{{ $t('components.pipeline.composite.OutputValidationTab.llm_judge') }}</option>
          </select>
        </div>
      </div>

      <div class="space-y-1">
        <label class="text-xs text-muted-foreground">{{ $t('components.pipeline.composite.OutputValidationTab.failure_behaviour') }}</label>
        <select
          :value="evalDef.failure_behaviour"
          class="bg-background border-input focus-visible:border-ring h-8 w-full rounded-lg border px-2.5 py-1 text-sm"
          @change="
            (e: Event) =>
              updateEval(evalDef.id, {
                failure_behaviour: (e.target as HTMLSelectElement)
                  .value as EvalConfig['failure_behaviour'],
              })
          "
        >
          <option value="retry">Retry</option>
          <option value="block">Block</option>
          <option value="warn">Warn</option>
        </select>
      </div>

      <template v-if="evalDef.type === 'regex'">
        <div class="space-y-1">
          <label class="text-xs text-muted-foreground">Field</label>
          <Input
            :model-value="String(evalDef.config.field ?? '')"
            :placeholder="$t('components.pipeline.composite.OutputValidationTab.output_field_name')"
            @update:model-value="
              (val: string | number) =>
                updateConfig(evalDef.id, { field: String(val) })
            "
          />
        </div>
        <div class="space-y-1">
          <label class="text-xs text-muted-foreground">Pattern</label>
          <textarea
            :value="String(evalDef.config.pattern ?? '')"
            class="bg-background border-input focus-visible:border-ring h-20 w-full rounded-lg border px-2.5 py-1.5 text-sm font-mono resize-none outline-none"
            :placeholder="$t('components.pipeline.composite.OutputValidationTab.regex_pattern')"
            @change="
              (e: Event) =>
                updateConfig(evalDef.id, {
                  pattern: (e.target as HTMLTextAreaElement).value,
                })
            "
          />
        </div>
      </template>

      <template v-else-if="evalDef.type === 'json_schema'">
        <div class="space-y-1">
          <label class="text-xs text-muted-foreground">Field (optional)</label>
          <Input
            :model-value="String(evalDef.config.field ?? '')"
            placeholder="output field name (leave blank for entire output)"
            @update:model-value="
              (val: string | number) =>
                updateConfig(evalDef.id, { field: String(val) })
            "
          />
        </div>
        <div class="space-y-1">
          <label class="text-xs text-muted-foreground">Schema (JSON)</label>
          <textarea
            :value="JSON.stringify(evalDef.config.schema ?? {}, null, 2)"
            class="bg-background border-input focus-visible:border-ring h-28 w-full rounded-lg border px-2.5 py-1.5 text-sm font-mono resize-none outline-none"
            placeholder='{ "type": "object", "properties": { ... } }'
            @change="
              (e: Event) => {
                try {
                  updateConfig(evalDef.id, {
                    schema: JSON.parse((e.target as HTMLTextAreaElement).value),
                  });
                } catch {
                  console.warn("Invalid JSON schema, keeping current value");
                }
              }
            "
          />
        </div>
      </template>

      <template v-else-if="evalDef.type === 'llm_judge'">
        <div class="space-y-1">
          <label class="text-xs text-muted-foreground">Rubric</label>
          <textarea
            :value="String(evalDef.config.rubric ?? '')"
            class="bg-background border-input focus-visible:border-ring h-20 w-full rounded-lg border px-2.5 py-1.5 text-sm resize-none outline-none"
            placeholder="Describe what constitutes a passing evaluation"
            @change="
              (e: Event) =>
                updateConfig(evalDef.id, {
                  rubric: (e.target as HTMLTextAreaElement).value,
                })
            "
          />
        </div>
      </template>
    </div>

    <Button variant="outline" size="sm" class="w-full" @click="addEval">
      + Add Eval Definition
    </Button>

    <div class="pt-2 space-y-1">
      <label class="text-xs text-muted-foreground">
        Max Validation Retries: {{ localRetries }}
      </label>
      <div class="flex items-center gap-3">
        <span class="text-xs text-muted-foreground">0</span>
        <input
          type="range"
          min="0"
          max="5"
          step="1"
          :value="localRetries"
          class="h-2 w-full cursor-pointer appearance-none rounded-lg bg-muted accent-indigo-500"
          @input="updateRetries"
        />
        <span class="text-xs text-muted-foreground">5</span>
      </div>
    </div>
  </div>
</template>
