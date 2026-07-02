<script setup lang="ts">
import { ref } from "vue";
import { useApi } from "../../../composables/useApi";
import type { ParameterPort, ParameterPortType } from "../../../types/pipeline";

const props = defineProps<{
  ports: ParameterPort[];
  nodeIds: string[];
}>();

const emit = defineEmits<{
  (e: "update:ports", ports: ParameterPort[]): void;
}>();

const { post } = useApi();

function portRef(port: { name: string }) {
  return "{{parameter." + port.name + "}}";
}

const showAddForm = ref(false);
const editingIndex = ref<number | null>(null);

const formDefaults = {
  name: "",
  label: "",
  description: "",
  type: "string" as ParameterPortType,
  required: false,
  default: "",
  multiline: false,
};

const form = ref({ ...formDefaults });
const formError = ref<string | null>(null);

function resetForm() {
  form.value = { ...formDefaults };
  formError.value = null;
}

function openAddForm() {
  editingIndex.value = null;
  resetForm();
  showAddForm.value = true;
}

function openEditForm(index: number) {
  editingIndex.value = index;
  const p = props.ports[index];
  form.value = {
    name: p.name,
    label: p.label,
    description: p.description || "",
    type: p.type,
    required: p.required,
    default:
      p.default !== undefined && p.default !== null ? String(p.default) : "",
    multiline: p.multiline || false,
  };
  showAddForm.value = true;
  formError.value = null;
}

function cancelForm() {
  showAddForm.value = false;
  editingIndex.value = null;
  resetForm();
}

function savePort() {
  if (!form.value.name.trim() || !form.value.label.trim()) {
    formError.value = "Name and label are required";
    return;
  }
  formError.value = null;
  const port: ParameterPort = {
    id:
      editingIndex.value !== null
        ? props.ports[editingIndex.value].id
        : crypto.randomUUID(),
    name: form.value.name.trim(),
    label: form.value.label.trim(),
    description: form.value.description.trim() || undefined,
    type: form.value.type,
    required: form.value.required,
    default:
      form.value.type === "number"
        ? Number(form.value.default)
        : form.value.default || undefined,
    multiline: form.value.multiline,
  };

  const updated = [...props.ports];
  if (editingIndex.value !== null) {
    updated[editingIndex.value] = port;
  } else {
    updated.push(port);
  }
  emit("update:ports", updated);
  showAddForm.value = false;
  resetForm();
}

function deletePort(index: number) {
  const updated = props.ports.filter((_, i) => i !== index);
  emit("update:ports", updated);
}

function moveUp(index: number) {
  if (index === 0) return;
  const updated = [...props.ports];
  [updated[index - 1], updated[index]] = [updated[index], updated[index - 1]];
  emit("update:ports", updated);
}

function moveDown(index: number) {
  if (index >= props.ports.length - 1) return;
  const updated = [...props.ports];
  [updated[index], updated[index + 1]] = [updated[index + 1], updated[index]];
  emit("update:ports", updated);
}

const detectLoading = ref(false)

async function detectPlaceholders() {
  detectLoading.value = true
  try {
    const result = await post<{ ports: ParameterPort[] }>(
      "/api/v1/composite-templates/detect-params",
      { node_ids: props.nodeIds },
    );
    if (result.ports && result.ports.length > 0) {
      const existing = new Set(props.ports.map((p) => p.name));
      const newPorts = result.ports.filter((p) => !existing.has(p.name));
      emit("update:ports", [...props.ports, ...newPorts]);
    }
  } catch {
    // Best-effort detection - user can retry
  } finally {
    detectLoading.value = false
  }
}
</script>

<template>
  <div class="space-y-4">
    <div class="flex items-center justify-between">
      <h3 class="text-sm font-semibold">{{ $t('components.pipeline.composite.PortDefinitionPanel.parameter_ports') }}</h3>
      <div class="flex gap-1">
        <button
          class="rounded-md border border-input px-2 py-1 text-xs hover:bg-accent disabled:opacity-50"
          title="Scan prompts for {{parameter.*}} placeholders"
          :disabled="detectLoading"
          @click="detectPlaceholders"
        >
          {{ detectLoading ? '...' : 'Detect' }}
        </button>
        <button
          class="rounded-md bg-indigo-600 px-2 py-1 text-xs font-medium text-white hover:bg-indigo-500"
          @click="openAddForm"
        >
          + Add Port
        </button>
      </div>
    </div>

    <div
      v-if="ports.length === 0"
      class="py-8 text-center text-sm text-muted-foreground"
    >
      No parameter ports defined yet. Add ports for values that pipeline authors
      can configure.
    </div>

    <div
      v-for="(port, index) in ports"
      :key="port.id"
      class="rounded-lg border bg-muted/30 p-3"
    >
      <div class="flex items-start justify-between">
        <div class="min-w-0 flex-1">
          <div class="flex items-center gap-2">
            <span class="text-sm font-medium">{{ port.label }}</span>
            <span
              v-if="port.required"
              class="rounded bg-destructive/10 px-1.5 py-0.5 text-[10px] text-destructive"
              >required</span
            >
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
        <div class="ml-2 flex flex-col gap-1">
          <button
            class="rounded p-1 text-xs text-muted-foreground hover:bg-accent"
            :title="$t('components.pipeline.composite.PortDefinitionPanel.move_up')"
            @click="moveUp(index)"
          >
            &#x25B2;
          </button>
          <button
            class="rounded p-1 text-xs text-muted-foreground hover:bg-accent"
            :title="$t('components.pipeline.composite.PortDefinitionPanel.move_down')"
            @click="moveDown(index)"
          >
            &#x25BC;
          </button>
          <button
            class="rounded p-1 text-xs text-muted-foreground hover:bg-accent"
            title="Edit"
            @click="openEditForm(index)"
          >
            &#x270E;
          </button>
          <button
            class="rounded p-1 text-xs text-destructive hover:bg-destructive/10"
            title="Delete"
            @click="deletePort(index)"
          >
            &#x2715;
          </button>
        </div>
      </div>
    </div>

    <!-- Add / Edit Form -->
    <div v-if="showAddForm" class="rounded-lg border bg-card p-4">
      <h4 class="mb-3 text-sm font-medium">
        {{ editingIndex !== null ? "Edit Port" : "Add Port" }}
      </h4>
      <div class="space-y-3">
        <div>
          <label class="mb-1 block text-xs font-medium text-muted-foreground"
            >{{ $t('components.pipeline.composite.PortDefinitionPanel.name') }}</label
          >
          <input
            v-model="form.name"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
            placeholder="model_temperature"
          />
        </div>
        <div>
          <label class="mb-1 block text-xs font-medium text-muted-foreground"
            >{{ $t('components.pipeline.composite.PortDefinitionPanel.label') }}</label
          >
          <input
            v-model="form.label"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
            :placeholder="$t('components.pipeline.composite.PortDefinitionPanel.model_temperature')"
          />
        </div>
        <div>
          <label class="mb-1 block text-xs font-medium text-muted-foreground"
            >Description</label
          >
          <textarea
            v-model="form.description"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
            rows="2"
            :placeholder="$t('components.pipeline.composite.PortDefinitionPanel.controls_the_randomness_of_the_model_output')"
          />
        </div>
        <div>
          <label class="mb-1 block text-xs font-medium text-muted-foreground"
            >Type</label
          >
          <select
            v-model="form.type"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
          >
            <option value="string">String</option>
            <option value="number">Number</option>
            <option value="boolean">Boolean</option>
            <option value="select">Select</option>
            <option value="model_backend_ref">{{ $t('components.pipeline.composite.PortDefinitionPanel.model_backend_ref') }}</option>
            <option value="schema_ref">{{ $t('components.pipeline.composite.PortDefinitionPanel.schema_ref') }}</option>
          </select>
        </div>
        <div class="flex items-center gap-2">
          <input
            v-model="form.required"
            type="checkbox"
            class="h-4 w-4 rounded border-gray-300 text-indigo-500 focus:ring-indigo-500"
          />
          <label class="text-xs text-muted-foreground">Required</label>
        </div>
        <div v-if="form.type === 'string'" class="flex items-center gap-2">
          <input
            v-model="form.multiline"
            type="checkbox"
            class="h-4 w-4 rounded border-gray-300 text-indigo-500 focus:ring-indigo-500"
          />
          <label class="text-xs text-muted-foreground">Multiline</label>
        </div>
        <div v-if="form.type !== 'boolean'">
          <label class="mb-1 block text-xs font-medium text-muted-foreground"
            >{{ $t('components.pipeline.composite.PortDefinitionPanel.default_value') }}</label
          >
          <input
            v-model="form.default"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
            ::placeholder="$t('components.pipeline.composite.PortDefinitionPanel.formtype_number_07_default_value')"
            :type="form.type === 'number' ? 'number' : 'text'"
          />
        </div>

        <div
          v-if="formError"
          class="rounded-lg border border-destructive/50 bg-destructive/10 p-2 text-xs text-destructive"
        >
          {{ formError }}
        </div>

        <div class="flex justify-end gap-2">
          <button
            class="rounded-lg border border-input bg-background px-3 py-1.5 text-xs hover:bg-accent"
            @click="cancelForm"
          >
            Cancel
          </button>
          <button
            class="rounded-lg bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:bg-primary/90"
            @click="savePort"
          >
            {{ editingIndex !== null ? "Update" : "Add" }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>
