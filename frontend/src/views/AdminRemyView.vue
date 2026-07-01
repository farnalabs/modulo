<template>
  <FeatureGate feature-name="remy" required-tier="team">
    <template #locked="{ tooltip }">
      <div data-theme="agent" class="mx-auto max-w-6xl space-y-6 p-6">
        <div class="mb-4 flex items-center gap-2 rounded-lg border border-warning/30 bg-warning/5 p-4 text-sm text-warning">
          <LockIcon :locked="true" :tooltip="tooltip" />
          <span>Remy is not available on your current plan.</span>
        </div>
      </div>
    </template>

    <div data-theme="agent" class="mx-auto max-w-6xl space-y-8 p-6">
      <header>
        <h1 class="text-3xl font-bold tracking-tight">Remy Configuration</h1>
        <p class="mt-1 text-muted-foreground">Configure Remy AI assistant behaviour, access, and skills</p>
      </header>

      <LoadingSpinner v-if="loading" />

      <ErrorAlert v-else-if="loadError" :message="loadError" :on-retry="loadAll" />

      <template v-else>
        <!-- Access List -->
        <div class="card p-4">
          <h2 class="mb-3 text-lg font-semibold">Access List</h2>
          <p class="mb-4 text-sm text-muted-foreground">Control who can use Remy within the organisation</p>

          <div class="space-y-4">
            <div>
              <label class="mb-1 block text-sm font-medium">User IDs</label>
              <textarea
                v-model="accessList.userIds"
                rows="3"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 font-mono text-sm"
                placeholder="One per line or comma-separated UUIDs"
                data-testid="remy-access-users"
              />
            </div>
            <div>
              <label class="mb-1 block text-sm font-medium">Team IDs</label>
              <textarea
                v-model="accessList.teamIds"
                rows="3"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 font-mono text-sm"
                placeholder="One per line or comma-separated UUIDs"
                data-testid="remy-access-teams"
              />
            </div>
            <div>
              <label class="mb-1 block text-sm font-medium">Org Roles</label>
              <div class="flex flex-wrap gap-4">
                <label class="flex items-center gap-2 text-sm cursor-pointer" v-for="role in orgRoles" :key="role">
                  <input
                    type="checkbox"
                    :value="role"
                    :checked="accessList.selectedRoles.includes(role)"
                    class="rounded border-input"
                    @change="toggleRole(role)"
                  />
                  {{ role }}
                </label>
              </div>
            </div>
            <div v-if="accessError" class="text-sm text-destructive">{{ accessError }}</div>
            <button
              :disabled="accessSaving"
              class="rounded-lg bg-primary px-4 py-2 text-sm font-semibold text-primary-foreground hover:brightness-110 disabled:opacity-50 transition-all"
              data-testid="remy-access-save"
              @click="saveAccessList"
            >
              {{ accessSaving ? 'Saving...' : 'Save Access List' }}
            </button>
          </div>
        </div>

        <!-- Default Model Configuration -->
        <div class="card p-4">
          <h2 class="mb-3 text-lg font-semibold">Default Model Configuration</h2>
          <p class="mb-4 text-sm text-muted-foreground">Set the default model and allowed providers for Remy</p>

          <div class="space-y-4">
            <div>
              <label class="mb-1 block text-sm font-medium">Default Provider</label>
              <select
                v-model="modelConfig.defaultProvider"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                data-testid="remy-model-provider"
              >
                <option value="anthropic">Anthropic</option>
                <option value="openai">OpenAI</option>
                <option value="gemini">Google Gemini</option>
                <option value="deepseek">DeepSeek</option>
                <option value="groq">Groq</option>
              </select>
            </div>
            <div>
              <label class="mb-1 block text-sm font-medium">Default Model</label>
              <input
                v-model="modelConfig.defaultModel"
                type="text"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                placeholder="claude-sonnet-4-20250514"
                data-testid="remy-model-name"
              />
            </div>
            <div>
              <label class="mb-1 block text-sm font-medium">Default Context Window Size</label>
              <input
                v-model.number="modelConfig.contextWindow"
                type="number"
                min="1024"
                step="1024"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                placeholder="200000"
                data-testid="remy-model-context"
              />
            </div>
            <div>
              <label class="mb-1 block text-sm font-medium">Allowed Providers</label>
              <div class="flex flex-wrap gap-2" data-testid="remy-allowed-providers">
                <button
                  v-for="provider in allProviders"
                  :key="provider"
                  type="button"
                  class="rounded-full px-3 py-1 text-xs font-medium transition-all"
                  :class="modelConfig.allowedProviders.includes(provider) ? 'bg-primary text-primary-foreground' : 'bg-muted text-muted-foreground hover:bg-muted/80'"
                  @click="toggleAllowedProvider(provider)"
                >
                  {{ provider }}
                </button>
              </div>
            </div>
            <div>
              <label class="mb-1 block text-sm font-medium">Allowed Models</label>
              <input
                v-model="modelConfig.allowedModels"
                type="text"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                placeholder="claude-sonnet-4-20250514, gpt-4o, gemini-2.5-pro"
                data-testid="remy-allowed-models"
              />
            </div>
            <div v-if="modelError" class="text-sm text-destructive">{{ modelError }}</div>
            <button
              :disabled="modelSaving"
              class="rounded-lg bg-primary px-4 py-2 text-sm font-semibold text-primary-foreground hover:brightness-110 disabled:opacity-50 transition-all"
              data-testid="remy-model-save"
              @click="saveModelConfig"
            >
              {{ modelSaving ? 'Saving...' : 'Save Model Config' }}
            </button>
          </div>
        </div>

        <!-- System Prompt -->
        <div class="card p-4">
          <h2 class="mb-3 text-lg font-semibold">System Prompt</h2>
          <p class="mb-4 text-sm text-muted-foreground">Base system prompt that guides Remy's behaviour</p>

          <div class="space-y-4">
            <div>
              <textarea
                v-model="systemPrompt"
                rows="8"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 font-mono text-sm"
                placeholder="You are a helpful AI assistant..."
                data-testid="remy-system-prompt"
              />
            </div>
            <div v-if="promptError" class="text-sm text-destructive">{{ promptError }}</div>
            <button
              :disabled="promptSaving"
              class="rounded-lg bg-primary px-4 py-2 text-sm font-semibold text-primary-foreground hover:brightness-110 disabled:opacity-50 transition-all"
              data-testid="remy-prompt-save"
              @click="saveSystemPrompt"
            >
              {{ promptSaving ? 'Saving...' : 'Save System Prompt' }}
            </button>
          </div>
        </div>

        <!-- Additional Guidance -->
        <div class="card p-4">
          <h2 class="mb-3 text-lg font-semibold">Additional Guidance</h2>
          <p class="mb-4 text-sm text-muted-foreground">Extra instructions to append to the system prompt</p>

          <div class="space-y-4">
            <div>
              <textarea
                v-model="guidance"
                rows="5"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 font-mono text-sm"
                placeholder="Additional instructions..."
                data-testid="remy-guidance"
              />
            </div>
            <div v-if="guidanceError" class="text-sm text-destructive">{{ guidanceError }}</div>
            <button
              :disabled="guidanceSaving"
              class="rounded-lg bg-primary px-4 py-2 text-sm font-semibold text-primary-foreground hover:brightness-110 disabled:opacity-50 transition-all"
              data-testid="remy-guidance-save"
              @click="saveGuidance"
            >
              {{ guidanceSaving ? 'Saving...' : 'Save Guidance' }}
            </button>
          </div>
        </div>

        <!-- Skills Manager -->
        <div class="card p-4">
          <div class="flex items-center justify-between mb-4">
            <div>
              <h2 class="text-lg font-semibold">Skills</h2>
              <p class="text-sm text-muted-foreground">Organisation-level skills that Remy can use</p>
            </div>
            <button
              class="rounded-lg bg-primary px-4 py-2 text-sm font-semibold text-primary-foreground border border-primary/30 hover:brightness-110 transition-all"
              data-testid="remy-skills-add"
              @click="skillDialogRef?.openCreate()"
            >
              Add Skill
            </button>
          </div>

          <div v-if="skills.length === 0" class="py-8 text-center">
            <p class="text-sm text-muted-foreground">No skills configured yet.</p>
          </div>
          <div v-else class="overflow-hidden rounded-lg border">
            <table class="w-full text-left text-sm">
              <thead class="bg-muted/50">
                <tr>
                  <th class="px-4 py-3 font-medium">Name</th>
                  <th class="px-4 py-3 font-medium">Description</th>
                  <th class="px-4 py-3 font-medium">Triggers</th>
                  <th class="px-4 py-3 font-medium">Active</th>
                  <th class="px-4 py-3 font-medium text-right">Actions</th>
                </tr>
              </thead>
              <tbody class="divide-y">
                <tr
                  v-for="skill in skills"
                  :key="skill.id"
                  class="hover:bg-muted/30 transition-colors"
                >
                  <td class="px-4 py-3 font-medium">{{ skill.name }}</td>
                  <td class="px-4 py-3 text-muted-foreground max-w-xs truncate">{{ skill.description || '—' }}</td>
                  <td class="px-4 py-3">
                    <div class="flex flex-wrap gap-1">
                      <span
                        v-for="trigger in (skill.triggers || [])"
                        :key="trigger"
                        class="rounded-full bg-muted px-2 py-0.5 text-xs text-muted-foreground"
                      >
                        {{ trigger }}
                      </span>
                      <span v-if="!skill.triggers?.length" class="text-xs text-muted-foreground">—</span>
                    </div>
                  </td>
                  <td class="px-4 py-3">
                    <button
                      class="inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium transition-colors"
                      :class="skill.active ? 'bg-success/10 text-success' : 'bg-muted text-muted-foreground'"
                      @click="toggleSkillActive(skill)"
                    >
                      <span
                        class="h-1.5 w-1.5 rounded-full"
                        :class="skill.active ? 'bg-success' : 'bg-muted-foreground'"
                      />
                      {{ skill.active ? 'Active' : 'Inactive' }}
                    </button>
                  </td>
                  <td class="px-4 py-3 text-right">
                    <div class="flex items-center justify-end gap-1">
                      <button
                        class="rounded p-1 text-muted-foreground hover:bg-accent"
                        :aria-label="'Edit skill'"
                        title="Edit skill"
                        @click="skillDialogRef?.openEdit(skill)"
                      >
                        <svg class="h-4 w-4" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                          <path d="M17 3a2.85 2.85 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z" />
                        </svg>
                      </button>
                      <button
                        class="rounded p-1 text-destructive hover:bg-destructive/10"
                        :aria-label="'Delete skill'"
                        title="Delete skill"
                        @click="skillDialogRef?.openDelete(skill)"
                      >
                        <svg class="h-4 w-4" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                          <path d="M3 6h18" /><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6" /><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2" />
                        </svg>
                      </button>
                    </div>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <div v-if="skillError" class="px-3 pt-2 text-sm text-destructive">{{ skillError }}</div>
        </div>

        <RemySkillDialog
          ref="skillDialogRef"
          create-description="Create a new organisation-level skill for Remy."
          edit-description="Update the skill configuration."
          @saved="loadSkills"
        />
      </template>
    </div>
  </FeatureGate>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { api } from '../lib/api/client'
import { usePlanStore } from '../stores/planStore'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import FeatureGate from '../components/FeatureGate.vue'
import LockIcon from '../components/LockIcon.vue'
import RemySkillDialog from '../components/remy/RemySkillDialog.vue'
import type { SkillItem } from '../types/remy'

const planStore = usePlanStore()

const loading = ref(true)
const loadError = ref<string | null>(null)

const orgRoles = ['admin', 'operator', 'runner', 'viewer']

// Access list
const accessList = reactive({
  userIds: '',
  teamIds: '',
  selectedRoles: [] as string[],
})
const accessSaving = ref(false)
const accessError = ref<string | null>(null)

function toggleRole(role: string) {
  const idx = accessList.selectedRoles.indexOf(role)
  if (idx >= 0) {
    accessList.selectedRoles.splice(idx, 1)
  } else {
    accessList.selectedRoles.push(role)
  }
}

async function saveAccessList() {
  accessSaving.value = true
  accessError.value = null
  try {
    const userIds = accessList.userIds.split(/[\n,]+/).map(s => s.trim()).filter(Boolean)
    const teamIds = accessList.teamIds.split(/[\n,]+/).map(s => s.trim()).filter(Boolean)
    const { error: err } = await (api as any).PUT('/api/v1/admin/remy/config', {
      body: {
        access_list: { user_ids: userIds, team_ids: teamIds, org_roles: accessList.selectedRoles },
      },
    })
    if (err) {
      accessError.value = `Failed to save access list: ${err}`
    } else {
      accessError.value = null
    }
  } catch (e: unknown) {
    accessError.value = `Failed to save access list: ${e instanceof Error ? e.message : String(e)}`
  } finally {
    accessSaving.value = false
  }
}

// Model config
const allProviders = ['anthropic', 'openai', 'gemini', 'deepseek', 'groq']

const modelConfig = reactive({
  defaultProvider: 'anthropic',
  defaultModel: '',
  contextWindow: 200000,
  allowedProviders: ['anthropic'] as string[],
  allowedModels: '',
})
const modelSaving = ref(false)
const modelError = ref<string | null>(null)

function toggleAllowedProvider(provider: string) {
  const idx = modelConfig.allowedProviders.indexOf(provider)
  if (idx >= 0) {
    modelConfig.allowedProviders.splice(idx, 1)
  } else {
    modelConfig.allowedProviders.push(provider)
  }
}

async function saveModelConfig() {
  modelSaving.value = true
  modelError.value = null
  try {
    const allowedModels = modelConfig.allowedModels.split(/[\s,]+/).map(s => s.trim()).filter(Boolean)
    const { error: err } = await (api as any).PUT('/api/v1/admin/remy/config', {
      body: {
        default_provider: modelConfig.defaultProvider,
        default_model: modelConfig.defaultModel,
        default_context_window: modelConfig.contextWindow,
        allowed_providers: modelConfig.allowedProviders,
        allowed_models: allowedModels,
      },
    })
    if (err) {
      modelError.value = `Failed to save model config: ${err}`
    } else {
      modelError.value = null
    }
  } catch (e: unknown) {
    modelError.value = `Failed to save model config: ${e instanceof Error ? e.message : String(e)}`
  } finally {
    modelSaving.value = false
  }
}

// System prompt
const systemPrompt = ref('')
const promptSaving = ref(false)
const promptError = ref<string | null>(null)

async function saveSystemPrompt() {
  promptSaving.value = true
  promptError.value = null
  try {
    const { error: err } = await (api as any).PUT('/api/v1/admin/remy/config', {
      body: { system_prompt: systemPrompt.value },
    })
    if (err) {
      promptError.value = `Failed to save system prompt: ${err}`
    } else {
      promptError.value = null
    }
  } catch (e: unknown) {
    promptError.value = `Failed to save system prompt: ${e instanceof Error ? e.message : String(e)}`
  } finally {
    promptSaving.value = false
  }
}

// Guidance
const guidance = ref('')
const guidanceSaving = ref(false)
const guidanceError = ref<string | null>(null)

async function saveGuidance() {
  guidanceSaving.value = true
  guidanceError.value = null
  try {
    const { error: err } = await (api as any).PUT('/api/v1/admin/remy/config', {
      body: { additional_guidance: guidance.value },
    })
    if (err) {
      guidanceError.value = `Failed to save guidance: ${err}`
    } else {
      guidanceError.value = null
    }
  } catch (e: unknown) {
    guidanceError.value = `Failed to save guidance: ${e instanceof Error ? e.message : String(e)}`
  } finally {
    guidanceSaving.value = false
  }
}

// Skills
const skills = ref<SkillItem[]>([])
const skillError = ref<string | null>(null)
const skillDialogRef = ref<InstanceType<typeof RemySkillDialog> | null>(null)

async function toggleSkillActive(skill: SkillItem) {
  const newActive = !skill.active
  skillError.value = null
  try {
    const { error: err } = await (api as any).PUT('/api/v1/admin/remy/skills/{skill_id}', {
      params: { path: { skill_id: skill.id } },
      body: { active: newActive },
    })
    if (err) {
      skillError.value = `Failed to toggle skill: ${err}`
      return
    }
    skill.active = newActive
  } catch (e: unknown) {
    skillError.value = `Failed to toggle skill: ${e instanceof Error ? e.message : String(e)}`
  }
}

async function loadSkills() {
  try {
    const { data, error: err } = await (api as any).GET('/api/v1/admin/remy/skills')
    if (err) {
      loadError.value = `Failed to load skills: ${err}`
    } else if (data) {
      skills.value = (data as { items: SkillItem[] }).items || (data as SkillItem[])
    }
  } catch (e: unknown) {
    loadError.value = `Failed to load skills: ${e instanceof Error ? e.message : String(e)}`
  }
}

async function loadConfig() {
  try {
    const { data, error: err } = await (api as any).GET('/api/v1/admin/remy/config')
    if (!err && data) {
      const cfg = data as {
        access_list?: { user_ids?: string[]; team_ids?: string[]; org_roles?: string[] }
        default_provider?: string
        default_model?: string
        default_context_window?: number
        allowed_providers?: string[]
        allowed_models?: string[]
        system_prompt?: string
        additional_guidance?: string
      }
      const acl = cfg.access_list || {}
      accessList.userIds = (acl.user_ids || []).join('\n')
      accessList.teamIds = (acl.team_ids || []).join('\n')
      accessList.selectedRoles = acl.org_roles || []
      modelConfig.defaultProvider = cfg.default_provider || 'anthropic'
      modelConfig.defaultModel = cfg.default_model || ''
      modelConfig.contextWindow = cfg.default_context_window || 200000
      modelConfig.allowedProviders = cfg.allowed_providers || ['anthropic']
      modelConfig.allowedModels = (cfg.allowed_models || []).join(', ')
      systemPrompt.value = cfg.system_prompt || ''
      guidance.value = cfg.additional_guidance || ''
    }
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e)
      console.warn('Failed to load Remy config:', msg)
    }
}

async function loadAll() {
  loading.value = true
  loadError.value = null
  try {
    await Promise.all([
      loadConfig(),
      loadSkills(),
    ])
  } finally {
    loading.value = false
  }
}

onMounted(() => { planStore.fetchPlan(); loadAll() })
</script>
