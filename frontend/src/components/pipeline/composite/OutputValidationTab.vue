<script setup lang="ts">
import { ref, computed } from "vue";
import { Button } from "../../ui/button";
import { Input } from "../../ui/input";
import { Select, SelectTrigger, SelectContent, SelectItem, SelectValue } from "../../ui/select";
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

function handleSchemaChange(e: Event, evalDefId: string) {
  try {
    updateConfig(evalDefId, {
      schema: JSON.parse((e.target as HTMLTextAreaElement).value),
    });
  } catch {
    console.warn("Invalid JSON schema, keeping current value");
  }
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
      <span class="text-sm font-medium">{{ $t('components.pipeline.composite.OutputValidationTab.output_validation') }}</span>
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
          <span class="text-xs text-muted-foreground">Name</span>
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
          <span class="text-xs text-muted-foreground">Type</span>
          <Select :model-value="evalDef.type" @update:model-value="(val) => updateEval(evalDef.id, {type: val as EvalConfig['type']})">
            <SelectTrigger class="bg-background border-input focus-visible:border-ring h-8 w-full rounded-lg border px-2.5 py-1 text-sm" aria-label="Eval type">
              <SelectValue placeholder="Select type" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="regex">Regex</SelectItem>
              <SelectItem value="json_schema">{{ $t('components.pipeline.composite.OutputValidationTab.json_schema') }}</SelectItem>
              <SelectItem value="llm_judge">{{ $t('components.pipeline.composite.OutputValidationTab.llm_judge') }}</SelectItem>
            </SelectContent>
          </Select>
        </div>
      </div>

      <div class="space-y-1">
        <span class="text-xs text-muted-foreground">{{ $t('components.pipeline.composite.OutputValidationTab.failure_behaviour') }}</span>
        <Select :model-value="evalDef.failure_behaviour" @update:model-value="(val) => updateEval(evalDef.id, {failure_behaviour: val as EvalConfig['failure_behaviour']})">
          <SelectTrigger class="bg-background border-input focus-visible:border-ring h-8 w-full rounded-lg border px-2.5 py-1 text-sm" aria-label="Failure behaviour">
            <SelectValue placeholder="Select behaviour" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="retry">Retry</SelectItem>
            <SelectItem value="block">Block</SelectItem>
            <SelectItem value="warn">Warn</SelectItem>
          </SelectContent>
        </Select>
      </div>

      <template v-if="evalDef.type === 'regex'">
        <div class="space-y-1">
          <span class="text-xs text-muted-foreground">Field</span>
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
          <label for="outputvalidationtab-field-4" class="text-xs text-muted-foreground">Pattern</label>
          <textarea id="outputvalidationtab-field-4"
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
          <span class="text-xs text-muted-foreground">Field (optional)</span>
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
          <label for="outputvalidationtab-field-3" class="text-xs text-muted-foreground">Schema (JSON)</label>
          <textarea id="outputvalidationtab-field-3"
            :value="JSON.stringify(evalDef.config.schema ?? {}, null, 2)"
            class="bg-background border-input focus-visible:border-ring h-28 w-full rounded-lg border px-2.5 py-1.5 text-sm font-mono resize-none outline-none"
            placeholder='{ "type": "object", "properties": { ... } }'
            @change="(e: Event) => handleSchemaChange(e, evalDef.id)"
          />
        </div>
      </template>

      <template v-else-if="evalDef.type === 'llm_judge'">
        <div class="space-y-1">
          <label for="outputvalidationtab-field-2" class="text-xs text-muted-foreground">Rubric</label>
          <textarea id="outputvalidationtab-field-2"
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
      <label for="outputvalidationtab-field-1" class="text-xs text-muted-foreground">
        Max Validation Retries: {{ localRetries }}
      </label>
      <div class="flex items-center gap-3">
        <span class="text-xs text-muted-foreground">0</span>
        <input id="outputvalidationtab-field-1"
          type="range"
          min="0"
          max="5"
          step="1"
          :value="localRetries"
          class="h-2 w-full cursor-pointer appearance-none rounded-lg bg-muted accent-indigo-500"
          aria-label="Max validation retries"
          @input="updateRetries"
        />
        <span class="text-xs text-muted-foreground">5</span>
      </div>
    </div>
  </div>
</template>
