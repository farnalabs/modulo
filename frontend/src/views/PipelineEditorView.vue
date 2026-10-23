<template>
  <!-- `relative` anchors the mobile properties-panel overlay (FAR-1841);
       `editorFitStyle` pins the editor to the height actually left in the
       viewport below the app chrome (see the script block) so nothing hangs
       below the fold behind the touch-capturing canvas on mobile (FAR-1841)
       and the editor does not overflow the fold behind the breadcrumb on
       desktop (FAR-1851). The `h-[calc(100vh-3.5rem)]` class is only the
       pre-measurement fallback. -->
  <div ref="editorRootRef" data-testid="pipeline-editor-root" class="relative flex h-[calc(100vh-3.5rem)] overflow-hidden" :style="editorFitStyle">
    <div v-if="loading || historicResolving" class="flex flex-1 items-center justify-center">
      <div class="h-8 w-8 animate-spin rounded-full border-4 border-primary border-t-transparent" />
    </div>
    <!-- FAR-1812: graceful miss — an unknown/deleted version never renders a
         raw error or a dead page; it offers the route back to the latest
         editor. Sits ABOVE pageError so a deleted pipeline (whose graph GET
         404s too) resolves here rather than as a generic load failure. -->
    <div
      v-else-if="versionMissing"
      class="flex flex-1 flex-col items-center justify-center gap-3 px-6 text-center"
      data-testid="pipeline-editor-version-missing"
    >
      <p class="text-base font-semibold">{{ $t('views.PipelineEditorView.version_missing_title') }}</p>
      <p class="text-sm text-muted-foreground">{{ $t('views.PipelineEditorView.version_missing_body') }}</p>
      <button
        type="button"
        :class="btnToolbarSecondary"
        data-testid="pipeline-editor-version-missing-open-latest"
        @click="viewLatest"
      >
        {{ $t('views.PipelineEditorView.version_missing_open_latest') }}
      </button>
    </div>
    <div v-else-if="pageError" class="flex flex-1 items-center justify-center">
      <ErrorAlert :message="pageError" :on-retry="reloadPage" />
    </div>
    <template v-else>
      <!-- The content column is the editor's INTERNAL scroll container
           (FAR-1861). The root is pinned to the space left below the app
           chrome and clips (`overflow-hidden`); if that space is smaller than
           the toolbar's own min-content height (a short landscape viewport with
           the chrome stack active), the toolbar would otherwise paint below
           the pinned box and out of reach. Scrolling this column instead of
           the page keeps every control reachable while holding the
           no-page-scroll invariant for the whole band. -->
      <div class="flex min-h-0 min-w-0 flex-1 flex-col overflow-y-auto" data-testid="pipeline-editor-content">
        <!-- Docked toolbar: in-flow above the canvas so it never covers node details.
             `shrink-0`: when the content column is shorter than the wrapped
             toolbar, the toolbar must keep its min-content height (scroll the
             column to reach it) rather than being squeezed. -->
        <div class="flex shrink-0 flex-wrap items-center gap-x-2 gap-y-2 border-b bg-card px-3 py-2" data-testid="pipeline-editor-toolbar">
          <!-- Group: identity -->
          <div class="flex min-w-0 items-center gap-1.5" data-testid="pipeline-editor-toolbar-group-identity">
            <h2 class="truncate text-sm font-semibold">{{ pipeline?.name || $t('views.PipelineEditorView.pipeline_editor') }}</h2>
            <button v-if="!isViewing" type="button" :class="btnToolbarIcon" :aria-label="$t('views.PipelineEditorView.rename_pipeline')" :title="$t('views.PipelineEditorView.rename_pipeline')" data-testid="pipeline-editor-rename" @click="openRenameDialog">
              <PencilIcon class="h-3 w-3" aria-hidden="true" />
            </button>
            <span v-if="pipeline?.archived_at" class="shrink-0 rounded bg-warning/20 px-1.5 py-0.5 text-[10px] font-medium text-warning-text">{{ $t('views.PipelineEditorView.archived') }}</span>
            <span v-if="folderPath.length > 0" class="flex min-w-0 items-center gap-1 text-xs text-muted-foreground">
              <span v-for="(f, i) in folderPath" :key="f.id" class="truncate">
                <template v-if="i > 0"><span class="text-muted-foreground/50">/</span></template>
                <router-link :to="`/pipelines?folder_id=${f.id}`" class="hover:text-foreground">{{ f.name }}</router-link>
              </span>
            </span>
            <template v-if="linkedLifecycleMaps.length > 0">
              <span :class="groupDividerClass" />
              <span class="flex items-center gap-1 text-xs text-muted-foreground">
                <router-link
                  v-for="map in linkedLifecycleMaps"
                  :key="map.id"
                  :to="`/lifecycle-maps/${map.id}`"
                  class="whitespace-nowrap hover:text-foreground"
                >
                  {{ map.name }}
                </router-link>
              </span>
            </template>
            <!-- FAR-1812: version indicator chip — `v{n}` beside the pipeline
                 name/status. Reads the VIEWED version while in the historic
                 view and the current (latest) version otherwise; hides only
                 when the pipeline has no version to show yet. -->
            <PipelineVersionChip
              v-if="versionChipSnapshot"
              :snapshot="versionChipSnapshot"
              :mode="isViewing ? 'viewing' : 'latest'"
              @open-timeline="openVersionTimeline"
            />
            <!-- FAR-1895: the chip resolves through an AUXILIARY filtered
                 request. On failure the chip falls back to the substantive
                 version in the loaded catalog; if the catalog's first page has
                 no substantive version, the chip is hidden and the failure is
                 surfaced via `versionChipError` / retry. It never serves a
                 STALE version. -->
            <button
              v-if="versionChipError && !isViewing"
              type="button"
              class="shrink-0 rounded text-xs text-destructive hover:underline"
              :title="versionChipError"
              data-testid="pipeline-editor-version-chip-error"
              @click="loadCurrentVersionSnapshot()"
            >
              {{ $t('views.PipelineEditorView.version_chip_unavailable') }}
            </button>
          </div>
          <span :class="groupDividerClass" />
          <!-- Group: graph file actions -->
          <div class="flex items-center gap-1.5" data-testid="pipeline-editor-toolbar-group-file">
            <button v-if="!isViewing" type="button" :class="btnToolbarSecondary" :disabled="savingGraph" data-testid="pipeline-editor-save" @click="saveGraph">
              <LoaderCircleIcon v-if="savingGraph" class="h-3 w-3 animate-spin" aria-hidden="true" />
              {{ savingGraph ? $t('views.PipelineEditorView.saving_graph') : $t('views.PipelineEditorView.save') }}
            </button>
            <button type="button" :class="btnToolbarSecondary" data-testid="pipeline-editor-version-timeline" @click="showVersionTimeline = !showVersionTimeline">
              {{ $t('views.PipelineEditorView.versions') }}
            </button>
            <div v-if="!isViewing" class="relative" @click.stop @keydown.enter.stop @keydown.space.prevent.stop>
              <button
                ref="saveAsDropdownTriggerRef"
                type="button"
                :class="btnToolbarSecondary"
                :aria-expanded="showSaveAsDropdown"
                aria-haspopup="menu"
                data-testid="pipeline-editor-save-as-template"
                @click="toggleSaveAsDropdown()"
              >
                {{ $t('views.PipelineEditorView.save_as_template') }}
              </button>
              <div
                v-if="showSaveAsDropdown"
                :class="[
                  'fixed z-30 max-h-[calc(100vh-1rem)] overflow-y-auto overscroll-contain rounded-lg border bg-card py-1 shadow-lg',
                  SAVE_AS_DROPDOWN_METRICS.className,
                ]"
                :style="saveAsDropdownStyle"
                data-testid="pipeline-editor-save-as-dropdown"
              >
                <button
                  type="button"
                  class="flex w-full items-center gap-2 px-3 py-2 text-left text-sm hover:bg-accent"
                  data-testid="pipeline-editor-save-as-composite"
                  @click="openSaveAsComposite"
                >
                  <DownloadIcon class="h-4 w-4 text-indigo-400" aria-hidden="true" />
                  {{ $t('views.PipelineEditorView.composite') }}
                </button>
              </div>
            </div>
          </div>
          <!-- FAR-1812: in the historic (read-only) view every write affordance
               is UNMOUNTED, not disabled — Run, Save, Save-as-template, the
               whole pipeline-settings group and the node palette are absent,
               because a disabled button is one state change away from a
               data-loss path. Only read/inspect controls stay mounted. -->
          <template v-if="!isViewing">
            <span :class="groupDividerClass" />
            <!-- Group: run -->
            <div class="flex items-center gap-1.5" data-testid="pipeline-editor-toolbar-group-run">
              <button type="button" :class="btnToolbarPrimary" :disabled="running || flowNodes.length === 0" :title="flowNodes.length === 0 ? $t('views.PipelineEditorView.no_nodes_to_run') : ''" data-testid="pipeline-editor-run" @click="openRunDialog">
                <PlayIcon class="h-3 w-3" aria-hidden="true" />
                {{ running ? $t('views.PipelineEditorView.running') : $t('views.PipelineEditorView.run_pipeline') }}
              </button>
              <span v-if="saveGraphError" class="max-w-40 truncate text-xs text-destructive" :title="saveGraphError" data-testid="pipeline-editor-save-error">{{ saveGraphError }}</span>
            </div>
          </template>
          <template v-if="!isViewing">
          <span :class="groupDividerClass" />
          <!-- Group: pipeline settings -->
          <div class="flex items-center gap-1.5" data-testid="pipeline-editor-toolbar-group-settings">
            <button v-if="!pipeline?.archived_at" type="button" :class="btnToolbarSecondary" data-testid="pipeline-editor-archive" @click="handleArchive">{{ $t('views.PipelineEditorView.archive') }}</button>
            <button v-else type="button" :class="btnToolbarSecondary" data-testid="pipeline-editor-unarchive" @click="handleUnarchive">{{ $t('views.PipelineEditorView.unarchive') }}</button>
            <!-- FAR-1530: per-pipeline Paused execution state. Paused is a
                 visible state, not an archived one - the pipeline stays in the
                 list and in the editor, it just starts no runs. Pause/Resume
                 are execution-state toggles for a LIVE pipeline, so both are
                 gated on !archived_at (the same guard the Archive/Unarchive
                 pairing above uses): an archived pipeline offers Unarchive,
                 never a pause toggle. The two conditions are mutually
                 exclusive, so exactly one of the pair renders when it should. -->
            <button
              v-if="pipeline && !pipeline.archived_at && pipeline.run_enabled !== false"
              type="button"
              :class="btnToolbarSecondary"
              data-testid="pipeline-editor-pause"
              @click="handlePause"
            >{{ $t('views.PipelineEditorView.pause') }}</button>
            <button
              v-else-if="pipeline && !pipeline.archived_at && pipeline.run_enabled === false"
              type="button"
              :class="btnToolbarSecondary"
              :disabled="pipeline?.run_disabled_reason === 'circuit_breaker'"
              :title="pipeline?.run_disabled_reason === 'circuit_breaker' ? $t('views.PipelineEditorView.resume_blocked_circuit_breaker') : undefined"
              data-testid="pipeline-editor-resume"
              @click="handleResume"
            >{{ $t('views.PipelineEditorView.resume') }}</button>
            <button v-if="planStore.featureEnabled('pipeline_delete')" type="button" :class="btnToolbarDestructive" data-testid="pipeline-editor-delete" @click="showDeleteConfirm = true">{{ $t('common.delete') }}</button>
            <div class="flex items-center gap-1">
              <label for="pipeline-max-duration" class="whitespace-nowrap text-[10px] text-muted-foreground">{{ $t('views.PipelineEditorView.max_duration_s') }}:</label>
              <input id="pipeline-max-duration"
                v-model.number="maxDurationInput"
                type="number"
                min="0"
                :placeholder="$t('views.PipelineEditorView.no_limit')"
                class="w-20 rounded-md border border-input bg-background px-1.5 py-1 text-xs"
                @change="updateMaxDuration"
                data-testid="pipeline-editor-max-duration"
              />
            </div>
            <!-- FAR-1182: monthly spend circuit breaker (Community tier). Empty = disabled. -->
            <div class="flex items-center gap-1" v-tooltip.bottom="$t('views.PipelineEditorView.spend_circuit_breaker_help')">
              <label for="pipeline-spend-circuit-breaker" class="whitespace-nowrap text-[10px] text-muted-foreground">{{ $t('views.PipelineEditorView.spend_circuit_breaker_label') }}:</label>
              <input id="pipeline-spend-circuit-breaker"
                v-model="circuitBreakerInput"
                type="number"
                min="0.01"
                step="0.01"
                inputmode="decimal"
                :placeholder="$t('views.PipelineEditorView.no_limit')"
                aria-describedby="pipeline-spend-circuit-breaker-help"
                class="w-24 rounded-md border border-input bg-background px-1.5 py-1 text-xs"
                @change="updateCircuitBreakerThreshold"
                data-testid="pipeline-editor-circuit-breaker-threshold"
              />
              <span id="pipeline-spend-circuit-breaker-help" class="sr-only">{{ $t('views.PipelineEditorView.spend_circuit_breaker_help') }}</span>
            </div>
            <div
              v-if="pipeline?.circuit_breaker_tripped"
              class="flex items-center gap-1"
              data-testid="pipeline-editor-circuit-breaker-tripped"
            >
              <output class="badge badge-status-destructive whitespace-nowrap" :title="$t('views.PipelineEditorView.circuit_breaker_tripped_help')">{{ $t('views.PipelineEditorView.circuit_breaker_tripped') }}</output>
              <button
                v-if="canResetCircuitBreaker"
                type="button"
                :class="btnToolbarSecondary"
                :disabled="resettingCircuitBreaker"
                @click="resetCircuitBreaker"
                data-testid="pipeline-editor-circuit-breaker-reset"
              >
                {{ resettingCircuitBreaker ? $t('views.PipelineEditorView.circuit_breaker_resetting') : $t('views.PipelineEditorView.circuit_breaker_reset') }}
              </button>
            </div>
            <div class="flex items-center gap-1">
              <label
                for="pipeline-max-autonomy"
                class="whitespace-nowrap text-[10px] text-muted-foreground"
                :title="$t('views.PipelineEditorView.max_autonomy_level_hint')"
              >{{ $t('views.PipelineEditorView.max_autonomy_level') }}:</label>
              <select
                id="pipeline-max-autonomy"
                :value="maxAutonomyInput ?? ''"
                class="w-44 rounded-md border border-input bg-background px-1.5 py-1 text-xs"
                :title="$t('views.PipelineEditorView.max_autonomy_level_hint')"
                @change="updateMaxAutonomyLevel"
                data-testid="pipeline-editor-max-autonomy"
              >
                <option value="">{{ $t('views.PipelineEditorView.max_autonomy_level_inherit') }}</option>
                <option value="manual_approval">{{ $t('views.PipelineEditorView.autonomy_manual_approval') }}</option>
                <option value="notify_on_complete">{{ $t('views.PipelineEditorView.autonomy_notify_on_complete') }}</option>
                <option value="fully_autonomous">{{ $t('views.PipelineEditorView.autonomy_fully_autonomous') }}</option>
              </select>
            </div>
            <!-- FAR-1696 (ADR 043 S5): progressive-autonomy visibility panel,
                 mounted next to the ceiling control it reads alongside. The
                 panel renders the pipeline's default / ceiling / earned levels
                 and carries the admin/owner manual demote / promote actions;
                 a successful change refetches the pipeline below. -->
            <div class="relative">
              <button
                type="button"
                ref="autonomyPanelToggleRef"
                :class="btnToolbarSecondary"
                :aria-expanded="autonomyPanelOpen"
                aria-haspopup="dialog"
                :aria-controls="autonomyPanelDialogId"
                data-testid="pipeline-editor-autonomy-panel-toggle"
                @click="toggleAutonomyPanel"
              >
                {{ $t('views.PipelineEditorView.autonomy_panel_toggle') }}
              </button>
              <dialog
                v-if="autonomyPanelOpen"
                open
                :id="autonomyPanelDialogId"
                ref="autonomyPanelDialogRef"
                :class="[
                  'fixed z-50 max-h-[calc(100vh-1rem)] max-w-[calc(100vw-1rem)] overflow-y-auto overscroll-contain rounded-lg border border-border bg-card p-3 shadow-lg',
                  AUTONOMY_PANEL_METRICS.className,
                ]"
                :style="autonomyPanelStyle"
                tabindex="-1"
                :aria-label="$t('views.PipelineEditorView.autonomy_panel_title')"
                data-testid="pipeline-editor-autonomy-panel-dialog"
              >
                <ProgressiveAutonomyPanel
                  :pipeline-id="pipelineId"
                  :default-level="pipeline?.default_autonomy_level ?? null"
                  :max-level="pipeline?.max_autonomy_level ?? null"
                  :earned-level="pipeline?.earned_autonomy_level ?? null"
                  :earned-updated-at="pipeline?.earned_autonomy_updated_at ?? null"
                  @changed="onAutonomyChanged"
                />
                <div class="mt-3 flex justify-end">
                  <button
                    type="button"
                    :class="btnToolbarSecondary"
                    data-testid="pipeline-editor-autonomy-panel-close"
                    @click="closeAutonomyPanel"
                  >
                    {{ $t('common.close') }}
                  </button>
                </div>
              </dialog>
            </div>
            <!-- FAR-1558: per-pipeline environment profile - the runtime provider
                 this pipeline's runs dispatch on. Gated on the
                 `environment_profiles` plan feature because the profile list
                 endpoint is feature-gated (an ungated fetch would fail on plans
                 without it). Change semantics: an untouched select sends
                 nothing (the stored binding stays OMITTED/unchanged), choosing
                 "None (default route)" sends an explicit `null` (clears the
                 binding and restores the default route), choosing a profile
                 sends its id. -->
            <div
              v-if="showEnvironmentProfileControl"
              class="flex items-center gap-1"
              v-tooltip.bottom="$t('views.PipelineEditorView.environment_profile_hint')"
            >
              <label
                for="pipeline-environment-profile"
                class="whitespace-nowrap text-[10px] text-muted-foreground"
                :title="$t('views.PipelineEditorView.environment_profile_hint')"
              >{{ $t('views.PipelineEditorView.environment_profile_label') }}:</label>
              <select
                id="pipeline-environment-profile"
                :value="environmentProfileInput"
                class="w-56 rounded-md border border-input bg-background px-1.5 py-1 text-xs"
                :title="$t('views.PipelineEditorView.environment_profile_hint')"
                aria-describedby="pipeline-environment-profile-help"
                @change="updateEnvironmentProfile"
                data-testid="pipeline-editor-environment-profile"
              >
                <option value="">{{ $t('views.PipelineEditorView.environment_profile_none') }}</option>
                <option v-for="p in environmentProfileOptions" :key="p.id" :value="p.id">{{ p.label }}</option>
              </select>
              <span id="pipeline-environment-profile-help" class="sr-only">{{ $t('views.PipelineEditorView.environment_profile_hint') }}</span>
              <!-- A bind/clear write is async: announce its outcome, and fail
                   LOUD on error (role="alert"), never silently. -->
              <span
                v-if="environmentProfileError"
                role="alert"
                class="max-w-56 truncate text-xs text-destructive"
                :title="environmentProfileError"
                data-testid="pipeline-editor-environment-profile-error"
              >{{ environmentProfileError }}</span>
              <output
                v-else-if="environmentProfileStatus"
                class="max-w-56 truncate text-[10px] text-muted-foreground"
                data-testid="pipeline-editor-environment-profile-status"
              >{{ environmentProfileStatus }}</output>
            </div>
            <!-- FAR-1257: per-pipeline HITL review window override. Empty =
                 clear the override (inherit the org default); the value is
                 seconds, the same envelope the API enforces (60..604800). -->
            <div class="flex items-center gap-1" v-tooltip.bottom="$t('views.PipelineEditorView.hitl_review_window_help')">
              <label
                for="pipeline-hitl-review-window"
                class="whitespace-nowrap text-[10px] text-muted-foreground"
                :title="$t('views.PipelineEditorView.hitl_review_window_help')"
              >{{ $t('views.PipelineEditorView.hitl_review_window_label') }}:</label>
              <input id="pipeline-hitl-review-window"
                v-model="hitlReviewWindowInput"
                type="number"
                min="60"
                max="604800"
                step="1"
                inputmode="numeric"
                :placeholder="$t('views.PipelineEditorView.hitl_review_window_inherit')"
                :aria-describedby="'pipeline-hitl-review-window-help'"
                class="w-24 rounded-md border border-input bg-background px-1.5 py-1 text-xs"
                @change="updateHitlReviewWindow"
                data-testid="pipeline-editor-hitl-review-window"
              />
              <span id="pipeline-hitl-review-window-help" class="sr-only">{{ $t('views.PipelineEditorView.hitl_review_window_help') }}</span>
            </div>
            <!-- FAR-1883: per-pipeline capacity-queue timeout. Empty = null =
                 never (the default - a capacity-queued run waits
                 indefinitely). The value is seconds; on expiry the run ends
                 as a queue.expired supersession, never a failure. -->
            <div class="flex items-center gap-1" v-tooltip.bottom="$t('views.PipelineEditorView.queue_timeout_help')">
              <label
                for="pipeline-queue-timeout"
                class="whitespace-nowrap text-[10px] text-muted-foreground"
                :title="$t('views.PipelineEditorView.queue_timeout_help')"
              >{{ $t('views.PipelineEditorView.queue_timeout_label') }}:</label>
              <input id="pipeline-queue-timeout"
                v-model="queueTimeoutInput"
                type="number"
                min="1"
                max="604800"
                step="1"
                inputmode="numeric"
                :placeholder="$t('views.PipelineEditorView.queue_timeout_never')"
                :aria-describedby="'pipeline-queue-timeout-help'"
                class="w-24 rounded-md border border-input bg-background px-1.5 py-1 text-xs"
                @change="updateQueueTimeout"
                data-testid="pipeline-editor-queue-timeout"
              />
              <span id="pipeline-queue-timeout-help" class="sr-only">{{ $t('views.PipelineEditorView.queue_timeout_help') }}</span>
            </div>
            <div class="relative">
              <button
                type="button"
                ref="retryPolicyToggleRef"
                :id="retryPolicyToggleId"
                :class="btnToolbarSecondary"
                @click="toggleRetryPolicy"
                :aria-expanded="retryPolicyOpen"
                aria-haspopup="dialog"
                :aria-controls="retryPolicyPanelId"
                data-testid="pipeline-editor-retry-policy-toggle"
              >
                {{ $t('views.PipelineEditorView.retry_policy') }}
              </button>
              <dialog
                v-if="retryPolicyOpen"
                open
                :id="retryPolicyPanelId"
                ref="retryPolicyPanelRef"
                :class="[
                  'fixed z-50 max-h-[calc(100vh-1rem)] max-w-[calc(100vw-1rem)] overflow-y-auto overscroll-contain rounded-lg border border-border bg-card p-3 shadow-lg',
                  RETRY_POLICY_PANEL_METRICS.className,
                ]"
                :style="retryPolicyPanelStyle"
                tabindex="-1"
                :aria-label="$t('views.PipelineEditorView.retry_policy')"
                data-testid="pipeline-editor-retry-policy-panel"
              >
                <div class="mb-1 text-xs font-medium text-foreground">{{ $t('views.PipelineEditorView.retry_policy') }}</div>
                <div class="mb-2 text-[10px] text-muted-foreground">
                  {{ $t('views.PipelineEditorView.retry_policy_description') }}
                </div>
                <!-- FAR-649: coverage mode. "All errors" (the default) saves the
                     policy WITHOUT the `on` key (absent on = all retryable
                     events at runtime); "Choose specific errors" saves the
                     explicit event list. -->
                <div
                  class="mb-2 space-y-1"
                  role="radiogroup"
                  :aria-label="$t('views.PipelineEditorView.retry_policy_mode_label')"
                >
                  <label class="flex min-h-6 items-center gap-2 text-xs">
                    <input
                      type="radio"
                      value="all"
                      v-model="retryPolicyMode"
                      class="h-4 w-4"
                      data-testid="pipeline-editor-retry-mode-all"
                    />
                    {{ $t('views.PipelineEditorView.retry_policy_mode_all') }}
                  </label>
                  <label class="flex min-h-6 items-center gap-2 text-xs">
                    <input
                      type="radio"
                      value="specific"
                      v-model="retryPolicyMode"
                      class="h-4 w-4"
                      data-testid="pipeline-editor-retry-mode-specific"
                    />
                    {{ $t('views.PipelineEditorView.retry_policy_mode_specific') }}
                  </label>
                </div>
                <div v-if="retryPolicyMode === 'specific'" class="space-y-1">
                  <label
                    v-for="opt in retryPolicyOptions"
                    :key="opt.value"
                    class="flex min-h-6 items-center gap-2 text-xs"
                  >
                    <input
                      type="checkbox"
                      :value="opt.value"
                      v-model="retryPolicyEvents"
                      class="h-4 w-4"
                      :data-testid="`pipeline-editor-retry-event-${opt.value}`"
                    />
                    {{ $t(opt.labelKey) }}
                  </label>
                </div>
                <div class="mt-3 flex items-center gap-2">
                  <label for="retry-policy-max" class="whitespace-nowrap text-[10px] text-muted-foreground">
                    {{ $t('views.PipelineEditorView.max_retries') }}
                  </label>
                  <input
                    id="retry-policy-max"
                    v-model.number="retryPolicyMaxRetries"
                    type="number"
                    min="0"
                    max="5"
                    class="w-14 rounded-md border border-input bg-background px-1.5 py-1 text-xs"
                    data-testid="pipeline-editor-retry-policy-max"
                  />
                </div>
                <div class="mt-2 flex items-center gap-2">
                  <label for="retry-policy-delay" class="whitespace-nowrap text-[10px] text-muted-foreground">
                    {{ $t('views.PipelineEditorView.backoff_delay_seconds') }}
                  </label>
                  <input
                    id="retry-policy-delay"
                    v-model.number="retryPolicyDelaySeconds"
                    type="number"
                    min="1"
                    max="300"
                    step="1"
                    class="w-14 rounded-md border border-input bg-background px-1.5 py-1 text-xs"
                    data-testid="pipeline-editor-retry-policy-delay"
                  />
                </div>
                <div class="mt-2 flex items-center gap-2">
                  <label for="retry-policy-multiplier" class="whitespace-nowrap text-[10px] text-muted-foreground">
                    {{ $t('views.PipelineEditorView.backoff_multiplier') }}
                  </label>
                  <input
                    id="retry-policy-multiplier"
                    v-model.number="retryPolicyMultiplier"
                    type="number"
                    min="1"
                    max="10"
                    step="0.1"
                    class="w-14 rounded-md border border-input bg-background px-1.5 py-1 text-xs"
                    data-testid="pipeline-editor-retry-policy-multiplier"
                  />
                </div>
                <p class="mt-1 text-[10px] leading-snug text-muted-foreground">
                  {{ $t('views.PipelineEditorView.backoff_schedule_help') }}
                </p>
                <div
                  v-if="retryPolicyScheduleWarning"
                  class="mt-2 text-xs text-warning-text"
                  role="alert"
                  data-testid="pipeline-editor-retry-policy-schedule-warning"
                >
                  {{ retryPolicyScheduleWarning }}
                </div>
                <div
                  v-if="retryPolicyNoRetriesWarning"
                  class="mt-2 text-xs text-warning-text"
                  role="alert"
                  data-testid="pipeline-editor-retry-policy-warning"
                >
                  {{ retryPolicyNoRetriesWarning }}
                </div>
                <div
                  v-if="retryPolicyError"
                  class="mt-2 text-xs text-destructive"
                  role="alert"
                  data-testid="pipeline-editor-retry-policy-error"
                >
                  {{ retryPolicyError }}
                </div>
                <div class="mt-3 flex justify-end gap-2">
                  <button
                    type="button"
                    class="rounded-md border border-input bg-background px-2 py-1 text-xs hover:bg-accent"
                    data-testid="pipeline-editor-retry-policy-close"
                    @click="closeRetryPolicy"
                  >
                    {{ $t('views.PipelineEditorView.cancel') }}
                  </button>
                  <button
                    type="button"
                    class="rounded-md border border-input bg-primary px-2 py-1 text-xs text-primary-foreground hover:bg-primary/90 disabled:cursor-not-allowed disabled:opacity-50"
                    :disabled="retryPolicySaving || retryPolicySaveBlocked"
                    @click="saveRetryPolicy"
                    data-testid="pipeline-editor-retry-policy-save"
                  >
                    {{ retryPolicySaving ? $t('views.PipelineEditorView.saving') : $t('views.PipelineEditorView.save') }}
                  </button>
                </div>
              </dialog>
            </div>
          </div>
          </template>
          <span :class="groupDividerClass" />
          <!-- Group: canvas tools -->
          <div class="flex items-center gap-1.5" data-testid="pipeline-editor-toolbar-group-canvas">
            <template v-if="!isViewing">
              <Select
                v-model="newNodeType"
                :options="nodeTypeOptions"
                option-label="label"
                option-value="value"
                class="toolbar-select w-40 text-xs"
                append-to="body"
                :aria-label="$t('views.PipelineEditorView.new_node_type')"
                :title="$t('views.PipelineEditorView.new_node_type_hint')"
                data-testid="pipeline-editor-node-type-select"
              />
              <button
                type="button"
                :class="btnToolbarSecondary"
                :title="$t('views.PipelineEditorView.add_node')"
                data-testid="pipeline-editor-add-node"
                @click="addNode()"
              >
                <PlusIcon class="h-3 w-3" aria-hidden="true" />
                {{ $t('views.PipelineEditorView.add_node') }}
              </button>
            </template>
            <button
              type="button"
              :class="btnToolbarSecondary"
              :title="$t('views.PipelineEditorView.fit_to_view')"
              data-testid="pipeline-editor-fit-view"
              @click="() => fitView()"
            >
              <Maximize2Icon class="h-3 w-3" aria-hidden="true" />
              {{ $t('views.PipelineEditorView.fit_to_view') }}
            </button>
          </div>
        </div>
        <!-- Folder-list load failure. In-flow directly beneath the toolbar —
             never absolutely positioned and never inside the fixed-height
             canvas, because a degraded breadcrumb must not replace the page
             with pageError yet must still be visible. NOT breakpoint-gated: this
             view renders no FolderTree, so a gate would hide the failure at
             >=md — exactly where it occurs. The shared contract lives in
             FolderListError.vue. -->
        <FolderListError
          v-if="folderError"
          :message="folderError"
          :on-retry="loadFolders"
          class="mx-3 my-2"
          :data-testid="'pipeline-editor-folder-error'"
        />
        <!-- FAR-1816: transient success notice after a restore. A plain toast
             would be unmounted the moment the view navigates back to latest,
             so it lives here as a persistent-until-explicit status region
             (role=status announces it; the timer clears it). Names the exact
             outcome: which version was restored and which new version was
             created. -->
        <output
          v-if="restoreNotice"
          class="block border-b bg-success/10 px-3 py-2 text-xs text-success-text"
          data-testid="pipeline-editor-restore-notice"
        >
          {{ restoreNotice }}
        </output>
        <!-- FAR-1812: persistent historical-version banner. Amber/warning tint
             (NOT destructive red — nothing here is being destroyed), always
             visible while a historic version is on screen, with BOTH actions
             inside it so they are reachable without scrolling. <output> gives
             it the implicit status role (see the paused-banner note below:
             never combine an explicit status role with aria-live). -->
        <output
          v-if="viewSnapshot && isViewing"
          class="block border-b bg-warning/10 px-3 py-2 text-xs text-warning-text"
          :aria-label="$t('views.PipelineEditorView.historic_banner_title', { version: viewSnapshot.snapshot_version })"
          data-testid="pipeline-editor-historic-banner"
        >
          <p class="font-medium">
            {{ $t('views.PipelineEditorView.historic_banner_title', { version: viewSnapshot.snapshot_version }) }}
            <span class="font-normal">{{ $t('views.PipelineEditorView.historic_banner_meta', { date: viewedVersionDate, author: viewedVersionAuthor }) }}</span>
          </p>
          <p class="mt-0.5">{{ $t('views.PipelineEditorView.historic_banner_body') }}</p>
          <div class="mt-1.5 flex flex-wrap items-center gap-2">
            <button
              type="button"
              :class="btnToolbarSecondary"
              data-testid="pipeline-editor-historic-restore"
              @click="openRestoreConfirm()"
            >
              {{ $t('views.PipelineEditorView.historic_restore') }}
            </button>
            <button
              type="button"
              :class="btnToolbarSecondary"
              data-testid="pipeline-editor-historic-view-latest"
              @click="viewLatest"
            >
              {{ $t('views.PipelineEditorView.historic_view_latest') }}
            </button>
            <!-- FAR-1818: one-click entry into semantic-led compare mode
                 (viewed version vs the latest). Hidden while the viewed
                 version IS the latest — that would be a self-compare. -->
            <button
              v-if="resolvedSnapshotId !== latestSnapshotId"
              type="button"
              :class="btnToolbarSecondary"
              data-testid="pipeline-editor-historic-compare"
              @click="openCompare"
            >
              {{ $t('views.PipelineEditorView.historic_compare') }}
            </button>
            <button
              type="button"
              :class="btnToolbarSecondary"
              data-testid="pipeline-editor-historic-copy-link"
              @click="copyHistoricLink"
            >
              {{ linkCopied ? $t('views.PipelineEditorView.historic_copy_link_copied') : $t('views.PipelineEditorView.historic_copy_link') }}
            </button>
            <span
              v-if="copyLinkError"
              class="text-destructive"
              role="alert"
              data-testid="pipeline-editor-historic-copy-link-error"
            >{{ copyLinkError }}</span>
          </div>
        </output>
        <!-- FAR-1530: persistent Paused banner. Deliberately NOT dismissible -
             while it holds, no run of any origin (trigger, manual, REST, MCP)
             will start, so the state must stay announced. aria-live (NOT
             role="status"): FAR-1257's ownerless-gate advisory codifies that
             aria-live alone announces without SonarCloud Web:S6819. Text
             colour is --warning-text (WCAG AA), never text-warning. -->
        <div
          v-if="pipeline && pipeline.run_enabled === false"
          class="border-b bg-warning/10 px-3 py-2 text-xs text-warning-text"
          aria-live="polite"
          data-testid="pipeline-editor-paused-banner"
        >
          <p class="font-medium">{{ $t('views.PipelineEditorView.paused_banner_title') }}</p>
          <p class="mt-0.5 text-warning-text">{{ $t('views.PipelineEditorView.paused_banner_body') }}</p>
          <p v-if="pipeline.run_disabled_reason === 'circuit_breaker'" class="mt-0.5 text-warning-text">{{ $t('views.PipelineEditorView.paused_banner_circuit_breaker') }}</p>
        </div>
        <!-- FAR-1287: system-admin-only snapshot-lock diagnostics. Gated on the
             JWT is_system_admin claim (useCurrentUser) so a non-admin never
             renders it - and never issues the admin-only GET. In-flow under the
             toolbar so it never covers the canvas. -->
        <!-- FAR-1812: hidden in the historic view — its release action is a
             write affordance, and a read-only view mounts none. -->
        <SnapshotLockCard v-if="isSystemAdmin && !isViewing" :pipeline-id="pipelineId" />
        <!-- FAR-688: legacy HITL gates whose descriptions predate the
             minimum (reads never hard-fail on them) — surface WHICH gates
             need descriptions so the user can find and fix them. -->
        <div
          v-if="!isViewing && showLegacyHitlBanner && legacyHitlIssues.length > 0"
          class="border-b bg-warning/10 px-3 py-2 text-xs text-warning-text"
          role="alert"
          data-testid="pipeline-editor-legacy-hitl-banner"
        >
          <div class="flex items-start justify-between gap-3">
            <div class="min-w-0">
              <p class="font-medium">{{ $t('views.PipelineEditorView.legacy_hitl_description_title') }}</p>
              <!-- FAR-688 per-kind hint: edge gates are fixable in this editor
                   ("open the edge"); node-level gates have no hitl_config
                   panel here (documented deferral), so they get a distinct,
                   truthful hint instead of an unactionable one. -->
              <p v-if="hasLegacyEdgeIssues" class="mt-0.5 text-warning-text">{{ $t('views.PipelineEditorView.legacy_hitl_description_hint') }}</p>
              <p v-if="hasLegacyNodeIssues" class="mt-0.5 text-warning-text">{{ $t('views.PipelineEditorView.legacy_hitl_hint_node') }}</p>
              <ul class="mt-1 list-inside list-disc space-y-0.5">
                <li v-for="issue in legacyHitlIssues" :key="issue.key" class="max-w-full truncate" :title="issue.label">
                  {{ $t(issue.kind === 'node' ? 'views.PipelineEditorView.legacy_hitl_item_node' : 'views.PipelineEditorView.legacy_hitl_item_edge', { label: issue.label }) }}
                </li>
              </ul>
            </div>
            <button
              type="button"
              class="shrink-0 rounded p-1 hover:bg-warning/20"
              :aria-label="$t('common.close')"
              data-testid="pipeline-editor-legacy-hitl-banner-dismiss"
              @click="showLegacyHitlBanner = false"
            >
              <XIcon class="h-3 w-3" aria-hidden="true" />
            </button>
          </div>
        </div>
        <!-- FAR-1818: the canvas is hidden (not unmounted — viewport state is
             preserved) while the compare view occupies the same flex slot.
             Canvas region. `min-h-[8rem]` (replacing the old `min-h-0`) keeps a
             real, focusable canvas even when the pinned height is smaller than
             the wrapped toolbar: the toolbar + this minimum make the content
             column scroll internally (never the page), so both stay reachable.
             `tabindex="-1"` makes it the focus-restore fallback for a properties
             sheet whose trigger (a VueFlow node/edge wrapper) is not focusable
             (FAR-1861). -->
        <div
          v-show="!compareMode"
          ref="canvasRef"
          class="relative min-h-[8rem] flex-1"
          tabindex="-1"
          data-testid="pipeline-editor-canvas"
        >
        <!-- Empty-state overlay on top of the canvas (latest mode only — its
             rename/archive/delete/add-node row is all write affordances, so a
             historical view of an empty graph shows the canvas alone). -->
        <div v-if="!isViewing && displayNodes.length === 0" class="absolute inset-0 z-20 flex flex-col items-center justify-center gap-4 pointer-events-none">
          <div class="text-center">
            <h2 class="text-xl font-semibold">{{ pipeline?.name || $t('views.PipelineEditorView.pipeline_fallback_title') }}</h2>
            <p v-if="pipeline?.description" class="mt-1 text-sm text-muted-foreground">{{ pipeline.description }}</p>
            <p class="mt-4 text-sm italic text-muted-foreground/60 select-none">{{ $t('views.PipelineEditorView.empty_state_hint') }}</p>
          </div>
          <div class="flex items-center gap-2 pointer-events-auto">
            <Button size="small" type="button" class="text-xs" @click="openRenameDialog">{{ $t('views.PipelineEditorView.rename') }}</Button>
            <button v-if="!pipeline?.archived_at" type="button" class="rounded-md border border-input bg-background px-3 py-1 text-xs font-medium hover:bg-accent" data-testid="pipeline-editor-empty-archive" @click="handleArchive">{{ $t('views.PipelineEditorView.archive') }}</button>
            <button v-else type="button" class="rounded-md border border-input bg-background px-3 py-1 text-xs font-medium hover:bg-accent" data-testid="pipeline-editor-empty-unarchive" @click="handleUnarchive">{{ $t('views.PipelineEditorView.unarchive') }}</button>
            <button v-if="planStore.featureEnabled('pipeline_delete')" type="button" class="rounded-md border border-destructive/50 bg-destructive/10 px-3 py-1 text-xs font-medium text-destructive-text hover:bg-destructive/20" data-testid="pipeline-editor-empty-delete" @click="showDeleteConfirm = true">{{ $t('common.delete') }}</button>
            <Select
              v-model="newNodeType"
              :options="nodeTypeOptions"
              option-label="label"
              option-value="value"
              class="toolbar-select w-40 text-xs"
              append-to="body"
              :aria-label="$t('views.PipelineEditorView.new_node_type')"
              data-testid="pipeline-editor-empty-node-type-select"
            />
            <Button severity="secondary" outlined size="small" type="button" class="text-xs" @click="addNode()">{{ $t('views.PipelineEditorView.add_node') }}</Button>
          </div>
        </div>
        <!-- Run dialog modal -->
        <!-- eslint-disable-next-line vuejs-accessibility/no-static-element-interactions --><!-- modal backdrop: needs keyboard Escape handler for accessible dismissal -->
        <div
          v-if="showRunDialog"
          class="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
          @click.self="closeRunDialog"
          @keydown.escape="closeRunDialog"
          tabindex="-1"
          data-testid="pipeline-editor-run-dialog-backdrop"
        >
          <dialog
            open
            aria-modal="true"
            :aria-label="$t('views.PipelineEditorView.run_dialog_title')"
            class="bg-card border border-border rounded-xl shadow-xl w-full max-w-lg mx-4 p-6 space-y-4"
            style="position: static"
          >
            <div class="flex items-center justify-between">
              <h2 class="text-base font-semibold text-foreground">{{ $t('views.PipelineEditorView.run_dialog_title') }}</h2>
              <button
                type="button"
                class="text-muted-foreground hover:text-foreground transition-colors"
                @click="closeRunDialog"
                data-testid="pipeline-editor-run-dialog-close"
                :aria-label="$t('views.PipelineEditorView.close')"
              >
                <XIcon class="h-5 w-5" aria-hidden="true" />
              </button>
            </div>
            <p class="text-sm text-muted-foreground">
              {{ $t('views.PipelineEditorView.run_label') }} <span class="font-medium text-foreground">{{ pipeline?.name }}</span>
            </p>
            <div v-if="isWebhookTriggered" class="rounded-lg bg-muted border p-3 text-sm text-muted-foreground">
              {{ $t('views.PipelineEditorView.webhook_triggered_info') }}
            </div>
            <div v-else class="space-y-2">
              <label for="pipeline-editor-run-prompt" class="block text-sm font-medium text-foreground">{{ $t('views.PipelineEditorView.prompt') }}</label>
              <textarea id="pipeline-editor-run-prompt"
                v-model="runPrompt"
                :placeholder="$t('views.PipelineEditorView.run_prompt_placeholder')"
                rows="4"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm resize-none focus:outline-none focus:ring-2 focus:ring-primary"
                data-testid="pipeline-editor-run-prompt"
              />
            </div>
            <ErrorAlert v-if="runError" :message="runError" :retryable="false" />
            <div v-if="emptyRunWarning" class="rounded-lg bg-warning/10 border border-warning/30 p-3 text-sm text-warning-text">
              {{ emptyRunWarning }}
            </div>
            <div class="flex justify-end gap-2 pt-2">
              <button
                type="button"
                class="px-4 py-2 border border-input bg-background text-foreground text-sm font-medium rounded-lg hover:bg-accent transition-colors"
                data-testid="pipeline-editor-run-dialog-cancel"
                @click="closeRunDialog"
              >
                {{ $t('views.PipelineEditorView.cancel') }}
              </button>
              <Button v-if="!isWebhookTriggered" type="button" class="border-indigo-300 bg-indigo-600 text-white hover:bg-indigo-500" :disabled="running" @click="triggerRun" data-testid="pipeline-editor-run-submit">
                <LoaderCircleIcon
                  v-if="running"
                  class="animate-spin h-4 w-4 mr-1"
                  aria-hidden="true"
                />
                {{ running ? $t('views.PipelineEditorView.running') : $t('views.PipelineEditorView.run_pipeline') }}
              </Button>
            </div>
          </dialog>
        </div>
        <!-- FAR-1812: in the historic view the canvas is pan/zoom/select only
             — node drag, edge creation/updating and keyboard delete are
             switched OFF (undefined keeps each control's default in the
             latest editor). The v-model binds displayNodes/displayEdges,
             which resolve to the FROZEN snapshot graph while viewing and to
             the editable live graph otherwise. -->
        <VueFlow
          :key="pipelineId"
          v-model:nodes="displayNodes"
          v-model:edges="displayEdges"
          :node-types="nodeTypes"
          :default-edge-options="{ type: 'smoothstep', animated: false, style: { stroke: CANVAS_EDGE_STROKE } }"
          :fit-view-on-init="false"
          :source-position="Position.Right"
          :target-position="Position.Left"
          :nodes-draggable="!isViewing"
          :nodes-connectable="!isViewing"
          :edges-updatable="!isViewing"
          :delete-key-code="isViewing ? null : undefined"
          @node-click="onNodeClick"
          @edge-click="onEdgeClick"
          @pane-click="onPaneClick"
        >
          <Background :gap="20" :size="1" />
          <FlowControls :show-interactive="false" />
          <template #node-manual="nodeProps"><div class="rounded-lg border-2 border-warning/60 bg-warning/10 px-4 py-2 shadow-sm" v-tooltip.top="nodeProps.data.description">
                    <div class="font-brand-mono text-[11px] font-medium lowercase tracking-wide text-warning-text">{{ $t('views.PipelineEditorView.node_manual_badge') }}</div>
                    <div class="text-sm font-semibold">{{ nodeProps.data.label }}</div>
                  </div></template>
          <template #node-agent="nodeProps"><div class="rounded-lg border-2 border-primary/60 bg-primary/10 px-4 py-2 shadow-sm" v-tooltip.top="nodeProps.data.description">
                    <div class="font-brand-mono text-[11px] font-medium lowercase tracking-wide text-primary-text">{{ $t('views.PipelineEditorView.node_agent_badge') }}</div>
                    <div class="text-sm font-semibold">{{ nodeProps.data.label }}</div>
                    <div v-if="nodeProps.data.hasCapabilityScope" class="mt-1 inline-flex rounded bg-indigo-100 px-1.5 py-0.5 text-[10px] font-medium text-indigo-600 dark:bg-indigo-900 dark:text-indigo-300" data-testid="pipeline-node-scope-badge">{{ $t('views.PipelineEditorView.capability_scope_scoped_badge') }}</div>
                  </div></template>
          <template #node-router="nodeProps"><div class="rounded-lg border-2 border-indigo-500/60 bg-indigo-500/10 px-4 py-2 shadow-sm" v-tooltip.top="nodeProps.data.description">
                    <div class="font-brand-mono text-[11px] font-medium lowercase tracking-wide text-indigo-600 dark:text-indigo-300">{{ $t('views.PipelineEditorView.node_router_badge') }}</div>
                    <div class="text-sm font-semibold">{{ nodeProps.data.label || $t('views.PipelineEditorView.node_router_label') }}</div>
                  </div></template>
          <template #node-hitl="nodeProps"><div class="rounded-lg border-2 border-rose-500/60 bg-rose-500/10 px-4 py-2 shadow-sm" v-tooltip.top="nodeProps.data.description">
                    <div class="font-brand-mono text-[11px] font-medium lowercase tracking-wide text-rose-600 dark:text-rose-300">{{ $t('views.PipelineEditorView.node_hitl_badge') }}</div>
                    <div class="text-sm font-semibold">{{ nodeProps.data.label || $t('views.PipelineEditorView.node_hitl_label') }}</div>
                  </div></template>
          <!-- FAR-1141: dispatch nodes render with their own badge/colour so
               they never fall back to the `agent` node rendering. Display
               only — no config panel or palette entry in this slice. -->
          <template #node-dispatch="nodeProps"><div class="rounded-lg border-2 border-cyan-500/60 bg-cyan-500/10 px-4 py-2 shadow-sm" v-tooltip.top="nodeProps.data.description">
                    <div class="font-brand-mono text-[11px] font-medium lowercase tracking-wide text-cyan-700 dark:text-cyan-300">{{ $t('views.PipelineEditorView.node_dispatch_badge') }}</div>
                    <div class="text-sm font-semibold">{{ nodeProps.data.label || $t('views.PipelineEditorView.node_dispatch_label') }}</div>
                  </div></template>
          <!-- FAR-2089: connector nodes render with their own badge/colour
               instead of the generic `agent` card. Reachable because
               CANVAS_TYPE_BY_BACKEND_TYPE maps 'connector' to this canvas
               type; without that entry the node would be emitted as an
               `agent` node and this slot would never render. Display only —
               no config panel or palette entry in this slice. -->
          <template #node-connector="nodeProps"><div class="rounded-lg border-2 border-teal-500/60 bg-teal-500/10 px-4 py-2 shadow-sm" v-tooltip.top="nodeProps.data.description">
                    <div class="font-brand-mono text-[11px] font-medium lowercase tracking-wide text-teal-700 dark:text-teal-300">{{ $t('views.PipelineEditorView.node_connector_badge') }}</div>
                    <div class="text-sm font-semibold">{{ nodeProps.data.label || $t('views.PipelineEditorView.node_connector_label') }}</div>
                  </div></template>
          <template #edge-default="edgeProps">
            <div v-if="edgeProps.data?.hitl_review_config" class="absolute -translate-y-4 translate-x-2">
              <span class="rounded bg-warning/20 px-1.5 py-0.5 text-[10px] font-medium text-warning-text">{{ $t('views.PipelineEditorView.node_hitl_badge') }}</span>
            </div>
            <div v-if="edgeProps.data?.edge_type === 'loop'" class="absolute translate-y-4 translate-x-2">
              <span class="rounded bg-blue-100 px-1.5 py-0.5 text-[10px] font-medium text-blue-600 dark:bg-blue-900 dark:text-blue-300">
                {{ $t('views.PipelineEditorView.loop') }}{{ edgeProps.data?.max_iterations ? ` (${edgeProps.data.max_iterations})` : '' }}
              </span>
            </div>
            <div v-if="edgeProps.data?.edge_type === 'llm'" class="absolute translate-y-4 translate-x-2">
              <span class="rounded bg-purple-100 px-1.5 py-0.5 text-[10px] font-medium text-purple-600 dark:bg-purple-900 dark:text-purple-300">
                {{ $t('views.PipelineEditorView.llm_badge') }}{{ edgeProps.data?.routing_label ? `: ${edgeProps.data.routing_label}` : '' }}
              </span>
            </div>
          </template>
        </VueFlow>
        </div>
        <!-- FAR-1818: semantic-led old-vs-latest compare mode. Rendered in the
             canvas slot (the canvas above is hidden) so the presentation has
             room; the pair pickers keep the route query authoritative, which is
             what makes the view deep-linkable for sharing. -->
        <div v-if="compareMode" class="min-h-0 flex-1 overflow-y-auto">
          <PipelineSnapshotCompare
            :pipeline-id="pipelineId"
            :base-snapshot-id="resolvedSnapshotId ?? ''"
            :compare-snapshot-id="compareParam"
            :snapshots="snapshots"
            :latest-snapshot-id="latestSnapshotId"
            @change="updateCompare"
            @close="exitCompare"
          />
        </div>
      </div>
      <!-- Node Properties Panel. Mobile (FAR-1841): an in-editor overlay sheet
           (`max-md:absolute max-md:inset-0`) with an explicit Close button.
           As a flex sibling its fixed w-96 swallowed the whole viewport and
           crushed the canvas to 0x0 — and with the canvas gone there was no
           pane-click target to deselect, trapping the user in the panel.
           Desktop keeps the docked side panel unchanged. Below the desktop
           breakpoint the sheet is a modal dialog (FAR-1861): labelled
           `role="dialog"`, Escape + focus-trap handled by `onSheetKeydown`,
           focus moved in on open / restored on close (see the script block). -->
      <!-- eslint-disable-next-line vuejs-accessibility/no-static-element-interactions --><!-- mobile node sheet: a role="dialog" container whose Escape/Tab focus-trap needs the keydown handler -->
      <aside
        v-if="selectedNodeData && !selectedEdgeData"
        ref="nodePanelRef"
        v-bind="sheetDialogAttrs(NODE_SHEET_TITLE_ID)"
        class="w-96 overflow-y-auto border-l bg-card p-4 max-md:absolute max-md:inset-0 max-md:z-40 max-md:w-auto max-md:border-l-0"
        @keydown="onSheetKeydown"
      >
        <div class="mb-4 flex items-center justify-between">
          <h2 :id="NODE_SHEET_TITLE_ID" class="text-base font-semibold">{{ $t('views.PipelineEditorView.node_properties') }}</h2>
          <button
            type="button"
            class="rounded p-1 text-muted-foreground hover:bg-accent hover:text-foreground md:hidden"
            :aria-label="$t('common.close')"
            data-testid="pipeline-editor-node-props-close"
            @click="onPaneClick"
          >
            <XIcon class="h-4 w-4" aria-hidden="true" />
          </button>
        </div>
        <dl class="space-y-4 text-sm">
          <div>
            <dt class="text-muted-foreground text-xs uppercase tracking-wider">{{ $t('views.PipelineEditorView.id_label') }}</dt>
            <dd class="font-mono text-[10px] text-muted-foreground break-all select-all" :title="selectedNodeData.id">{{ shortId(selectedNodeData.id) }}</dd>
          </div>
          <div>
            <dt class="text-muted-foreground text-xs uppercase tracking-wider">{{ $t('views.PipelineEditorView.type_label') }}</dt>
            <dd>
              <span :class="nodeTypeBadgeClass(selectedNodeData.node_type)">
                {{ nodeTypeLabel(selectedNodeData.node_type) }}
              </span>
            </dd>
          </div>
          <div>
            <label for="pipeline-editor-node-label" class="text-muted-foreground text-xs uppercase tracking-wider">{{ $t('views.PipelineEditorView.label') }}</label>
            <input id="pipeline-editor-node-label"
              v-model="selectedNodeData.label"
              :readonly="isViewing"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm font-medium mt-1"
              :placeholder="$t('views.PipelineEditorView.enter_node_label')"
              @input="syncNodeToFlow"
              data-testid="pipeline-editor-node-label"
            />
          </div>
          <div>
            <label for="pipeline-editor-node-desc" class="text-muted-foreground text-xs uppercase tracking-wider">{{ $t('views.PipelineEditorView.description') }}</label>
            <textarea id="pipeline-editor-node-desc"
              v-model="selectedNodeData.description"
              :readonly="isViewing"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm mt-1"
              :placeholder="$t('views.PipelineEditorView.optional_description')"
              rows="2"
              @input="syncNodeToFlow"
              data-testid="pipeline-editor-node-description"
            />
          </div>
          <!-- FAR-295: retry-safety indicator for every node type -->
          <div>
            <dt class="text-muted-foreground text-xs uppercase tracking-wider" data-testid="pipeline-editor-idempotent-label">{{ $t('views.PipelineEditorView.idempotent') }}</dt>
            <dd data-testid="pipeline-editor-idempotent-value">
              {{ selectedNodeData.idempotent === false ? $t('views.PipelineEditorView.disabled') : $t('views.PipelineEditorView.enabled') }}
            </dd>
            <p class="mt-0.5 text-[11px] text-muted-foreground">{{ $t('views.PipelineEditorView.idempotent_description') }}</p>
          </div>
          <!-- Manual node: Output Schema -->
          <div v-if="selectedNodeData.node_type === 'manual' && selectedNodeData.output_schema_id">
            <dt class="text-muted-foreground text-xs uppercase tracking-wider">{{ $t('views.PipelineEditorView.output_schema') }}</dt>
            <dd class="font-medium">{{ schemaName(selectedNodeData.output_schema_id) || shortId(selectedNodeData.output_schema_id) }}</dd>
          </div>
          <!-- Agent node: Agent details -->
          <template v-if="(selectedNodeData.node_type === 'agent' || selectedNodeData.node_type === 'sandbox_agent') && selectedNodeData.agent_id">
            <div>
              <dt class="text-muted-foreground text-xs uppercase tracking-wider">{{ $t('views.PipelineEditorView.agent') }}</dt>
              <dd class="font-medium">{{ agentName(selectedNodeData.agent_id) || shortId(selectedNodeData.agent_id) }}</dd>
              <router-link
                v-if="selectedNodeData.agent_id"
                :to="`/admin/agents/${selectedNodeData.agent_id}`"
                class="mt-0.5 inline-flex items-center gap-1 text-xs text-indigo-500 hover:text-indigo-400"
              >
                {{ $t('views.PipelineEditorView.view_agent') }}
                <ExternalLinkIcon class="h-2.5 w-2.5" aria-hidden="true" />
              </router-link>
            </div>
            <div v-if="agentModelBackendId(selectedNodeData.agent_id)">
              <dt class="text-muted-foreground text-xs uppercase tracking-wider">{{ $t('views.PipelineEditorView.model_backend') }}</dt>
              <dd class="font-medium">{{ agentModelBackendName(selectedNodeData.agent_id) || shortId(agentModelBackendId(selectedNodeData.agent_id)) }}</dd>
            </div>
            <div v-if="agentInputSchemaId(selectedNodeData.agent_id)">
              <dt class="text-muted-foreground text-xs uppercase tracking-wider">{{ $t('views.PipelineEditorView.input_schema') }}</dt>
              <dd class="font-medium">{{ schemaName(agentInputSchemaId(selectedNodeData.agent_id) ?? '') || shortId(agentInputSchemaId(selectedNodeData.agent_id) ?? '') }}</dd>
            </div>
            <div v-if="agentOutputSchemaId(selectedNodeData.agent_id)">
              <dt class="text-muted-foreground text-xs uppercase tracking-wider">{{ $t('views.PipelineEditorView.output_schema') }}</dt>
              <dd class="font-medium">{{ schemaName(agentOutputSchemaId(selectedNodeData.agent_id) ?? '') || shortId(agentOutputSchemaId(selectedNodeData.agent_id) ?? '') }}</dd>
            </div>
            <div v-if="selectedNodeData.connector_binding">
              <dt class="text-muted-foreground text-xs uppercase tracking-wider">{{ $t('views.PipelineEditorView.connector') }}</dt>
              <dd class="font-medium">{{ connectorName(selectedNodeData.connector_binding) }}</dd>
            </div>
            <!-- Parameter Schema + Set -->
            <div v-if="agentParamSchema(selectedNodeData.agent_id)">
              <dt class="text-muted-foreground text-xs uppercase tracking-wider">{{ $t('views.PipelineEditorView.param_schema') }}</dt>
              <dd class="font-medium">{{ agentParamSchemaName(selectedNodeData.agent_id) }}</dd>
            </div>
            <div v-if="agentParamSchema(selectedNodeData.agent_id)">
              <dt class="text-muted-foreground text-xs uppercase tracking-wider">{{ $t('views.PipelineEditorView.param_set') }}</dt>
              <dd>
                <select
                  v-model="selectedNodeParamSetId"
                  :aria-label="$t('views.PipelineEditorView.param_set')"
                  :disabled="isViewing"
                  class="w-full rounded-lg border border-input bg-background px-2 py-1.5 text-sm"
                  @change="onParamSetChange"
                  data-testid="pipeline-node-param-set"
                >
                  <option :value="undefined">{{ $t('views.PipelineEditorView.no_set') }}</option>
                  <option
                    v-for="ps in availableParamSets"
                    :key="ps.id"
                    :value="ps.id"
                  >{{ ps.name }}</option>
                </select>
              </dd>
            </div>
            <div v-if="selectedNodeParamSetId && paramSetOverridesKeys.length > 0">
              <dt class="text-muted-foreground text-xs uppercase tracking-wider">{{ $t('views.PipelineEditorView.overrides') }}</dt>
              <dd class="space-y-2">
                <div v-for="pkey in paramSetOverridesKeys" :key="pkey" class="flex flex-col gap-0.5">
                  <label :for="'pipelineeditorview-override-' + pkey" class="text-xs text-muted-foreground">{{ paramDefLabel(pkey) }}</label>
                  <textarea
                    v-if="paramDefByKey(pkey)?.type === 'string' && paramDefByKey(pkey)?.multiline"
                    :id="'pipelineeditorview-override-' + pkey"
                    v-model="selectedNodeOverrides[pkey]"
                    :readonly="isViewing"
                    class="w-full rounded-lg border border-input bg-background px-2 py-1 text-xs"
                    rows="2"
                  />
                  <input
                    v-else-if="paramDefByKey(pkey)?.type === 'string'"
                    :id="'pipelineeditorview-override-' + pkey"
                    v-model="selectedNodeOverrides[pkey]"
                    type="text"
                    :readonly="isViewing"
                    class="w-full rounded-lg border border-input bg-background px-2 py-1 text-xs"
                  />
                  <input
                    v-else-if="paramDefByKey(pkey)?.type === 'number'"
                    :id="'pipelineeditorview-override-' + pkey"
                    v-model.number="selectedNodeOverrides[pkey]"
                    type="number"
                    :disabled="isViewing"
                    class="w-full rounded-lg border border-input bg-background px-2 py-1 text-xs"
                  />
                  <select
                    v-else-if="paramDefByKey(pkey)?.type === 'boolean'"
                    :id="'pipelineeditorview-override-' + pkey"
                    v-model="selectedNodeOverrides[pkey]"
                    :disabled="isViewing"
                    class="w-full rounded-lg border border-input bg-background px-2 py-1 text-xs"
                  >
                    <option :value="undefined"></option>
                    <option :value="true">true</option>
                    <option :value="false">false</option>
                  </select>
                  <select
                    v-else-if="paramDefByKey(pkey)?.type === 'select'"
                    :id="'pipelineeditorview-override-' + pkey"
                    v-model="selectedNodeOverrides[pkey]"
                    :disabled="isViewing"
                    class="w-full rounded-lg border border-input bg-background px-2 py-1 text-xs"
                  >
                    <option :value="undefined"></option>
                    <option v-for="o in (paramDefByKey(pkey)?.options || [])" :key="o" :value="o">{{ o }}</option>
                  </select>
                  <select
                    v-else-if="paramDefByKey(pkey)?.type === 'model_backend_ref'"
                    :id="'pipelineeditorview-override-' + pkey"
                    v-model="selectedNodeOverrides[pkey]"
                    :disabled="isViewing"
                    class="w-full rounded-lg border border-input bg-background px-2 py-1 text-xs"
                  >
                    <option :value="undefined"></option>
                    <option v-for="mb in modelBackends" :key="mb.id" :value="mb.id">{{ mb.display_name || mb.name || shortId(mb.id) }}</option>
                  </select>
                  <select
                    v-else-if="paramDefByKey(pkey)?.type === 'schema_ref'"
                    :id="'pipelineeditorview-override-' + pkey"
                    v-model="selectedNodeOverrides[pkey]"
                    :disabled="isViewing"
                    class="w-full rounded-lg border border-input bg-background px-2 py-1 text-xs"
                  >
                    <option :value="undefined"></option>
                    <option v-for="s in schemas" :key="s.id" :value="s.id">{{ s.name || shortId(s.id) }}</option>
                  </select>
                  <span v-else class="text-xs text-muted-foreground">—</span>
                </div>
                <button
                  v-if="!isViewing"
                  type="button"
                  class="mt-1 text-xs text-indigo-500 hover:text-indigo-400"
                  data-testid="pipeline-save-param-set"
                  @click="saveAsNewParamSet"
                >
                  {{ $t('views.PipelineEditorView.save_as_new_set') }}
                </button>
              </dd>
            </div>
          </template>
          <!-- Capability Scope (FAR-437): narrow-not-widen authoring -->
          <div v-if="(selectedNodeData.node_type === 'agent' || selectedNodeData.node_type === 'sandbox_agent') && selectedNodeData.agent_id" data-testid="pipeline-editor-capability-scope">
            <dt class="text-muted-foreground text-xs uppercase tracking-wider">{{ $t('views.PipelineEditorView.capability_scope') }}</dt>
            <dd class="mt-1 space-y-3">
              <p class="text-[11px] text-muted-foreground">{{ $t('views.PipelineEditorView.capability_scope_description') }}</p>
              <!-- Allowed Connectors (from the Agent's grants) -->
              <div>
                <div class="text-xs font-medium">{{ $t('views.PipelineEditorView.capability_scope_allowed_connectors') }}</div>
                <div v-if="availableConnectorsForNode.length === 0" class="mt-1 text-[11px] text-muted-foreground" data-testid="pipeline-editor-scope-no-connectors">
                  {{ $t('views.PipelineEditorView.capability_scope_no_connectors') }}
                </div>
                <div v-else class="mt-1 space-y-1">
                  <label v-for="c in availableConnectorsForNode" :key="c.id" class="flex min-h-6 items-center gap-2 text-xs">
                    <input
                      type="checkbox"
                      :value="c.id"
                      v-model="nodeCapabilityScope.allowed_connectors"
                      :disabled="isViewing"
                      class="h-4 w-4"
                      :data-testid="`pipeline-editor-scope-connector-${c.id}`"
                    />
                    <span>{{ connectorName({ type: c.connector_type_id, instance_id: c.id }) }}</span>
                  </label>
                </div>
                <div v-if="outOfScopeConnectors.length > 0" class="mt-1 text-[11px] text-warning-text" role="alert" data-testid="pipeline-editor-scope-widen-warning">
                  {{ $t('views.PipelineEditorView.capability_scope_widen_warning') }}{{ outOfScopeConnectors.join(', ') }}
                </div>
              </div>
              <!-- Allowed Tools (free-form list) -->
              <div>
                <div class="text-xs font-medium">{{ $t('views.PipelineEditorView.capability_scope_allowed_tools') }}</div>
                <div v-if="nodeCapabilityScope.allowed_tools.length > 0" class="mt-1 flex flex-wrap gap-1">
                  <span v-for="t in nodeCapabilityScope.allowed_tools" :key="t" class="inline-flex items-center gap-1 rounded bg-muted px-1.5 py-0.5 text-[11px]">
                    {{ t }}
                    <button
                      v-if="!isViewing"
                      type="button"
                      class="text-muted-foreground hover:text-foreground"
                      :aria-label="$t('views.PipelineEditorView.capability_scope_remove_tool', { name: t })"
                      :data-testid="`pipeline-editor-scope-tool-remove-${t}`"
                      @click="removeCapabilityEntry('allowed_tools', t)"
                    >&times;</button>
                  </span>
                </div>
                <div v-if="!isViewing" class="mt-1 flex gap-1">
                  <input
                    v-model="capabilityToolInput"
                    :placeholder="$t('views.PipelineEditorView.capability_scope_tool_placeholder')"
                    :aria-label="$t('views.PipelineEditorView.capability_scope_allowed_tools')"
                    class="w-full rounded border border-input bg-background px-2 py-1 text-xs"
                    @keydown.enter.prevent="addCapabilityEntry('allowed_tools')"
                    data-testid="pipeline-editor-scope-tool-input"
                  />
                  <button
                    type="button"
                    class="shrink-0 rounded border border-input bg-background px-2 py-1 text-xs hover:bg-accent"
                    @click="addCapabilityEntry('allowed_tools')"
                    data-testid="pipeline-editor-scope-tool-add"
                  >{{ $t('views.PipelineEditorView.capability_scope_add') }}</button>
                </div>
              </div>
              <!-- Context Scope (free-form list of run_context keys) -->
              <div>
                <div class="text-xs font-medium">{{ $t('views.PipelineEditorView.capability_scope_context_scope') }}</div>
                <div v-if="nodeCapabilityScope.context_scope.length > 0" class="mt-1 flex flex-wrap gap-1">
                  <span v-for="k in nodeCapabilityScope.context_scope" :key="k" class="inline-flex items-center gap-1 rounded bg-muted px-1.5 py-0.5 text-[11px]">
                    {{ k }}
                    <button
                      v-if="!isViewing"
                      type="button"
                      class="text-muted-foreground hover:text-foreground"
                      :aria-label="$t('views.PipelineEditorView.capability_scope_remove_context', { name: k })"
                      :data-testid="`pipeline-editor-scope-context-remove-${k}`"
                      @click="removeCapabilityEntry('context_scope', k)"
                    >&times;</button>
                  </span>
                </div>
                <div v-if="!isViewing" class="mt-1 flex gap-1">
                  <input
                    v-model="capabilityContextInput"
                    :placeholder="$t('views.PipelineEditorView.capability_scope_context_placeholder')"
                    :aria-label="$t('views.PipelineEditorView.capability_scope_context_scope')"
                    class="w-full rounded border border-input bg-background px-2 py-1 text-xs"
                    @keydown.enter.prevent="addCapabilityEntry('context_scope')"
                    data-testid="pipeline-editor-scope-context-input"
                  />
                  <button
                    type="button"
                    class="shrink-0 rounded border border-input bg-background px-2 py-1 text-xs hover:bg-accent"
                    @click="addCapabilityEntry('context_scope')"
                    data-testid="pipeline-editor-scope-context-add"
                  >{{ $t('views.PipelineEditorView.capability_scope_add') }}</button>
                </div>
              </div>
              <!-- Reset to unrestricted -->
              <div v-if="!isViewing" class="flex items-center justify-between gap-2">
                <span v-if="!doesNodeHaveCapabilityScope(selectedNodeData)" class="text-[11px] text-muted-foreground">{{ $t('views.PipelineEditorView.capability_scope_unrestricted') }}</span>
                <button
                  type="button"
                  class="text-[11px] text-indigo-500 hover:text-indigo-400"
                  @click="resetCapabilityScope"
                  data-testid="pipeline-editor-scope-reset"
                >{{ $t('views.PipelineEditorView.capability_scope_reset') }}</button>
              </div>
            </dd>
          </div>
          <!-- Runner (sandbox_agent): template, command, env, context -->
          <template v-if="selectedNodeData.node_type === 'sandbox_agent'">
            <div v-if="selectedNodeData.template_id">
              <dt class="text-muted-foreground text-xs uppercase tracking-wider">{{ $t('views.PipelineEditorView.template') }}</dt>
              <dd class="font-mono text-xs select-all" :title="selectedNodeData.template_id">{{ shortId(selectedNodeData.template_id) }}</dd>
            </div>
            <div data-testid="pipeline-editor-node-commands">
              <dt class="text-muted-foreground text-xs uppercase tracking-wider">{{ $t('views.PipelineEditorView.command') }}</dt>
              <!-- FAR-1812: the command AUTHORING editor is a write affordance;
                   while viewing a historic version the frozen commands render
                   as a plain read-only list (same shape the non-sandbox nodes
                   use below). -->
              <dd v-if="!isViewing" class="mt-1">
                <SandboxCommandsEditor
                  v-model:commands="selectedNodeData.agent_commands"
                  v-model:joiner="selectedNodeData.commands_concatenation_string"
                />
              </dd>
              <dd v-else class="mt-1">
                <ul class="list-inside list-decimal text-xs font-mono text-muted-foreground">
                  <li v-for="(cmd, idx) in selectedNodeData.agent_commands || []" :key="idx">{{ cmd }}</li>
                </ul>
              </dd>
            </div>
            <div v-if="selectedNodeData.timeout_seconds">
              <dt class="text-muted-foreground text-xs uppercase tracking-wider">{{ $t('views.PipelineEditorView.timeout') }}</dt>
              <dd>{{ selectedNodeData.timeout_seconds }}s</dd>
            </div>
            <div v-if="selectedNodeData.stall_timeout_seconds">
              <dt class="text-muted-foreground text-xs uppercase tracking-wider" data-testid="pipeline-editor-stall-timeout-label">{{ $t('views.PipelineEditorView.stall_timeout') }}</dt>
              <dd data-testid="pipeline-editor-stall-timeout-value">{{ selectedNodeData.stall_timeout_seconds }}s</dd>
            </div>
            <div>
              <dt class="text-muted-foreground text-xs uppercase tracking-wider" data-testid="pipeline-editor-heartbeat-label">{{ $t('views.PipelineEditorView.heartbeat') }}</dt>
              <dd data-testid="pipeline-editor-heartbeat-value">{{ selectedNodeData.enable_heartbeat === false ? $t('views.PipelineEditorView.disabled') : $t('views.PipelineEditorView.enabled') }}</dd>
            </div>
            <!-- FAR-1273: explicit one-PR-per-run guard flag (sandbox_agent only) -->
            <div data-testid="pipeline-editor-single-pr-per-run">
              <dt class="text-muted-foreground text-xs uppercase tracking-wider" data-testid="pipeline-editor-single-pr-per-run-label">{{ $t('views.PipelineEditorView.single_pr_per_run') }}</dt>
              <dd data-testid="pipeline-editor-single-pr-per-run-value">
                <label class="flex items-start gap-2 text-xs">
                  <input
                    v-model="singlePrPerRun"
                    type="checkbox"
                    :disabled="isViewing"
                    class="mt-0.5 size-3.5 shrink-0 accent-indigo-500"
                    :aria-label="$t('views.PipelineEditorView.single_pr_per_run')"
                    aria-describedby="single-pr-per-run-help"
                    data-testid="pipeline-editor-single-pr-per-run-toggle"
                  />
                  <span id="single-pr-per-run-help" class="text-muted-foreground">{{ $t('views.PipelineEditorView.single_pr_per_run_help') }}</span>
                </label>
              </dd>
            </div>
            <div v-if="selectedNodeData.watch_log_path">
              <dt class="text-muted-foreground text-xs uppercase tracking-wider" data-testid="pipeline-editor-watch-log-path-label">{{ $t('views.PipelineEditorView.watch_log_path') }}</dt>
              <dd class="font-mono text-xs break-all" data-testid="pipeline-editor-watch-log-path-value">{{ selectedNodeData.watch_log_path }}</dd>
            </div>
            <div v-if="selectedNodeData.stdout_percentage_delta != null">
              <dt class="text-muted-foreground text-xs uppercase tracking-wider" data-testid="pipeline-editor-stdout-delta-label">{{ $t('views.PipelineEditorView.stdout_delta') }}</dt>
              <dd data-testid="pipeline-editor-stdout-delta-value">{{ selectedNodeData.stdout_percentage_delta }}</dd>
            </div>
            <div v-if="selectedNodeData.watch_globs && selectedNodeData.watch_globs.length > 0">
              <dt class="text-muted-foreground text-xs uppercase tracking-wider" data-testid="pipeline-editor-watch-globs-label">{{ $t('views.PipelineEditorView.watch_globs') }}</dt>
              <dd class="font-mono text-[10px] break-all" data-testid="pipeline-editor-watch-globs-value">{{ selectedNodeData.watch_globs.join(', ') }}</dd>
            </div>
            <div v-if="selectedNodeData.env_vars && Object.keys(selectedNodeData.env_vars).length > 0">
              <dt class="text-muted-foreground text-xs uppercase tracking-wider">{{ $t('views.PipelineEditorView.env_vars') }}</dt>
              <dd class="font-mono text-[10px] break-all">{{ Object.keys(selectedNodeData.env_vars).join(', ') }}</dd>
            </div>
            <!-- FAR-1812: runner bindings WRITE the LIVE agent (PUT) and READ
                 its live rows — the snapshot freezes only the GRAPH, so
                 there is no frozen binding state a read-only mode could
                 show. While viewing the component is ABSENT, not disabled:
                 no Save, no add/remove rows, and no live-state read either
                 (the frozen view must not present live agent config as if it
                 were frozen). -->
            <AgentRunnerBindings v-if="!isViewing" :agent-id="selectedNodeData.agent_id" />
            <div v-if="selectedNodeData.context_files && Object.keys(selectedNodeData.context_files).length > 0">
              <dt class="text-muted-foreground text-xs uppercase tracking-wider">{{ $t('views.PipelineEditorView.context_files') }}</dt>
              <dd><ul class="list-inside list-disc text-xs text-muted-foreground"><li v-for="(content, fpath) in selectedNodeData.context_files" :key="fpath">{{ fpath }} <span class="text-[10px] opacity-60">({{ content.length }} bytes)</span></li></ul></dd>
            </div>
            <div v-if="selectedNodeData.agent_prompt">
              <dt class="text-muted-foreground text-xs uppercase tracking-wider">{{ $t('views.PipelineEditorView.prompt') }}</dt>
              <dd class="text-xs text-muted-foreground italic whitespace-pre-wrap max-h-32 overflow-y-auto">{{ selectedNodeData.agent_prompt.substring(0, 300) }}{{ selectedNodeData.agent_prompt.length > 300 ? '...' : '' }}</dd>
            </div>
          </template>
          <!-- Non-sandbox nodes: commands are read-only. The authoring editor is
               sandbox-only — a command typed on an agent node would flow into the
               graph save and FAR-488a would then overwrite the bound Agent's row
               command. What the graph already carries (the bound Agent's
               agent_commands list) is displayed, never edited. The scalar
               agent_command field was removed in FAR-820. -->
          <div
            v-if="selectedNodeData.node_type !== 'sandbox_agent' && selectedNodeData.agent_commands && selectedNodeData.agent_commands.length > 0"
            data-testid="pipeline-editor-node-commands-readonly"
          >
            <dt class="text-muted-foreground text-xs uppercase tracking-wider">{{ $t('views.PipelineEditorView.commands') }}</dt>
            <dd>
              <ul class="list-inside list-decimal text-xs font-mono text-muted-foreground">
                <li v-for="(cmd, idx) in selectedNodeData.agent_commands" :key="idx">{{ cmd }}</li>
              </ul>
              <div v-if="selectedNodeData.commands_concatenation_string" class="text-[10px] text-muted-foreground mt-1">
                {{ $t('views.PipelineEditorView.commands_concatenated_with') }} <code class="font-mono">{{ selectedNodeData.commands_concatenation_string }}</code>
              </div>
            </dd>
          </div>
          <!-- Lifecycle maps -->
          <div v-if="linkedLifecycleMaps.length > 0">
            <dt class="text-muted-foreground text-xs uppercase tracking-wider">{{ $t('views.PipelineEditorView.lifecycle_maps') }}</dt>
            <dd>
              <div v-for="map in linkedLifecycleMaps" :key="map.id" class="flex items-center gap-1">
                <router-link :to="`/lifecycle-maps/${map.id}`" class="text-xs text-indigo-500 hover:text-indigo-400">
                  {{ map.name }}
                </router-link>
              </div>
            </dd>
          </div>
        </dl>
        <div v-if="!isViewing" class="mt-6 space-y-2">
          <Button v-if="selectedNodeData.node_type === 'manual'" type="button" class="w-full" data-testid="pipeline-editor-convert-to-agent" @click="openAgentPicker">
            {{ $t('views.PipelineEditorView.convert_to_agent') }}
          </Button>
          <button
            v-if="selectedNodeData.node_type === 'agent'"
            type="button"
            data-testid="pipeline-editor-revert-to-manual"
            class="inline-flex w-full items-center justify-center rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
            @click="openRevertDialog"
          >
            {{ $t('views.PipelineEditorView.revert_to_manual') }}
          </button>
        </div>
      </aside>
      <!-- Edge Properties Panel (with HITL gate config). Mobile (FAR-1841):
           same overlay-sheet treatment as the node panel above — a flex
           sibling of this width crushes the canvas to 0x0 on a phone. Like the
           node panel it carries exactly ONE Close affordance, the conventional
           header X (mobile-only; desktop is a docked panel dismissed by
           clicking the canvas, matching the node panel) — the duplicated
           bottom Close was removed in FAR-1861. Below the desktop breakpoint
           it is a modal dialog with the same semantics as the node sheet. -->
      <!-- eslint-disable-next-line vuejs-accessibility/no-static-element-interactions --><!-- mobile edge sheet: a role="dialog" container whose Escape/Tab focus-trap needs the keydown handler -->
      <aside
        v-if="selectedEdgeData"
        ref="edgePanelRef"
        v-bind="sheetDialogAttrs(EDGE_SHEET_TITLE_ID)"
        class="w-96 overflow-y-auto border-l bg-card p-4 max-md:absolute max-md:inset-0 max-md:z-40 max-md:w-auto max-md:border-l-0"
        @keydown="onSheetKeydown"
      >
        <div class="mb-4 flex items-center justify-between">
          <h2 :id="EDGE_SHEET_TITLE_ID" class="text-base font-semibold">{{ $t('views.PipelineEditorView.edge_properties') }}</h2>
          <button
            type="button"
            class="rounded p-1 text-muted-foreground hover:bg-accent hover:text-foreground md:hidden"
            :aria-label="$t('common.close')"
            data-testid="pipeline-editor-edge-props-close"
            @click="selectedEdgeData = null"
          >
            <XIcon class="h-4 w-4" aria-hidden="true" />
          </button>
        </div>
        <dl class="space-y-3 text-sm">
          <div>
            <dt class="text-muted-foreground">{{ $t('views.PipelineEditorView.source') }}</dt>
            <dd class="font-mono text-xs">{{ shortId(selectedEdgeData.source_node_id) }}</dd>
          </div>
          <div>
            <dt class="text-muted-foreground">{{ $t('views.PipelineEditorView.target') }}</dt>
            <dd class="font-mono text-xs">{{ shortId(selectedEdgeData.target_node_id) }}</dd>
          </div>
          <div>
            <dt class="text-muted-foreground">{{ $t('views.PipelineEditorView.type_label') }}</dt>
            <dd>
              <Select
  :aria-label="$t('views.PipelineEditorView.edge_type')"
  v-model="edgeForm.edge_type"
  :disabled="isViewing"
  append-to="body"
  :placeholder="$t('views.PipelineEditorView.normal')"
  class="w-full"
  :options="[{ value: 'normal', label: $t('views.PipelineEditorView.normal') }, { value: 'reject', label: $t('views.PipelineEditorView.reject') }, { value: 'conditional', label: $t('views.PipelineEditorView.conditional') }, { value: 'loop', label: $t('views.PipelineEditorView.loop') }, { value: 'llm', label: $t('views.PipelineEditorView.llm_routing') }]"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
            </dd>
          </div>
          <div>
            <label for="pipelineeditorview-field-16" class="text-muted-foreground">{{ $t('views.PipelineEditorView.condition_expression') }}</label>
            <dd>
              <input
                id="pipelineeditorview-field-16"
                v-model="edgeForm.condition_expression"
                :readonly="isViewing"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm font-mono"
                :placeholder="$t('views.PipelineEditorView.jmespath_expression_placeholder')"
                :disabled="edgeForm.edge_type !== 'conditional' && edgeForm.edge_type !== 'loop'"
              />
            </dd>
          </div>
          <div v-if="edgeForm.edge_type === 'loop'">
            <dt class="text-muted-foreground">{{ $t('views.PipelineEditorView.max_iterations') }}</dt>
            <dd>
              <input
                v-model.number="edgeForm.max_iterations"
                type="number"
                min="0"
                :disabled="isViewing"
                :aria-label="$t('views.PipelineEditorView.max_iterations')"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                :placeholder="$t('views.PipelineEditorView.max_iterations_placeholder')"
              />
              <p class="mt-1 text-xs text-muted-foreground">{{ $t('views.PipelineEditorView.max_iterations_hint') }}</p>
            </dd>
          </div>
          <div v-if="edgeForm.edge_type === 'llm'">
            <label for="pipelineeditorview-field-17" class="text-muted-foreground">{{ $t('views.PipelineEditorView.routing_label') }}</label>
            <dd>
              <input
                id="pipelineeditorview-field-17"
                v-model="edgeForm.routing_label"
                :readonly="isViewing"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm font-mono"
                :placeholder="$t('views.PipelineEditorView.routing_label_placeholder')"
              />
              <p class="mt-1 text-xs text-muted-foreground">{{ $t('views.PipelineEditorView.the_llm_uses_this_label_to_select_this_path_must_be_unique_among_outgoing_edges') }}</p>
            </dd>
          </div>
        </dl>
        <hr class="my-4 border-t" />
        <div class="flex items-center justify-between">
          <h3 class="text-sm font-semibold">{{ $t('views.PipelineEditorView.hitl_review') }}</h3>
          <label for="pipelineeditorview-field-15" class="inline-flex cursor-pointer items-center">
            <input id="pipelineeditorview-field-15"
              v-model="edgeForm.hitl_enabled"
              type="checkbox"
              :disabled="isViewing"
              class="h-4 w-4 rounded border-gray-300 text-primary-text focus:ring-primary"
            />
            <span class="ml-2 text-xs text-muted-foreground">{{ $t('views.PipelineEditorView.enabled') }}</span>
          </label>
        </div>
        <div v-if="edgeForm.hitl_enabled" class="mt-4 space-y-4">
          <div>
            <label for="pipelineeditorview-field-14" class="mb-1 block text-xs font-medium text-muted-foreground">{{ $t('views.PipelineEditorView.label') }}</label>
            <input id="pipelineeditorview-field-14"
              v-model="edgeForm.label"
              :readonly="isViewing"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
              :placeholder="$t('views.PipelineEditorView.hitl_label_placeholder')"
            />
          </div>
          <div>
            <label for="pipelineeditorview-field-13" class="mb-1 block text-xs font-medium text-muted-foreground">
              {{ $t('views.PipelineEditorView.description') }} <span class="text-destructive">*</span>
            </label>
            <textarea id="pipelineeditorview-field-13"
              v-model="edgeForm.description"
              :readonly="isViewing"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
              :placeholder="$t('views.PipelineEditorView.hitl_description_placeholder')"
              :aria-invalid="hitlDescriptionTooShort ? 'true' : undefined"
              rows="2"
            />
            <p class="mt-1 text-xs text-muted-foreground">{{ $t('views.PipelineEditorView.hitl_description_hint') }}</p>
          </div>
          <div>
            <label for="pipelineeditorview-field-12" class="mb-1 block text-xs font-medium text-muted-foreground">{{ $t('views.PipelineEditorView.claim_expiry_minutes') }}</label>
            <input id="pipelineeditorview-field-12"
              v-model.number="edgeForm.claim_expiry_minutes"
              type="number"
              min="1"
              max="1440"
              :disabled="isViewing"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
            />
          </div>
          <div class="flex items-center gap-2">
            <input :aria-label="$t('views.PipelineEditorView.aria_checkbox')"
              v-model="edgeForm.human_only"
              type="checkbox"
              :disabled="isViewing"
              class="h-4 w-4 rounded border-gray-300 text-primary-text focus:ring-primary"
            />
            <span class="text-xs text-muted-foreground">{{ $t('views.PipelineEditorView.human_only_block_llm_auto_approval') }}</span>
          </div>
          <p class="mt-1 text-xs text-muted-foreground" data-testid="pipeline-editor-human-only-credential-note">
            {{ $t('views.PipelineEditorView.human_only_machine_credential_note') }}
          </p>
          <!-- FAR-1257 advisory (NON-BLOCKING): a human-only gate nobody can be
               notified about will simply age out at the review window and the
               run gets cancelled. Warn, never gate the save. aria-live (not
               role="status") so assistive tech announces it without SonarCloud
               Web:S6819. -->
          <p
            v-if="ownerlessGateAdvisory"
            class="rounded-lg border border-warning/40 bg-warning/10 p-2 text-xs text-warning-text"
            aria-live="polite"
            data-testid="pipeline-editor-ownerless-gate-advisory"
          >
            {{ $t('views.PipelineEditorView.ownerless_gate_advisory') }}
          </p>
          <hr class="border-t" />
          <div>
            <label for="pipelineeditorview-field-11" class="mb-1 block text-xs font-medium text-muted-foreground">{{ $t('views.PipelineEditorView.condition_type') }}</label>
            <Select
  :aria-label="$t('views.PipelineEditorView.condition_type')"
  v-model="edgeForm.condition_type"
  :disabled="isViewing"
  append-to="body"
  :placeholder="$t('views.PipelineEditorView.none_always_gate')"
  class="w-full"
  :options="[{ value: 'none', label: $t('views.PipelineEditorView.none_always_gate') }, { value: 'jmespath', label: $t('views.PipelineEditorView.jmespath_expression') }, { value: 'eval', label: $t('views.PipelineEditorView.eval_reference') }]"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
          </div>
          <div v-if="edgeForm.condition_type === 'jmespath'">
            <label for="pipelineeditorview-field-10" class="mb-1 block text-xs font-medium text-muted-foreground">{{ $t('views.PipelineEditorView.jmespath_condition') }}</label>
            <input id="pipelineeditorview-field-10"
              v-model="edgeForm.condition"
              :readonly="isViewing"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm font-mono"
              :placeholder="$t('views.PipelineEditorView.jmespath_condition_placeholder')"
            />
            <p class="mt-1 text-[10px] text-muted-foreground">
              {{ $t('views.PipelineEditorView.jmespath_condition_hint') }}
            </p>
          </div>
          <div v-if="edgeForm.condition_type === 'eval'" class="space-y-3">
            <div>
              <label for="pipelineeditorview-field-9" class="mb-1 block text-xs font-medium text-muted-foreground">{{ $t('views.PipelineEditorView.eval_name') }}</label>
              <input id="pipelineeditorview-field-9"
                v-model="edgeForm.eval_name"
                :readonly="isViewing"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm font-mono"
                :placeholder="$t('views.PipelineEditorView.eval_name_placeholder')"
              />
            </div>
            <div class="flex gap-2">
              <div class="flex-1">
                <label for="pipelineeditorview-field-8" class="mb-1 block text-xs font-medium text-muted-foreground">{{ $t('views.PipelineEditorView.threshold') }}</label>
                <input id="pipelineeditorview-field-8"
                  v-model.number="edgeForm.eval_threshold"
                  type="number"
                  min="0"
                  max="1"
                  step="0.01"
                  :disabled="isViewing"
                  class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                />
              </div>
              <div class="flex-1">
                <label for="pipelineeditorview-field-7" class="mb-1 block text-xs font-medium text-muted-foreground">{{ $t('views.PipelineEditorView.operator') }}</label>
                <Select
  :aria-label="$t('views.PipelineEditorView.operator')"
  v-model="edgeForm.eval_operator"
  :disabled="isViewing"
  append-to="body"
  :placeholder="$t('views.PipelineEditorView.eval_operator_placeholder')"
  class="w-full"
  :options="[{ value: 'lt', label: $t('views.PipelineEditorView.eval_operator_lt') }, { value: 'gt', label: $t('views.PipelineEditorView.eval_operator_gt') }, { value: 'lte', label: $t('views.PipelineEditorView.eval_operator_lte') }, { value: 'gte', label: $t('views.PipelineEditorView.eval_operator_gte') }, { value: 'eq', label: $t('views.PipelineEditorView.eval_operator_eq') }, { value: 'neq', label: $t('views.PipelineEditorView.eval_operator_neq') }]"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
              </div>
            </div>
            <p class="mt-1 text-[10px] text-muted-foreground">
              {{ $t('views.PipelineEditorView.eval_condition_hint') }}
            </p>
          </div>
          <div class="flex gap-2 pt-2">
            <Button v-if="!isViewing" type="button" data-testid="pipeline-editor-save-edge" class="flex-1" :disabled="savingEdge" @click="saveEdgeConfig">
              {{ savingEdge ? $t('views.PipelineEditorView.saving') : $t('views.PipelineEditorView.save_edge') }}
            </Button>
          </div>
          <!-- retry = click Save Edge again; no separate loader -->
          <ErrorAlert v-if="edgeSaveError" :message="edgeSaveError" :retryable="false" :compact="true" />
        </div>
      </aside>
    </template>
    <FormDialog
      :open="showAgentPicker"
      @update:open="showAgentPicker = false"
      :title="$t('views.PipelineEditorView.convert_to_agent')"
      :confirmText="$t('views.PipelineEditorView.convert')"
      :confirmDisabled="!canConvert"
      @confirm="convertToAgent"
    >
      <div class="space-y-4">
          <div>
            <label for="pipelineeditorview-field-6" class="mb-1 block text-sm font-medium">{{ $t('views.PipelineEditorView.agent_select_label') }}</label>
            <Select
  :aria-label="$t('views.PipelineEditorView.agent_select_label')"
  :title="$t('views.PipelineEditorView.agent_select_hint')"
  v-model="pickerAgentId"
  @update:model-value="onAgentChange"
  :placeholder="$t('views.PipelineEditorView.select_agent_placeholder')"
  data-testid="pipeline-editor-agent-select"
  class="w-full"
  :options="[{ value: '__all__', label: $t('common.none') }, ...agents.map(a => ({ value: a.id, label: a.name }))]"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
          </div>
          <div v-if="selectedAgent">
            <label for="pipelineeditorview-field-5" class="mb-1 block text-sm font-medium">{{ $t('views.PipelineEditorView.connector') }}</label>
            <Select
  :aria-label="$t('views.PipelineEditorView.connector')"
  v-model="pickerConnectorId"
  :placeholder="$t('views.PipelineEditorView.select_connector_placeholder')"
  data-testid="pipeline-editor-connector-select"
  class="w-full"
  :options="[{ value: '__all__', label: $t('common.none') }, ...eligibleConnectors.map(c => ({ value: c.id, label: c.name + '(' + c.connector_type_id + ')' }))]"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
          </div>
          <div v-if="selectedAgent">
            <span class="mb-1 block text-sm font-medium">{{ $t('views.PipelineEditorView.model_backend_label') }}</span>
            <div class="rounded-lg border bg-muted px-3 py-2 text-sm">
              {{ modelBackendName || $t('views.PipelineEditorView.loading') }}
            </div>
          </div>
          <div v-if="selectedAgent" class="rounded-lg border bg-muted p-3 text-sm">
            <p class="text-xs text-muted-foreground">{{ $t('views.PipelineEditorView.schema') }}</p>
            <p class="mt-0.5 font-medium">{{ $t('views.PipelineEditorView.input_label') }} {{ agentSchemaName(selectedAgent, 'input') }}</p>
            <p class="font-medium">{{ $t('views.PipelineEditorView.output_label') }} {{ agentSchemaName(selectedAgent, 'output') }}</p>
          </div>
          <!-- retry = click Convert again; no separate loader -->
          <ErrorAlert v-if="convertError" :message="convertError" :retryable="false" :compact="true" />
      </div>
    </FormDialog>
    <FormDialog
      :open="showRevertDialog"
      @update:open="showRevertDialog = false"
      :title="$t('views.PipelineEditorView.revert_dialog_title')"
      :confirmText="$t('views.PipelineEditorView.revert')"
      :confirmDisabled="!revertSnapshotId"
      @confirm="revertToManual"
    >
      <div v-if="revertLoading" class="flex items-center justify-center py-8">
        <div class="h-6 w-6 animate-spin rounded-full border-4 border-primary border-t-transparent" />
      </div>
      <div v-else class="space-y-4">
        <p class="text-sm text-muted-foreground">
          {{ $t('views.PipelineEditorView.select_snapshot_description') }}
        </p>
        <div>
          <label for="pipelineeditorview-field-4" class="mb-1 block text-sm font-medium">{{ $t('views.PipelineEditorView.snapshot_label') }}</label>
          <Select
  :aria-label="$t('views.PipelineEditorView.snapshot_label')"
  v-model="revertSnapshotId"
  :placeholder="$t('views.PipelineEditorView.select_snapshot_placeholder')"
  data-testid="pipeline-editor-snapshot-select"
  class="w-full"
  :options="[{ value: '__all__', label: $t('common.none') }, ...snapshots.map(s => ({ value: s.id, label: 'v' + s.snapshot_version + (s.tag ? ` — ${s.tag}` : '') }))]"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
        </div>
        <!-- retry = click Revert again; no separate loader -->
        <ErrorAlert v-if="revertError" :message="revertError" :retryable="false" :compact="true" />
      </div>
    </FormDialog>
    <FormDialog
      :open="showSaveAsComposite"
      @update:open="showSaveAsComposite = false"
      :title="$t('views.PipelineEditorView.save_as_composite')"
      :confirmText="$t('views.PipelineEditorView.save')"
      :confirmDisabled="!saveAsName || saveAsSelectedNodeIds.length === 0 || saving"
      :loading="saving"
      @confirm="handleSaveAsComposite"
    >
      <p class="mb-4 text-sm text-muted-foreground">
        {{ $t('views.PipelineEditorView.save_as_composite_description') }}
      </p>
      <div class="space-y-4">
        <div>
          <label for="pipelineeditorview-field-3" class="mb-1 block text-sm font-medium">{{ $t('views.PipelineEditorView.name_required') }}</label>
          <input id="pipelineeditorview-field-3"
            v-model="saveAsName"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
            :placeholder="$t('views.PipelineEditorView.composite_name_placeholder')"
          />
        </div>
        <div>
          <label for="pipelineeditorview-field-2" class="mb-1 block text-sm font-medium">{{ $t('views.PipelineEditorView.description') }}</label>
          <textarea id="pipelineeditorview-field-2"
            v-model="saveAsDescription"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
            rows="3"
            :placeholder="$t('views.PipelineEditorView.optional_description')"
          />
        </div>
        <div>
          <span class="mb-1 block text-sm font-medium">{{ $t('views.PipelineEditorView.selected_nodes') }}</span>
          <div class="max-h-32 space-y-1 overflow-y-auto">
            <label
              v-for="node in rawNodes"
              :key="node.id"
              class="flex items-center gap-2 rounded-md bg-muted/30 px-3 py-1.5 text-sm"
            >
              <input
                v-model="saveAsSelectedNodeIds"
                type="checkbox"
                :value="node.id"
                class="h-4 w-4 rounded border-gray-300 text-indigo-500 focus:ring-indigo-500"
              />
              <span>{{ node.label || $t('views.PipelineEditorView.node_fallback_label', { id: shortId(node.id) }) }}</span>
            </label>
          </div>
        </div>
        <!-- retry = click Save again; no separate loader -->
        <ErrorAlert v-if="saveAsError" :message="saveAsError" :retryable="false" :compact="true" />
      </div>
    </FormDialog>
    <FormDialog
      :open="showRenameDialog"
      @update:open="showRenameDialog = false"
      :title="$t('views.PipelineEditorView.rename_pipeline')"
      :confirmText="$t('views.PipelineEditorView.save')"
      :confirmDisabled="!renameName.trim() || renaming"
      :loading="renaming"
      @confirm="handleRename"
    >
      <div class="space-y-4">
        <div>
          <label for="pipelineeditorview-field-1" class="mb-1 block text-sm font-medium">{{ $t('views.PipelineEditorView.name') }}</label>
          <input id="pipelineeditorview-field-1"
            v-model="renameName"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
            :placeholder="$t('views.PipelineEditorView.pipeline_name_placeholder')"
            @keyup.enter="handleRename"
          />
        </div>
        <!-- retry = click Save again; no separate loader -->
        <ErrorAlert v-if="renameError" :message="renameError" :retryable="false" :compact="true" />
      </div>
    </FormDialog>
    <FormDialog
      :open="showDeleteConfirm"
      @update:open="showDeleteConfirm = false"
      :title="$t('views.PipelineEditorView.delete_pipeline_title')"
      :confirmText="$t('common.delete')"
      @confirm="handleDelete"
    >
      <p class="mb-4 text-sm text-muted-foreground">
        {{ $t('views.PipelineEditorView.delete_pipeline_confirm') }}
      </p>
      <!-- retry = click Delete again; no separate loader -->
      <ErrorAlert v-if="deleteError" :message="deleteError" :retryable="false" class="mb-4" :compact="true" />
    </FormDialog>

    <!-- FAR-1816: the SHARED restore dialog — used by the historic banner AND
         the version timeline's "Restore this version" action. It shows the
         old-vs-latest change summary (from the diff endpoint) and the real
         consequences of restoring before the user confirms. Confirmation is a
         plain confirm (no typed phrase): restore is reversible because it
         creates a NEW version rather than rewriting history. The server's own
         guards (HITL-gate weakening, guardrail strip, connector-team mismatch)
         remain the backstop and their refusals are surfaced as human copy. -->
    <FormDialog
      :open="showRestoreConfirm"
      @update:open="showRestoreConfirm = false"
      :title="$t('views.PipelineEditorView.historic_restore_dialog_title')"
      :confirmText="$t('views.PipelineEditorView.historic_restore_confirm')"
      :loading="restoreLoading"
      @confirm="confirmRestore"
    >
      <p class="text-sm text-muted-foreground">
        {{ $t('views.PipelineEditorView.historic_restore_dialog_intro', { version: restoreTargetVersion ?? '' }) }}
      </p>

      <!-- Change summary: what restoring this version does to the live config.
           C2: EVERY reachable state renders something truthful. The box never
           shows its title with nothing under it, and "no changes" is asserted
           only from a real diff every class of which is empty. -->
      <div class="mt-3 rounded-md border bg-muted/30 p-2" data-testid="pipeline-editor-restore-diff">
        <p class="text-xs font-medium">{{ $t('views.PipelineEditorView.historic_restore_diff_title') }}</p>
        <p v-if="restoreDiffLoading" class="mt-1 text-[11px] text-muted-foreground">
          {{ $t('views.PipelineEditorView.historic_restore_diff_loading') }}
        </p>
        <div v-else-if="restoreDiffError" class="mt-1 space-y-1">
          <p role="alert" class="text-[11px] text-muted-foreground" data-testid="pipeline-editor-restore-diff-error">
            {{ $t('views.PipelineEditorView.historic_restore_diff_failed') }}
          </p>
          <Button
            size="small"
            class="text-xs"
            data-testid="pipeline-editor-restore-diff-retry"
            @click="loadRestoreDiff"
          >
            {{ $t('views.PipelineEditorView.historic_restore_diff_retry') }}
          </Button>
        </div>
        <p
          v-else-if="restoreDiffIsCurrent"
          class="mt-1 text-[11px] text-muted-foreground"
          data-testid="pipeline-editor-restore-diff-current"
        >
          {{ $t('views.PipelineEditorView.historic_restore_diff_is_current') }}
        </p>
        <div v-else-if="restoreDiffNoBaseline" class="mt-1 space-y-1">
          <p class="text-[11px] text-muted-foreground" data-testid="pipeline-editor-restore-diff-no-baseline">
            {{ $t('views.PipelineEditorView.historic_restore_diff_no_baseline') }}
          </p>
          <Button
            size="small"
            class="text-xs"
            data-testid="pipeline-editor-restore-diff-reload"
            @click="loadRestoreDiff"
          >
            {{ $t('views.PipelineEditorView.historic_restore_diff_retry') }}
          </Button>
        </div>
        <template v-else-if="restoreDiffSummary">
          <p
            v-if="!restoreDiffSummary.hasChanges"
            class="mt-1 text-[11px] text-muted-foreground"
          >
            {{ $t('views.PipelineEditorView.historic_restore_diff_no_changes') }}
          </p>
          <ul v-else class="mt-1 list-disc space-y-0.5 pl-4 text-[11px] text-muted-foreground">
            <li v-if="restoreDiffSummary.nodesRestored > 0">
              {{ $t('views.PipelineEditorView.historic_restore_diff_nodes_restored', restoreDiffSummary.nodesRestored) }}
            </li>
            <li v-if="restoreDiffSummary.nodesRemoved > 0">
              {{ $t('views.PipelineEditorView.historic_restore_diff_nodes_removed', restoreDiffSummary.nodesRemoved) }}
            </li>
            <li v-if="restoreDiffSummary.nodesModified > 0">
              {{ $t('views.PipelineEditorView.historic_restore_diff_nodes_modified', restoreDiffSummary.nodesModified) }}
            </li>
            <li v-if="restoreDiffSummary.edgesRestored > 0">
              {{ $t('views.PipelineEditorView.historic_restore_diff_edges_restored', restoreDiffSummary.edgesRestored) }}
            </li>
            <li v-if="restoreDiffSummary.edgesRemoved > 0">
              {{ $t('views.PipelineEditorView.historic_restore_diff_edges_removed', restoreDiffSummary.edgesRemoved) }}
            </li>
            <li v-if="restoreDiffSummary.edgesModified > 0">
              {{ $t('views.PipelineEditorView.historic_restore_diff_edges_modified', restoreDiffSummary.edgesModified) }}
            </li>
            <li v-if="restoreDiffSummary.impacted > 0">
              {{ $t('views.PipelineEditorView.historic_restore_diff_impacted', restoreDiffSummary.impacted) }}
            </li>
            <li
              v-for="(b, i) in restoreDiffSummary.breaking"
              :key="i"
              :class="isBlockingSeverity(b.severity) ? 'text-destructive' : 'text-warning-text'"
            >
              {{ $t('views.PipelineEditorView.historic_restore_diff_breaking', { severity: restoreSeverityLabel(b.severity), reason: b.reason }) }}
            </li>
          </ul>
        </template>
        <!-- Defensive catch-all: reachable only if a state transition left the
             model in no state at all, but it guarantees the box is never bare. -->
        <p v-else class="mt-1 text-[11px] text-muted-foreground">
          {{ $t('views.PipelineEditorView.historic_restore_diff_unavailable') }}
        </p>
      </div>

      <!-- The real risks of restoring, in plain language. -->
      <ul class="mt-3 list-disc space-y-1 pl-4 text-xs text-muted-foreground">
        <li>{{ $t('views.PipelineEditorView.historic_restore_effect_runs') }}</li>
        <li>{{ $t('views.PipelineEditorView.historic_restore_effect_edits') }}</li>
        <li>{{ $t('views.PipelineEditorView.historic_restore_effect_history') }}</li>
        <li>{{ $t('views.PipelineEditorView.historic_restore_effect_in_progress') }}</li>
      </ul>
      <p class="mt-2 text-xs text-muted-foreground">
        {{ $t('views.PipelineEditorView.historic_restore_blocked_note') }}
      </p>
      <ErrorAlert v-if="restoreError" :message="restoreError" :retryable="false" :compact="true" class="mt-3" />
    </FormDialog>
    <!-- FAR-1812: unsaved-changes guard — never silently discard live edits
         when the user navigates into a historic version. -->
    <FormDialog
      :open="unsavedConfirmVisible"
      @update:open="resolveUnsavedConfirm(false)"
      :title="$t('views.PipelineEditorView.unsaved_changes_title')"
      :confirmText="$t('views.PipelineEditorView.unsaved_changes_continue')"
      @confirm="resolveUnsavedConfirm(true)"
    >
      <p class="text-sm text-muted-foreground">{{ $t('views.PipelineEditorView.unsaved_changes_body') }}</p>
    </FormDialog>

    <PipelineSnapshotTimeline
      v-if="showVersionTimeline"
      ref="timelineRef"
      :pipeline-id="pipelineId"
      :current-version="currentVersionSnapshot"
      @close="showVersionTimeline = false"
      @view="requestViewSnapshot"
      @restore="openRestoreConfirm"
    />
  </div>
</template>
<script setup lang="ts">
import { ref, computed, reactive, watch, nextTick, onMounted, onBeforeUnmount } from 'vue'
import { useMediaQuery } from '@vueuse/core'
import { useI18n } from 'vue-i18n'
import { useRoute, useRouter } from 'vue-router'
import { VueFlow, useVueFlow, Position } from '@vue-flow/core'
import { Background } from '@vue-flow/background'
import '@vue-flow/core/dist/style.css'
import '@vue-flow/core/dist/theme-default.css'
import { useDataFetch } from '../composables/useDataFetch'
import { trapTabInElement } from '../composables/useFocusTrap'
import { formatApiError, throwOnError } from '../lib/api/formatError'
import { usePlanStore } from '../stores/planStore'

import FormDialog from '../components/shared/FormDialog.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import FolderListError from '../components/pipelines/FolderListError.vue'
import FlowControls from '../components/shared/FlowControls.vue'
import PipelineSnapshotTimeline from '../components/pipeline/PipelineSnapshotTimeline.vue'
import PipelineSnapshotCompare from '../components/pipeline/PipelineSnapshotCompare.vue'
import PipelineVersionChip from '../components/pipeline/PipelineVersionChip.vue'
import SandboxCommandsEditor from '../components/pipeline/SandboxCommandsEditor.vue'
import SnapshotLockCard from '../components/pipeline/SnapshotLockCard.vue'
import ProgressiveAutonomyPanel from '../components/pipeline/ProgressiveAutonomyPanel.vue'
import AgentRunnerBindings from '../components/agent/AgentRunnerBindings.vue'
import { shortId } from '../utils/format'
import {
  isBlockingSeverity,
  readBreakingChanges,
  readChangeClassCounts,
  readImpactedNodes,
  severityLabel as resolveSeverityLabel,
  type SnapshotBreakingChange,
  type SnapshotDiffPayload,
} from '../lib/pipelineSnapshotDiff'
import { formatDateShort } from '../lib/formatDate'
import { withTimeout } from '../lib/requestTimeout'
import { CANVAS_EDGE_STROKE } from '../constants/canvas'
import { api } from '../lib/api/client'
import type { components } from '../lib/api/schema'
import { invalidFolderListMessage, isFolderList, type FolderItem } from '../lib/api/folders'
import type { LocationQueryRaw } from 'vue-router'
import { useApi } from '../composables/useApi'
import { useCurrentUser } from '../composables/useCurrentUser'
import { useProductAnalytics } from '../composables/useProductAnalytics'
import { ANALYTICS_EVENTS } from '../composables/productAnalyticsEvents'
import { useEnvironmentProfilesStore } from '../stores/environmentProfiles'
import { runnerTierLabelKeyForProvider } from '../lib/runnerTiers'
import { currentSubstantiveVersion, NON_VERSION_CREATED_KINDS, SNAPSHOT_PAGE_SIZE } from '../lib/pipelineVersion'
import Button from 'primevue/button'
import Select from '../components/shared/AppSelect.vue'
import { Pencil as PencilIcon, LoaderCircle as LoaderCircleIcon, Download as DownloadIcon, Play as PlayIcon, Plus as PlusIcon, Maximize2 as Maximize2Icon, X as XIcon, ExternalLink as ExternalLinkIcon } from '@lucide/vue'

// The legacy useApi `post` helper aborted every request after 30s; the typed
// client has no deadline of its own, so the archive/pause toggles keep the
// same bound and still surface an error if the request stalls.
const TOGGLE_REQUEST_TIMEOUT_MS = 30000

const { t } = useI18n()
const { track } = useProductAnalytics()

function nodeTypeBadgeClass(nodeType: string): string {
  if (nodeType === 'manual') return 'badge badge-status-warning'
  if (nodeType === 'router') return 'badge bg-indigo-500/10 text-indigo-600 dark:bg-indigo-900 dark:text-indigo-300'
  if (nodeType === 'hitl') return 'badge bg-rose-500/10 text-rose-600 dark:bg-rose-900 dark:text-rose-300'
  if (nodeType === 'dispatch') return 'badge bg-cyan-500/10 text-cyan-700 dark:bg-cyan-900 dark:text-cyan-300'
  if (nodeType === 'connector') return 'badge bg-teal-500/10 text-teal-700 dark:bg-teal-900 dark:text-teal-300'
  return 'badge badge-status-primary'
}

function nodeTypeLabel(nodeType: string): string {
  if (nodeType === 'manual') return t('views.PipelineEditorView.manual')
  if (nodeType === 'sandbox_agent') return t('views.PipelineEditorView.sandbox_agent')
  if (nodeType === 'router') return t('views.PipelineEditorView.node_router_label')
  if (nodeType === 'hitl') return t('views.PipelineEditorView.node_hitl_label')
  // FAR-1141: a dispatch node must never fall through to the agent label —
  // its work ran outside Modulo, and mislabelling it hides that provenance.
  if (nodeType === 'dispatch') return t('views.PipelineEditorView.node_dispatch_label')
  // FAR-2089: a connector node's work is done by the bound connector, not by an
  // LLM, so it must never fall through to the agent label.
  if (nodeType === 'connector') return t('views.PipelineEditorView.node_connector_label')
  return t('views.PipelineEditorView.node_type_agent')
}
const planStore = usePlanStore()
const route = useRoute()
const router = useRouter()
const pipelineId = route.params.id as string

const rawNodes = ref<any[]>([])
const rawEdges = ref<any[]>([])
const flowNodes = ref<any[]>([])
const flowEdges = ref<any[]>([])

const selectedNodeData = ref<any | null>(null)
// FAR-1273: explicit one-PR-per-run guard flag for sandbox_agent nodes. The
// raw node value may be absent on legacy graphs, so the getter normalises to a
// boolean for the checkbox while the setter writes the field back onto the raw
// node (it then rides the spread-based save payload like every other model
// field).
const singlePrPerRun = computed<boolean>({
  get: () => selectedNodeData.value?.single_pr_per_run === true,
  set: (value: boolean) => {
    if (selectedNodeData.value) selectedNodeData.value.single_pr_per_run = value
  },
})
const selectedEdgeData = ref<any | null>(null)
const showSaveAsDropdown = ref(false)
const saveAsDropdownTriggerRef = ref<HTMLButtonElement | null>(null)
const saveAsDropdownStyle = ref<Record<string, string>>({})
const nodeTypes = { agent: 'agent', manual: 'manual', router: 'router', hitl: 'hitl', dispatch: 'dispatch', connector: 'connector' }
const { fitView, onPaneReady } = useVueFlow()

// Docked-toolbar styling: one height/radius/spacing per action class so the
// menu bar reads as one consistent control set (presentation only).
const btnToolbarBase = 'inline-flex h-7 shrink-0 items-center justify-center gap-1.5 whitespace-nowrap rounded-md px-2.5 text-xs font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50'
const btnToolbarSecondary = `${btnToolbarBase} border border-input bg-background hover:bg-accent`
const btnToolbarPrimary = `${btnToolbarBase} bg-indigo-600 text-white hover:bg-indigo-500`
const btnToolbarDestructive = `${btnToolbarBase} border border-destructive/50 bg-destructive/10 text-destructive-text hover:bg-destructive/20`
const btnToolbarIcon = 'inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground'
const groupDividerClass = 'mx-0.5 hidden h-5 w-px shrink-0 bg-border md:block'

// Fit the graph as soon as the canvas is ready and nodes exist, so the whole
// graph is visible on load without user action. Runs after layout settles
// (nextTick + two animation frames) so the docked toolbar's final height is
// accounted for by the fit.
let initialFitDone = false
function nextFrame(): Promise<void> {
  return new Promise((resolve) => {
    if (typeof requestAnimationFrame === 'function') {
      requestAnimationFrame(() => requestAnimationFrame(() => resolve()))
    } else {
      resolve()
    }
  })
}
async function runInitialFitView() {
  if (initialFitDone || displayNodes.value.length === 0) return
  await nextTick()
  await nextFrame()
  try {
    // fitView resolves false when the viewport has nothing measurable to fit,
    // and is a noop resolving undefined before the viewport initialises, so
    // the guard latches only on a truthy result. Until a fit succeeds,
    // pane-ready and the nodes watch keep re-attempting.
    const ok = await fitView({ padding: 0.1 })
    if (ok) initialFitDone = true
  } catch (error) {
    // fitView signals an unmeasured pane via its resolved value, so a
    // rejection here is a genuine error — surface it instead of swallowing.
    console.warn('[PipelineEditorView] initial fitView failed; retry paths remain armed', error)
  }
}
onPaneReady(() => { void runInitialFitView() })
watch(() => flowNodes.value.length, (len) => { if (len > 0) void runInitialFitView() })

const agents = ref<any[]>([])
const connectors = ref<any[]>([])
const modelBackends = ref<any[]>([])
const schemas = ref<any[]>([])
const snapshots = ref<any[]>([])
// FAR-1895: the newest SUBSTANTIVE version, resolved by a server-filtered
// query (`exclude_created_kind=<NON_VERSION_CREATED_KINDS>` + `exclude_drafts`,
// enough rows to survive the version-0 post-filter). Deliberately separate from
// `snapshots` (the unfiltered catalog page the compare pickers use): the chip
// must not depend on a substantive version happening to fall inside a
// fixed-size page of run-streaked history.
//
// This is an AUXILIARY request. On failure `currentVersionSnapshot` falls back
// to the substantive version in the already-loaded catalog; if that catalog's
// first page has no substantive version, the chip is hidden and the failure is
// surfaced via `versionChipError` / retry. It never serves a STALE version (the
// loader clears `currentVersionSnapshots` on every failure).
const currentVersionSnapshots = ref<any[]>([])
const versionChipError = ref<string | null>(null)
// Monotonic token: loadCatalog re-runs on mount and after a rollback, so a
// stale in-flight filtered response must never overwrite a newer one (same
// guard as versionLookupToken for the `?version=` alias search).
let versionChipToken = 0

const showAgentPicker = ref(false)
const showRevertDialog = ref(false)
const showSaveAsComposite = ref(false)
const showVersionTimeline = ref(false)
const pickerAgentId = ref<string>('__all__')
const pickerConnectorId = ref<string>('__all__')
const revertSnapshotId = ref<string>('__all__')
const convertError = ref<string | null>(null)
const { get } = useApi()
const revertError = ref<string | null>(null)
const revertLoading = ref(false)

const saveAsName = ref('')
const saveAsDescription = ref('')
const saveAsSelectedNodeIds = ref<string[]>([])
const saveAsError = ref<string | null>(null)
const saving = ref(false)

const savingEdge = ref(false)
const edgeSaveError = ref<string | null>(null)

const pipeline = ref<any>(null)
const showRenameDialog = ref(false)
const renameName = ref('')
const renameError = ref<string | null>(null)
const renaming = ref(false)
const showDeleteConfirm = ref(false)
const deleteError = ref<string | null>(null)

const savingGraph = ref(false)
const saveGraphError = ref<string | null>(null)
const showRunDialog = ref(false)
const runPrompt = ref('')
const running = ref(false)
const runError = ref<string | null>(null)
const confirmEmptyRun = ref(false)
const emptyRunWarning = ref<string | null>(null)

watch(runPrompt, () => {
  if (confirmEmptyRun.value) {
    confirmEmptyRun.value = false
    emptyRunWarning.value = null
  }
})

const maxDurationInput = ref<number | undefined>(undefined)
// FAR-1163: pipeline autonomy ceiling. null = inherit (effective ceiling is
// the pipeline's default_autonomy_level).
const maxAutonomyInput = ref<string | null>(null)

// FAR-1182: monthly spend circuit breaker. '' = disabled (null on the API).
const circuitBreakerInput = ref<string | number>('')
const resettingCircuitBreaker = ref(false)
const { orgRole, isSystemAdmin } = useCurrentUser()
// Mirrors the backend reset gate: POST /admin/costs/circuit-breaker/{id}/reset
// requires the org-admin `cost.manage` permission (no plan gate).
const canResetCircuitBreaker = computed(() => orgRole.value === 'admin')

// FAR-1257: per-pipeline HITL review window override (seconds; 60..604800).
// '' = no override (inherit the org default). Empty on save sends an explicit
// null so the backend's exclude_unset path CLEARS the column rather than
// leaving it untouched.
const HITL_WINDOW_MIN_SECONDS = 60
const HITL_WINDOW_MAX_SECONDS = 604800
const hitlReviewWindowInput = ref<string | number>('')

function syncHitlReviewWindowFromPipeline() {
  const stored = (pipeline.value as { hitl_review_window_seconds?: number | null } | null)?.hitl_review_window_seconds
  hitlReviewWindowInput.value = stored ?? ''
}

async function updateHitlReviewWindow() {
  const raw = String(hitlReviewWindowInput.value ?? '').trim()
  let seconds: number | null
  if (raw === '') {
    seconds = null
  } else {
    const parsed = Number(raw)
    if (!Number.isFinite(parsed) || !Number.isInteger(parsed) || parsed < HITL_WINDOW_MIN_SECONDS || parsed > HITL_WINDOW_MAX_SECONDS) {
      saveGraphError.value = t('views.PipelineEditorView.hitl_review_window_out_of_range', {
        min: HITL_WINDOW_MIN_SECONDS,
        max: HITL_WINDOW_MAX_SECONDS,
      })
      // Never display a value the server would reject — restore the stored one.
      syncHitlReviewWindowFromPipeline()
      return
    }
    seconds = parsed
  }
  try {
    await withTimeout((signal) => api.PATCH('/api/v1/pipelines/{pipeline_id}', {
      params: { path: { pipeline_id: pipelineId } },
      body: { hitl_review_window_seconds: seconds },
      signal,
    }))
    if (pipeline.value) pipeline.value.hitl_review_window_seconds = seconds
    hitlReviewWindowInput.value = seconds ?? ''
    saveGraphError.value = null
  } catch (e: unknown) {
    saveGraphError.value = t('views.PipelineEditorView.hitl_review_window_update_failed', { error: formatApiError(e) })
    syncHitlReviewWindowFromPipeline()
  }
}

// FAR-1883: per-pipeline capacity-queue timeout (seconds; 1..604800).
// '' = never (the default - a capacity-queued run waits indefinitely). Empty
// on save sends an explicit null so the backend's exclude_unset path CLEARS
// the column rather than leaving it untouched.
const QUEUE_TIMEOUT_MIN_SECONDS = 1
const QUEUE_TIMEOUT_MAX_SECONDS = 604800
const queueTimeoutInput = ref<string | number>('')

function syncQueueTimeoutFromPipeline() {
  const stored = (pipeline.value as { queue_timeout_seconds?: number | null } | null)?.queue_timeout_seconds
  queueTimeoutInput.value = stored ?? ''
}

async function updateQueueTimeout() {
  const raw = String(queueTimeoutInput.value ?? '').trim()
  let seconds: number | null
  if (raw === '') {
    seconds = null
  } else {
    const parsed = Number(raw)
    if (!Number.isFinite(parsed) || !Number.isInteger(parsed) || parsed < QUEUE_TIMEOUT_MIN_SECONDS || parsed > QUEUE_TIMEOUT_MAX_SECONDS) {
      saveGraphError.value = t('views.PipelineEditorView.queue_timeout_out_of_range', {
        min: QUEUE_TIMEOUT_MIN_SECONDS,
        max: QUEUE_TIMEOUT_MAX_SECONDS,
      })
      // Never display a value the server would reject - restore the stored one.
      syncQueueTimeoutFromPipeline()
      return
    }
    seconds = parsed
  }
  try {
    await withTimeout((signal) => api.PATCH('/api/v1/pipelines/{pipeline_id}', {
      params: { path: { pipeline_id: pipelineId } },
      body: { queue_timeout_seconds: seconds },
      signal,
    }))
    if (pipeline.value) pipeline.value.queue_timeout_seconds = seconds
    queueTimeoutInput.value = seconds ?? ''
    saveGraphError.value = null
  } catch (e: unknown) {
    saveGraphError.value = t('views.PipelineEditorView.queue_timeout_update_failed', { error: formatApiError(e) })
    syncQueueTimeoutFromPipeline()
  }
}

// FAR-1257: does ANYONE own this pipeline? A human-only gate with no required
// team on an unowned pipeline has no assignable reviewer — it can only age out
// at the review window. Advisory only; never used to block a save.
const pipelineHasOwner = computed(() => {
  const p = pipeline.value as {
    owner_team_id?: string | null
    business_owner_id?: string | null
    reliability_owner_id?: string | null
  } | null
  if (!p) return false
  if (p.owner_team_id) return true
  if (p.business_owner_id) return true
  if (p.reliability_owner_id) return true
  return false
})

const ownerlessGateAdvisory = computed(() => {
  if (!selectedEdgeData.value) return false
  if (!edgeForm.hitl_enabled || !edgeForm.human_only) return false
  if (pipelineHasOwner.value) return false
  return !selectedEdgeData.value.hitl_review_config?.required_team_id
})

// Viewport margin kept clear on every clamped edge, in px — the class cap
// `max-w-[calc(100vw-1rem)]` reserves the same 1rem (16px) across both edges.
const OVERLAY_VIEWPORT_MARGIN_PX = 8
// Offset between the trigger's near edge and the panel, in px.
const OVERLAY_TRIGGER_GAP_PX = 4
// The panel's minimum comfortable height, in px: below this much room under
// the trigger — and only when the space above is genuinely roomier — the panel
// flips to sit above it.
const OVERLAY_MIN_COMFORTABLE_HEIGHT_PX = 160
// Hard floor for the clamped panel height, in px — a panel is never collapsed
// below this even when there is no real room.
const OVERLAY_MIN_HEIGHT_PX = 120

/**
 * A panel's Tailwind width class paired with the same width in px — the single
 * source of truth for both. The class sets the panel's real width; the px value
 * feeds the horizontal clamp in `anchoredPopoverStyle`. They MUST agree: if
 * they drift, only the left-edge fallback clamp degrades (the default
 * right-anchored case is unaffected). Reference `.className` on the panel and
 * `.widthPx` at its positioner — never repeat either literal.
 */
interface OverlayPanelMetrics {
  readonly className: string
  readonly widthPx: number
}

const SAVE_AS_DROPDOWN_METRICS: OverlayPanelMetrics = { widthPx: 192, className: 'w-48' }
const RETRY_POLICY_PANEL_METRICS: OverlayPanelMetrics = { widthPx: 288, className: 'w-72' }
const AUTONOMY_PANEL_METRICS: OverlayPanelMetrics = { widthPx: 320, className: 'w-80' }

/**
 * Viewport-anchored positioning for the toolbar popovers (FAR-1899).
 *
 * FAR-1861 made the editor content column the internal scroll container
 * (`overflow-y-auto`) and clipped the editor root (`overflow-hidden`). A
 * popover that stays in-tree is therefore clipped to the column's box, which
 * in the sub-fit band (a viewport shorter than the wrapped toolbar's
 * min-content height) is only a few pixels tall — the panel is reachable by
 * scrolling the column but can never be seen whole. A `position: fixed`
 * popover escapes that clipping (no ancestor establishes a containing block
 * for it — the editor root only sets height), so it is anchored to the
 * trigger's viewport rect and its height clamped to the room actually
 * available: the panel scrolls its own content rather than being cut off.
 * Returns {} when the trigger ref is not bound yet (jsdom mounts), leaving the
 * CSS class fallback in charge.
 *
 * `preferredWidth` is the panel's CSS width in px, taken from that panel's
 * `OverlayPanelMetrics.widthPx` so it can never drift from its class, clamped
 * here by the same `max-w-[calc(100vw-1rem)]` the class applies. With the
 * default `align: 'right'` the panel is RIGHT-aligned to the
 * trigger's right edge; when that would push its LEFT edge off-screen (a
 * trigger near the left edge — the w-80/w-72 panels are wider than the room
 * left of a left-edge trigger), it falls back to LEFT-anchoring with the left
 * edge clamped inside the margin, so both edges stay on screen. `align: 'left'`
 * (the save-as-template menu, which hangs off the left edge of its trigger)
 * always left-anchors, clamping the left edge inside the margin so a trigger
 * near the right edge does not push the panel off-screen.
 */
function anchoredPopoverStyle(
  trigger: HTMLElement | null,
  preferredWidth: number,
  align: 'left' | 'right' = 'right',
): Record<string, string> {
  if (!trigger) return {}
  const rect = trigger.getBoundingClientRect()
  const margin = OVERLAY_VIEWPORT_MARGIN_PX
  const gap = OVERLAY_TRIGGER_GAP_PX
  const spaceBelow = window.innerHeight - rect.bottom - gap - margin
  const spaceAbove = rect.top - gap - margin
  // Stay below unless that leaves less than a usable panel AND there is
  // genuinely more room above (so a tall viewport keeps the familiar
  // below-the-trigger placement).
  const openUp = spaceBelow < OVERLAY_MIN_COMFORTABLE_HEIGHT_PX && spaceAbove > spaceBelow
  const available = openUp ? spaceAbove : spaceBelow
  const maxHeight = Math.max(
    OVERLAY_MIN_HEIGHT_PX,
    Math.min(available, window.innerHeight - margin * 2),
  )
  // Effective panel width: the class width, capped by the viewport cap.
  const width = Math.min(preferredWidth, window.innerWidth - margin * 2)
  let left: string
  let right: string
  if (align === 'left') {
    // Left-anchored to the trigger's left edge, clamped inside the margin.
    left = `${Math.max(margin, Math.min(rect.left, window.innerWidth - margin - width))}px`
    right = 'auto'
  } else {
    // Right-aligned by default; left-anchor when the panel would overflow the
    // left edge (a trigger within one panel-width of the viewport's left edge).
    const overflowsLeft = rect.right - width < margin
    left = overflowsLeft
      ? `${Math.max(margin, Math.min(rect.left, window.innerWidth - margin - width))}px`
      : 'auto'
    right = overflowsLeft ? 'auto' : `${Math.max(margin, window.innerWidth - rect.right)}px`
  }
  return {
    left,
    right,
    top: openUp ? 'auto' : `${rect.bottom + gap}px`,
    bottom: openUp ? `${Math.max(margin, window.innerHeight - rect.top + gap)}px` : 'auto',
    maxHeight: `${maxHeight}px`,
  }
}

const retryPolicyOpen = ref(false)
const retryPolicySaving = ref(false)
// FAR-649: coverage mode. 'all' = All errors (the default; saves the policy
// WITHOUT the `on` key, which the runtime resolves to all retryable events).
// 'specific' = granular (saves the explicit event list).
const retryPolicyMode = ref<'all' | 'specific'>('all')
const retryPolicyEvents = ref<string[]>([])
const retryPolicyMaxRetries = ref(0)
const retryPolicyDelaySeconds = ref(45)
const retryPolicyMultiplier = ref(2)
const retryPolicyScheduleWarning = ref<string | null>(null)
const retryPolicyLegacyBackoff = ref<number | undefined>(undefined)
const retryPolicyError = ref<string | null>(null)
const retryPolicyToggleRef = ref<HTMLButtonElement | null>(null)
const retryPolicyPanelRef = ref<HTMLElement | null>(null)
const retryPolicyPanelStyle = ref<Record<string, string>>({})
const retryPolicyToggleId = 'pipeline-editor-retry-policy-toggle'
const retryPolicyPanelId = 'pipeline-editor-retry-policy-panel'

function positionRetryPolicyPanel() {
  retryPolicyPanelStyle.value = anchoredPopoverStyle(
    retryPolicyToggleRef.value,
    RETRY_POLICY_PANEL_METRICS.widthPx,
  )
}

// Capture phase: scroll does not bubble, so this also catches the content
// column's own scroll while the panel is open (keeping it pinned to its
// trigger) — not just the window's. Passive: the handler only reads geometry
// and never calls preventDefault, so it must not block the scroll it observes.
function bindRetryPolicyPositioning() {
  document.addEventListener('scroll', positionRetryPolicyPanel, { capture: true, passive: true })
  window.addEventListener('resize', positionRetryPolicyPanel, { passive: true })
}

function unbindRetryPolicyPositioning() {
  // Only `capture` participates in listener matching; `passive` is add-only.
  document.removeEventListener('scroll', positionRetryPolicyPanel, true)
  window.removeEventListener('resize', positionRetryPolicyPanel)
}
const retryPolicyOptions = [
  { value: 'stall', labelKey: 'views.PipelineEditorView.retry_policy_stall' },
  { value: 'timeout', labelKey: 'views.PipelineEditorView.retry_policy_timeout' },
  { value: 'failure', labelKey: 'views.PipelineEditorView.retry_policy_failure' },
  { value: 'eval_failed', labelKey: 'views.PipelineEditorView.retry_policy_eval_failed' },
]
const retryPolicyEventValues = new Set(retryPolicyOptions.map((o) => o.value))

interface RetryPolicy {
  on?: string[]
  max_retries?: number
  [key: string]: unknown
}

type PipelineRetryPolicySource = {
  retry_policy?: RetryPolicy | null
}

const retryPolicyNoRetriesWarning = computed(() => {
  const max = Number(retryPolicyMaxRetries.value) || 0
  // FAR-649: granular with ZERO events selected = retry effectively off —
  // the zero-selected warning (save is blocked too, no silent inert policy).
  if (retryPolicyMode.value === 'specific' && retryPolicyEvents.value.length === 0) {
    return t('views.PipelineEditorView.retry_policy_warning_no_events')
  }
  if (max === 0) {
    return t('views.PipelineEditorView.retry_policy_warning_no_max')
  }
  return null
})

// FAR-649: granular with zero events selected cannot save (disable-save with
// warning, never a silent inert policy). All-errors mode is never blocked by
// this (a 0 budget there is the explicit "retry nothing" state).
const retryPolicySaveBlocked = computed(
  () => retryPolicyMode.value === 'specific' && retryPolicyEvents.value.length === 0,
)

// FAR-649: coverage mode from the stored shape. An ABSENT `on` (key
// missing or null) renders as All-errors — the runtime resolves it to all
// retryable events. An explicit `on` list renders as granular (a stored
// empty list is retry-effectively-off, surfaced by the zero-selected
// warning). A malformed non-list `on` fail-closes at runtime (no retry),
// so it renders as granular with nothing selected to surface the warning.
function _coverageFromStored(rp: RetryPolicy): { mode: 'all' | 'specific'; events: string[] } {
  const stored = rp as Record<string, unknown>
  if (stored.on === undefined || stored.on === null) {
    return { mode: 'all', events: [] }
  }
  if (Array.isArray(stored.on)) {
    return {
      mode: 'specific',
      events: (stored.on as unknown[]).filter((e): e is string =>
        retryPolicyEventValues.has(e as string),
      ),
    }
  }
  return { mode: 'specific', events: [] }
}

function _maxRetriesFromStored(rp: RetryPolicy): number {
  const max = typeof rp.max_retries === 'number' ? Math.round(rp.max_retries) : 0
  return Math.min(5, Math.max(0, max))
}

// FAR-525: the run-level backoff schedule. Out-of-range stored values are
// clamped here for editing ONLY — the runtime resolver fails open to the
// default 45s x 2.0 schedule, which the caller surfaces via `clamped`.
function _scheduleFromStored(rp: RetryPolicy): {
  delay: number
  multiplier: number
  clamped: boolean
} {
  const stored = rp as Record<string, unknown>
  const schedule = stored.backoff_schedule
  if (!schedule || typeof schedule !== 'object' || Array.isArray(schedule)) {
    return { delay: 45, multiplier: 2, clamped: false }
  }
  const s = schedule as Record<string, unknown>
  let clamped = false

  const rawDelay = Number(s.delay_seconds)
  let delay: number
  if (Number.isFinite(rawDelay) && rawDelay >= 1 && rawDelay <= 300) {
    delay = Math.round(rawDelay)
  } else {
    delay = Number.isFinite(rawDelay) ? Math.min(300, Math.max(1, Math.round(rawDelay))) : 45
    clamped = true
  }

  let multiplier: number
  if (s.multiplier === undefined) {
    multiplier = 2
  } else {
    const rawMult = Number(s.multiplier)
    if (Number.isFinite(rawMult) && rawMult >= 1 && rawMult <= 10) {
      multiplier = rawMult
    } else {
      multiplier = Number.isFinite(rawMult) ? Math.min(10, Math.max(1, rawMult)) : 2
      clamped = true
    }
  }

  return { delay, multiplier, clamped }
}

function syncRetryPolicyFromPipeline() {
  const rp = (pipeline.value as PipelineRetryPolicySource | null)?.retry_policy
  retryPolicyScheduleWarning.value = null
  if (rp && typeof rp === 'object' && !Array.isArray(rp)) {
    const coverage = _coverageFromStored(rp)
    retryPolicyMode.value = coverage.mode
    retryPolicyEvents.value = coverage.events
    retryPolicyMaxRetries.value = _maxRetriesFromStored(rp)

    // FAR-525: keep the legacy node-level inherited `backoff` so a save can
    // re-send it losslessly (the editor rebuilds the policy from a whitelist).
    const legacyBackoff = (rp as Record<string, unknown>).backoff
    retryPolicyLegacyBackoff.value =
      typeof legacyBackoff === 'number' && Number.isFinite(legacyBackoff) ? legacyBackoff : undefined

    const schedule = _scheduleFromStored(rp)
    retryPolicyDelaySeconds.value = schedule.delay
    retryPolicyMultiplier.value = schedule.multiplier
    if (schedule.clamped) {
      retryPolicyScheduleWarning.value = t('views.PipelineEditorView.retry_policy_schedule_clamped_warning')
    }
  } else {
    retryPolicyMode.value = 'all'
    retryPolicyEvents.value = []
    retryPolicyMaxRetries.value = 0
    retryPolicyLegacyBackoff.value = undefined
    retryPolicyDelaySeconds.value = 45
    retryPolicyMultiplier.value = 2
  }
  retryPolicyError.value = null
}

function toggleRetryPolicy() {
  if (retryPolicyOpen.value) {
    retryPolicyOpen.value = false
    retryPolicyToggleRef.value?.focus()
    return
  }
  syncRetryPolicyFromPipeline()
  // Anchor BEFORE the panel paints so it never flashes at its static
  // in-column position for a frame.
  positionRetryPolicyPanel()
  retryPolicyOpen.value = true
  nextTick(() => {
    retryPolicyPanelRef.value?.focus()
  })
}

function closeRetryPolicy() {
  retryPolicyOpen.value = false
  retryPolicyToggleRef.value?.focus()
}

function onRetryPolicyKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape' && retryPolicyOpen.value) {
    event.preventDefault()
    closeRetryPolicy()
  }
}

watch(retryPolicyOpen, (open) => {
  if (open) {
    document.addEventListener('keydown', onRetryPolicyKeydown)
    bindRetryPolicyPositioning()
  } else {
    document.removeEventListener('keydown', onRetryPolicyKeydown)
    unbindRetryPolicyPositioning()
  }
})

onBeforeUnmount(() => {
  document.removeEventListener('keydown', onRetryPolicyKeydown)
  unbindRetryPolicyPositioning()
})

// FAR-1696 (ADR 043 S5): progressive-autonomy panel popover. Same
// open/Escape/focus-return shape as the retry-policy panel above it, and the
// same toggle→panel aria-controls wiring (the toggle button references this
// dialog id so AT can announce the controlled element).
const autonomyPanelDialogId = 'pipeline-editor-autonomy-panel-dialog'
const autonomyPanelOpen = ref(false)
const autonomyPanelToggleRef = ref<HTMLButtonElement | null>(null)
const autonomyPanelDialogRef = ref<HTMLDialogElement | null>(null)
const autonomyPanelStyle = ref<Record<string, string>>({})

function positionAutonomyPanel() {
  autonomyPanelStyle.value = anchoredPopoverStyle(
    autonomyPanelToggleRef.value,
    AUTONOMY_PANEL_METRICS.widthPx,
  )
}

// Passive for the same reason as the retry-policy panel: the handler only
// reads geometry and never calls preventDefault.
function bindAutonomyPanelPositioning() {
  document.addEventListener('scroll', positionAutonomyPanel, { capture: true, passive: true })
  window.addEventListener('resize', positionAutonomyPanel, { passive: true })
}

function unbindAutonomyPanelPositioning() {
  // Only `capture` participates in listener matching; `passive` is add-only.
  document.removeEventListener('scroll', positionAutonomyPanel, true)
  window.removeEventListener('resize', positionAutonomyPanel)
}

function toggleAutonomyPanel() {
  if (autonomyPanelOpen.value) {
    autonomyPanelOpen.value = false
    autonomyPanelToggleRef.value?.focus()
    return
  }
  // Same FAR-1899 anchor-before-paint as the retry-policy panel above.
  positionAutonomyPanel()
  autonomyPanelOpen.value = true
  nextTick(() => {
    autonomyPanelDialogRef.value?.focus()
  })
}

function closeAutonomyPanel() {
  autonomyPanelOpen.value = false
  autonomyPanelToggleRef.value?.focus()
}

function onAutonomyPanelKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape' && autonomyPanelOpen.value) {
    event.preventDefault()
    closeAutonomyPanel()
  }
}

watch(autonomyPanelOpen, (open) => {
  if (open) {
    document.addEventListener('keydown', onAutonomyPanelKeydown)
    bindAutonomyPanelPositioning()
  } else {
    document.removeEventListener('keydown', onAutonomyPanelKeydown)
    unbindAutonomyPanelPositioning()
  }
})

onBeforeUnmount(() => {
  document.removeEventListener('keydown', onAutonomyPanelKeydown)
  unbindAutonomyPanelPositioning()
})

// FAR-1899 follow-up: the save-as-template menu is a plain in-tree popover in
// the SAME clipped content column as the retry-policy and autonomy panels, so
// it carries the same sub-fit clipping (FAR-1861). It gets the same
// `position: fixed` viewport anchoring, but LEFT-anchored (the menu hangs off
// the left edge of its trigger) rather than right-aligned like the panels —
// hence the explicit `align`.
function positionSaveAsDropdown() {
  saveAsDropdownStyle.value = anchoredPopoverStyle(
    saveAsDropdownTriggerRef.value,
    SAVE_AS_DROPDOWN_METRICS.widthPx,
    'left',
  )
}

// Passive for the same reason as the panels: the handler only reads geometry
// and never calls preventDefault.
function bindSaveAsDropdownPositioning() {
  document.addEventListener('scroll', positionSaveAsDropdown, { capture: true, passive: true })
  window.addEventListener('resize', positionSaveAsDropdown, { passive: true })
}

function unbindSaveAsDropdownPositioning() {
  // Only `capture` participates in listener matching; `passive` is add-only.
  document.removeEventListener('scroll', positionSaveAsDropdown, true)
  window.removeEventListener('resize', positionSaveAsDropdown)
}

function toggleSaveAsDropdown() {
  if (showSaveAsDropdown.value) {
    closeSaveAsDropdown()
    return
  }
  // Anchor before paint so the menu never flashes at its static in-column
  // position for a frame (same FAR-1899 anchor-before-paint as the panels).
  positionSaveAsDropdown()
  showSaveAsDropdown.value = true
}

// FAR-1899 review note: the save-as menu was the only toolbar popover without
// an Escape handler, so a keyboard user had to reach the toggle (or pick an
// item) to dismiss it. Same open/Escape/focus-return shape as the retry-policy
// and autonomy panels: Escape closes it and returns focus to the toggle.
function closeSaveAsDropdown() {
  showSaveAsDropdown.value = false
  saveAsDropdownTriggerRef.value?.focus()
}

function onSaveAsDropdownKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape' && showSaveAsDropdown.value) {
    event.preventDefault()
    closeSaveAsDropdown()
  }
}

watch(showSaveAsDropdown, (open) => {
  if (open) {
    document.addEventListener('keydown', onSaveAsDropdownKeydown)
    bindSaveAsDropdownPositioning()
  } else {
    document.removeEventListener('keydown', onSaveAsDropdownKeydown)
    unbindSaveAsDropdownPositioning()
  }
})

onBeforeUnmount(() => {
  document.removeEventListener('keydown', onSaveAsDropdownKeydown)
  unbindSaveAsDropdownPositioning()
})

// A manual demote/promote writes earned_autonomy_level server-side; refetch
// the pipeline so the toolbar ceiling control, the panel and any other
// pipeline-derived state all re-read the same persisted value.
async function onAutonomyChanged() {
  await loadPipeline()
}

async function saveRetryPolicy() {
  if (retryPolicySaving.value) return
  retryPolicyError.value = null
  const max = Math.min(5, Math.max(0, Number(retryPolicyMaxRetries.value) || 0))
  const granular = retryPolicyMode.value === 'specific'
  const on = [...retryPolicyEvents.value]
  if (granular && on.length === 0) {
    // FAR-649: no silent inert policy — granular with zero events selected is
    // blocked (the save button is disabled; this guard also blocks direct
    // invocation). Switch to All-errors or select at least one event.
    retryPolicyError.value = t('views.PipelineEditorView.retry_policy_warning_no_events')
    return
  }
  if (granular && on.length > 0 && max === 0) {
    retryPolicyError.value = t('views.PipelineEditorView.retry_policy_warning_no_max')
    return
  }
  // FAR-525: rebuild the policy from a whitelist so no API-set key is silently
  // destroyed: the legacy node-level `backoff` is preserved explicitly, and the
  // run-level `backoff_schedule` is rebuilt from the panel inputs (never spread
  // from the stored object, so junk inner keys are dropped). FAR-649: All-errors
  // mode saves WITHOUT the `on` key (absent `on` = all retryable events at
  // runtime); granular mode saves the explicit event list.
  const delay = Math.min(300, Math.max(1, Math.round(Number(retryPolicyDelaySeconds.value) || 0)))
  const multiplier = Math.min(10, Math.max(1, Number(retryPolicyMultiplier.value) || 0))
  const policy: RetryPolicy = { max_retries: max }
  if (granular) {
    policy.on = on
  }
  if (retryPolicyLegacyBackoff.value !== undefined) {
    policy.backoff = retryPolicyLegacyBackoff.value
  }
  policy.backoff_schedule = { delay_seconds: delay, multiplier }
  const body: { retry_policy: RetryPolicy } = {
    retry_policy: policy,
  }
  retryPolicySaving.value = true
  try {
    await withTimeout((signal) => api.PATCH('/api/v1/pipelines/{pipeline_id}', {
      params: { path: { pipeline_id: pipelineId } },
      body,
      signal,
    }))
    await loadPipeline()
    retryPolicyOpen.value = false
    retryPolicyToggleRef.value?.focus()
    saveGraphError.value = null
  } catch (e) {
    retryPolicyError.value = `${t('views.PipelineEditorView.retry_policy_update_failed')}${formatApiError(e)}`
  } finally {
    retryPolicySaving.value = false
  }
}

const folders = ref<FolderItem[]>([])
// A failed folder-list load must be visible AND retryable: the folder
// breadcrumb is derived from this list, so a silent failure leaves it wrong or
// absent with no way to recover short of a reload. Mirrors PipelineListView.
const folderError = ref<string | null>(null)
const linkedLifecycleMaps = ref<any[]>([])

const folderPath = computed(() => {
  const path: { name: string; id: string }[] = []
  let current: any = folders.value.find((f: any) => f.id === pipeline.value?.folder_id)
  while (current) {
    path.unshift({ name: current.name, id: current.id })
    current = current.parent_id ? folders.value.find((f: any) => f.id === current.parent_id) : null
  }
  return path
})

const defaultEdgeForm = {
  edge_type: 'normal',
  condition_expression: '',
  max_iterations: 0,
  routing_label: '',
  hitl_enabled: false,
  label: '',
  description: '',
  claim_expiry_minutes: 15,
  // FAR-609: every HITL gate defaults to human_only; the checkbox opts out.
  human_only: true,
  condition_type: 'none',
  condition: '',
  eval_name: '',
  eval_threshold: 0.8,
  eval_operator: 'lt',
}

const edgeForm = reactive({ ...defaultEdgeForm })

// FAR-613/FAR-688: a HITL gate must carry a human description of at least
// this many trimmed characters — ONE constant for the template's
// aria-invalid hint and the save guard (previously the literal 20 in two
// places). Counted as CODE POINTS ([...str].length) to match the backend's
// Python len() — a description padded with astral-plane characters (emoji)
// must not be miscounted by UTF-16 units.
const HITL_DESCRIPTION_MIN_LENGTH = 20

function codePointLength(value: string): number {
  return [...value].length
}

const hitlDescriptionTooShort = computed(
  () => edgeForm.hitl_enabled && codePointLength(edgeForm.description.trim()) < HITL_DESCRIPTION_MIN_LENGTH,
)

// ---------------------------------------------------------------------------
// FAR-688: legacy HITL description violations.
//
// Graph READS never hard-fail on gate descriptions below the minimum (the
// forcing function applies to the next SAVE — legacy pipelines stay
// readable/editable), and the read response's ``validation_issues`` is
// always empty for reads. So the editor scans the loaded graph CLIENT-SIDE
// for gates whose description would fail the save-time check and surfaces
// them in a dismissible banner, listing the offending edges/nodes so the
// user can find legacy gates without hunting the graph.
// ---------------------------------------------------------------------------

interface LegacyHitlIssue {
  /** Stable list key (kind + id). */
  key: string
  /** 'edge' gates report the edge; 'node' gates the FAR-402 HITL node. */
  kind: 'edge' | 'node'
  /** Human label for the list (node label or shortId pair). */
  label: string
}

function hitlConfigDescriptionTooShort(config: unknown): boolean {
  // Mirrors GraphValidator._hitl_description_issue: a config without a
  // usable description string of >= HITL_DESCRIPTION_MIN_LENGTH trimmed
  // code points violates the rule.
  if (!config || typeof config !== 'object' || Array.isArray(config)) return true
  const description = (config as Record<string, unknown>).description
  if (typeof description !== 'string') return true
  return codePointLength(description.trim()) < HITL_DESCRIPTION_MIN_LENGTH
}

function isHitlNode(node: any): boolean {
  if (!node || typeof node !== 'object' || !node.id) return false
  return node.node_type === 'hitl'
}

function hitlNodeIds(nodes: any[]): Set<string> {
  const ids = new Set<string>()
  for (const node of nodes) {
    if (isHitlNode(node)) ids.add(String(node.id))
  }
  return ids
}

function hitlNodeIssue(node: any): LegacyHitlIssue {
  const label = (typeof node.label === 'string' && node.label.trim()) || shortId(String(node.id))
  return { key: `node:${node.id}`, kind: 'node', label }
}

function hitlNodeIssues(nodes: any[]): LegacyHitlIssue[] {
  const issues: LegacyHitlIssue[] = []
  for (const node of nodes) {
    if (!isHitlNode(node)) continue
    if (!hitlConfigDescriptionTooShort(node.hitl_config)) continue
    issues.push(hitlNodeIssue(node))
  }
  return issues
}

function edgeGateOwnedByHitlNode(edge: any, nodeIds: Set<string>): boolean {
  return edge.source_node_id != null && nodeIds.has(String(edge.source_node_id))
}

function hitlEdgeIssue(edge: any, nodeIds: Set<string>): LegacyHitlIssue | null {
  if (!edge || typeof edge !== 'object') return null
  const config = edge.hitl_review_config
  if (!config || typeof config !== 'object') return null
  // A node-level gate's config is injected onto its outgoing edges at
  // compile time; the persisted definition carries it on the NODE, and
  // the node pass already reported it — never double-list one gate.
  if (edgeGateOwnedByHitlNode(edge, nodeIds)) return null
  if (!hitlConfigDescriptionTooShort(config)) return null
  const source = shortId(String(edge.source_node_id ?? edge.source ?? '?'))
  const target = shortId(String(edge.target_node_id ?? edge.target ?? '?'))
  const fallbackKey = `${source}->${target}`
  return { key: `edge:${edge.id ?? fallbackKey}`, kind: 'edge', label: `${source} ? ${target}` }
}

function hitlEdgeIssues(edges: any[], nodeIds: Set<string>): LegacyHitlIssue[] {
  const issues: LegacyHitlIssue[] = []
  for (const edge of edges) {
    const issue = hitlEdgeIssue(edge, nodeIds)
    if (issue) issues.push(issue)
  }
  return issues
}

function findLegacyHitlDescriptionIssues(nodes: any[], edges: any[]): LegacyHitlIssue[] {
  return [...hitlNodeIssues(nodes), ...hitlEdgeIssues(edges, hitlNodeIds(nodes))]
}

// Reactive over the loaded graph — a save that fixed the last violation
// clears the banner; a reverted graph brings it back.
const legacyHitlIssues = computed(() => findLegacyHitlDescriptionIssues(rawNodes.value, rawEdges.value))
const showLegacyHitlBanner = ref(true)

// FAR-688: which KINDS of violations are present drives the per-kind hints —
// edge gates keep the "open the edge and add one" hint; node-level gates get
// the API/MCP hint because the editor cannot edit their hitl_config yet.
const hasLegacyEdgeIssues = computed(() => legacyHitlIssues.value.some(issue => issue.kind === 'edge'))
const hasLegacyNodeIssues = computed(() => legacyHitlIssues.value.some(issue => issue.kind === 'node'))

const selectedAgent = computed(() => agents.value.find(a => a.id === pickerAgentId.value) || null)

const eligibleConnectors = computed(() => {
  if (!selectedAgent.value) return []
  const refs: Array<{ connector_type: string }> = selectedAgent.value.connector_type_refs || []
  const allowedTypes = new Set(refs.map(r => r.connector_type))
  return connectors.value.filter(c => allowedTypes.has(c.connector_type_id))
})

const modelBackendName = computed(() => {
  if (!selectedAgent.value) return ''
  const mb = modelBackends.value.find(b => b.id === selectedAgent.value.model_backend_id)
  return mb ? `${mb.display_name} (${mb.provider})` : 'Unknown'
})

function agentSchemaName(agent: any, dir: 'input' | 'output') {
  const s = schemas.value.find(s => s.id === agent[`${dir}_schema_id`])
  return s ? s.name : `${dir}_schema_id`
}

const isWebhookTriggered = computed(() => pipeline.value?.trigger_type === 'webhook')

function agentName(agentId: string): string | undefined {
  return agents.value.find((a: any) => a.id === agentId)?.name
}

function agentModelBackendId(agentId: string): string | undefined {
  const agent = agents.value.find((a: any) => a.id === agentId)
  return agent?.model_backend_id
}

function agentModelBackendName(agentId: string): string | undefined {
  const agent = agents.value.find((a: any) => a.id === agentId)
  if (!agent?.model_backend_id) return undefined
  const mb = modelBackends.value.find((b: any) => b.id === agent.model_backend_id)
  return mb?.display_name
}

function agentInputSchemaId(agentId: string): string | undefined {
  const agent = agents.value.find((a: any) => a.id === agentId)
  return agent?.input_schema_id
}

function agentOutputSchemaId(agentId: string): string | undefined {
  const agent = agents.value.find((a: any) => a.id === agentId)
  return agent?.output_schema_id
}

function schemaName(schemaId: string): string | undefined {
  const s = schemas.value.find((s: any) => s.id === schemaId)
  return s?.name
}

function connectorName(binding: any): string {
  if (!binding) return '-'
  const conn = connectors.value.find((c: any) => c.id === binding.instance_id)
  if (conn) return `${conn.name} (${binding.type})`
  return binding.instance_id ? `${binding.type} / ${shortId(binding.instance_id)}` : binding.type
}

// FAR-437: node-level capability_scope authoring (narrow-not-widen)
const nodeCapabilityScope = reactive<{ allowed_connectors: string[]; allowed_tools: string[]; context_scope: string[] }>({
  allowed_connectors: [],
  allowed_tools: [],
  context_scope: [],
})
const capabilityToolInput = ref('')
const capabilityContextInput = ref('')

function doesNodeHaveCapabilityScope(node: any): boolean {
  const cs = node?.capability_scope
  if (!cs || typeof cs !== 'object') return false
  return [cs.allowed_connectors, cs.allowed_tools, cs.context_scope]
    .some((arr) => Array.isArray(arr) && arr.length > 0)
}

function nodeAgent(node: any): any | undefined {
  return agents.value.find((a: any) => a.id === node?.agent_id)
}

function agentConnectorTypes(agent: any): Set<string> {
  const refs: Array<{ connector_type: string }> = agent?.connector_type_refs || []
  return new Set(refs.map((r) => r.connector_type))
}

const availableConnectorsForNode = computed(() => {
  const agent = nodeAgent(selectedNodeData.value)
  const types = agentConnectorTypes(agent)
  if (types.size === 0) return []
  return connectors.value.filter((c: any) => types.has(c.connector_type_id))
})

const outOfScopeConnectors = computed(() => {
  const available = new Set(availableConnectorsForNode.value.map((c: any) => c.id))
  return nodeCapabilityScope.allowed_connectors.filter((v) => !available.has(v))
})

function syncCapabilityScopeToNode() {
  if (isViewing.value) return
  if (!selectedNodeData.value) return
  const cs = nodeCapabilityScope
  const hasAny = cs.allowed_connectors.length > 0 || cs.allowed_tools.length > 0 || cs.context_scope.length > 0
  const capability_scope = hasAny
    ? {
        ...(cs.allowed_connectors.length > 0 ? { allowed_connectors: [...cs.allowed_connectors] } : {}),
        ...(cs.allowed_tools.length > 0 ? { allowed_tools: [...cs.allowed_tools] } : {}),
        ...(cs.context_scope.length > 0 ? { context_scope: [...cs.context_scope] } : {}),
      }
    : null
  selectedNodeData.value.capability_scope = capability_scope
  const fn = flowNodes.value.find((n: any) => n.id === selectedNodeData.value.id)
  if (fn) {
    fn.data = { ...fn.data, hasCapabilityScope: hasAny }
  }
}

watch(nodeCapabilityScope, () => syncCapabilityScopeToNode(), { deep: true })

function addCapabilityEntry(field: 'allowed_tools' | 'context_scope') {
  const input = field === 'allowed_tools' ? capabilityToolInput.value : capabilityContextInput.value
  const trimmed = input.trim()
  if (!trimmed) return
  const arr = nodeCapabilityScope[field]
  if (!arr.includes(trimmed)) arr.push(trimmed)
  if (field === 'allowed_tools') capabilityToolInput.value = ''
  else capabilityContextInput.value = ''
}

function removeCapabilityEntry(field: 'allowed_tools' | 'context_scope', value: string) {
  const arr = nodeCapabilityScope[field]
  const idx = arr.indexOf(value)
  if (idx >= 0) arr.splice(idx, 1)
}

function resetCapabilityScope() {
  nodeCapabilityScope.allowed_connectors = []
  nodeCapabilityScope.allowed_tools = []
  nodeCapabilityScope.context_scope = []
}

function syncNodeToFlow() {
  if (isViewing.value) return
  if (!selectedNodeData.value) return
  const fn = flowNodes.value.find((n: any) => n.id === selectedNodeData.value.id)
  if (fn) {
    fn.data = {
      ...fn.data,
      label: selectedNodeData.value.label,
      description: selectedNodeData.value.description || '',
      hasCapabilityScope: doesNodeHaveCapabilityScope(selectedNodeData.value),
    }
  }
}

// Parameter schema + set support
const paramSchemas = ref<any[]>([])
const paramSets = ref<any[]>([])
const selectedNodeParamSetId = ref<string | undefined>(undefined)
const selectedNodeOverrides = ref<Record<string, any>>({})

function agentParamSchema(agentId: string): any | undefined {
  const agent = agents.value.find((a: any) => a.id === agentId)
  if (!agent?.parameter_schema_id) return undefined
  return paramSchemas.value.find((ps: any) => ps.id === agent.parameter_schema_id)
}

function agentParamSchemaName(agentId: string): string | undefined {
  return agentParamSchema(agentId)?.name
}

const availableParamSets = computed(() => {
  const schema = agentParamSchema(selectedNodeData.value?.agent_id)
  if (!schema) return []
  return paramSets.value.filter((ps: any) => ps.parameter_schema_id === schema.id)
})

const paramSetOverridesKeys = computed(() => Object.keys(selectedNodeOverrides.value))

function paramDefByKey(key: string): any | undefined {
  const schema = agentParamSchema(selectedNodeData.value?.agent_id)
  return schema?.parameters?.find((p: any) => p.name === key)
}

function paramDefLabel(key: string): string {
  const def = paramDefByKey(key)
  return def?.label || def?.name || key
}

function onParamSetChange() {
  if (!selectedNodeParamSetId.value) {
    selectedNodeOverrides.value = {}
    return
  }
  const set = paramSets.value.find((ps: any) => ps.id === selectedNodeParamSetId.value)
  selectedNodeOverrides.value = { ...set?.values }
  // Also update the backend node data
  if (selectedNodeData.value) {
    selectedNodeData.value.parameter_set_id = selectedNodeParamSetId.value
    selectedNodeData.value.parameter_overrides = { ...selectedNodeOverrides.value }
  }
}

async function saveAsNewParamSet() {
  const schema = agentParamSchema(selectedNodeData.value?.agent_id)
  if (!schema) return
  const name = prompt('Name for new parameter set:')
  if (!name?.trim()) return
  try {
    const resp = await api.POST('/api/v1/parameter-schemas/{schema_id}/sets', {
      params: { path: { schema_id: schema.id } },
      body: { name: name.trim(), description: null, values: selectedNodeOverrides.value },
    })
    if (resp.error) {
      console.warn('Failed to create param set:', formatApiError(resp.error))
      return
    }
    await loadParamSets()
  } catch (err: unknown) {
    console.warn('Failed to create param set:', err)
  }
}

async function loadParamSets() {
  const schema = agentParamSchema(selectedNodeData.value?.agent_id)
  if (!schema) return
  try {
    const resp = await api.GET('/api/v1/parameter-schemas/{schema_id}/sets', {
      params: { path: { schema_id: schema.id } },
    })
    if (resp.data) {
      const data = resp.data as any
      paramSets.value = Array.isArray(data) ? data : (data.items ?? [])
    }
  } catch (e) {
    console.warn('Failed to load param sets:', e)
  }
}

const canConvert = computed(() => pickerAgentId.value !== '__all__' && pickerConnectorId.value !== '__all__')

// Backend node types that map 1:1 onto their canvas type. Everything else
// (including 'agent') collapses into the generic agent node — except dispatch
// (FAR-1141) and connector (FAR-2089), which are first-class canvas types with
// their own `#node-<type>` slot and must never be collapsed. Every canvas
// node originates here — both loadGraph() and loadViewedSnapshot() map through
// convertBackendNode — so this map is the single place that decides which
// card a node renders as.
// A Map (not an object literal) so an unknown node_type such as 'constructor'
// or 'toString' cannot resolve to an inherited Object.prototype member — it
// must fall through to 'agent', matching the original ternary chain.
const CANVAS_TYPE_BY_BACKEND_TYPE = new Map<string, string>([
  ['manual', 'manual'],
  ['router', 'router'],
  ['hitl', 'hitl'],
  ['dispatch', 'dispatch'],
  ['connector', 'connector'],
])

function convertBackendNode(n: any): any {
  const nodeType = CANVAS_TYPE_BY_BACKEND_TYPE.get(n.node_type) ?? 'agent'
  return {
    id: n.id,
    type: nodeType,
    position: n.position || { x: 0, y: 0 },
    data: {
      label: n.label || 'Node ' + shortId(n.id),
      description: n.description || '',
      node_type: n.node_type,
      parameter_set_id: n.parameter_set_id,
      parameter_overrides: n.parameter_overrides,
      hasCapabilityScope: doesNodeHaveCapabilityScope(n),
    },
  }
}

function convertBackendEdge(e: any, i: number): any {
  const isLoop = e.edge_type === 'loop'
  const isLlm = e.edge_type === 'llm'
  let style: Record<string, string>
  if (isLoop) {
    style = { stroke: '#3b82f6', strokeDasharray: '5,5' }
  } else if (isLlm) {
    style = { stroke: '#8b5cf6' }
  } else {
    style = { stroke: CANVAS_EDGE_STROKE }
  }
  return {
    id: e.id || `edge-${i}`,
    source: e.source_node_id,
    target: e.target_node_id,
    type: 'smoothstep',
    animated: isLoop,
    style,
    data: {
      hitl_review_config: e.hitl_review_config || null,
      edge_type: e.edge_type || 'normal',
      condition_expression: e.condition_expression || null,
      max_iterations: e.max_iterations || 0,
      routing_label: e.routing_label || '',
    },
  }
}

async function loadGraph() {
  pageError.value = null
  try {
    const { data, error: graphError } = await withTimeout((signal) => api.GET('/api/v1/pipelines/{pipeline_id}/graph', {
      params: { path: { pipeline_id: pipelineId } },
      signal,
    }))
    if (graphError) {
      pageError.value = t('views.PipelineEditorView.failed_to_load_graph', { error: formatApiError(graphError) })
      return
    }
    const result = data as any
    if (!result) {
      rawNodes.value = []
      rawEdges.value = []
      flowNodes.value = []
      flowEdges.value = []
      graphBaseline.value = graphFingerprint()
      return
    }
    rawNodes.value = result.nodes || []
    rawEdges.value = result.edges || []
    flowNodes.value = rawNodes.value.map(convertBackendNode)
    flowEdges.value = rawEdges.value.map(convertBackendEdge)
    // FAR-1812: every load (and therefore every successful save, which
    // round-trips through loadGraph) re-baselines the dirty check.
    graphBaseline.value = graphFingerprint()
  } catch (e: unknown) {
    pageError.value = t('views.PipelineEditorView.failed_to_load_graph', { error: formatApiError(e) })
  }
}

// FAR-1812: this list feeds the version chip / compare dropdown only. The
// `?version=` alias does NOT resolve against it — it resolves through
// resolveVersionAlias()'s own bounded search over the same endpoint (page 1
// at page_size=100 + a binary search), so a partial or failed catalog load
// can never make an existing deep version read as missing.
//
// FAR-1816/1817: the catalog fetches the same widened window the timeline
// fetches (lib/pipelineVersion SNAPSHOT_PAGE_SIZE) for the compare pickers.
//
// FAR-1895: the CHIP does not read that window. Run-start snapshots are
// created unconditionally per run and never pruned, so any fixed page can be
// entirely `run` rows on a long-lived scheduled pipeline. The chip therefore
// asks the server for the newest version directly — an `exclude_created_kind`
// filter plus `exclude_drafts` — a query that cannot be starved by history
// size. It is resolved by loadCurrentVersionSnapshot BELOW, on a request that
// is STARTED alongside this catalog load but is NOT awaited by it: its failure
// and its latency (including its own deadline) degrade the chip, never the
// whole catalog.
async function loadCatalog() {
  pageError.value = null
  // FAR-1895: start the chip's candidate request CONCURRENTLY with the catalog
  // and DO NOT await it. It is an independent auxiliary request; a stalled
  // filtered query sits until its own request deadline, and awaiting it here
  // (as this previously did) carried that deadline onto the editor's load —
  // loadCatalog is one leg of the useDataFetch Promise.all, so the whole-page
  // spinner waited on it. The catalog below resolves and the editor paints
  // without this request; the chip resolves or degrades when it settles.
  // loadCurrentVersionSnapshot records its own failure in `versionChipError`
  // and never rejects, so this can neither blank the catalog nor produce an
  // unhandled rejection.
  void loadCurrentVersionSnapshot()
  try {
    const [a, c, mb, s, snaps, ps] = await Promise.all([
      withTimeout((signal) => api.GET('/api/v1/agents', { signal }).then(r => (r.data as any)?.items ?? [])).catch(() => [] as any[]),
      withTimeout((signal) => api.GET('/api/v1/connectors', { signal }).then(r => (r.data as any)?.items ?? [])).catch(() => [] as any[]),
      withTimeout((signal) => api.GET('/api/v1/model-backends', { signal }).then(r => (r.data as any)?.items ?? [])).catch(() => [] as any[]),
      withTimeout((signal) => api.GET('/api/v1/schemas', { signal }).then(r => (r.data as any)?.items ?? [])).catch(() => [] as any[]),
      withTimeout((signal) => api.GET('/api/v1/pipelines/{pipeline_id}/snapshots', {
        params: { path: { pipeline_id: pipelineId }, query: { page_size: SNAPSHOT_PAGE_SIZE } },
        signal,
      }).then(r => (r.data as any)?.items ?? [])).catch(() => [] as any[]),
      withTimeout((signal) => api.GET('/api/v1/parameter-schemas', { signal }).then(r => (r.data as any)?.items ?? [])).catch(() => [] as any[]),
    ])
    agents.value = a
    connectors.value = c
    modelBackends.value = mb
    schemas.value = s
    snapshots.value = (snaps as any[]).filter((sn: any) => sn.snapshot_version > 0)
    paramSchemas.value = ps
  } catch (e) {
    console.warn('Failed to load pipeline data', e)
  }
}

/**
 * FAR-1895: resolve the newest VERSION (substantive kind, not a draft) with a
 * server-filtered query that no run streak can starve.
 *
 * Failure handling is the point of this helper. The typed client returns
 * `{ error }` WITHOUT throwing on 4xx/5xx, and `withTimeout` can reject on its
 * own deadline, so both HTTP failures and timeouts are caught here and recorded
 * in `versionChipError`. `currentVersionSnapshots` is CLEARED on failure — not
 * merely left untouched — so `currentVersionSnapshot` falls back to the
 * already-loaded unfiltered catalog even when a PREVIOUS query succeeded. That
 * matters after a rollback: the reload re-queries this endpoint, and without the
 * clear a failed re-query would keep serving the pre-rollback version. On
 * failure the chip falls back to the substantive version in the loaded catalog;
 * if the catalog's first page has no substantive version, the chip is hidden and
 * the failure is surfaced via `versionChipError` / retry. It never serves a
 * STALE version.
 */
async function loadCurrentVersionSnapshot() {
  const token = ++versionChipToken
  versionChipError.value = null
  try {
    // page_size 2, not 1: the shared client predicate post-filters
    // `snapshot_version > 0` (a version-0 placeholder must never be the chip),
    // so a single row could be consumed by that filter and collapse the chip.
    // (pipeline_id, snapshot_version) is unique, so at most one version-0 row
    // exists; two rows are enough to survive it. `exclude_drafts` keeps an
    // in-progress autosave (created_kind='edit', real version) from being
    // labelled the current version — the sibling channel resolver filters
    // drafts the same way.
    const { data, error } = await withTimeout((signal) => api.GET('/api/v1/pipelines/{pipeline_id}/snapshots', {
      params: {
        path: { pipeline_id: pipelineId },
        query: { page_size: 2, exclude_created_kind: [...NON_VERSION_CREATED_KINDS], exclude_drafts: true },
      },
      signal,
    }))
    if (token !== versionChipToken) return
    if (error) {
      // Clear the previous success FIRST. This ref is only reassigned on
      // success, so without the clear a failed RE-query (e.g. the reload after
      // a rollback) would leave the pre-rollback list in place; the `??` in
      // currentVersionSnapshot prefers it over the fresh catalog, so the chip
      // would show the OLD version and the documented catalog fallback would be
      // unreachable. Emptying it makes the fallback real and the docstring true.
      currentVersionSnapshots.value = []
      versionChipError.value = formatApiError(error)
      return
    }
    currentVersionSnapshots.value = ((data as any)?.items ?? []).filter((sn: any) => sn.snapshot_version > 0)
  } catch (e: unknown) {
    if (token !== versionChipToken) return
    console.warn('Failed to resolve the pipeline version chip', e)
    currentVersionSnapshots.value = []
    versionChipError.value = formatApiError(e)
  }
}

function onNodeClick(event: any) {
  selectedEdgeData.value = null
  const node = event.node
  if (!node) return
  // FAR-1812: the inspector reads the graph the canvas is SHOWING — the
  // frozen snapshot while viewing, the live graph otherwise.
  const backendNode = inspectSourceNodes.value.find((n: any) => n.id === node.id)
  selectedNodeData.value = backendNode || null
  // Populate capability scope (narrow-not-widen)
  const scope = backendNode?.capability_scope
  nodeCapabilityScope.allowed_connectors = Array.isArray(scope?.allowed_connectors) ? [...scope.allowed_connectors] : []
  nodeCapabilityScope.allowed_tools = Array.isArray(scope?.allowed_tools) ? [...scope.allowed_tools] : []
  nodeCapabilityScope.context_scope = Array.isArray(scope?.context_scope) ? [...scope.context_scope] : []
  // Populate parameter set + overrides
  if (backendNode?.parameter_set_id) {
    selectedNodeParamSetId.value = backendNode.parameter_set_id
    selectedNodeOverrides.value = { ...backendNode.parameter_overrides }
    loadParamSets()
  } else {
    selectedNodeParamSetId.value = undefined
    selectedNodeOverrides.value = {}
  }
}

function onEdgeClick(event: any) {
  selectedNodeData.value = null
  const edge = event.edge
  if (!edge) return
  const backendEdge = inspectSourceEdges.value.find((e: any) => e.id === edge.id)
  if (backendEdge) {
    selectedEdgeData.value = backendEdge
    populateEdgeForm(backendEdge)
  }
}

function populateEdgeForm(edge: any) {
  // Reset to defaults FIRST, then apply the edge's own values. Resetting in
  // the gate-less branch instead would wipe edge_type / condition_expression /
  // max_iterations / routing_label with defaults: a loop/llm edge without a
  // HITL gate would open as type "normal" and saving the panel would
  // overwrite the graph edge's type.
  Object.assign(edgeForm, { ...defaultEdgeForm })
  edgeForm.edge_type = edge.edge_type || 'normal'
  edgeForm.condition_expression = edge.condition_expression || ''
  edgeForm.max_iterations = edge.max_iterations || 0
  edgeForm.routing_label = edge.routing_label || ''
  const hc = edge.hitl_review_config
  if (hc) {
    edgeForm.hitl_enabled = true
    edgeForm.label = hc.label || ''
    edgeForm.description = hc.description || ''
    edgeForm.claim_expiry_minutes = hc.claim_expiry_minutes || 15
    // FAR-609: an existing config without the key is human-only now —
    // only an explicit `false` loads the checkbox unticked.
    edgeForm.human_only = hc.human_only !== false
    if (hc.condition) {
      edgeForm.condition_type = 'jmespath'
      edgeForm.condition = hc.condition
      edgeForm.eval_name = ''
      edgeForm.eval_threshold = 0.8
      edgeForm.eval_operator = 'lt'
    } else if (hc.eval_condition) {
      edgeForm.condition_type = 'eval'
      edgeForm.eval_name = hc.eval_condition.eval_name || ''
      edgeForm.eval_threshold = hc.eval_condition.threshold ?? 0.8
      edgeForm.eval_operator = hc.eval_condition.operator || 'lt'
      edgeForm.condition = ''
    } else {
      edgeForm.condition_type = 'none'
      edgeForm.condition = ''
      edgeForm.eval_name = ''
      edgeForm.eval_threshold = 0.8
      edgeForm.eval_operator = 'lt'
    }
  }
}

function buildHitlReviewConfig(): any {
  if (!edgeForm.hitl_enabled) return null
  const config: any = {
    label: edgeForm.label || t('views.PipelineEditorView.review_gate'),
    description: edgeForm.description || '',
    reject_target: selectedEdgeData.value?.hitl_review_config?.reject_target || null,
    claim_expiry_minutes: edgeForm.claim_expiry_minutes || 15,
    human_only: edgeForm.human_only || false,
    required_team_id: selectedEdgeData.value?.hitl_review_config?.required_team_id || null,
  }
  // FAR-1487: carry over the reject-disposition fields this form has no control
  // for - dropping them on a save would silently flip an explicit
  // `on_reject: proceed` (or a correction_target) back to the terminate default.
  const existingGate = selectedEdgeData.value?.hitl_review_config
  if (existingGate?.on_reject) config.on_reject = existingGate.on_reject
  if (existingGate?.correction_target) config.correction_target = existingGate.correction_target
  if (edgeForm.condition_type === 'jmespath' && edgeForm.condition) {
    config.condition = edgeForm.condition
  }
  if (edgeForm.condition_type === 'eval' && edgeForm.eval_name) {
    config.eval_condition = {
      eval_name: edgeForm.eval_name,
      threshold: edgeForm.eval_threshold,
      operator: edgeForm.eval_operator,
    }
  }
  return config
}

async function saveEdgeConfig() {
  if (isViewing.value) return
  if (!selectedEdgeData.value) return
  // FAR-613: a HITL gate must explain WHY it exists — the backend rejects
  // saves whose gate config carries no usable description (min 20 trimmed
  // chars, counted as code points to match Python's len()), so block
  // client-side first with a clear, localised message (FAR-688 single const).
  if (hitlDescriptionTooShort.value) {
    edgeSaveError.value = t('views.PipelineEditorView.hitl_description_required')
    return
  }
  savingEdge.value = true
  edgeSaveError.value = null

  const updatedEdges = rawEdges.value.map((e: any) => {
    if (e.id === selectedEdgeData.value.id) {
      return {
        id: e.id,
        source_node_id: e.source_node_id,
        target_node_id: e.target_node_id,
        edge_type: edgeForm.edge_type,
        condition_expression: edgeForm.condition_expression || null,
        max_iterations: edgeForm.edge_type === 'loop' ? (edgeForm.max_iterations || 0) : undefined,
        routing_label: edgeForm.edge_type === 'llm' ? (edgeForm.routing_label || undefined) : undefined,
        hitl_review_config: buildHitlReviewConfig(),
        source_port: e.source_port || 'out',
        target_port: e.target_port || 'in',
      }
    }
    return {
      id: e.id,
      source_node_id: e.source_node_id,
      target_node_id: e.target_node_id,
      edge_type: e.edge_type || 'normal',
      condition_expression: e.condition_expression || null,
      hitl_review_config: e.hitl_review_config || null,
      source_port: e.source_port || 'out',
      target_port: e.target_port || 'in',
    }
  })

  try {
    await withTimeout((signal) => api.PUT('/api/v1/pipelines/{pipeline_id}/graph', {
      params: { path: { pipeline_id: pipelineId } },
      body: {
        nodes: rawNodes.value.map(buildNodePayload),
        edges: updatedEdges,
      },
      signal,
    }))
    await loadGraph()
    const updatedEdge = rawEdges.value.find((e: any) => e.id === selectedEdgeData.value.id)
    if (updatedEdge) {
      selectedEdgeData.value = updatedEdge
      populateEdgeForm(updatedEdge)
    }
  } catch (e: unknown) {
    edgeSaveError.value = formatApiError(e)
  } finally {
    savingEdge.value = false
  }
}

function onPaneClick() {
  selectedNodeData.value = null
  selectedEdgeData.value = null
  showSaveAsDropdown.value = false
  selectedNodeParamSetId.value = undefined
  selectedNodeOverrides.value = {}
  nodeCapabilityScope.allowed_connectors = []
  nodeCapabilityScope.allowed_tools = []
  nodeCapabilityScope.context_scope = []
  capabilityToolInput.value = ''
  capabilityContextInput.value = ''
}

// --- Editor viewport fit (FAR-1841 mobile, FAR-1851 desktop) --------------
// The editor root is pinned to the space actually left in the viewport below
// the app chrome (banners, breadcrumb, fixed mobile header) at EVERY
// breakpoint, measured at runtime rather than guessed with a `100vh - fixed`
// height. On mobile that stops the canvas hanging below the fold where VueFlow
// captures wheel/touch (FAR-1841); on desktop the same pin stops the editor
// overflowing the fold by the height of the breadcrumb row above it (FAR-1851
// — the `h-[calc(100vh-3.5rem)]` fallback only ever accounted for the mobile
// fixed header). The 240px usability floor the mobile fix introduced yields to
// the viewport (FAR-1861): on a viewport shorter than the chrome stack the
// editor shrinks to whatever is left instead of spilling below the fold and
// re-introducing a trapped page scroll.
const editorRootRef = ref<HTMLElement | null>(null)
// The canvas region. Focusable (`tabindex="-1"`) so it can receive focus as the
// fallback return target when a properties sheet's trigger was not focusable
// (FAR-1861 — a VueFlow node/edge wrapper carries no tabindex, so capturing
// `document.activeElement` at open yields <body> and restoring it is a no-op).
const canvasRef = ref<HTMLElement | null>(null)
const editorFitHeightPx = ref<number | null>(null)
const editorFitStyle = computed(() =>
  editorFitHeightPx.value != null ? { height: `${editorFitHeightPx.value}px` } : undefined,
)

function refitEditorFit() {
  const el = editorRootRef.value
  if (!el) return
  // Document-relative top so the measurement is stable regardless of the
  // current scroll position and cannot feed back through the height set here.
  // top <= 0 means the element is not laid out yet (jsdom mounts return an
  // all-zero rect) — skip the measure, but also clear any previously pinned
  // height: leaving it would keep a stale pin that no longer matches layout.
  const top = el.getBoundingClientRect().top + window.scrollY
  if (top <= 0) {
    editorFitHeightPx.value = null
    return
  }
  const available = Math.floor(window.innerHeight - top)
  // available <= 0 (chrome taller than the viewport) falls back to the height
  // class: there is no space to fit into and a zero-height editor would be
  // worse than a page that scrolls.
  const next = available > 0 ? available : null
  // The measurement above is the forced layout; skip the reactive write (and
  // the re-render it causes) when nothing changed.
  if (editorFitHeightPx.value !== next) editorFitHeightPx.value = next
}

let refitFrame = 0
function scheduleEditorRefit() {
  if (refitFrame) return
  refitFrame = window.requestAnimationFrame(() => {
    refitFrame = 0
    refitEditorFit()
  })
}

let editorFitObserver: MutationObserver | null = null
// Re-measure only when the CHROME ABOVE the editor changes — that is the
// only thing that moves the editor's document-relative top. A body-wide
// observer (childList+subtree+attributes:class/style) also fired on
// VueFlow's viewport transform, which is written as an inline style on
// every pointermove during pan/zoom/node-drag — turning the mobile hot
// path into a forced synchronous layout (the getBoundingClientRect in
// refitEditorFit) once per animation frame. Narrow targets instead:
//  - the editor's parent (AppLayout's <main>): its `class` carries the
//    padding-top offsets (fixed mobile header / onboarding banner) and its
//    direct childList catches a new chrome block inserted above the editor,
//    without ever descending into the editor where VueFlow lives;
//  - each element ABOVE the editor root inside that parent (banner
//    container, breadcrumb): subtree childList + class/style attributes
//    catch those banners mounting, unmounting and toggling asynchronously —
//    the behaviour the original body-wide observer existed for.
// Extracted from onMounted so each guard (root / parent present) is
// unit-testable; `root` is null only in the defensive detached case.
function setupEditorFitObserver(root: HTMLElement | null) {
  const parent = root?.parentElement ?? null
  if (root && parent) {
    // Stop at the first element containing the root (the root itself when it
    // is a direct child of <main>): the editor subtree — and therefore
    // VueFlow's viewport — is never observed, however deeply the root nests.
    const observeChromeSiblings = () => {
      for (
        let sibling = parent.firstElementChild;
        sibling && !sibling.contains(root);
        sibling = sibling.nextElementSibling
      ) {
        editorFitObserver?.observe(sibling, {
          childList: true,
          subtree: true,
          attributes: true,
          attributeFilter: ['class', 'style'],
        })
      }
    }
    editorFitObserver = new MutationObserver((mutations) => {
      // A chrome block inserted above the editor is a new sibling — observe
      // it too, so mutations inside it keep re-fitting (observe() on an
      // already-observed node just refreshes its options, no duplicates).
      if (mutations.some((m) => m.type === 'childList' && m.target === parent)) {
        observeChromeSiblings()
      }
      scheduleEditorRefit()
    })
    editorFitObserver.observe(parent, {
      childList: true,
      attributes: true,
      attributeFilter: ['class'],
    })
    observeChromeSiblings()
  }
}

// --- Mobile properties sheets as modal dialogs (FAR-1861) ------------------
// Below the desktop breakpoint each properties aside renders as a full-screen
// overlay sheet, and must behave as a modal dialog for keyboard/AT users
// (ux-conformance A11Y): exposed as a labelled `role="dialog"` with
// `aria-modal`, Escape closes it, focus moves into it on open and returns to
// the trigger on close, and Tab is trapped inside so it can never reach the
// content behind it. On desktop the identical element is a docked, non-modal
// side panel, so none of the dialog semantics apply. `useMediaQuery` keeps the
// breakpoint reactive so a resize/rotation across 768px re-applies the right
// semantics.
const DESKTOP_MEDIA_QUERY = '(min-width: 768px)'
const isDesktopViewport = useMediaQuery(DESKTOP_MEDIA_QUERY)
const isMobileSheet = computed(() => !isDesktopViewport.value)

const NODE_SHEET_TITLE_ID = 'pipeline-editor-node-sheet-title'
const EDGE_SHEET_TITLE_ID = 'pipeline-editor-edge-sheet-title'

const nodePanelRef = ref<HTMLElement | null>(null)
const edgePanelRef = ref<HTMLElement | null>(null)
// The element focus is returned to when the sheet closes — captured at open.
let sheetReturnFocus: HTMLElement | null = null

// Spread onto a properties aside: the mobile sheet is a labelled modal dialog;
// the desktop docked panel gets no dialog attrs (and stays out of the tab
// order's focus entry points, hence tabindex only on the sheet).
function sheetDialogAttrs(titleId: string): Record<string, string> {
  return isMobileSheet.value
    ? { role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': titleId, tabindex: '-1' }
    : {}
}

// The properties sheet currently rendered — edge wins when both are set (the
// node aside is `selectedNodeData && !selectedEdgeData`); null when neither pane
// is open.
function activeSheetElement(): HTMLElement | null {
  return selectedEdgeData.value ? edgePanelRef.value : nodePanelRef.value
}

function onSheetKeydown(event: KeyboardEvent) {
  if (!isMobileSheet.value) return
  if (event.key === 'Escape') {
    event.preventDefault()
    onPaneClick()
    return
  }
  if (event.key !== 'Tab') return
  const panel = activeSheetElement()
  if (!panel) return
  // The Tab-cycle (including the freshly-opened panel-container case and the
  // zero-focusable guard) lives in the shared helper so the algorithm and its
  // selector have a single owner — see `src/composables/useFocusTrap.ts`.
  trapTabInElement(event, panel)
}

// Move focus into a sheet when it opens and return it to the trigger when it
// closes. Skipped entirely on desktop (docked panels keep the normal tab flow).
// `flush: 'post'` matters: the sheet's template ref only points at the rendered
// element after the DOM update, so a default (pre) flush would read a stale ref
// and miss the open.
watch([selectedNodeData, selectedEdgeData], () => {
  if (!isMobileSheet.value) return
  const panel = activeSheetElement()
  if (!panel) {
    sheetReturnFocus?.focus()
    sheetReturnFocus = null
    return
  }
  // Focus already inside the sheet (e.g. the selection changed while it was
  // open) — nothing to move or to remember.
  if (!panel.contains(document.activeElement)) {
    // The real triggers (VueFlow node/edge wrappers) carry no tabindex, so a
    // canvas click leaves `document.activeElement` on <body>; restoring <body>
    // is a no-op. Fall back to the focusable canvas so focus actually returns
    // somewhere sensible when the sheet closes (FAR-1861).
    const active = document.activeElement as HTMLElement | null
    sheetReturnFocus = active && active !== document.body ? active : canvasRef.value
    panel.focus()
  }
}, { flush: 'post' })

onMounted(() => {
  refitEditorFit()
  window.addEventListener('resize', scheduleEditorRefit)
  setupEditorFitObserver(editorRootRef.value)
})
onBeforeUnmount(() => {
  window.removeEventListener('resize', scheduleEditorRefit)
  editorFitObserver?.disconnect()
  if (refitFrame) window.cancelAnimationFrame(refitFrame)
})

const newNodeType = ref<'agent' | 'router' | 'hitl'>('agent')
const nodeTypeOptions = [
  { value: 'agent', label: t('views.PipelineEditorView.node_type_agent') },
  // Router/HITL authoring is deferred: addNode cannot yet emit the mandatory
  // router_config/hitl_config the backend requires, so exposing them here
  // produces unsavable (422) nodes. Imported router/HITL graphs still render
  // correctly via convertBackendNode. Re-add once a config editor lands.
]

function addNode(nodeType: 'agent' | 'router' | 'hitl' = newNodeType.value) {
  if (isViewing.value) return
  const id = `node-${Date.now()}`
  const newNode = {
    id,
    type: nodeType,
    position: { x: 250, y: 100 },
    data: { label: t('views.PipelineEditorView.new_node_label'), description: '', node_type: nodeType },
  }
  flowNodes.value = [...flowNodes.value, newNode]
  rawNodes.value = [...rawNodes.value, {
    id,
    node_type: nodeType,
    label: t('views.PipelineEditorView.new_node_label'),
    description: '',
    position: { x: 250, y: 100 },
  }]
}

function openAgentPicker() {
  convertError.value = null
  pickerAgentId.value = '__all__'
  pickerConnectorId.value = '__all__'
  showAgentPicker.value = true
}

function openRevertDialog() {
  revertError.value = null
  revertSnapshotId.value = '__all__'
  showRevertDialog.value = true
}

function openSaveAsComposite() {
  showSaveAsDropdown.value = false
  saveAsName.value = ''
  saveAsDescription.value = ''
  saveAsSelectedNodeIds.value = rawNodes.value.map((n: any) => n.id)
  saveAsError.value = null
  showSaveAsComposite.value = true
}

async function handleSaveAsComposite() {
  if (isViewing.value) return
  if (!saveAsName.value || saveAsSelectedNodeIds.value.length === 0) return
  saving.value = true
  saveAsError.value = null
  try {
    await withTimeout((signal) => api.POST('/api/v1/pipelines/{pipeline_id}/save-as-composite', {
      params: { path: { pipeline_id: pipelineId } },
      body: {
        name: saveAsName.value,
        description: saveAsDescription.value || null,
        selected_node_ids: saveAsSelectedNodeIds.value,
      },
      signal,
    }))
    showSaveAsComposite.value = false
    router.push({ name: 'library' })
  } catch (e: unknown) {
    saveAsError.value = formatApiError(e)
  } finally {
    saving.value = false
  }
}

function onAgentChange() {
  pickerConnectorId.value = '__all__'
}

async function convertToAgent() {
  if (isViewing.value) return
  if (!canConvert.value || !selectedNodeData.value) return
  convertError.value = null
  try {
    const nodeId = selectedNodeData.value.id
    await withTimeout((signal) => api.POST('/api/v1/pipelines/{pipeline_id}/nodes/{node_id}/convert-to-agent', {
      params: { path: { pipeline_id: pipelineId, node_id: nodeId } },
      body: {
        agent_id: pickerAgentId.value,
        // FAR-1141: `operation` / `dispatch_action` are now part of the
        // generated ConnectorBinding contract (they used to be silently
        // dropped on save). A converted agent node performs a plain query
        // binding, which is exactly the API default — stated explicitly so
        // the wire payload matches the schema instead of relying on it.
        connector_binding: {
          type: connectors.value.find(c => c.id === pickerConnectorId.value)?.connector_type_id || '',
          instance_id: pickerConnectorId.value,
          operation: 'query' as const,
          dispatch_action: 'trigger_run' as const,
        },
        model_backend_id: selectedAgent.value?.model_backend_id,
      },
      signal,
    }))
    showAgentPicker.value = false
    await loadGraph()
    selectedNodeData.value = rawNodes.value.find((n: any) => n.id === nodeId) || null
  } catch (e: unknown) {
    convertError.value = formatApiError(e)
  }
}

async function revertToManual() {
  if (revertSnapshotId.value === '__all__' || !selectedNodeData.value) return
  revertError.value = null
  revertLoading.value = true
  try {
    const nodeId = selectedNodeData.value.id
    await withTimeout((signal) => api.POST('/api/v1/pipelines/{pipeline_id}/nodes/{node_id}/revert-to-manual', {
      params: {
        path: { pipeline_id: pipelineId, node_id: nodeId },
        query: { snapshot_id: revertSnapshotId.value },
      },
      signal,
    }))
    showRevertDialog.value = false
    await loadGraph()
    selectedNodeData.value = rawNodes.value.find((n: any) => n.id === nodeId) || null
  } catch (e: unknown) {
    revertError.value = formatApiError(e)
  } finally {
    revertLoading.value = false
  }
}

async function loadPipeline() {
  pageError.value = null
  try {
    const { data } = await withTimeout((signal) => api.GET('/api/v1/pipelines/{pipeline_id}', {
      params: { path: { pipeline_id: pipelineId } },
      signal,
    }))
    pipeline.value = data as any
    maxDurationInput.value = (data as any)?.max_duration_seconds ?? undefined
    circuitBreakerInput.value = data?.circuit_breaker_threshold ?? ''
    maxAutonomyInput.value = (data as any)?.max_autonomy_level ?? null
    syncHitlReviewWindowFromPipeline()
    syncQueueTimeoutFromPipeline()
    syncEnvironmentProfileFromPipeline()
    syncRetryPolicyFromPipeline()
  } catch (e) {
    pageError.value = t('views.PipelineEditorView.failed_to_load_pipeline', { error: formatApiError(e) })
  }
}

function openRenameDialog() {
  renameName.value = pipeline.value?.name || ''
  renameError.value = null
  showRenameDialog.value = true
}

async function handleRename() {
  if (!renameName.value.trim()) return
  renaming.value = true
  renameError.value = null
  try {
    const { data } = await withTimeout((signal) => api.PATCH('/api/v1/pipelines/{pipeline_id}', {
      params: { path: { pipeline_id: pipelineId } },
      body: { name: renameName.value.trim() },
      signal,
    }))
    pipeline.value = data as any
    showRenameDialog.value = false
  } catch (e: unknown) {
    renameError.value = formatApiError(e)
  } finally {
    renaming.value = false
  }
}

async function handleArchive() {
  try {
    pipeline.value = throwOnError(await withTimeout((signal) => api.POST('/api/v1/pipelines/{pipeline_id}/archive', {
      params: { path: { pipeline_id: pipelineId } },
      signal,
    }), TOGGLE_REQUEST_TIMEOUT_MS))
  } catch (e: unknown) {
    pageError.value = t('views.PipelineEditorView.failed_to_archive_pipeline', { error: formatApiError(e) })
  }
}

async function handleUnarchive() {
  try {
    pipeline.value = throwOnError(await withTimeout((signal) => api.POST('/api/v1/pipelines/{pipeline_id}/unarchive', {
      params: { path: { pipeline_id: pipelineId } },
      signal,
    }), TOGGLE_REQUEST_TIMEOUT_MS))
  } catch (e: unknown) {
    pageError.value = t('views.PipelineEditorView.failed_to_unarchive_pipeline', { error: formatApiError(e) })
  }
}

// FAR-1530: pause/resume are in-place toolbar toggles, so their failures report
// through the inline toolbar error (not `pageError`, which replaces the whole
// editor and would drop the open graph over a failed toggle).
async function handlePause() {
  try {
    pipeline.value = throwOnError(await withTimeout((signal) => api.POST('/api/v1/pipelines/{pipeline_id}/pause', {
      params: { path: { pipeline_id: pipelineId } },
      signal,
    }), TOGGLE_REQUEST_TIMEOUT_MS))
    saveGraphError.value = null
  } catch (e: unknown) {
    saveGraphError.value = t('views.PipelineEditorView.failed_to_pause_pipeline', { error: formatApiError(e) })
  }
}

async function handleResume() {
  try {
    pipeline.value = throwOnError(await withTimeout((signal) => api.POST('/api/v1/pipelines/{pipeline_id}/resume', {
      params: { path: { pipeline_id: pipelineId } },
      signal,
    }), TOGGLE_REQUEST_TIMEOUT_MS))
    saveGraphError.value = null
  } catch (e: unknown) {
    saveGraphError.value = t('views.PipelineEditorView.failed_to_resume_pipeline', { error: formatApiError(e) })
  }
}

async function handleDelete() {
  deleteError.value = null
  try {
    await withTimeout((signal) => api.DELETE('/api/v1/pipelines/{pipeline_id}', {
      params: { path: { pipeline_id: pipelineId } },
      signal,
    }))
    router.push({ name: 'library' })
  } catch (e: unknown) {
    deleteError.value = formatApiError(e)
  }
}

async function updateMaxDuration() {
  const val = maxDurationInput.value && maxDurationInput.value > 0 ? maxDurationInput.value : undefined
  try {
    await withTimeout((signal) => api.PATCH('/api/v1/pipelines/{pipeline_id}', {
      params: { path: { pipeline_id: pipelineId } },
      body: { max_duration_seconds: val },
      signal,
    }))
    if (pipeline.value) pipeline.value.max_duration_seconds = val
    saveGraphError.value = null
  } catch (e) {
    saveGraphError.value = t('views.PipelineEditorView.failed_to_update_max_duration', { error: formatApiError(e) })
  }
}

async function updateCircuitBreakerThreshold() {
  const raw = circuitBreakerInput.value
  const empty = raw === '' || raw === null || raw === undefined
  const parsed = empty ? null : Number(raw)
  if (parsed !== null && (!Number.isFinite(parsed) || parsed <= 0)) {
    saveGraphError.value = t('views.PipelineEditorView.spend_circuit_breaker_invalid')
    return
  }
  try {
    const { data } = await withTimeout((signal) => api.PATCH('/api/v1/pipelines/{pipeline_id}', {
      params: { path: { pipeline_id: pipelineId } },
      body: { circuit_breaker_threshold: parsed },
      signal,
    }))
    if (pipeline.value) pipeline.value.circuit_breaker_threshold = data?.circuit_breaker_threshold ?? parsed
    circuitBreakerInput.value = data?.circuit_breaker_threshold ?? parsed ?? ''
    saveGraphError.value = null
  } catch (e) {
    saveGraphError.value = t('views.PipelineEditorView.failed_to_update_spend_circuit_breaker', { error: formatApiError(e) })
  }
}

async function resetCircuitBreaker() {
  resettingCircuitBreaker.value = true
  try {
    await withTimeout((signal) => api.POST('/api/v1/admin/costs/circuit-breaker/{pipeline_id}/reset', {
      params: { path: { pipeline_id: pipelineId } },
      signal,
    }))
    if (pipeline.value) {
      pipeline.value.circuit_breaker_tripped = false
      pipeline.value.circuit_breaker_tripped_at = null
    }
    saveGraphError.value = null
  } catch (e) {
    saveGraphError.value = t('views.PipelineEditorView.failed_to_reset_circuit_breaker', { error: formatApiError(e) })
  } finally {
    resettingCircuitBreaker.value = false
  }
}

async function updateMaxAutonomyLevel(event: Event) {
  const raw = (event.target as HTMLSelectElement).value
  const val = raw === '' ? null : raw
  maxAutonomyInput.value = val
  try {
    const { error, response } = await withTimeout((signal) => api.PATCH('/api/v1/pipelines/{pipeline_id}', {
      params: { path: { pipeline_id: pipelineId } },
      body: { max_autonomy_level: val },
      signal,
    }))
    if (error) {
      // A 422 means the ceiling is below the pipeline's default level.
      saveGraphError.value = response?.status === 422
        ? t('views.PipelineEditorView.max_autonomy_below_default', { error: formatApiError(error) })
        : t('views.PipelineEditorView.failed_to_update_max_autonomy', { error: formatApiError(error) })
      // The optimistic set above must not outlive a failed PATCH — restore the
      // control to the persisted value (pipeline.value was never updated).
      maxAutonomyInput.value = pipeline.value?.max_autonomy_level ?? null
      return
    }
    if (pipeline.value) pipeline.value.max_autonomy_level = val
    saveGraphError.value = null
  } catch (e: unknown) {
    saveGraphError.value = t('views.PipelineEditorView.failed_to_update_max_autonomy', { error: formatApiError(e) })
    // Timeout / network failure: same revert — never display a value the
    // server rejected.
    maxAutonomyInput.value = pipeline.value?.max_autonomy_level ?? null
  }
}

// FAR-1558 slice 2: per-pipeline environment profile - the runtime provider
// this pipeline's runs dispatch on. The select is the ONLY send site, which
// is what makes omit-vs-null unambiguous: an untouched control fires no
// request at all (the stored binding stays OMITTED and therefore unchanged),
// while choosing "None (default route)" sends an explicit `null` (clears the
// binding and restores the default route).
const environmentProfilesStore = useEnvironmentProfilesStore()
const environmentProfileInput = ref('')
const environmentProfileUpdateError = ref<string | null>(null)
const environmentProfileStatus = ref<string | null>(null)

// The profile list endpoint is feature-gated (`environment_profiles`), so
// render + fetch only when the plan grants it - never a request we know fails.
const showEnvironmentProfileControl = computed(() =>
  planStore.featureEnabled('environment_profiles'),
)

// Options are the org's profiles, PLUS a placeholder for a binding the list
// cannot resolve (the list is team-scoped, so a profile bound by another team
// may be invisible here). Rendering the bound id as an option keeps the
// control from displaying "None" while a binding is actually stored - the
// select never misrepresents the current state.
const environmentProfileOptions = computed(() => {
  const rows = environmentProfilesStore.profiles.map((p) => ({
    id: p.id,
    label: profileOptionLabel(p.name, p.provider_type),
  }))
  const bound = environmentProfileInput.value
  if (bound && !rows.some((row) => row.id === bound)) {
    rows.unshift({
      id: bound,
      label: t('views.PipelineEditorView.environment_profile_unavailable'),
    })
  }
  return rows
})

// Load failure (feature gate, network) surfaces alongside a write failure -
// a profile list we could not load must not render as a silent empty menu.
const environmentProfileError = computed(() => {
  if (environmentProfileUpdateError.value) return environmentProfileUpdateError.value
  const loadError = environmentProfilesStore.error
  if (showEnvironmentProfileControl.value && loadError) {
    return t('views.PipelineEditorView.environment_profile_load_failed', { error: loadError })
  }
  return null
})

// Option/status text: "<profile name> — <provider tier>" (e.g. "Staging
// Docker — Bundled Runner (Docker)"). A profile with no resolvable tier
// renders its name alone rather than trailing a dangling separator.
function profileOptionLabel(name: string, providerType?: string | null): string {
  const labelKey = runnerTierLabelKeyForProvider(providerType)
  return labelKey ? `${name} — ${t(labelKey)}` : name
}

function profileLabelForId(id: string): string {
  const profile = environmentProfilesStore.profiles.find((p) => p.id === id)
  return profile ? profileOptionLabel(profile.name, profile.provider_type) : id
}

function syncEnvironmentProfileFromPipeline() {
  const stored = (pipeline.value as { environment_profile_id?: string | null } | null)
    ?.environment_profile_id
  environmentProfileInput.value = stored ?? ''
}

watch(
  showEnvironmentProfileControl,
  (show) => {
    if (show && environmentProfilesStore.profiles.length === 0) {
      void environmentProfilesStore.fetchProfiles()
    }
  },
  { immediate: true },
)

async function updateEnvironmentProfile(event: Event) {
  const raw = (event.target as HTMLSelectElement).value
  // '' clears (explicit null on the wire); anything else binds that profile.
  const val = raw === '' ? null : raw
  environmentProfileInput.value = raw
  environmentProfileUpdateError.value = null
  environmentProfileStatus.value = null
  try {
    const { error } = await withTimeout((signal) => api.PATCH('/api/v1/pipelines/{pipeline_id}', {
      params: { path: { pipeline_id: pipelineId } },
      body: { environment_profile_id: val },
      signal,
    }))
    if (error) {
      // A 422 is a refused bind (profile not visible to the pipeline's owner
      // team, cross-org, ...) - never display a binding the server rejected.
      environmentProfileUpdateError.value = t('views.PipelineEditorView.environment_profile_update_failed', {
        error: formatApiError(error),
      })
      syncEnvironmentProfileFromPipeline()
      return
    }
    if (pipeline.value) pipeline.value.environment_profile_id = val
    environmentProfileStatus.value = val
      ? t('views.PipelineEditorView.environment_profile_bound', { label: profileLabelForId(val) })
      : t('views.PipelineEditorView.environment_profile_cleared')
  } catch (e: unknown) {
    // Timeout / network failure: same revert - the stored value was never
    // updated, so the control must not keep showing the rejected choice.
    environmentProfileUpdateError.value = t('views.PipelineEditorView.environment_profile_update_failed', {
      error: formatApiError(e),
    })
    syncEnvironmentProfileFromPipeline()
  }
}

function openRunDialog() {
  runPrompt.value = ''
  runError.value = null
  confirmEmptyRun.value = false
  emptyRunWarning.value = null
  showRunDialog.value = true
}

function closeRunDialog() {
  showRunDialog.value = false
  runPrompt.value = ''
  runError.value = null
  confirmEmptyRun.value = false
  emptyRunWarning.value = null
}

function onRunDialogKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape' && showRunDialog.value) {
    event.preventDefault()
    closeRunDialog()
  }
}

watch(showRunDialog, (open) => {
  if (open) {
    document.addEventListener('keydown', onRunDialogKeydown)
  } else {
    document.removeEventListener('keydown', onRunDialogKeydown)
  }
})

onBeforeUnmount(() => {
  document.removeEventListener('keydown', onRunDialogKeydown)
})

// Sandbox command authoring ? graph-save payload. The graph save REPLACES
// graph_nodes_json wholesale, so every command field must be serialised here
// or it is wiped. Mirrors the backend contract (routes/pipelines.py +
// sandbox_mode): sandbox_agent nodes carry ONLY the agent_commands list
// (the scalar agent_command field was removed in FAR-820 — the backend
// PipelineGraphNode no longer declares it, so any scalar save is silently
// dropped and the runtime join would crash on a missing list). The joiner is
// persisted as a non-empty string — a null joiner would crash the runtime
// join (None.join), so an unset joiner saves the " && " default. Non-sandbox
// nodes never gain command mutations here: the commands editor is
// sandbox-gated and the spread round-trips whatever the graph already
// carried (FAR-488a syncs a bound Agent's row from a node-level command —
// the editor must not fabricate one).
function nodeCommandFields(n: any): {
  agent_commands?: string[] | null
  commands_concatenation_string?: string | null
} {
  if (n.node_type !== 'sandbox_agent') {
    return {}
  }
  const rows = (Array.isArray(n.agent_commands) ? n.agent_commands : [])
    .map((c: unknown) => (typeof c === 'string' ? c : String(c ?? '')))
    .filter((c: string) => c.trim() !== '')
  if (rows.length === 0) {
    return { agent_commands: null, commands_concatenation_string: null }
  }
  const joiner = typeof n.commands_concatenation_string === 'string' && n.commands_concatenation_string.length > 0
    ? n.commands_concatenation_string
    : ' && '
  return { agent_commands: rows, commands_concatenation_string: joiner }
}

// View-only / UI-only keys that must never leak into a graph-save payload.
// rawNodes come from the GET /graph response (every key is a PipelineGraphNode
// field) or addNode, so none occur in practice today — this block is durable
// insurance against UI-state markers (VueFlow node fields like type/data/
// selected/dragging/dimensions, editor conveniences like hasCapabilityScope)
// ever being merged into rawNodes and silently persisted. model_backend_id is
// an Agent-level field (convert-to-agent endpoint), not a PipelineGraphNode
// field; the model tolerates extras, but it is omitted for hygiene.
// agent_command is the removed scalar (FAR-820); PipelineGraphNode no longer
// declares it, so any legacy value must never be persisted.
const VIEW_ONLY_NODE_KEYS = new Set([
  'type',
  'data',
  'selected',
  'dragging',
  'dimensions',
  'hasCapabilityScope',
  'model_backend_id',
  'agent_command',
])

// Build one node's save payload by spreading the raw node data — the GET
// response is typed by PipelineGraphNode, so every model-accepted field
// (template_id, composite_*, schema pins, ports, egress/resource/sandbox
// config, autonomy_recommendation, command/prompt fields, ...) survives the
// save — then layering the command normalisation on top. Position flows
// through the spread exactly as the previous hand-maintained map sent it
// (n.position, the graph's {x, y} — VueFlow drag state is not synced).
// Return is `any` because the payload is dynamically spread from the (any)
// raw node data; the generated client's node body type cannot express that.
function buildNodePayload(n: any): any {
  const payload: Record<string, unknown> = {}
  for (const [key, value] of Object.entries(n)) {
    if (!VIEW_ONLY_NODE_KEYS.has(key)) payload[key] = value
  }
  payload.node_type = n.node_type || 'agent'
  Object.assign(payload, nodeCommandFields(n))
  return payload
}

async function saveGraph() {
  if (isViewing.value) return
  savingGraph.value = true
  saveGraphError.value = null
  try {
    // Sync current param set + overrides into selected node data
    if (selectedNodeData.value) {
      selectedNodeData.value.parameter_set_id = selectedNodeParamSetId.value || null
      selectedNodeData.value.parameter_overrides = Object.keys(selectedNodeOverrides.value).length > 0
        ? { ...selectedNodeOverrides.value }
        : null
    }
    await withTimeout((signal) => api.PUT('/api/v1/pipelines/{pipeline_id}/graph', {
      params: { path: { pipeline_id: pipelineId } },
      body: {
        nodes: rawNodes.value.map(buildNodePayload),
        edges: rawEdges.value.map((e: any) => ({
          id: e.id,
          source_node_id: e.source_node_id,
          target_node_id: e.target_node_id,
          edge_type: e.edge_type || 'normal',
          condition_expression: e.condition_expression || null,
          max_iterations: e.edge_type === 'loop' ? (e.max_iterations || 0) : undefined,
          routing_label: e.edge_type === 'llm' ? (e.routing_label || undefined) : undefined,
          hitl_review_config: e.hitl_review_config || null,
          source_port: e.source_port || 'out',
          target_port: e.target_port || 'in',
        })),
      },
      signal,
    }))
    // Reload graph to sync flowNodes with saved state
    await loadGraph()
  } catch (e: unknown) {
    saveGraphError.value = formatApiError(e)
  } finally {
    savingGraph.value = false
  }
}

async function triggerRun() {
  if (isViewing.value) return
  if (!pipeline.value) return
  const trimmedPrompt = runPrompt.value.trim()
  if (!trimmedPrompt) {
    if (!confirmEmptyRun.value) {
      confirmEmptyRun.value = true
      emptyRunWarning.value = t('views.PipelineEditorView.empty_run_warning')
      return
    }
    emptyRunWarning.value = null
  }
  running.value = true
  runError.value = null
  try {
    await saveGraph()
    if (saveGraphError.value) {
      runError.value = t('views.PipelineEditorView.failed_to_save_graph', { error: saveGraphError.value })
      return
    }
    const { data } = await withTimeout((signal) => api.POST('/api/v1/runs', {
      body: {
        pipeline_id: pipelineId,
        input_payload: trimmedPrompt ? { prompt: trimmedPrompt } : {},
      },
      signal,
    }))
    showRunDialog.value = false
    // RunResponse's id field is run_id (POST /api/v1/runs) — reading `.id`
    // navigated to /runs/undefined (FAR-1246). Typed off the generated schema
    // so a field rename fails vue-tsc instead of silently breaking the nav.
    if (data) {
      track(ANALYTICS_EVENTS.PIPELINE_RUN_STARTED, { run_id: data.run_id })
      router.push({ name: 'run-detail', params: { id: data.run_id } })
    }
  } catch (e: unknown) {
    runError.value = formatApiError(e)
  } finally {
    running.value = false
  }
}

async function loadFolders() {
  folderError.value = null
  try {
    const response = await get<FolderItem[]>('/api/v1/pipeline-folders')
    // Guard before assigning: `get<FolderItem[]>` is an unchecked cast and
    // useApi returns `undefined as T` on a 204, while the folderPath computed
    // iterates folders.value (and dereferences f.id / f.parent_id) on every
    // render — a non-array or a bad element would throw on every render.
    // /api/v1/pipeline-folders returns a BARE array (backend `list[FolderResponse]`,
    // generated contract `FolderResponse[]`), so the `{ items: [...] }` envelope
    // this view used to tolerate was copy-paste from loadLifecycleMaps (which
    // genuinely returns an envelope) and is rejected here like every other
    // folder consumer. Do NOT coerce to []: that hides a real contract
    // violation. A bad shape is logged loudly AND surfaced as a retryable
    // alert below the toolbar (pageError replaces the whole page, which a
    // degraded breadcrumb must not do), and the previous list is left intact.
    if (!isFolderList(response)) {
      console.error(invalidFolderListMessage('/api/v1/pipeline-folders', response))
      folderError.value = t('views.PipelineEditorView.invalid_folder_list_response')
      return
    }
    folders.value = response
  } catch (e) {
    folderError.value = formatApiError(e)
    console.warn('Failed to load folders', e)
  }
}

async function loadLifecycleMaps() {
  pageError.value = null
  try {
    const response = await get<any[] | { items?: any[] }>('/api/v1/lifecycle-maps')
    const summaries = Array.isArray(response) ? response : (response.items ?? [])
    const first10 = (summaries ?? []).slice(0, 10)
    const fullMaps = await Promise.all(
      first10.map((m: any) =>
        get<any>(`/api/v1/lifecycle-maps/${m.id}`).catch(() => null)
      )
    )
    linkedLifecycleMaps.value = fullMaps.filter(
      (m: any) => m && m.stages?.some((s: any) => s.pipeline_id === pipelineId)
    )
  } catch (e) {
    console.warn('Failed to load lifecycle maps', e)
  }
}

// Declared before useDataFetch: vue-query invokes the fetcher synchronously
// during setup, and the fetcher + the loaders it calls (loadPipeline,
// loadGraph, loadLifecycleMaps) read/write pageError — the binding must be
// initialised before the fetcher can run.
const pageError = ref<string | null>(null)

// ---------------------------------------------------------------------------
// FAR-1812: read-only historic version view.
//
// The route query selects the mode: `?snapshot=<uuid>` is the canonical deep
// link and `?version=<n>` an alias resolved against the server's
// version-DESCENDING snapshot list by resolveVersionAlias()'s bounded search
// (snapshot wins when both are present).
//
// THE DATA-LOSS HAZARD this block exists to prevent: the editor's Save writes
// rawNodes/rawEdges (the LIVE graph), so hydrating those refs from an old
// snapshot would let one Save clobber newer work with a stale graph. Viewing
// therefore keeps its frozen graph in SEPARATE refs (viewRaw*/viewFlow*) that
// only feed the canvas + inspector via displayNodes/inspectSource*; the
// editable state is never touched, and every write function additionally
// early-returns while isViewing holds.
// ---------------------------------------------------------------------------

function readQueryValue(value: unknown): string | null {
  const raw = Array.isArray(value) ? value[0] : value
  return typeof raw === 'string' && raw.length > 0 ? raw : null
}

const snapshotParam = computed(() => readQueryValue(route.query.snapshot))
const versionParam = computed(() => readQueryValue(route.query.version))
const historicRequested = computed(() => snapshotParam.value !== null || versionParam.value !== null)

// FAR-1818: version compare mode. `?compare=<uuid>` — the KEY's presence is
// what selects the mode, and an empty/absent id means "compare against the
// latest version". The base side is the resolved `?snapshot=` version (or its
// `?version=` alias), so the canonical deep link is
// `?snapshot=<base>&compare=<other>`. Kept separate from the historic-view
// refs: entering compare never hydrates the editable graph.
const compareRequested = computed(() => route.query.compare !== undefined)
const compareParam = computed(() => readQueryValue(route.query.compare))

// The latest saved version (the snapshot list is version-DESCENDING and
// loadCatalog filters version 0) — the default "compare against" target.
const latestSnapshotId = computed<string | null>(() => snapshots.value[0]?.id ?? null)

/** `?version=<n>`: the positive integer, -1 when malformed, null when absent. */
const requestedVersion = computed(() => {
  const raw = versionParam.value
  if (raw === null) return null
  return /^\d+$/.test(raw) ? Number(raw) : -1
})

// --- `?version=<n>` alias resolution ---------------------------------------
// The alias resolves against the server's version-DESCENDING snapshot list
// (`GET /{pipeline_id}/snapshots`), which is PAGINATED (page_size caps at
// 100, default 20). Resolving only the first loaded page made a deep link
// like `?version=3` on a long-lived pipeline report "This version is no
// longer available" for a version that exists — a false statement on the
// exact promise the feature exists to keep. The search here is BOUNDED by
// construction:
//
//   * page 1 at SNAPSHOT_PAGE_SIZE — a single request covers the top
//     100 versions (every pipeline up to 100 versions), and
//   * for deeper versions, a binary search over the remaining pages — at
//     most log2(pageCount) further requests, hard-capped at
//     SNAPSHOT_LOOKUP_MAX_PROBES (12 probes = 409,600 versions; exhausting
//     the budget throws rather than guessing "missing").
//
// The `?snapshot=<uuid>` path never enters this code — no extra request, no
// latency change for the canonical link. Nothing depends on loadCatalog's
// page-1 list, so a partial or failed catalog load cannot make an existing
// deep version read as missing either.
//
// FAR-1816/1817: SNAPSHOT_PAGE_SIZE is the shared window this lookup and the
// timeline fetch (lib/pipelineVersion). The chip no longer uses it (FAR-1895:
// it resolves through a server-filtered query), so the number is NOT a
// chip/timeline parity guarantee — that parity is structural (see
// currentVersionSnapshot below and PipelineSnapshotTimeline's groupHistory).
const SNAPSHOT_LOOKUP_MAX_PROBES = 12

type VersionLookupStatus = 'idle' | 'loading' | 'found' | 'absent' | 'failed'
const versionLookupStatus = ref<VersionLookupStatus>('idle')
const versionLookupId = ref<string | null>(null)
// Monotonic token: a search superseded by a newer query change (or a page
// reload) must never write its result over the newer one.
let versionLookupToken = 0

async function fetchSnapshotLookupPage(
  page: number,
): Promise<{ items: components['schemas']['SnapshotResponse'][]; total: number }> {
  const { data, error } = await withTimeout((signal) =>
    api.GET('/api/v1/pipelines/{pipeline_id}/snapshots', {
      params: { path: { pipeline_id: pipelineId }, query: { page, page_size: SNAPSHOT_PAGE_SIZE } },
      signal,
    }),
  )
  if (error || !data) throw new Error(`snapshot list page ${page} could not be loaded`)
  const items = data.items ?? []
  // `total` is always sent by the server; fall back to the page's own length
  // so a response without it degrades to "one page", never to NaN bounds.
  const total = Number.isFinite(data.total) ? data.total : items.length
  return { items, total }
}

/**
 * Find `version` in the server's version-DESC snapshot ordering.
 *
 * Returns the snapshot id, or null when the version is PROVABLY absent
 * (it lies outside the existing version range, or the page whose range
 * contains it has no such row — the list is sorted, so absence on that page
 * is absence everywhere). Throws when the search could not complete: a
 * failed search must never masquerade as "this version is no longer
 * available".
 */
async function findSnapshotIdByVersion(version: number): Promise<string | null> {
  const find = (items: components['schemas']['SnapshotResponse'][]) =>
    items.find((s) => s.snapshot_version === version) ?? null
  const pageOne = await fetchSnapshotLookupPage(1)
  const onFirst = find(pageOne.items)
  if (onFirst) return onFirst.id
  if (pageOne.items.length === 0) return null // no snapshots at all
  // Sorted version-DESC: page 1 spans [newest … oldest-on-page-1].
  if (version > pageOne.items[0].snapshot_version) return null // newer than the newest
  if (version >= pageOne.items[pageOne.items.length - 1].snapshot_version) {
    return null // inside page 1's range, not present — absent
  }
  // Below page 1: binary search the remaining pages by the range each spans.
  let lo = 2
  let hi = Math.ceil(pageOne.total / SNAPSHOT_PAGE_SIZE)
  let probes = 0
  while (lo <= hi) {
    if (probes >= SNAPSHOT_LOOKUP_MAX_PROBES) {
      throw new Error('version alias search exceeded its probe budget')
    }
    probes += 1
    const mid = Math.floor((lo + hi) / 2)
    const page = await fetchSnapshotLookupPage(mid)
    // Versions saved while this search runs push every older version DOWN
    // the ranking — refresh `hi` from each page's live total so a stale
    // bound can never truncate the search below the target's new rank (a
    // false "no longer available" of exactly the kind this search exists to
    // prevent). Termination is unaffected: `probes` still caps the loop.
    hi = Math.max(hi, Math.ceil(page.total / SNAPSHOT_PAGE_SIZE))
    if (page.items.length === 0) {
      hi = mid - 1 // past the end (list shrank) — search earlier
      continue
    }
    const hit = find(page.items)
    if (hit) return hit.id
    if (version > page.items[0].snapshot_version) {
      hi = mid - 1 // belongs above this page
      continue
    }
    if (version < page.items[page.items.length - 1].snapshot_version) {
      lo = mid + 1 // belongs below this page
      continue
    }
    return null // inside this page's range, not present — absent
  }
  return null
}

/**
 * Resolve the current `?version=<n>` alias. No-op for the canonical
 * `?snapshot=` link (which needs no list at all), for an absent alias, and
 * for a malformed one (versionMissing derives that case directly, with no
 * request). A completed search records found/absent; a FAILED one lands on
 * pageError — an accurate load error — never on the graceful miss, because
 * "no longer available" would be false when the search itself failed.
 */
async function resolveVersionAlias(): Promise<void> {
  const token = ++versionLookupToken
  const version = requestedVersion.value
  if (snapshotParam.value !== null || versionParam.value === null || version === null || version <= 0) {
    versionLookupId.value = null
    versionLookupStatus.value = 'idle'
    return
  }
  versionLookupId.value = null
  versionLookupStatus.value = 'loading'
  try {
    const id = await findSnapshotIdByVersion(version)
    if (token !== versionLookupToken) return
    versionLookupId.value = id
    versionLookupStatus.value = id ? 'found' : 'absent'
  } catch (e: unknown) {
    if (token !== versionLookupToken) return
    console.warn('Failed to resolve the ?version= alias', e)
    versionLookupStatus.value = 'failed'
    pageError.value = t('views.PipelineEditorView.version_load_failed', { error: formatApiError(e) })
  }
}

// Re-resolve whenever the alias (or its precedence against `?snapshot=`)
// changes: entering a version, switching versions, or leaving to latest.
// NOT immediate — mount is covered by the fetcher below, which AWAITS this
// same routine, so `loading` spans the search and the spinner never gaps.
// reloadPage (ErrorAlert's retry) re-runs it for the same reason: a failed
// search must be retryable from the error it produced.
watch([snapshotParam, versionParam], () => {
  void resolveVersionAlias()
})

const resolvedSnapshotId = computed<string | null>(() => {
  if (snapshotParam.value !== null) return snapshotParam.value
  if (versionLookupStatus.value !== 'found') return null
  return versionLookupId.value
})

type VersionDetailStatus = 'idle' | 'loading' | 'ready' | 'missing' | 'failed'
const versionDetailStatus = ref<VersionDetailStatus>('idle')
const viewSnapshot = ref<components['schemas']['SnapshotDetailResponse'] | null>(null)
const viewSnapshotId = ref<string | null>(null)

// The frozen graph the historic view renders. NEVER assigned to rawNodes/
// rawEdges/flowNodes/flowEdges — see the block comment above.
const viewRawNodes = ref<any[]>([])
const viewRawEdges = ref<any[]>([])
const viewFlowNodes = ref<any[]>([])
const viewFlowEdges = ref<any[]>([])

/** VueFlow's v-model: the frozen snapshot graph while viewing, live otherwise. */
const displayNodes = computed<any[]>({
  get: () => (isViewing.value ? viewFlowNodes.value : flowNodes.value),
  set: (value: any[]) => {
    if (isViewing.value) viewFlowNodes.value = value
    else flowNodes.value = value
  },
})

const displayEdges = computed<any[]>({
  get: () => (isViewing.value ? viewFlowEdges.value : flowEdges.value),
  set: (value: any[]) => {
    if (isViewing.value) viewFlowEdges.value = value
    else flowEdges.value = value
  },
})

/** What the inspector reads: the graph the canvas is showing. */
const inspectSourceNodes = computed(() => (isViewing.value ? viewRawNodes.value : rawNodes.value))
const inspectSourceEdges = computed(() => (isViewing.value ? viewRawEdges.value : rawEdges.value))

const isViewing = computed(
  () =>
    resolvedSnapshotId.value !== null &&
    viewSnapshotId.value === resolvedSnapshotId.value &&
    viewSnapshot.value !== null,
)

// FAR-1818: compare mode is the `?compare=` key ON TOP of the historic view —
// it needs a resolved base version, and it stays read-only (isViewing holds,
// so every write affordance remains unmounted).
const compareMode = computed(
  () => compareRequested.value && isViewing.value && resolvedSnapshotId.value !== null,
)

// Graceful miss: unknown/deleted snapshot, a `?version=` the bounded search
// PROVED absent, or a malformed version param — never a raw error, never a
// dead page. A search that could not complete ('failed') is deliberately not
// a miss: the pageError branch tells the truth instead of the false "no
// longer available".
const versionMissing = computed(() => {
  if (versionDetailStatus.value === 'missing') return true
  if (snapshotParam.value !== null) return false
  if (versionParam.value === null) return false
  const version = requestedVersion.value
  if (version === null || version <= 0) return true
  return versionLookupStatus.value === 'absent'
})

// Spinner only while an alias search or detail load is genuinely in flight;
// a failed search/detail falls through to pageError instead of spinning
// forever.
const historicResolving = computed(
  () =>
    historicRequested.value &&
    !isViewing.value &&
    !versionMissing.value &&
    versionLookupStatus.value !== 'failed' &&
    versionDetailStatus.value !== 'failed',
)

// Version chip: the VIEWED version while viewing, otherwise the pipeline's
// current SUBSTANTIVE version. FAR-1817: a version is a substantive snapshot
// (created_kind initial/edit/rollback) — run snapshots are NOT versions, so
// the chip must NOT label the newest snapshot (which after any run is a run
// snapshot) as the current version.
//
// FAR-1895: the candidate list comes from a SERVER-FILTERED query
// (`exclude_created_kind` + `exclude_drafts`), not from the head of the
// unfiltered catalog page. Run snapshots accumulate without bound and are
// never pruned, so any fixed page can be 100% run rows; the filtered query
// always returns the newest version regardless of history size. The shared
// client predicate (lib/pipelineVersion) still validates the row, so the chip
// can never label a run or a draft as a version even if the server filter were
// wrong.
//
// FALLBACK: when the auxiliary filtered request FAILED, `currentVersionSnapshots`
// is empty (the loader CLEARS it on every failure, so a failed re-query after a
// rollback cannot leave a stale pre-rollback list behind) and this falls back to
// the already-loaded unfiltered catalog — the pre-FAR-1895 behaviour. A version
// inside that window therefore still labels the chip even though one auxiliary
// request failed; if that first page has no substantive version either, the chip
// is hidden. Either way the failure is surfaced separately by `versionChipError`
// (with a retry) so degraded is never silent, and a stale version is never
// served.
//
// `currentVersionSnapshot` (viewing-independent) is ALSO handed to the
// timeline: when the timeline's own window has no substantive row it anchors
// on this exact value instead of fabricating one from a run row (FAR-1895), so
// the two surfaces cannot show different current versions.
const currentVersionSnapshot = computed(
  () =>
    currentSubstantiveVersion(currentVersionSnapshots.value)
    ?? currentSubstantiveVersion(snapshots.value),
)
const versionChipSnapshot = computed(() => (isViewing.value ? viewSnapshot.value : currentVersionSnapshot.value))

const viewedVersionDate = computed(() => formatDateShort(viewSnapshot.value?.created_at ?? null))
const viewedVersionAuthor = computed(() => {
  const createdBy = viewSnapshot.value?.created_by
  return createdBy ? shortId(createdBy) : t('views.PipelineEditorView.historic_unknown_author')
})

/**
 * Parse a snapshot's frozen graph payload. A response WITHOUT a graph object
 * means we were not given this version's graph — render the graceful miss
 * rather than an empty canvas that would claim the version had no nodes.
 */
function readHistoricGraph(value: unknown): { nodes: unknown[]; edges: unknown[] } | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const record = value as Record<string, unknown>
  return {
    nodes: Array.isArray(record.nodes) ? record.nodes : [],
    edges: Array.isArray(record.edges) ? record.edges : [],
  }
}

async function loadViewedSnapshot(id: string): Promise<void> {
  versionDetailStatus.value = 'loading'
  try {
    const { data, error, response } = await withTimeout((signal) =>
      api.GET('/api/v1/pipelines/{pipeline_id}/snapshots/{snapshot_id}', {
        params: { path: { pipeline_id: pipelineId, snapshot_id: id } },
        signal,
      }),
    )
    // A newer navigation superseded this request — its result is stale.
    if (resolvedSnapshotId.value !== id) return
    if (error) {
      if (response?.status === 404) {
        versionDetailStatus.value = 'missing'
      } else {
        versionDetailStatus.value = 'failed'
        pageError.value = t('views.PipelineEditorView.version_load_failed', { error: formatApiError(error) })
      }
      return
    }
    if (!data) {
      versionDetailStatus.value = 'missing'
      return
    }
    const graph = readHistoricGraph(data.graph_json)
    if (!graph) {
      versionDetailStatus.value = 'missing'
      return
    }
    viewSnapshot.value = data
    viewSnapshotId.value = id
    viewRawNodes.value = graph.nodes
    viewRawEdges.value = graph.edges
    viewFlowNodes.value = graph.nodes.map((n) => convertBackendNode(n))
    viewFlowEdges.value = graph.edges.map((e, i) => convertBackendEdge(e, i))
    versionDetailStatus.value = 'ready'
  } catch (e: unknown) {
    if (resolvedSnapshotId.value !== id) return
    versionDetailStatus.value = 'failed'
    pageError.value = t('views.PipelineEditorView.version_load_failed', { error: formatApiError(e) })
  }
}

// Re-resolve whenever the query (or the snapshot list behind a version alias)
// changes: entering a version, switching versions, or leaving to latest.
watch(
  resolvedSnapshotId,
  (id) => {
    viewSnapshot.value = null
    viewSnapshotId.value = null
    viewRawNodes.value = []
    viewRawEdges.value = []
    viewFlowNodes.value = []
    viewFlowEdges.value = []
    versionDetailStatus.value = id ? 'loading' : 'idle'
    // The canvas graph changes identity across this transition, so any open
    // inspector selection refers to the WRONG graph — drop it rather than
    // showing one graph's node config while the other renders (pane-click's
    // cleanup also resets the param-set / overrides buffers).
    onPaneClick()
    if (id) void loadViewedSnapshot(id)
  },
  { immediate: true },
)

// The initial-fit latch is per-graph-source: entering/leaving (or switching)
// a version replaces every canvas node, so re-fit the new graph once it has
// landed. Latest-mode edits keep today's behaviour (no re-fit on addNode).
watch([isViewing, resolvedSnapshotId], async () => {
  initialFitDone = false
  await nextTick()
  void runInitialFitView()
})

const VERSION_SELECTION_KEYS = new Set(['snapshot', 'version', 'compare'])

function versionParamsExceptSnapshot(): LocationQueryRaw {
  const out: LocationQueryRaw = {}
  for (const [key, value] of Object.entries(route.query)) {
    // `compare` is a view-selection param too (FAR-1818): leaving the version
    // view must not strand a stale compare id in the URL.
    if (!VERSION_SELECTION_KEYS.has(key)) out[key] = value
  }
  return out
}

/** Strip the version params and navigate back to the live editor. */
function viewLatest(): void {
  void router.push({ path: route.path, query: versionParamsExceptSnapshot() })
}

// --- FAR-1818: version compare mode ----------------------------------------
/** Banner action: open the viewed version against the latest (one click). */
function openCompare(): void {
  const base = resolvedSnapshotId.value
  if (!base) return
  onPaneClick() // the compare view replaces the canvas; drop any selection
  const query = versionParamsExceptSnapshot()
  query.snapshot = base
  query.compare = latestSnapshotId.value ?? ''
  void router.push({ path: route.path, query })
}

/** Pair-picker change from the compare view — deep-linkable via the route. */
function updateCompare(payload: { base: string; compare: string }): void {
  if (!payload.base) return
  const query = versionParamsExceptSnapshot()
  query.snapshot = payload.base
  query.compare = payload.compare
  void router.push({ path: route.path, query })
}

/** Leave compare, staying on the same historic base version. */
function exitCompare(): void {
  const query = versionParamsExceptSnapshot()
  const base = resolvedSnapshotId.value
  if (base) query.snapshot = base
  void router.push({ path: route.path, query })
}

// --- Unsaved-changes guard (FAR-1812 B) -----------------------------------
// The editor had no dirty tracking; a graph save round-trips through
// loadGraph, so a stable fingerprint of rawNodes/rawEdges against the last
// loaded/saved state is an exact match for "Save would send something new".
const graphBaseline = ref<string | null>(null)

function graphFingerprint(): string {
  return JSON.stringify({ nodes: rawNodes.value, edges: rawEdges.value })
}

const isGraphDirty = computed(
  () => graphBaseline.value !== null && graphFingerprint() !== graphBaseline.value,
)

const unsavedConfirmVisible = ref(false)
let unsavedConfirmResolve: ((proceed: boolean) => void) | null = null

function confirmLeaveUnsaved(): Promise<boolean> {
  return new Promise((resolve) => {
    unsavedConfirmResolve = resolve
    unsavedConfirmVisible.value = true
  })
}

function resolveUnsavedConfirm(proceed: boolean): void {
  unsavedConfirmVisible.value = false
  const resolve = unsavedConfirmResolve
  unsavedConfirmResolve = null
  resolve?.(proceed)
}

/** Timeline "View" action: warn on unsaved live edits, then deep-link. */
async function requestViewSnapshot(snapshotId: string): Promise<void> {
  if (isGraphDirty.value && !(await confirmLeaveUnsaved())) return
  showVersionTimeline.value = false
  const query = versionParamsExceptSnapshot()
  query.snapshot = snapshotId
  await router.push({ path: route.path, query })
}

function openVersionTimeline(): void {
  showVersionTimeline.value = true
}

// --- Restore (shared dialog: banner + timeline) ---------------------------
// FAR-1816: one restore dialog, backed by the existing POST
// /snapshots/{id}/rollback endpoint. Restore replays the chosen version's
// graph into the live pipeline and creates a NEW version from it (history is
// never rewritten), and the server's own guards still refuse a weakening
// restore. The dialog shows the old-vs-latest change summary from the diff
// endpoint and names the consequences before the user confirms.
const showRestoreConfirm = ref(false)
const restoreLoading = ref(false)
const restoreError = ref<string | null>(null)
const restoreTargetId = ref<string | null>(null)
const restoreTargetVersion = ref<number | null>(null)
const restoreNotice = ref<string | null>(null)
let restoreNoticeTimer: ReturnType<typeof setTimeout> | null = null

// The change summary between the version being restored (target) and the
// current live version (latest). `snapshot_a` is the live/latest graph and
// `snapshot_b` the target, so the diff's `nodes_added`/`edges_added` are what
// restoring brings back and `*_removed` what it drops; `*_modified` are
// in-place config changes (agent, prompt, commands, timeout, label, ports).
const restoreDiff = ref<SnapshotDiffPayload>(null)
const restoreDiffLoading = ref(false)
const restoreDiffError = ref<string | null>(null)
// C2: the early-return outcomes. Both must render an explicit message — never
// a bare "Change summary" title that reads as "nothing will change".
const restoreDiffIsCurrent = ref(false)
const restoreDiffNoBaseline = ref(false)
// M6: sequence guard so an earlier dialog's diff cannot land after a later one.
let restoreDiffRequestSeq = 0

interface RestoreDiffSummary {
  nodesRestored: number
  nodesRemoved: number
  nodesModified: number
  edgesRestored: number
  edgesRemoved: number
  edgesModified: number
  impacted: number
  breaking: SnapshotBreakingChange[]
  /** True when restoring would change ANYTHING (see the predicate below). */
  hasChanges: boolean
}

const restoreDiffSummary = computed<RestoreDiffSummary | null>(() => {
  const d = restoreDiff.value
  if (!d) return null
  // FAR-1893: the change-class enumeration, the semantic readers and the
  // severity label all come from the shared module. Only this dialog's LAYOUT
  // (the list of counts) stays local.
  const counts = readChangeClassCounts(d)
  const breaking = readBreakingChanges(d)
  const impacted = readImpactedNodes(d).length
  // Deliberately broader than the compare view's `hasStructuralChanges`
  // (structural only): this dialog answers "does restoring this version
  // change anything at all?", so a semantic-only difference (an impacted node
  // or a breaking port change) still means "not identical". Both predicates
  // are expressed from the same shared counts, so neither can drift on the
  // structural half.
  const hasChanges = counts.hasChanges || impacted > 0 || breaking.length > 0
  return {
    nodesRestored: counts.nodesAdded,
    nodesRemoved: counts.nodesRemoved,
    nodesModified: counts.nodesModified,
    edgesRestored: counts.edgesAdded,
    edgesRemoved: counts.edgesRemoved,
    edgesModified: counts.edgesModified,
    impacted,
    breaking,
    hasChanges,
  }
})

/** Shared severity rule (neutral namespace), injected with this view's `t`. */
function restoreSeverityLabel(severity?: string): string {
  return resolveSeverityLabel(severity, t)
}

const timelineRef = ref<InstanceType<typeof PipelineSnapshotTimeline> | null>(null)

function setRestoreNotice(message: string): void {
  restoreNotice.value = message
  if (restoreNoticeTimer !== null) clearTimeout(restoreNoticeTimer)
  restoreNoticeTimer = setTimeout(() => {
    restoreNotice.value = null
    restoreNoticeTimer = null
  }, 8000)
}

/**
 * Build the success notice without ever emitting an empty version ("Restored
 * v. Created v."). Each half is included only when the value is known.
 */
function restoreSuccessNotice(restored: number | null, created: number | null): string {
  if (restored != null && created != null) {
    return t('views.PipelineEditorView.historic_restore_success', { restored, created })
  }
  if (restored != null) {
    return t('views.PipelineEditorView.historic_restore_success_no_created', { restored })
  }
  if (created != null) {
    return t('views.PipelineEditorView.historic_restore_success_no_restored', { created })
  }
  return t('views.PipelineEditorView.historic_restore_success_generic')
}

async function loadRestoreDiff(): Promise<void> {
  const seq = ++restoreDiffRequestSeq
  restoreDiff.value = null
  restoreDiffError.value = null
  restoreDiffIsCurrent.value = false
  restoreDiffNoBaseline.value = false
  const target = restoreTargetId.value
  // The newest snapshot is the closest representation of the live/current
  // config. Without it (or when the target IS the current snapshot) there is
  // nothing to compare — but that outcome must still be stated explicitly
  // (C2), never left as an empty box.
  const latest = snapshots.value[0]?.id as string | undefined
  if (!target || !latest) {
    restoreDiffNoBaseline.value = true
    return
  }
  if (target === latest) {
    restoreDiffIsCurrent.value = true
    return
  }
  restoreDiffLoading.value = true
  try {
    const { data, error } = await withTimeout((signal) =>
      api.POST('/api/v1/pipelines/{pipeline_id}/snapshots/diff', {
        params: { path: { pipeline_id: pipelineId } },
        body: { snapshot_a_id: latest, snapshot_b_id: target },
        signal,
      }),
    )
    // C2/M6: an earlier dialog's response must not land after a later one.
    if (seq !== restoreDiffRequestSeq) return
    if (error) {
      console.warn('[PipelineEditorView] failed to load restore diff', formatApiError(error))
      restoreDiffError.value = formatApiError(error)
      return
    }
    restoreDiff.value = data ?? null
  } catch (e: unknown) {
    if (seq !== restoreDiffRequestSeq) return
    console.warn('[PipelineEditorView] failed to load restore diff', formatApiError(e))
    restoreDiffError.value = formatApiError(e)
  } finally {
    if (seq === restoreDiffRequestSeq) restoreDiffLoading.value = false
  }
}

/**
 * FAR-1816: turn a restore refusal into human language. The server refuses a
 * restore that would weaken a HITL gate, strip a node-bound guardrail, or pin
 * another team's connector — those must read as plain sentences, never the raw
 * server string. Any UNCLASSIFIED refusal gets generic human copy too (the
 * detail is logged for diagnostics); the raw server text is never rendered.
 */
function describeRestoreRefusal(err: unknown): string {
  const raw = formatApiError(err)
  if (/connector_team_mismatch/i.test(raw)) {
    return t('views.PipelineEditorView.historic_restore_blocked_connector')
  }
  if (/gate weakening denied/i.test(raw)) {
    return t('views.PipelineEditorView.historic_restore_blocked_hitl')
  }
  if (/guardrail/i.test(raw)) {
    return t('views.PipelineEditorView.historic_restore_blocked_guardrail')
  }
  // M8: never surface the raw server string as user-facing copy.
  console.warn('[PipelineEditorView] restore refused', raw)
  return t('views.PipelineEditorView.historic_restore_failed_generic')
}

async function openRestoreConfirm(snapshotId?: string, snapshotVersion?: number): Promise<void> {
  // Reuse the same unsaved-changes guard the "view historic version" path
  // runs: restoring replaces the live graph, so unsaved edits must not be
  // silently discarded.
  if (isGraphDirty.value && !(await confirmLeaveUnsaved())) return
  restoreTargetId.value = snapshotId ?? resolvedSnapshotId.value
  restoreTargetVersion.value = snapshotVersion ?? viewSnapshot.value?.snapshot_version ?? null
  restoreError.value = null
  restoreDiff.value = null
  restoreDiffError.value = null
  showRestoreConfirm.value = true
  void loadRestoreDiff()
}

async function confirmRestore(): Promise<void> {
  const snapshotId = restoreTargetId.value ?? resolvedSnapshotId.value
  if (!snapshotId) return
  const restoredVersion = restoreTargetVersion.value ?? viewSnapshot.value?.snapshot_version ?? null
  restoreLoading.value = true
  restoreError.value = null
  try {
    const { data, error } = await withTimeout((signal) =>
      api.POST('/api/v1/pipelines/{pipeline_id}/snapshots/{snapshot_id}/rollback', {
        params: { path: { pipeline_id: pipelineId, snapshot_id: snapshotId } },
        signal,
      }),
    )
    if (error) {
      restoreError.value = describeRestoreRefusal(error)
      return
    }
    const createdVersion = (data as { snapshot_version?: number } | null)?.snapshot_version ?? null
    showRestoreConfirm.value = false
    setRestoreNotice(restoreSuccessNotice(restoredVersion, createdVersion))
    // A restore replays this version into the LIVE graph and creates a new
    // version from it — reload the graph, the catalog (for the version chip)
    // and the timeline (so the new version shows as a rollback entry), then
    // land back in the latest editor with the historic banner gone.
    await loadGraph()
    await loadCatalog()
    await timelineRef.value?.reload()
    viewLatest()
  } catch (e: unknown) {
    restoreError.value = describeRestoreRefusal(e)
  } finally {
    restoreLoading.value = false
  }
}

// --- Copy link (banner action) --------------------------------------------
const linkCopied = ref(false)
const copyLinkError = ref<string | null>(null)
let copyLinkResetTimer: ReturnType<typeof setTimeout> | null = null

function historicShareLink(): string {
  const params = new URLSearchParams()
  const id = resolvedSnapshotId.value
  if (id) params.set('snapshot', id)
  // FAR-1818: while comparing, the canonical share link carries the compare id
  // too (an empty compare id means "against latest", which the link preserves).
  if (compareMode.value) {
    let compareId = compareParam.value
    if (compareId === null) compareId = latestSnapshotId.value
    if (compareId === null) compareId = ''
    params.set('compare', compareId)
  }
  const query = params.toString()
  return query ? `${window.location.origin}${route.path}?${query}` : `${window.location.origin}${route.path}`
}

async function copyHistoricLink(): Promise<void> {
  copyLinkError.value = null
  try {
    await navigator.clipboard.writeText(historicShareLink())
    linkCopied.value = true
    if (copyLinkResetTimer !== null) clearTimeout(copyLinkResetTimer)
    copyLinkResetTimer = setTimeout(() => {
      linkCopied.value = false
      copyLinkResetTimer = null
    }, 2000)
  } catch (e: unknown) {
    copyLinkError.value = t('views.PipelineEditorView.historic_copy_link_failed')
    console.warn('[PipelineEditorView] failed to copy the version link', e)
  }
}

onBeforeUnmount(() => {
  if (copyLinkResetTimer !== null) clearTimeout(copyLinkResetTimer)
  if (restoreNoticeTimer !== null) clearTimeout(restoreNoticeTimer)
  if (unsavedConfirmResolve) {
    unsavedConfirmResolve(false)
    unsavedConfirmResolve = null
  }
})

const { loading, load: reloadPage } = useDataFetch<null>(
  async () => {
    pageError.value = null
    // resolveVersionAlias is AWAITED here (not fire-and-forget): `loading`
    // then spans a `?version=` search on mount, so the spinner never gaps,
    // and reloadPage retries a failed search from the error it produced.
    await Promise.all([
      loadPipeline(),
      loadGraph(),
      loadCatalog(),
      loadFolders(),
      loadLifecycleMaps(),
      resolveVersionAlias(),
    ])
    // useDataFetch's queryFn forwards `result.data`; vue-query rejects an
    // undefined query result ("data is undefined"), so always resolve with a
    // defined payload (the loaders write their state directly).
    return { data: null }
  },
)
</script>

<style scoped>
/* The docked toolbar row is h-7 (1.75rem); PrimeVue's Select keeps its default
   ~2.5rem height inside it, breaking the row's uniform look. Size the toolbar
   Selects (node-type picker in the canvas-tools group + the empty-state
   picker, which share the visual row) to h-7 to match. The scoped attribute
   selector outranks PrimeVue styled-mode's single-class rules; dialog/aside
   Selects are intentionally NOT sized by this class. */
.toolbar-select {
  height: 1.75rem;
  font-size: 0.75rem;
}

.toolbar-select :deep(.p-select-label) {
  padding: 0 0.5rem;
  font-size: 0.75rem;
}
</style>
