<template>
  <FeatureGate feature-name="remy" required-tier="team" show-disabled>
  <div data-theme="agent" class="page-wide">
    <PageHeader :title="$t('views.AdminRemyView.remy_configuration')" :subtitle="$t('views.AdminRemyView.configure_remy_ai_assistant_behaviour_access_and_skills')" />
    <template v-if="loading">
      <div class="rounded-xl border border-border bg-card p-6 space-y-4">
        <div class="h-5 w-48 animate-pulse rounded bg-muted" />
        <div class="h-3 w-96 animate-pulse rounded bg-muted" />
        <div class="grid grid-cols-2 gap-3 sm:grid-cols-3 md:grid-cols-5 mt-4">
          <div v-for="n in 5" :key="n" class="flex flex-col items-center gap-2 rounded-lg border border-border p-4">
            <div class="h-10 w-10 animate-pulse rounded-full bg-muted" />
            <div class="h-4 w-20 animate-pulse rounded bg-muted" />
            <div class="h-3 w-16 animate-pulse rounded bg-muted" />
          </div>
        </div>
      </div>
      <div class="rounded-xl border border-border bg-card p-6 space-y-4 mt-6">
        <div class="h-5 w-40 animate-pulse rounded bg-muted" />
        <div class="h-3 w-80 animate-pulse rounded bg-muted" />
        <div class="h-24 w-full animate-pulse rounded bg-muted mt-4" />
      </div>
      <div class="rounded-xl border border-border bg-card p-6 space-y-4 mt-6">
        <div class="h-5 w-36 animate-pulse rounded bg-muted" />
        <div class="h-3 w-72 animate-pulse rounded bg-muted" />
        <div class="space-y-3 mt-4">
          <div v-for="n in 3" :key="n" class="h-12 w-full animate-pulse rounded bg-muted" />
        </div>
      </div>
      <div class="rounded-xl border border-border bg-card p-6 space-y-4 mt-6">
        <div class="h-5 w-36 animate-pulse rounded bg-muted" />
        <div class="h-3 w-64 animate-pulse rounded bg-muted" />
        <div class="h-32 w-full animate-pulse rounded bg-muted mt-4" />
      </div>
      <div class="rounded-xl border border-border bg-card p-6 space-y-4 mt-6">
        <div class="h-5 w-28 animate-pulse rounded bg-muted" />
        <div class="h-3 w-56 animate-pulse rounded bg-muted" />
        <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3 mt-4">
          <div v-for="n in 6" :key="n" class="h-20 w-full animate-pulse rounded bg-muted" />
        </div>
      </div>
    </template>
    <template v-else>
      <!-- Configured Providers -->
      <SectionCard
        :title="$t('views.AdminRemyView.configured_providers')"
        :description="$t('views.AdminRemyView.api_keys_configured_for_each_llm_provider_remy_will_use_thes')"
        data-testid="remy-providers"
      >
        <div v-if="providersLoading" class="py-4 text-center text-sm text-muted-foreground">
          {{ $t('views.AdminRemyView.providers_loading') }}
        </div>
        <div v-else class="grid grid-cols-2 gap-3 sm:grid-cols-3 md:grid-cols-5">
          <span
                v-for="p in providerStatus"
                :key="p.id"
                class="flex flex-col items-center gap-2 rounded-lg border p-4 text-center transition-all cursor-help"
                :class="p.configured ? 'border-success/40 bg-success/5' : 'border-muted bg-muted/20 opacity-60'"
                v-tooltip.top="providerTooltip(p)">
                <span
                  class="flex h-10 w-10 items-center justify-center rounded-full text-lg font-bold"
                  :class="p.configured ? 'bg-success/20 text-success' : 'bg-muted text-muted-foreground'"
                >
                  <Check v-if="p.configured" aria-hidden="true" class="h-[18px] w-[18px]" stroke-width="3" />
                  <X v-else aria-hidden="true" class="h-[18px] w-[18px]" stroke-width="3" />
                </span>
                <span class="text-sm font-medium">{{ p.label }}</span>
                <span class="text-xs" :class="p.configured ? 'text-success' : 'text-muted-foreground'">
                  {{ p.configured ? $t('views.AdminRemyView.configured') : $t('views.AdminRemyView.not_set') }}
                </span>
              </span>
        </div>
        <div class="mt-3 text-xs text-muted-foreground">
          <router-link :to="{ name: 'admin-model-backends' }" class="underline hover:text-foreground" v-tooltip.top="tooltipAddEditRemoveApiKeys">{{ $t('views.AdminRemyView.manage_model_backends') }}</router-link>
        </div>
      </SectionCard>
      <!-- Custom Backends -->
      <SectionCard
        :title="$t('views.AdminRemyView.custom_backends')"
        :description="$t('views.AdminRemyView.custom_backends_description')"
        data-testid="remy-custom-backends"
      >
        <div v-if="providersLoading" class="py-4 text-center text-sm text-muted-foreground">
          {{ $t('views.AdminRemyView.loading') }}
        </div>
        <div v-else class="space-y-2">
          <div
            v-for="p in customProviderStatus"
            :key="p.id"
            class="flex items-center justify-between rounded-lg border px-4 py-3"
            :class="p.configured ? 'border-success/40 bg-success/5' : 'border-muted bg-muted/20'"
          >
            <div class="flex items-center gap-3">
              <span
                class="flex h-8 w-8 items-center justify-center rounded-full text-sm font-bold"
                :class="p.configured ? 'bg-success/20 text-success' : 'bg-muted text-muted-foreground'"
              >
                  <Check v-if="p.configured" aria-hidden="true" class="h-[18px] w-[18px]" stroke-width="3" />
                  <X v-else aria-hidden="true" class="h-[18px] w-[18px]" stroke-width="3" />
              </span>
              <span class="text-sm font-medium">{{ p.label }}</span>
            </div>
            <span class="text-xs" :class="p.configured ? 'text-success' : 'text-muted-foreground'">
              {{ p.configured ? $t('views.AdminRemyView.configured') : $t('views.AdminRemyView.not_set') }}
            </span>
          </div>
          <div v-if="customProviderStatus.length === 0" class="py-4 text-center text-sm text-muted-foreground">
            {{ $t('views.AdminRemyView.no_custom_backends_configured') }}
          </div>
          <div class="mt-3 text-xs text-muted-foreground">
            <router-link :to="{ name: 'admin-model-backends' }" class="underline hover:text-foreground">
              {{ $t('views.AdminRemyView.manage_all_backends_in_model_backends') }}
            </router-link>
          </div>
        </div>
      </SectionCard>
      <!-- Access List -->
      <SectionCard
        :title="$t('views.AdminRemyView.access_list')"
        :description="$t('views.AdminRemyView.control_who_can_use_remy_within_the_organisation')"
      >
        <div class="space-y-6">
          <!-- Users -->
          <div>
            <span class="mb-2 block text-sm font-medium">{{ $t('views.AdminRemyView.users') }}</span>
            <AccessEntitySelector
              v-model="accessList.userIds"
              :entities="users"
              label-field="display_name"
              description-field="email"
              :placeholder="$t('views.AdminRemyView.search_users_placeholder')"
              :no-results-text="$t('views.AdminRemyView.no_users_found')"
              :empty-text="$t('views.AdminRemyView.no_users_selected')"
              test-id="remy-access-users"
            />
          </div>
          <!-- Teams -->
          <div>
            <span class="mb-2 block text-sm font-medium">{{ $t('views.AdminRemyView.teams') }}</span>
            <AccessEntitySelector
              v-model="accessList.teamIds"
              :entities="teams"
              label-field="name"
              description-field="member_count_label"
              :placeholder="$t('views.AdminRemyView.search_teams_placeholder')"
              :no-results-text="$t('views.AdminRemyView.no_teams_found')"
              :empty-text="$t('views.AdminRemyView.no_teams_selected')"
              test-id="remy-access-teams"
            />
          </div>
          <!-- Org roles -->
          <div>
            <span class="mb-1 block text-sm font-medium cursor-help" v-tooltip.right="tooltipUsersWithSelectedRoles">{{ $t('views.AdminRemyView.org_roles') }}</span>
            <div class="flex flex-wrap gap-4">
              <label
                  v-for="role in orgRoles"
                  :key="role"
                  for="adminremyview-field-13"
                  class="flex items-center gap-2 text-sm cursor-pointer"
                  v-tooltip.top="role === 'admin' ? $t('views.AdminRemyView.full_access_to_all_settings_and_remy_configuration') : role === 'operator' ? $t('views.AdminRemyView.can_create_and_manage_pipelines_use_remy') : role === 'runner' ? $t('views.AdminRemyView.can_execute_pipeline_runs_use_remy') : $t('views.AdminRemyView.readonly_access_can_view_but_not_edit_use_remy')"
                >
                    <input id="adminremyview-field-13"
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
          <Button
                  :disabled="accessSaving"
                  data-testid="remy-access-save"
                  @click="saveAccessList"
                 v-tooltip.top="tooltipSaveCurrentAccessList">
                  {{ accessSaving ? $t('views.AdminRemyView.saving') : $t('views.AdminRemyView.save_access_list') }}
                </Button>
        </div>
      </SectionCard>
      <!-- Default Model Configuration -->
      <SectionCard
        :title="$t('views.AdminRemyView.default_model_configuration')"
        :description="$t('views.AdminRemyView.set_the_default_model_and_allowed_providers_for_remy')"
      >
        <div class="space-y-4">
          <div>
            <label for="adminremyview-field-12" class="mb-1 block text-sm font-medium">{{ $t('views.AdminRemyView.default_provider') }}</label>
            <Select
                :aria-label="$t('views.AdminRemyView.default_provider')"
                v-model="modelConfig.defaultProvider"
                data-testid="remy-model-provider"
                class="w-full"
                :options="availableProviders.native.map((p) => ({ value: p.id, label: p.label }))"
                option-label="label"
                option-value="value"
                :placeholder="$t('views.AdminRemyView.select_provider')"
              />
          </div>
          <div>
            <label for="adminremyview-field-11" class="mb-1 block text-sm font-medium">{{ $t('views.AdminRemyView.default_model') }}</label>
            <input id="adminremyview-field-11"
              v-model="modelConfig.defaultModel"
              type="text"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
              :placeholder="$t('views.AdminRemyView.model_name_placeholder')"
              data-testid="remy-model-name"
            />
          </div>
          <div>
            <label for="adminremyview-field-10" class="mb-1 block text-sm font-medium">{{ $t('views.AdminRemyView.default_context_window_size') }}</label>
            <input id="adminremyview-field-10"
              v-model.number="modelConfig.contextWindow"
              type="number"
              min="1024"
              step="1024"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
              :placeholder="$t('views.AdminRemyView.context_window_placeholder')"
              data-testid="remy-model-context"
            />
          </div>
          <div>
            <span class="mb-1 block text-sm font-medium">{{ $t('views.AdminRemyView.allowed_providers') }}</span>
            <div class="flex flex-wrap gap-2" data-testid="remy-allowed-providers">
              <button
                v-for="provider in allProviders"
                :key="provider"
                type="button"
                class="rounded-full px-3 py-1 text-xs font-medium transition-all"
                :class="modelConfig.allowedProviders.includes(provider) ? 'bg-primary text-primary-foreground' : 'bg-muted text-muted-foreground hover:bg-muted/80'"
                :data-testid="'remy-allowed-provider-' + provider"
                :aria-pressed="modelConfig.allowedProviders.includes(provider)"
                @click="toggleAllowedProvider(provider)"
              >
                {{ provider }}
              </button>
            </div>
          </div>
          <div>
            <label for="adminremyview-field-9" class="mb-1 block text-sm font-medium">{{ $t('views.AdminRemyView.allowed_models') }}</label>
            <input id="adminremyview-field-9"
              v-model="modelConfig.allowedModels"
              type="text"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
              :placeholder="$t('views.AdminRemyView.claudesonnet420250514_gpt4o_gemini25pro')"
              data-testid="remy-allowed-models"
            />
          </div>
            <div v-if="modelError" class="text-sm text-destructive">{{ modelError }}</div>
            <Button
                  :disabled="modelSaving"
                  data-testid="remy-model-save"
                  @click="saveModelConfig"
                 v-tooltip.top="tooltipSaveDefaultModelProvider">
                  {{ modelSaving ? $t('views.AdminRemyView.saving') : $t('views.AdminRemyView.save_model_config') }}
                </Button>
        </div>
      </SectionCard>
      <!-- System Prompt -->
      <SectionCard
        :title="$t('views.AdminRemyView.system_prompt')"
        :description="$t('views.AdminRemyView.base_system_prompt_that_guides_remys_behaviour')"
      >
        <div class="space-y-4">
          <div>
            <textarea
              v-model="systemPrompt"
              rows="8"
              :aria-label="$t('views.AdminRemyView.system_prompt')"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 font-mono text-sm"
              :placeholder="$t('views.AdminRemyView.you_are_a_helpful_ai_assistant')"
              data-testid="remy-system-prompt"
            />
          </div>
            <div v-if="promptError" class="text-sm text-destructive">{{ promptError }}</div>
            <Button
                  :disabled="promptSaving"
                  data-testid="remy-prompt-save"
                  @click="saveSystemPrompt"
                  v-tooltip.top="tooltipSaveBaseSystemPrompt">
                  {{ promptSaving ? $t('views.AdminRemyView.saving') : $t('views.AdminRemyView.save_system_prompt') }}
                </Button>
        </div>
      </SectionCard>
      <!-- Additional Guidance -->
      <SectionCard
        :title="$t('views.AdminRemyView.additional_guidance')"
        :description="$t('views.AdminRemyView.extra_instructions_to_append_to_the_system_prompt')"
      >
        <div class="space-y-4">
          <div>
            <textarea
              v-model="guidance"
              rows="5"
              :aria-label="$t('views.AdminRemyView.additional_guidance')"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 font-mono text-sm"
              :placeholder="$t('views.AdminRemyView.additional_instructions')"
              data-testid="remy-guidance"
            />
          </div>
            <div v-if="guidanceError" class="text-sm text-destructive">{{ guidanceError }}</div>
            <Button
                  :disabled="guidanceSaving"
                  data-testid="remy-guidance-save"
                  @click="saveGuidance"
                 v-tooltip.top="tooltipSaveExtraInstructions">
                  {{ guidanceSaving ? $t('views.AdminRemyView.saving') : $t('views.AdminRemyView.save_guidance') }}
                </Button>
        </div>
      </SectionCard>
      <!-- Tool Permissions -->
      <SectionCard :title="$t('views.AdminRemyView.tool_permissions')" :description="$t('views.AdminRemyView.tool_permissions_description')" data-testid="remy-tool-permissions">
        <div class="mb-6">
          <span class="mb-1 block text-sm font-medium">{{ $t('views.AdminRemyView.permission_mode') }}</span>
          <Select
            :aria-label="$t('views.AdminRemyView.permission_mode')"
            v-model="toolPermMode"
            data-testid="remy-tool-perm-mode"
            class="w-full"
            :options="[
              { value: 'safe', label: $t('views.AdminRemyView.mode_safe') },
              { value: 'full_auto', label: $t('views.AdminRemyView.mode_full_auto') },
              { value: 'locked_down', label: $t('views.AdminRemyView.mode_locked_down') },
              { value: 'custom', label: $t('views.AdminRemyView.mode_custom') },
            ]"
            option-label="label"
            option-value="value"
            :placeholder="$t('views.AdminRemyView.select_mode')"
            @update:model-value="applyModePreset"
          />
        </div>
        <div class="table-wrapper">
          <table class="w-full text-left text-sm">
            <thead>
              <tr>
                <th class="table-header">{{ $t('views.AdminRemyView.tool') }}</th>
                <th class="table-header">{{ $t('views.AdminRemyView.description') }}</th>
                <th class="table-header">{{ $t('views.AdminRemyView.permission') }}</th>
              </tr>
            </thead>
            <tbody class="divide-y">
              <tr v-for="(info, toolName) in uiTools" :key="toolName">
                <td class="table-cell font-mono text-xs">{{ toolName }}</td>
                <td class="table-cell text-muted-foreground text-xs">{{ $t(info.descKey) }}</td>
                <td class="table-cell">
                  <Select
                    :aria-label="$t('views.AdminRemyView.tool_permission')"
                    v-model="toolPerms[toolName]"
                    :disabled="toolPermMode !== 'custom'"
                    class="rounded border border-input bg-background px-2 py-1 text-xs"
                    :options="[
                      { value: 'always_allowed', label: $t('views.AdminRemyView.auto_allow') },
                      { value: 'requires_approval', label: $t('views.AdminRemyView.requires_approval') },
                      { value: 'disabled', label: $t('views.AdminRemyView.disabled') },
                    ]"
                    option-label="label"
                    option-value="value"
                    :placeholder="$t('views.AdminRemyView.select')"
                  />
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <div v-if="toolPermError" class="mt-2 text-sm text-destructive">{{ toolPermError }}</div>
        <Button :disabled="toolPermSaving" class="mt-4" data-testid="remy-tool-perms-save" @click="saveToolPerms">
          {{ toolPermSaving ? $t('views.AdminRemyView.saving') : $t('views.AdminRemyView.save_tool_permissions') }}
        </Button>
      </SectionCard>
      <!-- Safety & Limits -->
      <SectionCard :title="$t('views.AdminRemyView.safety_and_limits')" :description="$t('views.AdminRemyView.safety_and_limits_description')" data-testid="remy-safety-limits">
        <div class="space-y-4">
          <div>
            <label for="adminremyview-field-7" class="mb-1 block text-sm font-medium">{{ $t('views.AdminRemyView.max_actions_per_minute') }}</label>
            <p class="mb-2 text-xs text-muted-foreground">{{ $t('views.AdminRemyView.maximum_number_of_ui_actions_remy_can_perform_in_a_one_minute_window') }}</p>
            <input id="adminremyview-field-7"
              v-model.number="safetyConfig.rateLimitMaxActions"
              type="number"
              min="1"
              max="120"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
              :placeholder="$t('views.AdminRemyView.rate_limit_max_actions_placeholder')"
              data-testid="remy-rate-limit-max-actions"
            />
          </div>
          <div>
            <label for="adminremyview-field-6" class="mb-1 block text-sm font-medium">{{ $t('views.AdminRemyView.rate_limit_window_seconds') }}</label>
            <p class="mb-2 text-xs text-muted-foreground">{{ $t('views.AdminRemyView.the_sliding_window_duration_for_rate_limit_calculations') }}</p>
            <input id="adminremyview-field-6"
              v-model.number="safetyConfig.rateLimitWindowSeconds"
              type="number"
              min="1"
              max="3600"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
              :placeholder="$t('views.AdminRemyView.rate_limit_window_seconds_placeholder')"
              data-testid="remy-rate-limit-window"
            />
          </div>
          <div>
            <label for="adminremyview-field-5" class="mb-1 block text-sm font-medium">{{ $t('views.AdminRemyView.auto_execute_confidence_threshold') }}</label>
            <p class="mb-2 text-xs text-muted-foreground">{{ $t('views.AdminRemyView.auto_execute_threshold_description') }}</p>
            <div class="flex items-center gap-3">
              <input id="adminremyview-field-5"
                v-model.number="safetyConfig.autoExecuteThreshold"
                type="range"
                min="0"
                max="1"
                step="0.05"
                class="flex-1"
                data-testid="remy-auto-execute-threshold"
                :aria-label="$t('views.AdminRemyView.auto_execute_confidence_threshold')"
              />
              <span class="text-sm font-mono w-12 text-right">{{ safetyConfig.autoExecuteThreshold.toFixed(2) }}</span>
            </div>
          </div>
          <div>
            <label for="adminremyview-field-4" class="mb-1 block text-sm font-medium">{{ $t('views.AdminRemyView.no_go_page_patterns') }}</label>
            <p class="mb-2 text-xs text-muted-foreground">{{ $t('views.AdminRemyView.url_patterns_comma_separated_that_remy_must_never_navigate_to_supports_wildcards') }}</p>
            <textarea id="adminremyview-field-4"
              v-model="safetyConfig.nogoPagePatterns"
              rows="2"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 font-mono text-sm"
              :placeholder="$t('views.AdminRemyView.nogo_page_patterns_placeholder')"
              data-testid="remy-nogo-page-patterns"
            />
          </div>
          <div>
            <label for="adminremyview-field-3" class="mb-1 block text-sm font-medium">{{ $t('views.AdminRemyView.no_go_selector_patterns') }}</label>
            <p class="mb-2 text-xs text-muted-foreground">{{ $t('views.AdminRemyView.css_selector_patterns_comma_separated_for_elements_remy_must_never_interact_with') }}</p>
            <textarea id="adminremyview-field-3"
              v-model="safetyConfig.nogoSelectorPatterns"
              rows="2"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 font-mono text-sm"
              :placeholder="$t('views.AdminRemyView.nogo_selector_patterns_placeholder')"
              data-testid="remy-nogo-selector-patterns"
            />
          </div>
          <div>
            <label for="adminremyview-field-2" class="mb-1 block text-sm font-medium">{{ $t('views.AdminRemyView.allowed_css_selectors') }}</label>
            <p class="mb-2 text-xs text-muted-foreground">{{ $t('views.AdminRemyView.when_set_remy_can_only_interact_with_elements_matching_these_css_selectors_or_data_testid_prefixes_leave_empty_to_allow_all') }}</p>
            <textarea id="adminremyview-field-2"
              v-model="safetyConfig.allowedSelectors"
              rows="2"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 font-mono text-sm"
              :placeholder="$t('views.AdminRemyView.allowed_selectors_placeholder')"
              data-testid="remy-allowed-selectors"
            />
          </div>
          <div>
            <label for="adminremyview-field-1" class="mb-1 block text-sm font-medium">{{ $t('views.AdminRemyView.allowed_page_url_patterns') }}</label>
            <p class="mb-2 text-xs text-muted-foreground">{{ $t('views.AdminRemyView.when_set_remy_can_only_navigate_to_pages_matching_these_url_patterns_leave_empty_to_allow_all') }}</p>
            <textarea id="adminremyview-field-1"
              v-model="safetyConfig.allowedPagePatterns"
              rows="2"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 font-mono text-sm"
              :placeholder="$t('views.AdminRemyView.allowed_page_url_patterns_placeholder')"
              data-testid="remy-allowed-page-patterns"
            />
          </div>
          <div v-if="safetyError" class="text-sm text-destructive">{{ safetyError }}</div>
          <Button
                :disabled="safetySaving"
                data-testid="remy-safety-save"
                @click="saveSafetyConfig"
               v-tooltip.top="tooltipSaveRateLimits">
                {{ safetySaving ? $t('views.AdminRemyView.saving') : $t('views.AdminRemyView.save_safety_config') }}
              </Button>
        </div>
      </SectionCard>
      <!-- Skills Manager -->
      <SectionCard :title="$t('views.AdminRemyView.skills')" :description="$t('views.AdminRemyView.organisationlevel_skills_that_remy_can_use')" data-testid="remy-skills">
        <template #header>
          <Button
            class="border border-primary/30"
            data-testid="remy-skills-add"
            @click="skillDialogRef?.openCreate()"
          >
            {{ $t('views.AdminRemyView.add_skill') }}
          </Button>
        </template>
        <div v-if="skills.length === 0" class="py-8 text-center">
          <p class="text-sm text-muted-foreground">{{ $t('views.AdminRemyView.no_skills_configured_yet') }}</p>
        </div>
        <div v-else class="table-wrapper">
          <table class="w-full text-left text-sm">
            <thead>
              <tr>
                <th class="table-header">{{ $t('views.AdminRemyView.name') }}</th>
                <th class="table-header">{{ $t('views.AdminRemyView.description') }}</th>
                <th class="table-header">{{ $t('views.AdminRemyView.triggers') }}</th>
                <th class="table-header">{{ $t('views.AdminRemyView.active') }}</th>
                <th class="table-header table-cell-numeric">{{ $t('views.AdminRemyView.actions') }}</th>
              </tr>
            </thead>
            <tbody class="divide-y">
              <tr
                v-for="skill in skills"
                :key="skill.id"
                class="hover:bg-muted/30 transition-colors"
              >
                <td class="table-cell font-medium">{{ skill.name }}</td>
                <td class="table-cell text-muted-foreground max-w-xs truncate">
                  <span v-tooltip.top="skill.description || '-'">{{ skill.description || '-' }}</span>
                </td>
                <td class="table-cell">
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
                <td class="table-cell">
                  <button type="button"
                    class="inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium transition-colors disabled:opacity-50"
                    :class="skill.active ? 'bg-success/10 text-success' : 'bg-muted text-muted-foreground'"
                    :disabled="skillToggling[skill.id]"
                    data-testid="remy-skill-toggle"
                    :aria-pressed="skill.active"
                    @click="toggleSkillActive(skill)"
                  >
                    <span
                      class="h-1.5 w-1.5 rounded-full"
                      :class="skill.active ? 'bg-success' : 'bg-muted-foreground'"
                    />
                    {{ skillToggling[skill.id] ? '...' : (skill.active ? $t('views.AdminRemyView.active') : $t('views.AdminRemyView.inactive')) }}
                  </button>
                </td>
                <td class="table-cell-numeric">
                  <div class="flex items-center justify-end gap-1">
                    <button type="button"
                      class="rounded p-1 text-muted-foreground hover:bg-accent"
                      :aria-label="$t('views.AdminRemyView.edit_skill')"
                      :title="$t('views.AdminRemyView.edit_skill')"
                      data-testid="remy-skill-edit"
                      @click="skillDialogRef?.openEdit(skill)"
                    >
                      <Pencil class="h-4 w-4" />
                    </button>
                    <button type="button"
                      class="rounded p-1 text-destructive hover:bg-destructive/10"
                      :aria-label="$t('views.AdminRemyView.delete_skill')"
                      :title="$t('components.remy.RemySkillDialog.delete_skill')"
                      data-testid="remy-skill-delete"
                      @click="skillDialogRef?.openDelete(skill)"
                    >
                      <Trash2 class="h-4 w-4" />
                    </button>
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <div v-if="skillError" class="px-3 pt-2 text-sm text-destructive">{{ skillError }}</div>
      </SectionCard>
      <RemySkillDialog
        ref="skillDialogRef"
        :create-description="$t('views.AdminRemyView.create_skill_description')"
        :edit-description="$t('views.AdminRemyView.edit_skill_description')"
        @saved="loadSkills"
      />
      <!-- Knowledge Sources -->
      <SectionCard
        :title="$t('views.AdminRemyView.knowledge_sources')"
        :description="$t('views.AdminRemyView.control_what_remy_knows')"
      >
        <div v-if="contextLoading" class="py-4 text-center text-sm text-muted-foreground">{{ $t('views.AdminRemyView.loading_sources') }}</div>
        <div v-else class="table-wrapper">
          <table class="w-full text-left text-sm">
            <thead>
              <tr>
                <th class="table-header">{{ $t('views.AdminRemyView.source') }}</th>
                <th class="table-header">{{ $t('views.AdminRemyView.mode') }}</th>
                <th class="table-header table-cell-numeric">{{ $t('views.AdminRemyView.tokens') }}</th>
              </tr>
            </thead>
            <tbody class="divide-y">
              <tr v-for="src in contextSourceDefs" :key="src.key" class="hover:bg-muted/30 transition-colors">
                <td class="table-cell">
                  <span class="font-medium cursor-help" v-tooltip.top="$t(src.labelKey)">{{ $t(src.labelKey) }}</span>
                </td>
                <td class="table-cell">
                  <Select
                    :aria-label="$t('views.AdminRemyView.context_source_mode')"
                    v-model="contextSources[src.key]"
                    :disabled="contextSaving"
                    class="rounded border border-input bg-background px-2 py-1 text-xs"
                    :options="[
                      { value: 'always_on', label: $t('views.AdminRemyView.always_on') },
                      { value: 'tool', label: $t('views.AdminRemyView.tool') },
                      { value: 'off', label: $t('views.AdminRemyView.off') },
                    ]"
                    option-label="label"
                    option-value="value"
                    :placeholder="$t('views.AdminRemyView.select')"
                    @update:model-value="saveContextSource(src.key)"
                  />
                </td>
                <td class="table-cell table-cell-numeric text-xs text-muted-foreground">
                  <span v-if="src.tokens">{{ src.tokens }}</span>
                  <code v-else-if="src.toolCall" class="text-[10px]">{{ src.toolCall }}</code>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <div v-if="contextError" class="mt-2 text-sm text-destructive">{{ contextError }}</div>
      </SectionCard>
      <!-- Skills as Knowledge Sources -->
      <SectionCard
        v-if="skills.length > 0"
        :title="$t('views.AdminRemyView.skills_as_knowledge')"
        :description="$t('views.AdminRemyView.control_what_remy_knows')"
      >
        <div class="table-wrapper">
          <table class="w-full text-left text-sm">
            <thead>
              <tr>
                <th class="table-header">{{ $t('views.AdminRemyView.skill_name') }}</th>
                <th class="table-header">{{ $t('views.AdminRemyView.mode') }}</th>
              </tr>
            </thead>
            <tbody class="divide-y">
              <tr v-for="skill in skills" :key="skill.id" class="hover:bg-muted/30 transition-colors">
                <td class="table-cell font-medium">{{ skill.name }}</td>
                <td class="table-cell">
                  <Select
                    :aria-label="$t('views.AdminRemyView.skill_knowledge_mode')"
                    v-model="skillModes[skill.id]"
                    :disabled="skillModeSaving[skill.id]"
                    class="rounded border border-input bg-background px-2 py-1 text-xs"
                    :options="[
                      { value: 'always_on', label: $t('views.AdminRemyView.always_on') },
                      { value: 'tool', label: $t('views.AdminRemyView.tool') },
                      { value: 'off', label: $t('views.AdminRemyView.off') },
                    ]"
                    option-label="label"
                    option-value="value"
                    :placeholder="$t('views.AdminRemyView.select')"
                    @update:model-value="saveSkillSourceMode(skill)"
                  />
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </SectionCard>
      <!-- Product Primer -->
      <SectionCard
        :title="$t('views.AdminRemyView.product_primer')"
        :description="$t('views.AdminRemyView.product_primer_description')"
      >
        <div class="flex items-center gap-3">
          <Button
            :disabled="primerSaving"
            data-testid="remy-primer-regenerate"
            @click="regeneratePrimer"
          >
            {{ primerSaving ? $t('views.AdminRemyView.regenerating') : $t('views.AdminRemyView.regenerate_primer') }}
          </Button>
          <span v-if="primerMessage" class="text-sm text-success">{{ primerMessage }}</span>
        </div>
      </SectionCard>
    </template>
  </div>
  </FeatureGate>
</template>
<script setup lang="ts">
import PageHeader from '../components/shared/PageHeader.vue'
import SectionCard from '../components/shared/SectionCard.vue'
import { ref, reactive, computed, watch, onUnmounted } from 'vue'
import { Check, X, Pencil, Trash2 } from '@lucide/vue'
import { api } from '../lib/api/client'
import { useDataFetch } from '../composables/useDataFetch'
import { formatApiError } from '../lib/api/formatError'
import FeatureGate from '../components/FeatureGate.vue'
import RemySkillDialog from '../components/remy/RemySkillDialog.vue'
import AccessEntitySelector from '../components/remy/AccessEntitySelector.vue'
import Button from 'primevue/button'
import Select from 'primevue/select'
import type { SkillItem } from '../types/remy'
import { useI18n } from 'vue-i18n'

interface ProviderStatus {
  id: string
  label: string
  configured: boolean
}

const { t } = useI18n()

const tooltipAddOneInModelBackends = computed(() => t('views.AdminRemyView.add_one_in_model_backends'))
const tooltipAddEditRemoveApiKeys = computed(() => t('views.AdminRemyView.add_edit_or_remove_api_keys_for_llm_providers'))
const tooltipUsersWithSelectedRoles = computed(() => t('views.AdminRemyView.users_with_the_selected_organisation_roles_will_have_access'))
const tooltipSaveCurrentAccessList = computed(() => t('views.AdminRemyView.save_the_current_access_list_configuration'))

function providerTooltip(p: ProviderStatus): string {
  const base = p.configured
    ? `${t('views.AdminRemyView.api_key_configured_remy_can_route_to')} ${p.label}`
    : `${t('views.AdminRemyView.no_api_key_set_remy_will_skip')} ${p.label}`
  return p.configured ? base : `${base}. ${tooltipAddOneInModelBackends.value}`
}

const tooltipSaveBaseSystemPrompt = computed(() => t('views.AdminRemyView.save_the_base_system_prompt_that_guides_remys_behaviour'))
const tooltipSaveDefaultModelProvider = computed(() => t('views.AdminRemyView.save_the_default_model_provider_and_allowed_model_configuration'))
const tooltipSaveExtraInstructions = computed(() => t('views.AdminRemyView.save_extra_instructions_appended_to_the_system_prompt'))
const tooltipSaveRateLimits = computed(() => t('views.AdminRemyView.save_rate_limits_threshold_no_go_patterns_and_allowlist'))

const configSaving = ref(false)

let primerTimer: ReturnType<typeof setTimeout> | null = null

interface ProviderInfo {
  id: string
  label: string
}

const availableProviders = ref<{ native: ProviderInfo[]; customTypes: ProviderInfo[] }>({
  native: [],
  customTypes: [],
})

const { data: configData, loading: configLoading, load: loadConfig } = useDataFetch(
  () => api.GET('/api/v1/admin/remy/config'),
  { immediate: false }
)

watch(() => configData.value, (cfg) => {
  if (!cfg) return
  const c = cfg as any
  const acl = c.access_list || {}
  accessList.userIds = acl.user_ids || []
  accessList.teamIds = acl.team_ids || []
  accessList.selectedRoles = acl.org_roles || ['admin']
  modelConfig.defaultProvider = c.default_provider || 'anthropic'
  modelConfig.defaultModel = c.default_model || ''
  modelConfig.contextWindow = c.default_context_window ?? 200000
  modelConfig.allowedProviders = c.allowed_providers || ['anthropic']
  modelConfig.allowedModels = (c.allowed_models || []).join(', ')
  systemPrompt.value = c.system_prompt || ''
  guidance.value = c.additional_guidance || ''
  loadPermsFromConfig(c)
  loadSafetyFromConfig(c)
})

const contextSourceDefs = [
  { key: 'product_primer', labelKey: 'views.AdminRemyView.context_source_primer_label', descKey: 'product_primer_description', tokens: '~700' },
  { key: 'page_context', labelKey: 'views.AdminRemyView.context_source_page_label', descKey: 'page_context_description', tokens: '~100' },
  { key: 'user_profile', labelKey: 'views.AdminRemyView.context_source_user_profile_label', descKey: 'user_profile_description', tokens: '~150' },
  { key: 'product_docs', labelKey: 'views.AdminRemyView.context_source_product_docs_label', descKey: 'product_docs_description', toolCall: 'search_documentation()' },
  { key: 'integration_status', labelKey: 'views.AdminRemyView.context_source_integration_status_label', descKey: 'integration_status_description', toolCall: 'get_integration_status()' },
  { key: 'org_config', labelKey: 'views.AdminRemyView.context_source_org_config_label', descKey: 'org_config_description', toolCall: 'get_org_config()' },
  { key: 'feature_overview', labelKey: 'views.AdminRemyView.context_source_feature_overview_label', descKey: 'feature_overview_description', toolCall: 'get_available_features()' },
]

const contextSources = ref<Record<string, string>>({})
const contextLoading = ref(true)
const contextSaving = ref(false)
const contextError = ref<string | null>(null)

const { data: contextSourcesData, loading: contextSourcesLoading, load: loadContextSources } = useDataFetch(
  () => api.GET('/api/v1/admin/remy/context-sources'),
  { immediate: false }
)

watch(() => contextSourcesData.value, (data) => {
  if (data) {
    const modes = { ...(data as Record<string, string>) }
    for (const src of contextSourceDefs) {
      if (!modes[src.key]) {
        modes[src.key] = 'always_on'
      }
    }
    contextSources.value = modes
  }
  contextLoading.value = false
})

const skillModes = ref<Record<string, string>>({})
const skillModeSaving = ref<Record<string, boolean>>({})
const primerSaving = ref(false)
const primerMessage = ref<string | null>(null)

const providerStatus = ref<ProviderStatus[]>([])
const customProviderStatus = ref<ProviderStatus[]>([])
const providersLoading = ref(true)

const orgRoles = ['admin', 'operator', 'runner', 'viewer']

// Access list
const accessList = reactive({
  userIds: [] as string[],
  teamIds: [] as string[],
  selectedRoles: ['admin'] as string[],
})

const users = ref<Array<{ id: string; display_name: string; email: string }>>([])
const teams = ref<Array<{ id: string; name: string; member_count?: number; member_count_label?: string }>>([])
const accessSaving = ref(false)
const accessError = ref<string | null>(null)

function toggleInArray(arr: string[], item: string) {
  const idx = arr.indexOf(item)
  if (idx >= 0) { arr.splice(idx, 1) } else { arr.push(item) }
}

function toggleRole(role: string) {
  toggleInArray(accessList.selectedRoles, role)
}

async function putConfig(body: Record<string, unknown>): Promise<string | null> {
  try {
    const { error: err } = await (api.PUT as (...args: unknown[]) => Promise<{ error?: unknown }>)('/api/v1/admin/remy/config', { body })
    return err ? formatApiError(err) : null
  } catch (e: unknown) {
    return formatApiError(e)
  }
}

async function saveAccessList() {
  if (configSaving.value) return
  configSaving.value = true
  accessSaving.value = true
  accessError.value = null
  const userIds = accessList.userIds.filter(Boolean)
  const teamIds = accessList.teamIds.filter(Boolean)
  const err = await putConfig({
    access_list: { user_ids: userIds, team_ids: teamIds, org_roles: accessList.selectedRoles },
  })
  if (err) accessError.value = `${t('views.AdminRemyView.failed_to_save_access_list')} ${formatApiError(err)}`
  accessSaving.value = false
  configSaving.value = false
}

// Model config
const allProviders = computed(() => (availableProviders.value.native ?? []).map(p => p.id))

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
  toggleInArray(modelConfig.allowedProviders, provider)
}

async function saveModelConfig() {
  if (configSaving.value) return
  configSaving.value = true
  modelSaving.value = true
  modelError.value = null
  const allowedModels = modelConfig.allowedModels.split(/[\s,]+/).map(s => s.trim()).filter(Boolean)
  const err = await putConfig({
    default_provider: modelConfig.defaultProvider,
    default_model: modelConfig.defaultModel,
    default_context_window: modelConfig.contextWindow,
    allowed_providers: modelConfig.allowedProviders,
    allowed_models: allowedModels,
  })
  if (err) modelError.value = `${t('views.AdminRemyView.failed_to_save_model_config')} ${formatApiError(err)}`
  modelSaving.value = false
  configSaving.value = false
}

// System prompt
const systemPrompt = ref('')
const promptSaving = ref(false)
const promptError = ref<string | null>(null)

async function saveSystemPrompt() {
  if (configSaving.value) return
  configSaving.value = true
  promptSaving.value = true
  promptError.value = null
  const err = await putConfig({ system_prompt: systemPrompt.value })
  if (err) promptError.value = `${t('views.AdminRemyView.failed_to_save_system_prompt')} ${formatApiError(err)}`
  promptSaving.value = false
  configSaving.value = false
}

// Tool Permissions
const toolPermMode = ref('safe')
const toolPerms = ref<Record<string, string>>({})
const toolPermSaving = ref(false)
const toolPermError = ref<string | null>(null)

const uiTools: Record<string, { descKey: string }> = {
  navigate: { descKey: 'views.AdminRemyView.tool_navigate' },
  click: { descKey: 'views.AdminRemyView.tool_click' },
  fill: { descKey: 'views.AdminRemyView.tool_fill' },
  select: { descKey: 'views.AdminRemyView.tool_select' },
  extract: { descKey: 'views.AdminRemyView.tool_extract' },
  extract_all: { descKey: 'views.AdminRemyView.tool_extract_all' },
  get_page_interactables: { descKey: 'views.AdminRemyView.tool_get_page_interactables' },
  wait: { descKey: 'views.AdminRemyView.tool_wait' },
  go_back: { descKey: 'views.AdminRemyView.tool_go_back' },
  get_url: { descKey: 'views.AdminRemyView.tool_get_url' },
  press: { descKey: 'views.AdminRemyView.tool_press' },
}

function getDefaultPerms(): Record<string, string> {
  return {
    navigate: 'always_allowed', click: 'always_allowed', fill: 'always_allowed',
    select: 'always_allowed', extract: 'always_allowed', extract_all: 'always_allowed',
    get_page_interactables: 'always_allowed', wait: 'always_allowed',
    go_back: 'always_allowed', get_url: 'always_allowed', press: 'requires_approval',
  }
}

function applyModePreset() {
  if (toolPermMode.value === 'custom') return
  const perms = getDefaultPerms()
  if (toolPermMode.value === 'locked_down') {
    const alwaysAllowed = new Set(['navigate', 'extract', 'extract_all', 'get_page_interactables', 'wait', 'get_url'])
    for (const key of Object.keys(perms)) {
      perms[key] = alwaysAllowed.has(key) ? 'always_allowed' : 'requires_approval'
    }
  }
  toolPerms.value = { ...perms }
}

async function saveToolPerms() {
  if (configSaving.value) return
  configSaving.value = true
  toolPermSaving.value = true
  toolPermError.value = null
  const err = await putConfig({
    permission_mode: toolPermMode.value,
    tool_permissions: toolPerms.value,
  })
  if (err) toolPermError.value = `${t('views.AdminRemyView.failed_to_save')} ${formatApiError(err)}`
  toolPermSaving.value = false
  configSaving.value = false
}

function loadPermsFromConfig(cfg: any) {
  toolPermMode.value = cfg.permission_mode || 'safe'
  toolPerms.value = cfg.tool_permissions || getDefaultPerms()
}

// Safety config
const safetyConfig = reactive({
  rateLimitMaxActions: 30,
  rateLimitWindowSeconds: 60,
  autoExecuteThreshold: 0.8,
  nogoPagePatterns: '',
  nogoSelectorPatterns: '',
  allowedSelectors: '',
  allowedPagePatterns: '',
})
const safetySaving = ref(false)
const safetyError = ref<string | null>(null)

function loadSafetyFromConfig(cfg: any) {
  safetyConfig.rateLimitMaxActions = cfg.rate_limit_max_actions ?? 30
  safetyConfig.rateLimitWindowSeconds = cfg.rate_limit_window_seconds ?? 60
  safetyConfig.autoExecuteThreshold = cfg.auto_execute_threshold ?? 0.8
  safetyConfig.nogoPagePatterns = (cfg.nogo_page_patterns ?? []).join(', ')
  safetyConfig.nogoSelectorPatterns = (cfg.nogo_selector_patterns ?? []).join(', ')
  safetyConfig.allowedSelectors = (cfg.allowed_selectors ?? []).join(', ')
  safetyConfig.allowedPagePatterns = (cfg.allowed_page_patterns ?? []).join(', ')
}

async function saveSafetyConfig() {
  if (configSaving.value) return
  configSaving.value = true
  safetySaving.value = true
  safetyError.value = null
  const nogoPagePatterns = safetyConfig.nogoPagePatterns.split(/[\s,]+/).map(s => s.trim()).filter(Boolean)
  const nogoSelectorPatterns = safetyConfig.nogoSelectorPatterns.split(/[\s,]+/).map(s => s.trim()).filter(Boolean)
  const allowedSelectors = safetyConfig.allowedSelectors.split(/[\s,]+/).map(s => s.trim()).filter(Boolean)
  const allowedPagePatterns = safetyConfig.allowedPagePatterns.split(/[\s,]+/).map(s => s.trim()).filter(Boolean)
  const err = await putConfig({
    rate_limit_max_actions: safetyConfig.rateLimitMaxActions,
    rate_limit_window_seconds: safetyConfig.rateLimitWindowSeconds,
    auto_execute_threshold: safetyConfig.autoExecuteThreshold,
    nogo_page_patterns: nogoPagePatterns,
    nogo_selector_patterns: nogoSelectorPatterns,
    allowed_selectors: allowedSelectors,
    allowed_page_patterns: allowedPagePatterns,
  })
  if (err) safetyError.value = `${t('views.AdminRemyView.failed_to_save_safety_config')} ${formatApiError(err)}`
  safetySaving.value = false
  configSaving.value = false
}

// Guidance
const guidance = ref('')
const guidanceSaving = ref(false)
const guidanceError = ref<string | null>(null)

async function saveGuidance() {
  if (configSaving.value) return
  configSaving.value = true
  guidanceSaving.value = true
  guidanceError.value = null
  const err = await putConfig({ additional_guidance: guidance.value })
  if (err) guidanceError.value = `${t('views.AdminRemyView.failed_to_save_guidance')} ${formatApiError(err)}`
  guidanceSaving.value = false
  configSaving.value = false
}

async function saveContextSource(sourceKey: string) {
  if (configSaving.value) return
  configSaving.value = true
  contextSaving.value = true
  contextError.value = null
  try {
    const mode = contextSources.value[sourceKey]
    const { error: err } = await api.PUT('/api/v1/admin/remy/context-sources/{source_key}', {
      params: { path: { source_key: sourceKey } },
      body: { source_mode: mode },
    })
    if (err) {
      contextError.value = `${t('views.AdminRemyView.failed_to_save_source')} ${formatApiError(err)}`
    }
  } catch (e: unknown) {
    contextError.value = `${t('views.AdminRemyView.failed_to_save_source')} ${formatApiError(e)}`
  } finally {
    contextSaving.value = false
    configSaving.value = false
  }
}

async function saveSkillSourceMode(skill: SkillItem) {
  if (configSaving.value) return
  configSaving.value = true
  skillModeSaving.value[skill.id] = true
  try {
    const mode = skillModes.value[skill.id] || 'tool'
    const { error: err } = await api.PUT('/api/v1/admin/remy/context-sources/{source_key}', {
      params: { path: { source_key: skill.id } },
      body: { source_mode: mode },
    })
    if (err) {
      contextError.value = `${t('views.AdminRemyView.failed_to_save_skill_source_mode')} ${formatApiError(err)}`
    }
  } catch (e: unknown) {
    contextError.value = `${t('views.AdminRemyView.failed_to_save_skill_source_mode')} ${formatApiError(e)}`
  } finally {
    skillModeSaving.value[skill.id] = false
    configSaving.value = false
  }
}

async function regeneratePrimer() {
  primerSaving.value = true
  primerMessage.value = null
  try {
    const { error: err } = await (api as any).POST('/api/v1/admin/remy/primer/regenerate')
    if (err) {
      primerMessage.value = `${t('views.AdminRemyView.failed')}: ${formatApiError(err)}`
    } else {
      primerMessage.value = t('views.AdminRemyView.primer_regenerated')
    }
  } catch (e: unknown) {
    primerMessage.value = `${t('views.AdminRemyView.failed')}: ${formatApiError(e)}`
  } finally {
    primerSaving.value = false
    if (primerTimer) clearTimeout(primerTimer)
    primerTimer = setTimeout(() => { primerMessage.value = null }, 4000)
  }
}

const { data: usersResp, loading: usersLoading, load: loadUsers } = useDataFetch(
  () => api.GET('/api/v1/admin/users', { params: { query: { page_size: 1000 } } }),
  { immediate: false }
)

watch(() => usersResp.value, (data) => {
  if (data) {
    const raw = data as { items: Array<{ id: string; display_name: string; email: string }> }
    users.value = raw.items || []
    users.value.sort((a, b) => a.display_name.localeCompare(b.display_name))
  }
})

const { data: teamsResp, loading: teamsLoading, load: loadTeams } = useDataFetch(
  () => api.GET('/api/v1/admin/teams', { params: { query: { page_size: 1000 } } }),
  { immediate: false }
)

watch(() => teamsResp.value, (data) => {
  if (data) {
    const raw = data as { items: Array<{ id: string; name: string; member_count: number }> }
    const items = raw.items || []
    teams.value = items.map(team => ({
      ...team,
      member_count_label: t('views.AdminRemyView.member_count', { count: team.member_count ?? 0 }),
    }))
    teams.value.sort((a, b) => a.name.localeCompare(b.name))
  }
})

// Skills
const skills = ref<SkillItem[]>([])
const skillError = ref<string | null>(null)
const skillDialogRef = ref<InstanceType<typeof RemySkillDialog> | null>(null)
const skillToggling = ref<Record<string, boolean>>({})

async function toggleSkillActive(skill: SkillItem) {
  skillToggling.value[skill.id] = true
  skillError.value = null
  try {
    const { error: err } = await api.PUT('/api/v1/admin/remy/skills/{skill_id}', {
      params: { path: { skill_id: skill.id } },
      body: { active: !skill.active },
    })
    if (err) {
      skillError.value = `${t('views.AdminRemyView.failed_to_toggle_skill')} ${formatApiError(err)}`
      return
    }
    await loadSkills()
  } catch (e: unknown) {
    skillError.value = `${t('views.AdminRemyView.failed_to_toggle_skill')} ${formatApiError(e)}`
  } finally {
    skillToggling.value[skill.id] = false
  }
}

function initSkillModes() {
  const modes: Record<string, string> = {}
  for (const skill of skills.value) {
    modes[skill.id] = (skill as any).source_mode || 'tool'
  }
  skillModes.value = modes
}

const { data: skillsResp, loading: skillsLoading, load: loadSkills } = useDataFetch(
  () => api.GET('/api/v1/admin/remy/skills'),
  { immediate: false }
)

watch(() => skillsResp.value, (data) => {
  if (data) {
    skills.value = data
  }
})

const { data: providersResp, loading: providersRespLoading, load: loadAvailableProviders } = useDataFetch(
  () => api.GET('/api/v1/admin/remy/available-providers'),
  { immediate: false }
)

watch(() => providersResp.value, (data) => {
  if (data) {
    availableProviders.value = data as unknown as { native: ProviderInfo[]; customTypes: ProviderInfo[] }
  }
})

async function loadProviders() {
  providersLoading.value = true
  try {
    const { data, error: err } = await api.GET('/api/v1/model-backends', {
      params: { query: { page_size: 100 } },
    })
    if (err) {
      console.warn('Failed to load providers', err)
      providerStatus.value = []
      customProviderStatus.value = []
      return
    }
    const backends = (data?.items ?? []) as { provider: string; has_credentials: boolean }[]
    const configuredProviders = new Set(backends.map(b => b.provider))
    providerStatus.value = (availableProviders.value.native ?? []).map(p => ({
      ...p,
      configured: configuredProviders.has(p.id),
    }))
    customProviderStatus.value = (availableProviders.value.customTypes ?? []).map(p => ({
      ...p,
      configured: configuredProviders.has(p.id),
    }))
  } catch (e: unknown) {
    console.warn('Failed to load providers', e)
    providerStatus.value = []
    customProviderStatus.value = []
  } finally {
    providersLoading.value = false
  }
}

const loading = computed(() =>
  configLoading.value || contextSourcesLoading.value || contextLoading.value ||
  providersLoading.value || providersRespLoading.value || usersLoading.value ||
  teamsLoading.value || skillsLoading.value
)

async function loadAll() {
  await Promise.allSettled([
    loadConfig(),
    loadAvailableProviders(),
    loadUsers(),
    loadTeams(),
    loadSkills(),
    loadContextSources(),
  ])
  await loadProviders()
  initSkillModes()
}

loadAll()

onUnmounted(() => {
  if (primerTimer) clearTimeout(primerTimer)
})
</script>
