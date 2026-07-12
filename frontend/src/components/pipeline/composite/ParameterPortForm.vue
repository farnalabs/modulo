<script setup lang="ts">
import { computed } from "vue";
import type { ParameterPort } from "../../../types/pipeline";

const props = defineProps<{
  port: ParameterPort;
  modelValue: unknown;
}>();

const emit = defineEmits<{
  (e: "update:modelValue", value: unknown): void;
}>();

const localValue = computed({
  get: () =>
    props.modelValue ??
    props.port.default_value ??
    (props.port.type === "boolean"
      ? false
      : props.port.type === "number"
        ? 0
        : ""),
  set: (val: unknown) => emit("update:modelValue", val),
});

function onStringChange(event: Event) {
  const target = event.target as HTMLInputElement;
  localValue.value = target.value;
}

function onNumberChange(event: Event) {
  const target = event.target as HTMLInputElement;
  localValue.value = target.valueAsNumber;
}

function onSelectChange(event: Event) {
  const target = event.target as HTMLSelectElement;
  localValue.value = target.value;
}

function onBooleanChange(event: Event) {
  const target = event.target as HTMLInputElement;
  localValue.value = target.checked;
}
</script>

<template>
  <div class="space-y-1.5">
    <label for="parameterportform-field-2" class="flex items-center gap-1 text-sm font-medium">
      {{ port.label }}
      <span v-if="port.required" class="text-destructive">*</span>
    </label>
    <p v-if="port.description" class="text-xs text-muted-foreground">
      {{ port.description }}
    </p>

    <template v-if="port.type === 'string' && port.multiline">
      <textarea id="parameterportform-field-2"
        :value="localValue as string"
        class="min-h-[80px] w-full rounded-lg border border-input bg-background px-3 py-2 text-sm placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"
        :placeholder="$t('components.pipeline.composite.ParameterPortForm.portdefault_as_string')"
        @change="onStringChange"
      />
    </template>

    <input
      v-else-if="port.type === 'string'"
      :value="localValue as string"
      class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
      :placeholder="(port.default_value as string) ?? ''"
      @change="onStringChange"
    />

    <input aria-label="port.default_value != null ? String(port.default_value) : "
      v-else-if="port.type === 'number'"
      :value="localValue as number"
      type="number"
      class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
      :placeholder="port.default_value != null ? String(port.default_value) : ''"
      @change="onNumberChange"
    />

    <label for="parameterportform-field-1"
      v-else-if="port.type === 'boolean'"
      class="inline-flex cursor-pointer items-center gap-2"
    >
      <input id="parameterportform-field-1"
        :checked="!!localValue"
        type="checkbox"
        class="rounded border-gray-300 text-indigo-500 focus:ring-indigo-500"
        @change="onBooleanChange"
      />
      <span class="text-sm">{{ localValue ? "Enabled" : "Disabled" }}</span>
    </label>

    <select
      v-else-if="port.type === 'select'"
      :value="localValue"
      aria-label="Select value"
      class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
      @change="onSelectChange"
    >
      <option value="">Select...</option>
      <option v-for="opt in port.options" :key="opt.value" :value="opt.value">
        {{ opt.label }}
      </option>
    </select>

    <div
      v-else-if="
        port.type === 'model_backend_ref' || port.type === 'schema_ref'
      "
      class="rounded-lg border bg-muted px-3 py-2 text-sm text-muted-foreground"
    >
      {{
        port.type === "model_backend_ref"
          ? "Model backend picker"
          : "Schema picker"
      }}
      <span class="block text-xs">(integration pending)</span>
    </div>
  </div>
</template>
