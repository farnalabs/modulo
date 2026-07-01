<template>
  <Dialog :open="dialogOpen" @update:open="closeForm">
    <DialogContent class="sm:max-w-lg">
      <DialogHeader>
        <DialogTitle>{{ editingId ? "Edit Skill" : "Add Skill" }}</DialogTitle>
        <DialogDescription>
          {{ editingId ? editDescription : createDescription }}
        </DialogDescription>
      </DialogHeader>

      <form @submit.prevent="save" class="space-y-4">
        <div>
          <label class="mb-1 block text-sm font-medium"
            >Name <span class="text-destructive">*</span></label
          >
          <input
            v-model="form.name"
            type="text"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
            placeholder="Skill name"
            required
            data-testid="remy-skills-form-name"
          />
        </div>
        <div>
          <label class="mb-1 block text-sm font-medium">Description</label>
          <textarea
            v-model="form.description"
            rows="2"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
            placeholder="What this skill does"
            data-testid="remy-skills-form-description"
          />
        </div>
        <div>
          <label class="mb-1 block text-sm font-medium">Triggers</label>
          <input
            v-model="form.triggersInput"
            type="text"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
            placeholder="trigger1, trigger2"
            data-testid="remy-skills-form-triggers"
          />
          <p class="mt-1 text-xs text-muted-foreground">
            Comma-separated trigger keywords
          </p>
        </div>
        <div>
          <label class="mb-1 block text-sm font-medium">Body (Markdown)</label>
          <textarea
            v-model="form.body"
            rows="6"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 font-mono text-sm"
            placeholder="# Skill instructions&#10;Write markdown here..."
            data-testid="remy-skills-form-body"
          />
        </div>
        <div class="flex items-center gap-2">
          <label class="flex items-center gap-2 text-sm cursor-pointer">
            <input
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
        <DialogFooter>
          <button
            type="button"
            class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
            data-testid="remy-skills-form-cancel"
            @click="closeForm"
          >
            Cancel
          </button>
          <button
            :disabled="saving || !form.name.trim()"
            type="submit"
            class="rounded-lg bg-primary px-4 py-2 text-sm font-semibold text-primary-foreground hover:brightness-110 disabled:opacity-50 transition-all"
            data-testid="remy-skills-form-submit"
          >
            {{ saving ? "Saving..." : editingId ? "Update" : "Create" }}
          </button>
        </DialogFooter>
      </form>
    </DialogContent>
  </Dialog>

  <Dialog :open="deleteOpen" @update:open="deleteOpen = false">
    <DialogContent class="sm:max-w-sm">
      <DialogHeader>
        <DialogTitle>Delete Skill</DialogTitle>
        <DialogDescription>
          Are you sure you want to delete "{{ deletingName }}"? This action
          cannot be undone.
        </DialogDescription>
      </DialogHeader>
      <DialogFooter>
        <button
          type="button"
          class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
          data-testid="remy-skills-delete-cancel"
          @click="deleteOpen = false"
        >
          Cancel
        </button>
        <button
          :disabled="deleting"
          type="button"
          class="rounded-lg bg-destructive px-4 py-2 text-sm font-semibold text-destructive-foreground hover:brightness-110 disabled:opacity-50 transition-all"
          data-testid="remy-skills-delete-confirm"
          @click="confirmDelete"
        >
          {{ deleting ? "Deleting..." : "Delete" }}
        </button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>

<script setup lang="ts">
import { ref, reactive } from "vue";
import { api } from "@/lib/api/client";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

export interface SkillFormItem {
  id: string;
  name: string;
  description?: string;
  triggers?: string[];
  body?: string;
  active: boolean;
}

const props = withDefaults(
  defineProps<{
    createDescription?: string;
    editDescription?: string;
    createEndpoint?: string;
    updateEndpoint?: string;
    deleteEndpoint?: string;
    listEndpoint?: string;
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
      const { error: err } = await (api as any).PUT(props.updateEndpoint, {
        params: { path: { skill_id: editingId.value } },
        body: payload,
      });
      if (err) {
        saveError.value = `Failed to update skill: ${err}`;
        return;
      }
    } else {
      const { error: err } = await (api as any).POST(props.createEndpoint, {
        body: payload,
      });
      if (err) {
        saveError.value = `Failed to create skill: ${err}`;
        return;
      }
    }
    closeForm();
    emit("saved");
  } catch (e: unknown) {
    saveError.value = `Failed to save skill: ${e instanceof Error ? e.message : String(e)}`;
  } finally {
    saving.value = false;
  }
}

async function confirmDelete() {
  if (!deletingId.value) return;
  deleting.value = true;
  try {
    const { error: err } = await (api as any).DELETE(props.deleteEndpoint, {
      params: { path: { skill_id: deletingId.value } },
    });
    if (err) {
      saveError.value = `Failed to delete skill: ${err}`;
      return;
    }
    deleteOpen.value = false;
    deletingId.value = null;
    emit("saved");
  } catch (e: unknown) {
    saveError.value = `Failed to delete skill: ${e instanceof Error ? e.message : String(e)}`;
  } finally {
    deleting.value = false;
  }
}

defineExpose({ openCreate, openEdit, openDelete });
</script>
