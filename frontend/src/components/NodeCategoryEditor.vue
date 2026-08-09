<template>
  <div class="space-y-4">
    <div>
      <label for="nodecategoryeditor-field-5" class="mb-1 block text-sm font-medium">{{ $t('components.NodeCategoryEditor.name') }}</label>
      <input id="nodecategoryeditor-field-5"
        v-model="form.name"
        type="text"
        class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        :placeholder="$t('components.NodeCategoryEditor.eg_llm_call_connector_read')"
      />
    </div>

    <div>
      <label for="nodecategoryeditor-field-4" class="mb-1 block text-sm font-medium">{{ $t('components.NodeCategoryEditor.description') }}</label>
      <textarea id="nodecategoryeditor-field-4"
        v-model="form.description"
        rows="3"
        class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        :placeholder="$t('components.NodeCategoryEditor.optional_description_of_this_category')"
      />
    </div>

    <div>
      <label for="nodecategoryeditor-field-3" class="mb-1 block text-sm font-medium">{{ $t('components.NodeCategoryEditor.color') }}</label>
      <div class="flex items-center gap-3">
        <input id="nodecategoryeditor-field-3"
          v-model="form.color"
          type="color"
          class="h-9 w-14 cursor-pointer rounded border border-input bg-background p-0.5"
        />
        <input aria-label="#6366f1"
          v-model="form.color"
          type="text"
          class="flex-1 rounded-lg border border-input bg-background px-3 py-2 text-sm font-mono ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          placeholder="#6366f1"
          pattern="^#[0-9a-fA-F]{6}$"
        />
      </div>
    </div>

    <div>
      <label for="nodecategoryeditor-field-2" class="mb-1 block text-sm font-medium">{{ $t('components.NodeCategoryEditor.icon') }}</label>
      <Select aria-label="Icon" v-model="form.icon">
        <SelectTrigger class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" aria-label="Icon">
          <SelectValue :placeholder="$t('components.NodeCategoryEditor.none')" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="__all__">{{ $t('components.NodeCategoryEditor.none') }}</SelectItem>
          <SelectItem value="bot">{{ $t('components.NodeCategoryEditor.bot') }}</SelectItem>
          <SelectItem value="database">{{ $t('components.NodeCategoryEditor.database') }}</SelectItem>
          <SelectItem value="globe">{{ $t('components.NodeCategoryEditor.globe') }}</SelectItem>
          <SelectItem value="mail">{{ $t('components.NodeCategoryEditor.mail') }}</SelectItem>
          <SelectItem value="message-circle">{{ $t('components.NodeCategoryEditor.message_circle') }}</SelectItem>
          <SelectItem value="refresh-cw">{{ $t('components.NodeCategoryEditor.refresh') }}</SelectItem>
          <SelectItem value="search">{{ $t('common.search') }}</SelectItem>
          <SelectItem value="settings">{{ $t('components.NodeCategoryEditor.settings') }}</SelectItem>
          <SelectItem value="sliders">{{ $t('components.NodeCategoryEditor.sliders') }}</SelectItem>
          <SelectItem value="terminal">{{ $t('components.NodeCategoryEditor.terminal') }}</SelectItem>
          <SelectItem value="upload">{{ $t('components.NodeCategoryEditor.upload') }}</SelectItem>
          <SelectItem value="zap">{{ $t('components.NodeCategoryEditor.zap') }}</SelectItem>
        </SelectContent>
      </Select>
    </div>

    <div>
      <label for="nodecategoryeditor-field-1" class="mb-1 block text-sm font-medium">{{ $t('components.NodeCategoryEditor.sort_order') }}</label>
      <input id="nodecategoryeditor-field-1"
        v-model.number="form.sort_order"
        type="number"
        min="0"
        class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      />
    </div>

    <div v-if="error" class="text-sm text-destructive">{{ error }}</div>

    <div class="flex items-center gap-2">
      <Button
        :disabled="!form.name.trim() || saving"
        variant="default"
        @click="save"
      >
        {{
          saving
            ? "Saving..."
            : isEditing
              ? "Update Category"
              : "Create Category"
        }}
      </Button>
      <button
        class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
        @click="emit('cancelled')"
      >
        Cancel
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, watch } from "vue";
import { api } from "../lib/api/client";
import { formatApiError } from "../lib/api/formatError";
import { Button } from '@/components/ui/button';
import { Select, SelectTrigger, SelectContent, SelectItem, SelectValue } from '@/components/ui/select';

export interface NodeCategoryForm {
  name: string;
  description: string;
  color: string;
  icon: string;
  sort_order: number;
}

interface CategoryData {
  id?: string;
  name?: string;
  description?: string | null;
  color?: string;
  icon?: string | null;
  sort_order?: number;
}

const props = defineProps<{
  category?: CategoryData | null;
}>();

const emit = defineEmits<{
  saved: [data: unknown];
  cancelled: [];
}>();

const saving = ref(false);
const error = ref<string | null>(null);

const form = reactive<NodeCategoryForm>({
  name: "",
  description: "",
  color: "#6366f1",
  icon: "__all__",
  sort_order: 0,
});

const isEditing = computed(() => !!props.category);

watch(
  () => props.category,
  (cat) => {
    if (cat) {
      form.name = cat.name ?? "";
      form.description = cat.description ?? "";
      form.color = cat.color ?? "#6366f1";
      form.icon = cat.icon ?? "__all__";
      form.sort_order = cat.sort_order ?? 0;
    }
  },
  { immediate: true },
);

async function save() {
  saving.value = true;
  error.value = null;

  const body = {
    name: form.name.trim(),
    description: form.description.trim() || null,
    color: form.color,
    icon: form.icon === '__all__' ? null : form.icon,
    sort_order: form.sort_order,
  };

  try {
    if (isEditing.value && props.category?.id) {
      const { data, error: err } = await api.PATCH(
        "/api/v1/node-categories/{category_id}",
        {
          params: { path: { category_id: props.category.id } },
          body,
        },
      );
      if (err) {
        throw new Error(formatApiError(err));
      }
      if (data) {
        emit("saved", data);
      }
    } else {
      const { data, error: err } = await api.POST("/api/v1/node-categories", {
        body,
      });
      if (err) {
        throw new Error(formatApiError(err));
      }
      if (data) {
        emit("saved", data);
      }
    }
  } catch (e) {
    error.value =
      e instanceof Error ? e.message : "An unexpected error occurred";
  } finally {
    saving.value = false;
  }
}
</script>
