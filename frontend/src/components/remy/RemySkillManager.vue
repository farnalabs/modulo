<template>
  <div class="remy-skills flex flex-col flex-1 overflow-hidden">
    <div class="flex items-center justify-between p-3 border-b">
      <h3
        class="text-xs font-semibold uppercase tracking-wider text-muted-foreground"
      >
        Skills
      </h3>
      <Button v-if="!showForm" severity="secondary" text icon-only @click="openCreateForm" :title="$t('components.remy.RemySkillManager.new_skill')" :aria-label="$t('components.remy.new_skill')">
        <svg
          xmlns="http://www.w3.org/2000/svg"
          width="14"
          height="14"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
        >
          <line x1="12" y1="5" x2="12" y2="19" />
          <line x1="5" y1="12" x2="19" y2="12" />
        </svg>
      </Button>
    </div>

    <div v-if="showForm" class="remy-skill-form p-3 space-y-3 border-b">
      <input
        v-for="field in skillFields"
        :key="field.id"
        :id="field.id"
        v-model="form[field.model]"
        class="remy-skill-input"
        :aria-label="$t(field.labelKey)"
        :placeholder="$t(field.labelKey)"
      />
      <textarea
        v-model="form.body"
        class="remy-skill-textarea"
        :aria-label="$t('components.remy.RemySkillManager.skill_body_markdown')"
        :placeholder="$t('components.remy.RemySkillManager.skill_body_markdown')"
        rows="4"
      />
      <div class="flex gap-2 justify-end">
        <Button severity="secondary" text size="small" @click="cancelForm">{{ $t('common.cancel') }}</Button>
        <Button size="small" :disabled="!form.name.trim() || saving" @click="saveSkill">{{
          editingId ? "Update" : "Create"
        }}</Button>
      </div>
    </div>

    <div v-if="skillError" class="px-3 py-2 text-sm text-destructive">
      {{ skillError }}
    </div>

    <div class="flex-1 overflow-auto divide-y">
      <div
        v-if="skills.length === 0 && !showForm"
        class="flex items-center justify-center py-12"
      >
        <p class="text-sm text-muted-foreground">{{ $t('components.remy.RemySkillManager.no_skills_yet') }}</p>
      </div>
      <div v-for="skill in skills" :key="skill.id" class="remy-skill-item p-3">
        <div class="flex items-start justify-between gap-2">
          <div class="flex-1 min-w-0">
            <h4 class="text-sm font-medium">{{ skill.name }}</h4>
            <p
              v-if="skill.description"
              class="text-xs text-muted-foreground mt-0.5"
            >
              {{ skill.description }}
            </p>
            <div
              v-if="skill.triggers && skill.triggers.length"
              class="flex flex-wrap gap-1 mt-1"
            >
              <span
                v-for="t in skill.triggers"
                :key="t"
                class="remy-trigger-tag"
                >{{ t }}</span
              >
            </div>
          </div>
          <div class="flex gap-1 shrink-0">
            <button
              type="button"
              class="remy-skill-action"
              @click="editSkill(skill)"
              title="Edit"
              :aria-label="$t('common.edit')"
            >
              <svg
                xmlns="http://www.w3.org/2000/svg"
                width="12"
                height="12"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
              >
                <path
                  d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"
                />
                <path
                  d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"
                />
              </svg>
            </button>
            <button
              type="button"
              class="remy-skill-action text-destructive hover:bg-destructive/10"
              @click="deleteSkill(skill.id)"
              title="Delete"
              :aria-label="$t('common.delete')"
            >
              <svg
                xmlns="http://www.w3.org/2000/svg"
                width="12"
                height="12"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
              >
                <polyline points="3 6 5 6 21 6" />
                <path
                  d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"
                />
              </svg>
            </button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from "vue";
import { api } from "@/lib/api/client";
import { formatApiError } from "@/lib/api/formatError";
import Button from 'primevue/button'
import { useRemyStore } from "@/composables/useRemyStore";
import type { UserSkill } from "@/types/remy";

const store = useRemyStore();
const skills = ref<UserSkill[]>([]);
const showForm = ref(false);
const editingId = ref<string | null>(null);
const form = ref({ name: "", description: "", triggersText: "", body: "", active: true });
const skillError = ref<string | null>(null);
const saving = ref(false);

type SkillFieldModel = "name" | "description" | "triggersText";
const skillFields: { id: string; model: SkillFieldModel; labelKey: string }[] = [
  { id: "remyskill-name-input", model: "name", labelKey: "components.remy.RemySkillDialog.skill_name" },
  { id: "remyskill-description-input", model: "description", labelKey: "views.AdminRemyView.skill_description" },
  { id: "remyskill-triggers-input", model: "triggersText", labelKey: "components.remy.RemySkillManager.triggers_commaseparated" },
];

async function fetchSkills() {
  skillError.value = null;
  try {
    const { data, error: err } = await api.GET("/api/v1/me/remy/skills");
    if (err) {
      skillError.value = `Failed to load skills: ${formatApiError(err)}`;
    } else if (data) {
      skills.value = (data as UserSkill[]) ?? [];
    }
  } catch (e: unknown) {
    skillError.value = e instanceof Error ? e.message : "Failed to load skills";
  }
}

function openCreateForm() {
  editingId.value = null;
  form.value = { name: "", description: "", triggersText: "", body: "", active: true };
  showForm.value = true;
}

function cancelForm() {
  showForm.value = false;
  editingId.value = null;
}

function editSkill(skill: UserSkill) {
  editingId.value = skill.id;
  form.value = {
    name: skill.name,
    description: skill.description ?? "",
    triggersText: (skill.triggers ?? []).join(", "),
    body: skill.body,
    active: skill.active ?? true,
  };
  showForm.value = true;
}

async function saveSkill() {
  if (saving.value) return;
  saving.value = true;
  const payload = {
    name: form.value.name.trim(),
    description: form.value.description.trim(),
    triggers: form.value.triggersText
      .split(/[\s,]+/)
      .map((s) => s.trim())
      .filter(Boolean),
    body: form.value.body,
    active: form.value.active,
  };

  skillError.value = null;
  try {
    if (editingId.value) {
      const { error: err } = await api.PUT(
        "/api/v1/me/remy/skills/{skill_id}",
        {
          params: { path: { skill_id: editingId.value } },
          body: payload,
        },
      );
      if (err) {
        skillError.value = `Failed to update skill: ${formatApiError(err)}`;
        return;
      }
    } else {
      const { data, error: err } = await api.POST(
        "/api/v1/me/remy/skills",
        { body: payload },
      );
      if (err || !data) {
        skillError.value = `Failed to create skill: ${formatApiError(err)}`;
        return;
      }
    }
    showForm.value = false;
    editingId.value = null;
    store.signalSkillsChanged();
    await fetchSkills();
  } catch (e: unknown) {
    skillError.value = e instanceof Error ? e.message : "Failed to save skill";
  } finally {
    saving.value = false;
  }
}

async function deleteSkill(id: string) {
  skillError.value = null;
  try {
    const { error: err } = await api.DELETE(
      "/api/v1/me/remy/skills/{skill_id}",
      {
        params: { path: { skill_id: id } },
      },
    );
    if (err) {
      skillError.value = `Failed to delete skill: ${formatApiError(err)}`;
    } else {
      skills.value = skills.value.filter((s) => s.id !== id);
      store.signalSkillsChanged();
    }
  } catch (e: unknown) {
    skillError.value =
      e instanceof Error ? e.message : "Failed to delete skill";
  }
}

onMounted(() => {
  fetchSkills();
});
</script>

<style scoped>
@reference "../../style.css";
.remy-skill-input {
  @apply w-full rounded-lg px-3 py-2 text-sm outline-none;
  background-color: hsl(var(--background));
  border: 1px solid hsl(var(--input));
  color: hsl(var(--foreground));
}
.remy-skill-input:focus {
  border-color: hsl(var(--ring));
  box-shadow: 0 0 0 1px hsla(var(--ring) / 0.3);
}
.remy-skill-textarea {
  @apply w-full rounded-lg px-3 py-2 text-sm outline-none resize-none;
  background-color: hsl(var(--background));
  border: 1px solid hsl(var(--input));
  color: hsl(var(--foreground));
}
.remy-skill-textarea:focus {
  border-color: hsl(var(--ring));
  box-shadow: 0 0 0 1px hsla(var(--ring) / 0.3);
}
.remy-skill-item {
  @apply transition-colors;
}
.remy-skill-item:hover {
  background-color: hsl(var(--accent));
}
.remy-skill-action {
  @apply rounded p-1 transition-colors;
  color: hsl(var(--muted-foreground));
}
.remy-skill-action:hover {
  color: hsl(var(--foreground));
  background-color: hsl(var(--accent));
}
.remy-trigger-tag {
  @apply inline-flex items-center rounded-full px-2 py-0.5 text-xs;
  background-color: hsl(var(--muted));
  color: hsl(var(--muted-foreground));
}
</style>
