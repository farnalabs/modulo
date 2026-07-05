<script setup lang="ts">
import { ref, computed } from "vue";
import { useCompositeStore } from "../../../stores/compositeStore";
import Tabs from "../../ui/tabs/Tabs.vue";
import TabsList from "../../ui/tabs/TabsList.vue";
import TabsTrigger from "../../ui/tabs/TabsTrigger.vue";
import TabsContent from "../../ui/tabs/TabsContent.vue";
import ParameterPortForm from "./ParameterPortForm.vue";
import SchemaMappingPanel from "./SchemaMappingPanel.vue";
import OutputValidationTab from "./OutputValidationTab.vue";
import type { ParameterPort, ParameterPortType } from "../../../types/pipeline";

interface EvalConfig {
  id: string;
  name: string;
  type: "regex" | "json_schema" | "llm_judge";
  config: Record<string, unknown>;
  failure_behaviour: "retry" | "block" | "warn";
}

const props = defineProps<{
  compositeRef: string | null;
  parameterValues: Record<string, unknown>;
  inputMapping?: Record<string, string>;
  outputMapping?: Record<string, string>;
  precedingNodeSchemaId?: string | null;
  downstreamNodeSchemaId?: string | null;
  evalDefinitions?: EvalConfig[];
  maxValidationRetries?: number;
}>();

const emit = defineEmits<{
  (e: "update:parameterValues", values: Record<string, unknown>): void;
  (e: "apply"): void;
  (e: "update:inputMapping", mapping: Record<string, string>): void;
  (e: "update:outputMapping", mapping: Record<string, string>): void;
  (e: "update:evalDefinitions", val: EvalConfig[]): void;
  (e: "update:maxValidationRetries", val: number): void;
}>();

const compositeStore = useCompositeStore();
const activeTab = ref("parameters");

const composite = computed(() => {
  if (!props.compositeRef) return null;
  return compositeStore.getCompositeById(props.compositeRef) ?? null;
});

const ports = computed(() => (composite.value?.parameter_ports_json ?? []) as ParameterPort[]);

const localValues = computed(() => props.parameterValues);

const hasRequiredErrors = computed(() => {
  return ports.value
    .filter((p: ParameterPort) => p.required)
    .some((p: ParameterPort) => {
      const val = localValues.value[p.name];
      return val === undefined || val === null || val === "";
    });
});

function updatePortValue(portName: string, value: unknown) {
  emit("update:parameterValues", {
    ...props.parameterValues,
    [portName]: value,
  });
}
</script>

<template>
  <div class="space-y-4">
    <div
      v-if="!composite"
      class="py-8 text-center text-sm text-muted-foreground"
    >
      <p>{{ $t('components.pipeline.composite.CompositeConfigPanel.no_composite_selected') }}</p>
      <p class="mt-1 text-xs">
        Select a composite node to configure its parameters.
      </p>
    </div>

    <template v-else>
      <div class="mb-4">
        <h3 class="text-lg font-semibold">{{ composite.name }}</h3>
        <p
          v-if="composite.description"
          class="mt-1 text-sm text-muted-foreground"
        >
          {{ composite.description }}
        </p>
        <span
          class="mt-1 inline-block rounded border border-indigo-500/30 bg-indigo-500/10 px-2 py-0.5 text-xs text-indigo-300"
        >
          v{{ composite.version }}
        </span>
      </div>

      <Tabs v-model:default-value="activeTab" class="w-full">
        <TabsList class="w-full">
          <TabsTrigger value="parameters"> Parameters </TabsTrigger>
          <TabsTrigger value="mapping"> Mapping </TabsTrigger>
          <TabsTrigger value="validation"> Validation </TabsTrigger>
        </TabsList>

        <TabsContent value="parameters" class="mt-4 space-y-4">
          <ParameterPortForm
            v-for="port in ports"
            :key="port.id"
            :port="port"
            :model-value="localValues[port.name]"
            @update:model-value="
              (val: unknown) => updatePortValue(port.name, val)
            "
          />

          <div
            v-if="ports.length === 0"
            class="py-4 text-center text-sm text-muted-foreground"
          >
            This composite has no configurable parameters.
          </div>

          <div class="flex items-center justify-between pt-4">
            <div v-if="hasRequiredErrors" class="text-xs text-destructive">
              Fill all required fields (*)
            </div>
            <div v-else />
            <button
              :disabled="hasRequiredErrors"
              class="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-500 disabled:opacity-50"
              @click="$emit('apply')"
            >
              Apply
            </button>
          </div>
        </TabsContent>

        <TabsContent value="mapping" class="mt-4">
          <SchemaMappingPanel
            :composite-ref="props.compositeRef"
            :input-mapping="props.inputMapping ?? {}"
            :output-mapping="props.outputMapping ?? {}"
            :preceding-node-schema-id="props.precedingNodeSchemaId ?? null"
            :downstream-node-schema-id="props.downstreamNodeSchemaId ?? null"
            @update:input-mapping="emit('update:inputMapping', $event)"
            @update:output-mapping="emit('update:outputMapping', $event)"
          />
        </TabsContent>

        <TabsContent value="validation" class="mt-4">
          <OutputValidationTab
            :eval-definitions="props.evalDefinitions ?? []"
            :max-validation-retries="props.maxValidationRetries ?? 0"
            @update:eval-definitions="
              (val: EvalConfig[]) => $emit('update:evalDefinitions', val)
            "
            @update:max-validation-retries="
              (val: number) => $emit('update:maxValidationRetries', val)
            "
          />
        </TabsContent>
      </Tabs>
    </template>
  </div>
</template>
