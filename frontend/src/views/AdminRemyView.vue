<template>
  <div data-theme="agent" class="mx-auto max-w-6xl space-y-8 p-6">
    <header>
      <h1 class="text-3xl font-bold tracking-tight">{{ $t('views.AdminRemyView.remy_configuration') }}</h1>
      <p class="mt-1 text-muted-foreground">{{ $t('views.AdminRemyView.configure_remy_ai_assistant_behaviour_access_and_skills') }}</p>
    </header>

    <LoadingSpinner v-if="loading" />

    <ErrorAlert v-else-if="loadError" :message="loadError" :on-retry="loadAll" />

    <template v-else>
      <TooltipProvider>
      <!-- Configured Providers -->
      <div class="card p-4" data-testid="remy-providers">
        <h2 class="mb-3 text-lg font-semibold">{{ $t('views.AdminRemyView.configured_providers') }}</h2>
        <p class="mb-4 text-sm text-muted-foreground">{{ $t('views.AdminRemyView.api_keys_configured_for_each_llm_provider_remy_will_use_thes') }}</p>

        <div v-if="providersLoading" class="py-4 text-center text-sm text-muted-foreground">
          Loading provider status...
        </div>
        <div v-else class="grid grid-cols-2 gap-3 sm:grid-cols-3 md:grid-cols-5">
          <Tooltip
            v-for="p in providerStatus"
            :key="p.id"
            :delay-duration="300"
          >
            <TooltipTrigger as-child>
              <span
                class="flex flex-col items-center gap-2 rounded-lg border p-4 text-center transition-all cursor-help"
                :class="p.configured ? 'border-success/40 bg-success/5' : 'border-muted bg-muted/20 opacity-60'"
              >
                <span
                  class="flex h-10 w-10 items-center justify-center rounded-full text-lg font-bold"
                  :class="p.configured ? 'bg-success/20 text-success' : 'bg-muted text-muted-foreground'"
                >
                  {{ p.configured ? '✓' : '?' }}
                </span>
                <span class="text-sm font-medium">{{ p.label }}</span>
                <span class="text-xs" :class="p.configured ? 'text-success' : 'text-muted-foreground'">
                  {{ p.configured ? 'Configured' : 'Not set' }}
                </span>
              </span>
            </TooltipTrigger>
            <TooltipContent side="top" class="max-w-xs text-left">
              <p>{{ p.configured ? 'API key configured — Remy can route to ' + p.label + '.' : 'No API key set. Remy will skip ' + p.label + ' until a backend is configured.' }}</p>
              <p v-if="!p.configured" class="mt-1 text-xs opacity-70">Add a model backend for {{ p.label }} in the Model Backends page.</p>
            </TooltipContent>
          </Tooltip>
        </div>
        <div class="mt-3 text-xs text-muted-foreground">
          <Tooltip :delay-duration="300">
            <TooltipTrigger as-child>
              <a href="/admin/model-backends" class="underline hover:text-foreground">{{ $t('views.AdminRemyView.manage_model_backends') }}</a>
            </TooltipTrigger>
            <TooltipContent side="top">
              <p>{{ $t('views.AdminRemyView.add_edit_or_remove_api_keys_for_llm_providers') }}</p>
            </TooltipContent>
          </Tooltip>
        </div>
      </div>

      <!-- Access List -->
      <div class="card p-4">
        <h2 class="mb-3 text-lg font-semibold">{{ $t('views.AdminRemyView.access_list') }}</h2>
        <p class="mb-4 text-sm text-muted-foreground">{{ $t('views.AdminRemyView.control_who_can_use_remy_within_the_organisation') }}</p>

          <div class="space-y-4">
            <div>
              <Tooltip :delay-duration="300">
                <TooltipTrigger as-child>
                  <label class="mb-1 block text-sm font-medium cursor-help">{{ $t('views.AdminRemyView.user_ids') }}</label>
                </TooltipTrigger>
                <TooltipContent side="right" class="max-w-xs">
                  <p>{{ $t('views.AdminRemyView.commaseparated_or_lineseparated_uuids_of_users_who_should_ha') }}</p>
                </TooltipContent>
              </Tooltip>
            <textarea
              v-model="accessList.userIds"
              rows="3"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 font-mono text-sm"
              :placeholder="$t('views.AdminRemyView.one_per_line_or_commaseparated_uuids')"
              data-testid="remy-access-users"
            />
          </div>
            <div>
              <Tooltip :delay-duration="300">
                <TooltipTrigger as-child>
                  <label class="mb-1 block text-sm font-medium cursor-help">{{ $t('views.AdminRemyView.team_ids') }}</label>
                </TooltipTrigger>
                <TooltipContent side="right" class="max-w-xs">
                  <p>{{ $t('views.AdminRemyView.commaseparated_or_lineseparated_team_uuids_members_of_these_') }}</p>
                </TooltipContent>
              </Tooltip>
            <textarea
              v-model="accessList.teamIds"
              rows="3"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 font-mono text-sm"
              :placeholder="$t('views.AdminRemyView.one_per_line_or_commaseparated_uuids')"
              data-testid="remy-access-teams"
            />
          </div>
            <div>
              <Tooltip :delay-duration="300">
                <TooltipTrigger as-child>
                  <label class="mb-1 block text-sm font-medium cursor-help">{{ $t('views.AdminRemyView.org_roles') }}</label>
                </TooltipTrigger>
                <TooltipContent side="right" class="max-w-xs">
                  <p>{{ $t('views.AdminRemyView.users_with_the_selected_organisation_roles_will_have_access_') }}</p>
                </TooltipContent>
              </Tooltip>
              <div class="flex flex-wrap gap-4">
                <Tooltip v-for="role in orgRoles" :key="role" :delay-duration="300">
                  <TooltipTrigger as-child>
                    <label class="flex items-center gap-2 text-sm cursor-pointer">
                      <input
                        type="checkbox"
                        :value="role"
                        :checked="accessList.selectedRoles.includes(role)"
                        class="rounded border-input"
                        @change="toggleRole(role)"
                      />
                      {{ role }}
                    </label>
                  </TooltipTrigger>
                  <TooltipContent side="top" class="max-w-xs">
                    <p>{{ role === 'admin' ? 'Full access to all settings and Remy configuration.' : role === 'operator' ? 'Can create and manage pipelines, use Remy.' : role === 'runner' ? 'Can execute pipeline runs, use Remy.' : 'Read-only access — can view but not edit, use Remy.' }}</p>
                  </TooltipContent>
                </Tooltip>
            </div>
          </div>
            <div v-if="accessError" class="text-sm text-destructive">{{ accessError }}</div>
            <Tooltip :delay-duration="300">
              <TooltipTrigger as-child>
                <button
                  :disabled="accessSaving"
                  class="rounded-lg bg-primary px-4 py-2 text-sm font-semibold text-primary-foreground hover:brightness-110 disabled:opacity-50 transition-all"
                  data-testid="remy-access-save"
                  @click="saveAccessList"
                >
                  {{ accessSaving ? 'Saving...' : 'Save Access List' }}
                </button>
              </TooltipTrigger>
              <TooltipContent side="top">
                <p>{{ $t('views.AdminRemyView.save_current_access_list_configuration') }}</p>
              </TooltipContent>
            </Tooltip>
        </div>
      </div>

      <!-- Default Model Configuration -->
      <div class="card p-4">
        <h2 class="mb-3 text-lg font-semibold">{{ $t('views.AdminRemyView.default_model_configuration') }}</h2>
        <p class="mb-4 text-sm text-muted-foreground">{{ $t('views.AdminRemyView.set_the_default_model_and_allowed_providers_for_remy') }}</p>

        <div class="space-y-4">
          <div>
            <label class="mb-1 block text-sm font-medium">{{ $t('views.AdminRemyView.default_provider') }}</label>
            <select
              v-model="modelConfig.defaultProvider"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
              data-testid="remy-model-provider"
            >
              <option value="anthropic">Anthropic</option>
              <option value="openai">OpenAI</option>
              <option value="gemini">{{ $t('views.AdminRemyView.google_gemini') }}</option>
              <option value="deepseek">DeepSeek</option>
              <option value="groq">Groq</option>
            </select>
          </div>
          <div>
            <label class="mb-1 block text-sm font-medium">{{ $t('views.AdminRemyView.default_model') }}</label>
            <input
              v-model="modelConfig.defaultModel"
              type="text"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
              placeholder="claude-sonnet-4-20250514"
              data-testid="remy-model-name"
            />
          </div>
          <div>
            <label class="mb-1 block text-sm font-medium">{{ $t('views.AdminRemyView.default_context_window_size') }}</label>
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
            <label class="mb-1 block text-sm font-medium">{{ $t('views.AdminRemyView.allowed_providers') }}</label>
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
            <label class="mb-1 block text-sm font-medium">{{ $t('views.AdminRemyView.allowed_models') }}</label>
            <input
              v-model="modelConfig.allowedModels"
              type="text"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
              :placeholder="$t('views.AdminRemyView.claudesonnet420250514_gpt4o_gemini25pro')"
              data-testid="remy-allowed-models"
            />
          </div>
            <div v-if="modelError" class="text-sm text-destructive">{{ modelError }}</div>
            <Tooltip :delay-duration="300">
              <TooltipTrigger as-child>
                <button
                  :disabled="modelSaving"
                  class="rounded-lg bg-primary px-4 py-2 text-sm font-semibold text-primary-foreground hover:brightness-110 disabled:opacity-50 transition-all"
                  data-testid="remy-model-save"
                  @click="saveModelConfig"
                >
                  {{ modelSaving ? 'Saving...' : 'Save Model Config' }}
                </button>
              </TooltipTrigger>
              <TooltipContent side="top">
                <p>{{ $t('views.AdminRemyView.save_default_model_provider_and_allowed_model_configuration') }}</p>
              </TooltipContent>
            </Tooltip>
        </div>
      </div>

      <!-- System Prompt -->
      <div class="card p-4">
        <h2 class="mb-3 text-lg font-semibold">{{ $t('views.AdminRemyView.system_prompt') }}</h2>
        <p class="mb-4 text-sm text-muted-foreground">{{ $t('views.AdminRemyView.base_system_prompt_that_guides_remys_behaviour') }}</p>

        <div class="space-y-4">
          <div>
            <textarea
              v-model="systemPrompt"
              rows="8"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 font-mono text-sm"
              :placeholder="$t('views.AdminRemyView.you_are_a_helpful_ai_assistant')"
              data-testid="remy-system-prompt"
            />
          </div>
            <div v-if="promptError" class="text-sm text-destructive">{{ promptError }}</div>
            <Tooltip :delay-duration="300">
              <TooltipTrigger as-child>
                <button
                  :disabled="promptSaving"
                  class="rounded-lg bg-primary px-4 py-2 text-sm font-semibold text-primary-foreground hover:brightness-110 disabled:opacity-50 transition-all"
                  data-testid="remy-prompt-save"
                  @click="saveSystemPrompt"
                >
                  {{ promptSaving ? 'Saving...' : 'Save System Prompt' }}
                </button>
              </TooltipTrigger>
              <TooltipContent side="top">
                <p>{{ $t('views.AdminRemyView.save_the_base_system_prompt_that_guides_remys_behaviour') }}</p>
              </TooltipContent>
            </Tooltip>
        </div>
      </div>

      <!-- Additional Guidance -->
      <div class="card p-4">
        <h2 class="mb-3 text-lg font-semibold">{{ $t('views.AdminRemyView.additional_guidance') }}</h2>
        <p class="mb-4 text-sm text-muted-foreground">{{ $t('views.AdminRemyView.extra_instructions_to_append_to_the_system_prompt') }}</p>

        <div class="space-y-4">
          <div>
            <textarea
              v-model="guidance"
              rows="5"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 font-mono text-sm"
              :placeholder="$t('views.AdminRemyView.additional_instructions')"
              data-testid="remy-guidance"
            />
          </div>
            <div v-if="guidanceError" class="text-sm text-destructive">{{ guidanceError }}</div>
            <Tooltip :delay-duration="300">
              <TooltipTrigger as-child>
                <button
                  :disabled="guidanceSaving"
                  class="rounded-lg bg-primary px-4 py-2 text-sm font-semibold text-primary-foreground hover:brightness-110 disabled:opacity-50 transition-all"
                  data-testid="remy-guidance-save"
                  @click="saveGuidance"
                >
                  {{ guidanceSaving ? 'Saving...' : 'Save Guidance' }}
                </button>
              </TooltipTrigger>
              <TooltipContent side="top">
                <p>{{ $t('views.AdminRemyView.save_extra_instructions_appended_to_the_system_prompt') }}</p>
              </TooltipContent>
            </Tooltip>
        </div>
      </div>

      <!-- Skills Manager -->
      <div class="card p-4">
        <div class="flex items-center justify-between mb-4">
          <div>
            <h2 class="text-lg font-semibold">Skills</h2>
            <p class="text-sm text-muted-foreground">{{ $t('views.AdminRemyView.organisationlevel_skills_that_remy_can_use') }}</p>
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
          <p class="text-sm text-muted-foreground">{{ $t('views.AdminRemyView.no_skills_configured_yet') }}</p>
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
                <Tooltip :delay-duration="300">
                  <TooltipTrigger as-child>
                    <td class="px-4 py-3 text-muted-foreground max-w-xs truncate">{{ skill.description || '—' }}</td>
                  </TooltipTrigger>
                  <TooltipContent side="top" class="max-w-xs">
                    <p>{{ skill.description || '—' }}</p>
                  </TooltipContent>
                </Tooltip>
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
                    class="inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium transition-colors disabled:opacity-50"
                    :class="skill.active ? 'bg-success/10 text-success' : 'bg-muted text-muted-foreground'"
                    :disabled="skillToggling[skill.id]"
                    @click="toggleSkillActive(skill)"
                  >
                    <span
                      class="h-1.5 w-1.5 rounded-full"
                      :class="skill.active ? 'bg-success' : 'bg-muted-foreground'"
                    />
                    {{ skillToggling[skill.id] ? '...' : (skill.active ? 'Active' : 'Inactive') }}
                  </button>
                </td>
                <td class="px-4 py-3 text-right">
                  <div class="flex items-center justify-end gap-1">
                    <button
                      class="rounded p-1 text-muted-foreground hover:bg-accent"
                      :aria-label="$t('views.AdminRemyView.edit_skill')"
                      :title="$t('views.AdminRemyView.edit_skill')"
                      @click="skillDialogRef?.openEdit(skill)"
                    >
                      <svg class="h-4 w-4" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <path d="M17 3a2.85 2.85 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z" />
                      </svg>
                    </button>
                    <button
                      class="rounded p-1 text-destructive hover:bg-destructive/10"
                      :aria-label="$t('views.AdminRemyView.delete_skill')"
                      :title="$t('components.remy.RemySkillDialog.delete_skill')"
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
      </TooltipProvider>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { api } from '../lib/api/client'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import RemySkillDialog from '../components/remy/RemySkillDialog.vue'
import {
  TooltipProvider,
  Tooltip,
  TooltipTrigger,
  TooltipContent,
} from '../components/ui/tooltip'
import type { SkillItem } from '../types/remy'

interface ProviderStatus {
  id: string
  label: string
  configured: boolean
}

const REMY_PROVIDERS: { id: string; label: string }[] = [
  { id: 'anthropic', label: 'Anthropic' },
  { id: 'openai', label: 'OpenAI' },
  { id: 'gemini', label: 'Gemini' },
  { id: 'deepseek', label: 'DeepSeek' },
  { id: 'groq', label: 'Groq' },
]

const PROVIDER_TOOLTIPS: Record<string, string> = {
  anthropic: 'Configure an Anthropic API key to enable Claude models (claude-sonnet, claude-haiku).',
  openai: 'Configure an OpenAI API key to enable GPT models (GPT-4o, GPT-4o-mini).',
  gemini: 'Configure a Google AI API key to enable Gemini models (Gemini 2.5 Pro, Gemini 2.0 Flash).',
  deepseek: 'Configure a DeepSeek API key to enable DeepSeek models (DeepSeek V3, DeepSeek R1).',
  groq: 'Configure a Groq API key to enable fast inference on open-weight models.',
}

const loading = ref(true)
const loadError = ref<string | null>(null)

const providerStatus = ref<ProviderStatus[]>([])
const providersLoading = ref(true)

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
const skillToggling = ref<Record<string, boolean>>({})

async function toggleSkillActive(skill: SkillItem) {
  skillToggling.value[skill.id] = true
  skillError.value = null
  try {
    const { error: err } = await (api as any).PUT('/api/v1/admin/remy/skills/{skill_id}', {
      params: { path: { skill_id: skill.id } },
      body: { active: !skill.active },
    })
    if (err) {
      skillError.value = `Failed to toggle skill: ${err}`
      return
    }
    await loadSkills()
  } catch (e: unknown) {
    skillError.value = `Failed to toggle skill: ${e instanceof Error ? e.message : String(e)}`
  } finally {
    skillToggling.value[skill.id] = false
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

async function loadProviders() {
  providersLoading.value = true
  try {
    const { data, error: err } = await (api as any).GET('/api/v1/model-backends', {
      params: { query: { page_size: 100 } },
    })
    if (err) {
      console.warn('Failed to load model backends:', err)
      return
    }
    const backends = (data?.items ?? []) as { provider: string; has_credentials: boolean }[]
    const configuredProviders = new Set(backends.map(b => b.provider))
    providerStatus.value = REMY_PROVIDERS.map(p => ({
      ...p,
      configured: configuredProviders.has(p.id),
    }))
  } catch (e: unknown) {
    const msg = e instanceof Error ? e.message : String(e)
    console.warn('Failed to load provider status:', msg)
  } finally {
    providersLoading.value = false
  }
}

async function loadAll() {
  loading.value = true
  loadError.value = null
  try {
    await Promise.all([
      loadConfig(),
      loadSkills(),
      loadProviders(),
    ])
  } finally {
    loading.value = false
  }
}

onMounted(() => { loadAll() })
</script>
