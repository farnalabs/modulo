<template>
  <Dialog :visible="dialogOpen" :modal="true" :dismissable-mask="true" class="sm:max-w-lg" @update:visible="closeForm">
    <template #header>
      <div>
        <div class="text-lg font-semibold">{{ editingId ? "Edit Skill" : "Add Skill" }}</div>
        <div class="mt-0.5 text-sm text-muted-foreground">
          {{ editingId ? editDescription : createDescription }}
        </div>
      </div>
    </template>

    <form @submit.prevent="save" class="space-y-4">
        <div>
          <label for="remyskilldialog-field-5" class="mb-1 block text-sm font-medium"
            >{{ $t('components.remy.RemySkillDialog.name') }} <span class="text-destructive">*</span></label
          >
          <input id="remyskilldialog-field-5"
            v-model="form.name"
            type="text"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
            :placeholder="$t('components.remy.RemySkillDialog.skill_name')"
            required
            data-testid="remy-skills-form-name"
          />
        </div>
        <div>
          <label for="remyskilldialog-field-4" class="mb-1 block text-sm font-medium">{{ $t('components.remy.RemySkillDialog.description') }}</label>
          <textarea id="remyskilldialog-field-4"
            v-model="form.description"
            rows="2"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
            :placeholder="$t('components.remy.RemySkillDialog.what_this_skill_does')"
            data-testid="remy-skills-form-description"
          />
        </div>
        <div>
          <label for="remyskilldialog-field-3" class="mb-1 block text-sm font-medium">{{ $t('components.remy.RemySkillDialog.triggers') }}</label>
          <input id="remyskilldialog-field-3"
            v-model="form.triggersInput"
            type="text"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
            :placeholder="$t('components.remy.RemySkillDialog.trigger1_trigger2')"
            data-testid="remy-skills-form-triggers"
          />
          <p class="mt-1 text-xs text-muted-foreground">
            Comma-separated trigger keywords
          </p>
        </div>
        <div>
          <label for="remyskilldialog-field-2" class="mb-1 block text-sm font-medium">{{ $t('components.remy.RemySkillDialog.body_markdown') }}</label>
          <textarea id="remyskilldialog-field-2"
            v-model="form.body"
            rows="6"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 font-mono text-sm"
            :placeholder="$t('components.remy.RemySkillDialog.skill_instructions_heading') + '\n' + $t('components.remy.RemySkillDialog.skill_instructions_body')"
            data-testid="remy-skills-form-body"
          />
        </div>
        <div class="flex items-center gap-2">
          <label for="remyskilldialog-field-1" class="flex items-center gap-2 text-sm cursor-pointer">
            <input id="remyskilldialog-field-1"
              v-model="form.active"
              type="checkbox"
              class="rounded border-input"
              data-testid="remy-skills-form-active"
            />
            Active
          </label>
        </div>
        <div v-if="saveError" class="text-sm text-destructive">
          {{ saveError }}
        </div>
        <div class="flex gap-2 justify-end">
          <button
            type="button"
            class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
            data-testid="remy-skills-form-cancel"
            @click="closeForm"
          >
            Cancel
          </button>
          <Button :disabled="saving || !form.name.trim()" type="submit" data-testid="remy-skills-form-submit">
            {{ submitLabel }}
          </Button>
        </div>
      </form>
  </Dialog>

  <Dialog :visible="deleteOpen" :modal="true" :dismissable-mask="true" class="sm:max-w-sm" @update:visible="deleteOpen = false">
    <template #header>
      <div class="text-lg font-semibold">{{ $t('components.remy.RemySkillDialog.delete_skill') }}</div>
    </template>
    <p class="text-sm text-muted-foreground">
      Are you sure you want to delete "{{ deletingName }}"? This action
      cannot be undone.
    </p>
    <div class="flex gap-2 justify-end">
      <button
        type="button"
        class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
        data-testid="remy-skills-delete-cancel"
        @click="deleteOpen = false"
      >
        Cancel
      </button>
      <Button :disabled="deleting" type="button" severity="danger" data-testid="remy-skills-delete-confirm" @click="confirmDelete">
        {{ deleting ? "Deleting..." : "Delete" }}
      </Button>
    </div>
  </Dialog>
</template>

<script setup lang="ts">
import { ref, reactive, computed } from "vue";
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import { api } from "@/lib/api/client";
import { formatApiError } from "@/lib/api/formatError";

export interface SkillFormItem {
  id: string;
  name: string;
  description?: string | null;
  triggers?: string[] | null;
  body?: string;
  active: boolean;
}

const props = withDefaults(
  defineProps<{
    createDescription?: string;
    editDescription?: string;
    createEndpoint?: '/api/v1/admin/remy/skills' | '/api/v1/me/remy/skills';
    updateEndpoint?: '/api/v1/admin/remy/skills/{skill_id}' | '/api/v1/me/remy/skills/{skill_id}';
    deleteEndpoint?: '/api/v1/admin/remy/skills/{skill_id}' | '/api/v1/me/remy/skills/{skill_id}';
  }>(),
  {
    createDescription: "Create a new skill.",
    editDescription: "Update the skill configuration.",
    createEndpoint: "/api/v1/admin/remy/skills",
    updateEndpoint: "/api/v1/admin/remy/skills/{skill_id}",
    deleteEndpoint: "/api/v1/admin/remy/skills/{skill_id}",
  },
);

const emit = defineEmits<{
  saved: [];
}>();

const dialogOpen = ref(false);
const deleteOpen = ref(false);
const editingId = ref<string | null>(null);
const deletingId = ref<string | null>(null);
const deletingName = ref("");
const saving = ref(false);
const deleting = ref(false);
const saveError = ref<string | null>(null);

const submitLabel = computed(() => {
  if (saving.value) return "Saving...";
  return editingId.value ? "Update" : "Create";
});

const form = reactive({
  name: "",
  description: "",
  triggersInput: "",
  body: "",
  active: true,
});

function openCreate() {
  editingId.value = null;
  form.name = "";
  form.description = "";
  form.triggersInput = "";
  form.body = "";
  form.active = true;
  saveError.value = null;
  dialogOpen.value = true;
}

function openEdit(skill: SkillFormItem) {
  editingId.value = skill.id;
  form.name = skill.name;
  form.description = skill.description || "";
  form.triggersInput = (skill.triggers || []).join(", ");
  form.body = skill.body || "";
  form.active = skill.active;
  saveError.value = null;
  dialogOpen.value = true;
}

function closeForm() {
  dialogOpen.value = false;
  editingId.value = null;
  saveError.value = null;
}

function openDelete(skill: SkillFormItem) {
  deletingId.value = skill.id;
  deletingName.value = skill.name;
  deleteOpen.value = true;
}

async function save() {
  if (!form.name.trim()) return;
  saving.value = true;
  saveError.value = null;
  try {
    const triggers = form.triggersInput
      .split(/[\s,]+/)
      .map((s) => s.trim())
      .filter(Boolean);
    const payload = {
      name: form.name.trim(),
      description: form.description.trim() || null,
      triggers,
      body: form.body,
      active: form.active,
    };

    if (editingId.value) {
      const { error: err } = await api.PUT(props.updateEndpoint, {
        params: { path: { skill_id: editingId.value } },
        body: payload,
      });
      if (err) {
        saveError.value = `Failed to update skill: ${formatApiError(err)}`;
        return;
      }
    } else {
      const { error: err } = await api.POST(props.createEndpoint, {
        body: payload,
      });
      if (err) {
        saveError.value = `Failed to create skill: ${formatApiError(err)}`;
        return;
      }
    }
    closeForm();
    emit("saved");
  } catch (e: unknown) {
    saveError.value = `Failed to save skill: ${formatApiError(e)}`;
  } finally {
    saving.value = false;
  }
}

async function confirmDelete() {
  if (!deletingId.value) return;
  deleting.value = true;
  try {
    const { error: err } = await api.DELETE(props.deleteEndpoint, {
      params: { path: { skill_id: deletingId.value } },
    });
    if (err) {
      saveError.value = `Failed to delete skill: ${formatApiError(err)}`;
      return;
    }
    deleteOpen.value = false;
    deletingId.value = null;
    emit("saved");
  } catch (e: unknown) {
    saveError.value = `Failed to delete skill: ${formatApiError(e)}`;
  } finally {
    deleting.value = false;
  }
}

defineExpose({ openCreate, openEdit, openDelete });
</script>
