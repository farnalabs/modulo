<template>
  <FeatureGate feature-name="audit_viewer" required-tier="team" show-disabled>

    <div class="page-wide">
    <header class="flex items-center justify-between">
      <PageHeader :title="$t('views.AdminAuditView.audit_log')" :subtitle="$t('views.AdminAuditView.tamper_evident_event_trail')" />
      <div v-if="auditSource === 'org'" class="flex items-center gap-2">
        <button
          type="button"
          :disabled="verifying"
          class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-50"
          data-testid="admin-audit-verify-chain"
          @click="verifyChain"
        >
          {{ verifying ? $t('views.AdminAuditView.verifying') : $t('views.AdminAuditView.verify_chain') }}
        </button>
        <button
          type="button"
          :disabled="exporting"
          class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-50"
          data-testid="admin-audit-export-csv"
          @click="exportCsv"
        >
          {{ exporting ? $t('views.AdminAuditView.exporting') : $t('views.AdminAuditView.export_csv') }}
        </button>
        <button
          type="button"
          :disabled="exportingJsonl"
          class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-50"
          data-testid="admin-audit-export-jsonl"
          @click="exportJsonl"
        >
          {{ exportingJsonl ? $t('views.AdminAuditView.exporting') : $t('views.AdminAuditView.export_jsonl') }}
        </button>
      </div>
    </header>

    <div
      v-if="isSystemAdmin"
      role="tablist"
      :aria-label="$t('views.AdminAuditView.audit_source_group')"
      class="mb-4 flex flex-wrap gap-2"
      data-testid="admin-audit-source-tabs"
    >
      <button
        id="admin-audit-tab-org"
        type="button"
        role="tab"
        :aria-selected="auditSource === 'org'"
        aria-controls="admin-audit-source-org"
        class="rounded-lg border border-input px-4 py-2 text-sm font-medium"
        :class="auditSource === 'org' ? 'bg-accent' : 'bg-background hover:bg-accent'"
        data-testid="admin-audit-tab-org"
        @click="auditSource = 'org'"
      >
        {{ $t('views.AdminAuditView.tab_org_events') }}
      </button>
      <button
        id="admin-audit-tab-system"
        type="button"
        role="tab"
        :aria-selected="auditSource === 'system'"
        aria-controls="admin-audit-source-system"
        class="rounded-lg border border-input px-4 py-2 text-sm font-medium"
        :class="auditSource === 'system' ? 'bg-accent' : 'bg-background hover:bg-accent'"
        data-testid="admin-audit-tab-system"
        @click="auditSource = 'system'"
      >
        {{ $t('views.AdminAuditView.tab_system_events') }}
      </button>
    </div>

    <div
      v-if="auditSource === 'org'"
      id="admin-audit-source-org"
      :role="isSystemAdmin ? 'tabpanel' : undefined"
      :aria-labelledby="isSystemAdmin ? 'admin-audit-tab-org' : undefined"
    >
    <div v-if="chainResult" class="rounded-lg border px-4 py-3 text-sm" :class="chainResult.valid ? 'border-green-500 bg-green-50 text-green-800' : 'border-red-500 bg-red-50 text-red-800'" data-testid="admin-audit-chain-result">
      <strong>{{ chainResult.valid ? $t('views.AdminAuditView.chain_valid') : $t('views.AdminAuditView.chain_broken') }}</strong>
      <span v-if="chainResult.event_count" class="ml-2">— {{ $t('views.AdminAuditView.events_verified', { count: chainResult.event_count }) }}</span>
      <span v-if="chainResult.error" class="ml-2">— {{ chainResult.error }}</span>
    </div>

    <div class="card p-4">
      <FilterBar
        :filters="[
          { key: 'event_type', label: $t('views.AdminAuditView.event_type'), options: [
            { value: 'pipeline.created', label: $t('views.AdminAuditView.opt_pipeline_created') },
            { value: 'pipeline.updated', label: $t('views.AdminAuditView.opt_pipeline_updated') },
            { value: 'pipeline.deleted', label: $t('views.AdminAuditView.opt_pipeline_deleted') },
            { value: 'run.started', label: $t('views.AdminAuditView.opt_run_started') },
            { value: 'run.completed', label: $t('views.AdminAuditView.opt_run_completed') },
            { value: 'run.failed', label: $t('views.AdminAuditView.opt_run_failed') },
            { value: 'run.cancelled', label: $t('views.AdminAuditView.opt_run_cancelled') },
            { value: 'user.created', label: $t('views.AdminAuditView.opt_user_created') },
            { value: 'user.updated', label: $t('views.AdminAuditView.opt_user_updated') },
            { value: 'user.deactivated', label: $t('views.AdminAuditView.opt_user_deactivated') },
            { value: 'user.activated', label: $t('views.AdminAuditView.opt_user_activated') },
            { value: 'team.created', label: $t('views.AdminAuditView.opt_team_created') },
            { value: 'team.updated', label: $t('views.AdminAuditView.opt_team_updated') },
            { value: 'team.deleted', label: $t('views.AdminAuditView.opt_team_deleted') },
            { value: 'schema.created', label: $t('views.AdminAuditView.opt_schema_created') },
            { value: 'schema.updated', label: $t('views.AdminAuditView.opt_schema_updated') },
            { value: 'schema.deleted', label: $t('views.AdminAuditView.opt_schema_deleted') },
            { value: 'connector.created', label: $t('views.AdminAuditView.opt_connector_created') },
            { value: 'connector.updated', label: $t('views.AdminAuditView.opt_connector_updated') },
            { value: 'connector.deleted', label: $t('views.AdminAuditView.opt_connector_deleted') },
            { value: 'model_backend.created', label: $t('views.AdminAuditView.opt_model_backend_created') },
            { value: 'model_backend.updated', label: $t('views.AdminAuditView.opt_model_backend_updated') },
            { value: 'model_backend.deleted', label: $t('views.AdminAuditView.opt_model_backend_deleted') },
            { value: 'sso_provider.created', label: $t('views.AdminAuditView.opt_sso_provider_created') },
            { value: 'sso_provider.updated', label: $t('views.AdminAuditView.opt_sso_provider_updated') },
            { value: 'sso_provider.deleted', label: $t('views.AdminAuditView.opt_sso_provider_deleted') },
            { value: 'sso_provider.toggled', label: $t('views.AdminAuditView.opt_sso_provider_toggled') },
            { value: 'settings.updated', label: $t('views.AdminAuditView.opt_settings_updated') },
            { value: 'api_key.created', label: $t('views.AdminAuditView.opt_api_key_created') },
            { value: 'api_key.deleted', label: $t('views.AdminAuditView.opt_api_key_deleted') },
            { value: 'export.csv', label: $t('views.AdminAuditView.opt_export_csv') },
          ]},
        ]"
        :filter-values="{ event_type: filterEventType }"
        @update:filter="(key, value) => { if (key === 'event_type') filterEventType = value }"
      >
        <template #after>
          <input
            v-model="filterActor"
            type="text"
            :aria-label="$t('views.AdminAuditView.actor_id')"
            :placeholder="$t('views.AdminAuditView.actor_id')"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
            data-testid="admin-audit-actor"
          />
          <input :aria-label="$t('views.AdminAuditView.from')"
            v-model="filterDateFrom"
            type="date"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
            data-testid="admin-audit-date-from"
          />
        </template>
        <div>
          <label for="adminauditview-field-2" class="mb-1 block text-xs font-medium text-muted-foreground">{{ $t('views.AdminAuditView.to') }}</label>
          <input id="adminauditview-field-2"
            v-model="filterDateTo"
            type="date"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
            data-testid="admin-audit-date-to"
          />
        </div>
        <div>
          <label for="adminauditview-field-1" class="mb-1 block text-xs font-medium text-muted-foreground">{{ $t('views.AdminAuditView.target_type') }}</label>
          <Select
  :aria-label="$t('views.AdminAuditView.target_type')"
  v-model="filterTargetType"
  :placeholder="$t('views.AdminAuditView.target_type')"
  data-testid="admin-audit-target-type"
  class="w-full"
  :options="[{ value: '__all__', label: $t('views.AdminAuditView.all_targets') }, { value: 'pipeline', label: $t('views.AdminAuditView.optgroup_pipeline') }, { value: 'run', label: $t('views.AdminAuditView.optgroup_run') }, { value: 'user', label: $t('views.AdminAuditView.optgroup_user') }, { value: 'team', label: $t('views.AdminAuditView.optgroup_team') }, { value: 'schema', label: $t('views.AdminAuditView.optgroup_schema') }, { value: 'connector', label: $t('views.AdminAuditView.optgroup_connector') }, { value: 'model_backend', label: $t('views.AdminAuditView.optgroup_model_backend') }, { value: 'sso_provider', label: $t('views.AdminAuditView.optgroup_sso_provider') }]"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
        </div>
      </FilterBar>
      <div class="mt-3 flex items-center gap-2">
        <button
          type="button"
          class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
          data-testid="admin-audit-reset"
          @click="resetFilters"
        >
          {{ $t('views.AdminAuditView.reset') }}
        </button>
        <span v-if="total > 0" class="ml-auto text-sm text-muted-foreground">
          {{ $t('views.AdminAuditView.events_count', { count: total }, total) }}
        </span>
      </div>
    </div>

    <div v-if="loading" aria-hidden="true" class="table-wrapper overflow-x-auto">
      <table class="w-full">
        <thead>
          <tr>
            <th class="table-header">{{ $t('views.AdminAuditView.timestamp') }}</th>
            <th class="table-header">{{ $t('views.AdminAuditView.event_type') }}</th>
            <th class="table-header">{{ $t('views.AdminAuditView.actor') }}</th>
            <th class="table-header">{{ $t('views.AdminAuditView.target') }}</th>
            <th class="table-header">{{ $t('views.AdminAuditView.summary') }}</th>
            <th class="w-8 table-header" />
          </tr>
        </thead>
        <tbody class="divide-y">
          <tr v-for="row in 8" :key="row">
            <td class="table-cell whitespace-nowrap"><div class="h-4 w-28 rounded bg-muted/50" /></td>
            <td class="table-cell"><div class="h-4 w-24 rounded bg-muted/50" /></td>
            <td class="table-cell"><div class="h-4 w-32 rounded bg-muted/50" /></td>
            <td class="table-cell"><div class="h-4 w-20 rounded bg-muted/50" /></td>
            <td class="table-cell"><div class="h-4 w-full max-w-sm rounded bg-muted/50" /></td>
            <td class="table-cell"><div class="ml-auto h-4 w-4 rounded bg-muted/50" /></td>
          </tr>
        </tbody>
      </table>
    </div>

    <ErrorAlert v-else-if="error" :message="error" :on-retry="loadEvents" />

    <EmptyState
      v-else-if="events.length === 0"
      :title="$t('views.AdminAuditView.no_audit_events_found')"
      :description="$t('views.AdminAuditView.try_adjusting_filters')"
    />

    <template v-else>
      <div class="table-wrapper overflow-x-auto">
        <table class="w-full">
          <thead>
            <tr>
              <th class="table-header">{{ $t('views.AdminAuditView.timestamp') }}</th>
              <th class="table-header">{{ $t('views.AdminAuditView.event_type') }}</th>
              <th class="table-header">{{ $t('views.AdminAuditView.actor') }}</th>
              <th class="table-header">{{ $t('views.AdminAuditView.target') }}</th>
              <th class="table-header">{{ $t('views.AdminAuditView.summary') }}</th>
              <th class="w-8 table-header" />
            </tr>
          </thead>
          <tbody class="divide-y">
            <tr
              v-for="event in events"
              :key="event.id"
              class="cursor-pointer transition-colors hover:bg-muted/30"
              :data-testid="'admin-audit-event-row-' + event.id"
              tabindex="0"
              @click="toggleExpand(event.id)"
              @keydown.enter="toggleExpand(event.id)"
              @keydown.space.prevent="toggleExpand(event.id)"
            >
              <td class="table-cell whitespace-nowrap">
                {{ formatTimestamp(event.created_at) }}
              </td>
              <td class="table-cell">
                <span :class="badgeClass(event.event_type)">
                  {{ event.event_type }}
                </span>
              </td>
              <td class="table-cell font-mono">
                {{ formatActor(event) }}
              </td>
              <td class="table-cell">
                <span v-if="event.resource_type" class="text-muted-foreground">
                  {{ event.resource_type }}
                </span>
                <span v-if="event.resource_id" class="ml-1 font-mono text-xs text-muted-foreground/70">
                  / {{ shortId(event.resource_id) }}
                </span>
                <span v-else class="text-muted-foreground/50">&mdash;</span>
              </td>
              <td class="table-cell max-w-xs truncate text-muted-foreground" v-tooltip.top="{ value: summarize(event), showDelay: 300 }">{{ summarize(event) }}</td>
              <td class="table-cell text-xs text-muted-foreground">
                <button
                  type="button"
                  class="inline-flex items-center rounded p-1 hover:bg-muted/30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  :aria-label="$t('views.AdminAuditView.expand_event', { id: event.id })"
                  :data-testid="'admin-audit-event-expand-' + event.id"
                  @click.stop="toggleExpand(event.id)"
                >
                  <ChevronDown
                    class="h-4 w-4 transition-transform"
                    :class="{ 'rotate-180': expandedId === event.id }"
                    aria-hidden="true"
                  />
                </button>
              </td>
            </tr>
            <tr v-if="expandedId">
              <td colspan="6" class="border-t bg-muted p-4">
                <div class="space-y-3">
                  <div v-if="expandedEvent?.payload_json && Object.keys(expandedEvent.payload_json).length > 0">
                    <h4 class="mb-1 text-xs font-semibold uppercase text-muted-foreground">{{ $t('views.AdminAuditView.details') }}</h4>
                    <JsonViewer :data="expandedEvent?.payload_json ?? null" :show-toolbar="true" :max-height="'20rem'" />
                  </div>
                  <div v-if="expandedEvent?.previous_hash" class="grid grid-cols-1 gap-2 sm:grid-cols-2">
                    <div>
                      <h4 class="mb-1 text-xs font-semibold uppercase text-muted-foreground">{{ $t('views.AdminAuditView.previous_hash') }}</h4>
                      <code class="block truncate rounded bg-background px-2 py-1 text-xs font-mono" v-tooltip.top="expandedEvent.previous_hash">{{ shortId(expandedEvent.previous_hash) }}</code>
                    </div>
                    <div>
                      <h4 class="mb-1 text-xs font-semibold uppercase text-muted-foreground">{{ $t('views.AdminAuditView.event_id') }}</h4>
                      <code class="block truncate rounded bg-background px-2 py-1 text-xs font-mono" v-tooltip.top="expandedEvent.id">{{ shortId(expandedEvent.id) }}</code>
                    </div>
                  </div>
                  <div v-if="expandedEvent?.request_id">
                    <h4 class="mb-1 text-xs font-semibold uppercase text-muted-foreground">{{ $t('views.AdminAuditView.request_id') }}</h4>
                    <code class="rounded bg-background px-2 py-1 text-xs font-mono">{{ shortId(expandedEvent.request_id) }}</code>
                  </div>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="flex items-center justify-between">
        <button
          type="button"
          :disabled="!prevCursor"
          class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-30 disabled:cursor-not-allowed"
          data-testid="admin-audit-previous"
          @click="() => goToPage(prevCursor)"
        >
          {{ $t('views.AdminAuditView.previous') }}
        </button>
        <span class="text-sm text-muted-foreground">
          {{ $t('views.AdminAuditView.page_of_total', { page: currentPage, count: events.length, total: total }) }}
        </span>
        <button
          type="button"
          :disabled="!nextCursor"
          class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-30 disabled:cursor-not-allowed"
          data-testid="admin-audit-next"
          @click="() => goToPage(nextCursor)"
        >
          {{ $t('views.AdminAuditView.next') }}
        </button>
      </div>
    </template>
    </div>

    <section
      v-else
      id="admin-audit-source-system"
      role="tabpanel"
      aria-labelledby="admin-audit-tab-system"
      class="space-y-4"
      data-testid="admin-audit-system-section"
    >
      <div>
        <h2 class="text-base font-semibold">{{ $t('views.AdminAuditView.tab_system_events') }}</h2>
        <p class="text-sm text-muted-foreground">{{ $t('views.AdminAuditView.system_subtitle') }}</p>
      </div>

      <div class="card p-4">
        <div class="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <div>
            <label for="admin-audit-system-event-type" class="mb-1 block text-xs font-medium text-muted-foreground">{{ $t('views.AdminAuditView.event_type') }}</label>
            <input
              id="admin-audit-system-event-type"
              v-model="systemFilterEventType"
              type="text"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
              data-testid="admin-audit-system-event-type"
            />
          </div>
          <div>
            <label for="admin-audit-system-org-id" class="mb-1 block text-xs font-medium text-muted-foreground">{{ $t('views.AdminAuditView.system_organisation') }}</label>
            <input
              id="admin-audit-system-org-id"
              v-model="systemFilterOrgId"
              type="text"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
              data-testid="admin-audit-system-org-id"
            />
          </div>
          <div>
            <label for="admin-audit-system-date-from" class="mb-1 block text-xs font-medium text-muted-foreground">{{ $t('views.AdminAuditView.from') }}</label>
            <input
              id="admin-audit-system-date-from"
              v-model="systemFilterDateFrom"
              type="date"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
              data-testid="admin-audit-system-date-from"
            />
          </div>
          <div>
            <label for="admin-audit-system-date-to" class="mb-1 block text-xs font-medium text-muted-foreground">{{ $t('views.AdminAuditView.to') }}</label>
            <input
              id="admin-audit-system-date-to"
              v-model="systemFilterDateTo"
              type="date"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
              data-testid="admin-audit-system-date-to"
            />
          </div>
        </div>
        <div class="mt-3 flex items-center gap-2">
          <button
            type="button"
            class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
            data-testid="admin-audit-system-reset"
            @click="resetSystemFilters"
          >
            {{ $t('views.AdminAuditView.reset') }}
          </button>
          <span
            role="status"
            aria-live="polite"
            class="ml-auto text-sm text-muted-foreground"
            data-testid="admin-audit-system-status"
          >
            <template v-if="systemLoading">{{ $t('views.AdminAuditView.system_loading') }}</template>
            <template v-else-if="systemError">{{ $t('views.AdminAuditView.system_failed_to_load') }} {{ systemError }}</template>
            <template v-else>{{ $t('views.AdminAuditView.system_records_count', { count: systemTotal }, systemTotal) }}</template>
          </span>
        </div>
      </div>

      <div v-if="systemLoading" aria-hidden="true" class="table-wrapper overflow-x-auto">
        <table class="w-full">
          <thead>
            <tr>
              <th class="table-header">{{ $t('views.AdminAuditView.timestamp') }}</th>
              <th class="table-header">{{ $t('views.AdminAuditView.event_type') }}</th>
              <th class="table-header">{{ $t('views.AdminAuditView.system_organisation') }}</th>
              <th class="table-header">{{ $t('views.AdminAuditView.actor') }}</th>
              <th class="table-header">{{ $t('views.AdminAuditView.summary') }}</th>
              <th class="w-8 table-header" />
            </tr>
          </thead>
          <tbody class="divide-y">
            <tr v-for="row in 5" :key="row">
              <td class="table-cell whitespace-nowrap"><div class="h-4 w-28 rounded bg-muted/50" /></td>
              <td class="table-cell"><div class="h-4 w-32 rounded bg-muted/50" /></td>
              <td class="table-cell"><div class="h-4 w-32 rounded bg-muted/50" /></td>
              <td class="table-cell"><div class="h-4 w-24 rounded bg-muted/50" /></td>
              <td class="table-cell"><div class="h-4 w-full max-w-sm rounded bg-muted/50" /></td>
              <td class="table-cell"><div class="ml-auto h-4 w-4 rounded bg-muted/50" /></td>
            </tr>
          </tbody>
        </table>
      </div>

      <ErrorAlert v-else-if="systemError" :message="systemError" :on-retry="loadSystemEvents" />

      <EmptyState
        v-else-if="systemEvents.length === 0"
        :title="$t('views.AdminAuditView.system_none_found')"
        :description="$t('views.AdminAuditView.system_try_adjusting')"
      />

      <template v-else>
        <div class="table-wrapper overflow-x-auto">
          <table class="w-full">
            <thead>
              <tr>
                <th class="table-header">{{ $t('views.AdminAuditView.timestamp') }}</th>
                <th class="table-header">{{ $t('views.AdminAuditView.event_type') }}</th>
                <th class="table-header">{{ $t('views.AdminAuditView.system_organisation') }}</th>
                <th class="table-header">{{ $t('views.AdminAuditView.actor') }}</th>
                <th class="table-header">{{ $t('views.AdminAuditView.summary') }}</th>
                <th class="w-8 table-header" />
              </tr>
            </thead>
            <tbody class="divide-y">
              <tr
                v-for="event in systemEvents"
                :key="event.id"
                class="cursor-pointer transition-colors hover:bg-muted/30"
                :data-testid="'admin-audit-system-event-row-' + event.id"
                tabindex="0"
                @click="toggleSystemExpand(event.id)"
                @keydown.enter="toggleSystemExpand(event.id)"
                @keydown.space.prevent="toggleSystemExpand(event.id)"
              >
                <td class="table-cell whitespace-nowrap">
                  {{ formatTimestamp(event.created_at) }}
                </td>
                <td class="table-cell">
                  <span :class="badgeClass(event.event_type)">
                    {{ event.event_type }}
                  </span>
                </td>
                <td class="table-cell font-mono text-xs">
                  <span v-if="event.org_id">{{ shortId(event.org_id) }}</span>
                  <span v-else class="text-muted-foreground/50">&mdash;</span>
                </td>
                <td class="table-cell font-mono">
                  <span v-if="event.actor_user_id">usr_{{ shortId(event.actor_user_id).replace('#', '') }}</span>
                  <span v-else class="text-muted-foreground/50">&mdash;</span>
                </td>
                <td class="table-cell max-w-xs truncate text-muted-foreground" v-tooltip.top="{ value: summarize(event), showDelay: 300 }">{{ summarize(event) }}</td>
                <td class="table-cell text-xs text-muted-foreground">
                  <button
                    type="button"
                    class="inline-flex items-center rounded p-1 hover:bg-muted/30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                    :aria-label="$t('views.AdminAuditView.expand_event', { id: event.id })"
                    :data-testid="'admin-audit-system-event-expand-' + event.id"
                    @click.stop="toggleSystemExpand(event.id)"
                  >
                    <ChevronDown
                      class="h-4 w-4 transition-transform"
                      :class="{ 'rotate-180': expandedSystemId === event.id }"
                      aria-hidden="true"
                    />
                  </button>
                </td>
              </tr>
              <tr v-if="expandedSystemId">
                <td colspan="6" class="border-t bg-muted p-4">
                  <div class="space-y-3">
                    <div v-if="expandedSystemEvent?.payload_json && Object.keys(expandedSystemEvent.payload_json).length > 0">
                      <h4 class="mb-1 text-xs font-semibold uppercase text-muted-foreground">{{ $t('views.AdminAuditView.details') }}</h4>
                      <JsonViewer :data="expandedSystemEvent?.payload_json ?? null" :show-toolbar="true" :max-height="'20rem'" />
                    </div>
                    <div v-if="expandedSystemEvent?.org_id">
                      <h4 class="mb-1 text-xs font-semibold uppercase text-muted-foreground">{{ $t('views.AdminAuditView.system_organisation') }}</h4>
                      <code class="block break-all rounded bg-background px-2 py-1 text-xs font-mono">{{ expandedSystemEvent.org_id }}</code>
                    </div>
                    <div v-if="expandedSystemEvent?.actor_user_id">
                      <h4 class="mb-1 text-xs font-semibold uppercase text-muted-foreground">{{ $t('views.AdminAuditView.actor') }}</h4>
                      <code class="block break-all rounded bg-background px-2 py-1 text-xs font-mono">{{ expandedSystemEvent.actor_user_id }}</code>
                    </div>
                    <div v-if="expandedSystemEvent?.request_id">
                      <h4 class="mb-1 text-xs font-semibold uppercase text-muted-foreground">{{ $t('views.AdminAuditView.request_id') }}</h4>
                      <code class="rounded bg-background px-2 py-1 text-xs font-mono">{{ shortId(expandedSystemEvent.request_id) }}</code>
                    </div>
                    <div>
                      <h4 class="mb-1 text-xs font-semibold uppercase text-muted-foreground">{{ $t('views.AdminAuditView.event_id') }}</h4>
                      <code class="block break-all rounded bg-background px-2 py-1 text-xs font-mono">{{ expandedSystemEvent?.id }}</code>
                    </div>
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <div class="flex items-center justify-between">
          <button
            type="button"
            :disabled="systemPage <= 1"
            class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-30 disabled:cursor-not-allowed"
            data-testid="admin-audit-system-previous"
            @click="goSystemPage(systemPage - 1)"
          >
            {{ $t('views.AdminAuditView.previous') }}
          </button>
          <span class="text-sm text-muted-foreground">
            {{ $t('views.AdminAuditView.system_page_of_total', { page: systemPage, count: systemEvents.length, total: systemTotal }) }}
          </span>
          <button
            type="button"
            :disabled="systemPage * SYSTEM_PAGE_SIZE >= systemTotal"
            class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-30 disabled:cursor-not-allowed"
            data-testid="admin-audit-system-next"
            @click="goSystemPage(systemPage + 1)"
          >
            {{ $t('views.AdminAuditView.next') }}
          </button>
        </div>
      </template>
    </section>
  </div>
  </FeatureGate>
</template>

<script setup lang="ts">
import PageHeader from '../components/shared/PageHeader.vue'
import JsonViewer from '../components/shared/JsonViewer.vue'
import FilterBar from '../components/shared/FilterBar.vue'
import { ref, computed, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { api } from '../lib/api/client'
import { useDataFetch } from '../composables/useDataFetch'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import EmptyState from '../components/shared/EmptyState.vue'
import { formatError } from '../lib/utils'
import { usePlanStore } from '../stores/planStore'
import FeatureGate from '../components/FeatureGate.vue'
import { formatApiError } from '../lib/api/formatError'
import { formatDateFilename } from '../lib/formatDate'
import { shortId } from '../utils/format'
import Select from '../components/shared/AppSelect.vue'
import { ChevronDown } from '@lucide/vue'
import { useCurrentUser } from '../composables/useCurrentUser'

const { t } = useI18n()

const planStore = usePlanStore()

// The system/organisation-lifecycle ledger is system-admin only: the tab that
// reveals it is rendered off the JWT is_system_admin claim, so a non-system
// admin never issues a request the backend would 403.
const { isSystemAdmin } = useCurrentUser()

interface AuditEvent {
  id: string
  event_type: string
  actor_user_id: string | null
  created_at: string | null
  resource_type: string | null
  resource_id: string | null
  payload_json: Record<string, unknown> | null
  request_id: string | null
  previous_hash: string | null
}
interface AuditPage {
  items: AuditEvent[]
  total: number
  next_cursor: string | null
  prev_cursor: string | null
}

interface SystemAuditEvent {
  id: string
  event_type: string
  org_id: string | null
  actor_user_id: string | null
  resource_type: string | null
  resource_id: string | null
  payload_json: Record<string, unknown> | null
  request_id: string | null
  created_at: string | null
}
interface SystemAuditPage {
  items: SystemAuditEvent[]
  total: number
  page: number
  page_size: number
}

// The summary heuristic only needs these three fields, so both audit sources
// (the org chain and the durable system ledger) share one implementation.
type Summarisable = Pick<AuditEvent, 'event_type' | 'resource_type' | 'payload_json'>

const SYSTEM_PAGE_SIZE = 50

// Two audit sources behind one view: the hash-chained per-organisation trail
// (default) and the org-independent durable ledger that survives a hard org
// delete. `auditSource` is the single switch between the two panels.
const auditSource = ref<'org' | 'system'>('org')

const systemPage = ref(1)
const systemFilterEventType = ref('')
const systemFilterOrgId = ref('')
const systemFilterDateFrom = ref('')
const systemFilterDateTo = ref('')
const systemEvents = ref<SystemAuditEvent[]>([])
const systemTotal = ref(0)
const systemLoading = ref(false)
const systemError = ref<string | null>(null)
const expandedSystemId = ref<string | null>(null)
const expandedSystemEvent = ref<SystemAuditEvent | null>(null)

const cursor = ref<string | null>(null)
const currentPage = ref(1)

// Filter refs must be declared BEFORE the useDataFetch call: the fetcher
// invokes buildQuery(), which reads them. Declaring them after caused a TDZ
// ReferenceError on the initial fetch (FAR-608).
const filterEventType = ref('')
const filterActor = ref('')
const filterDateFrom = ref('')
const filterDateTo = ref('')
const filterTargetType = ref('__all__')

const { data: auditData, loading, error, load: loadEvents } = useDataFetch(
  () => api.GET('/api/v1/admin/audit', { params: { query: buildQuery() as any } }),
  { initialValue: { items: [] as AuditEvent[], total: 0, next_cursor: null as string | null, prev_cursor: null as string | null } }
)

const auditPage = computed(() => auditData.value as unknown as AuditPage)
const events = computed(() => auditPage.value.items ?? [])
const total = computed(() => auditPage.value.total ?? 0)
const nextCursor = computed(() => auditPage.value.next_cursor ?? null)
const prevCursor = computed(() => auditPage.value.prev_cursor ?? null)

const expandedId = ref<string | null>(null)
const expandedEvent = ref<AuditEvent | null>(null)

const exporting = ref(false)
const exportingJsonl = ref(false)
const verifying = ref(false)
const chainResult = ref<{ valid: boolean; event_count?: number; error?: string } | null>(null)

function formatActor(event: AuditEvent): string {
  // Prefer the semantic actor label the backend composed at emit time
  // (trigger identity / "system"); fall back to the user id rendering.
  const label = (event.payload_json ?? {})['actor']
  if (typeof label === 'string' && label.length > 0) return label
  if (!event.actor_user_id) return '—'
  return 'usr_' + shortId(event.actor_user_id).replace('#', '')
}

function formatTimestamp(ts: string | null): string {
  if (!ts) return '—'
  const d = new Date(ts)
  return d.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function badgeClass(eventType: string): string {
  if (eventType.startsWith('pipeline.')) return 'badge badge-context-blue'
  if (eventType.startsWith('run.completed')) return 'badge badge-status-success'
  if (eventType.startsWith('run.failed')) return 'badge badge-status-destructive'
  if (eventType.startsWith('run.')) return 'badge badge-status-warning'
  if (eventType.startsWith('user.')) return 'badge badge-context-purple'
  if (eventType.startsWith('team.')) return 'badge badge-context-indigo'
  if (eventType.startsWith('schema.')) return 'badge badge-context-cyan'
  if (eventType.startsWith('connector.')) return 'badge badge-context-orange'
  if (eventType.startsWith('model_backend.')) return 'badge badge-context-pink'
  if (eventType.startsWith('sso_provider.')) return 'badge badge-context-slate'
  if (eventType.startsWith('settings.')) return 'badge badge-context-slate'
  if (eventType.startsWith('api_key.')) return 'badge badge-context-rose'
  if (eventType.startsWith('export.')) return 'badge badge-context-blue'
  return 'badge badge-context-slate'
}

function summarize(event: Summarisable): string {
  // Prefer the descriptive summary the backend composed at emit time (part of
  // the hash-chained payload); the heuristic below is the legacy fallback for
  // events written before summaries existed.
  const provided = (event.payload_json ?? {})['summary']
  if (typeof provided === 'string' && provided.length > 0) return provided

  const et = event.event_type
  const action = et.includes('.') ? et.split('.')[1] : et
  const resource = event.resource_type ?? 'resource'

  const p = event.payload_json ?? {}
  const name = (p as Record<string, unknown>).name ?? (p as Record<string, unknown>).display_name ?? null

  const parts = [action.charAt(0).toUpperCase() + action.slice(1), resource]
  if (name) parts.push(`"${name}"`)
  return parts.join(' ')
}

function toggleExpand(id: string) {
  if (expandedId.value === id) {
    expandedId.value = null
    expandedEvent.value = null
    return
  }
  expandedId.value = id
  expandedEvent.value = events.value.find(e => e.id === id) ?? null
}

function buildQuery() {
  const q: Record<string, unknown> = { limit: 50 }
  if (cursor.value) q.cursor = cursor.value
  if (filterEventType.value) q.event_type = filterEventType.value
  if (filterActor.value) q.user_id = filterActor.value
  if (filterDateFrom.value) q.from_date = filterDateFrom.value
  if (filterDateTo.value) q.to_date = filterDateTo.value
  if (filterTargetType.value !== '__all__') q.entity_type = filterTargetType.value
  return q
}

function goToPage(c: string | null) {
  if (!c) return
  currentPage.value = prevCursor.value === c
    ? Math.max(1, currentPage.value - 1)
    : currentPage.value + 1
  cursor.value = c
  loadEvents()
}

function applyAutoFilters() {
  currentPage.value = 1
  cursor.value = null
  loadEvents()
}

// Auto-apply on dropdown/select filter changes (immediate)
watch(filterEventType, applyAutoFilters)
watch(filterTargetType, applyAutoFilters)

// Auto-apply on text/date filter changes (debounced)
let actorDebounce: ReturnType<typeof setTimeout> | null = null
watch(filterActor, () => {
  if (actorDebounce) clearTimeout(actorDebounce)
  actorDebounce = setTimeout(applyAutoFilters, 300)
})

let dateFromDebounce: ReturnType<typeof setTimeout> | null = null
watch(filterDateFrom, () => {
  if (dateFromDebounce) clearTimeout(dateFromDebounce)
  dateFromDebounce = setTimeout(applyAutoFilters, 300)
})

let dateToDebounce: ReturnType<typeof setTimeout> | null = null
watch(filterDateTo, () => {
  if (dateToDebounce) clearTimeout(dateToDebounce)
  dateToDebounce = setTimeout(applyAutoFilters, 300)
})

function resetFilters() {
  filterEventType.value = ''
  filterActor.value = ''
  filterDateFrom.value = ''
  filterDateTo.value = ''
  filterTargetType.value = '__all__'
  currentPage.value = 1
  cursor.value = null
  loadEvents()
}

function buildSystemQuery() {
  const q: Record<string, unknown> = { page: systemPage.value, page_size: SYSTEM_PAGE_SIZE }
  if (systemFilterEventType.value) q.event_type = systemFilterEventType.value
  if (systemFilterOrgId.value) q.org_id = systemFilterOrgId.value
  if (systemFilterDateFrom.value) q.from_date = systemFilterDateFrom.value
  if (systemFilterDateTo.value) q.to_date = systemFilterDateTo.value
  return q
}

async function loadSystemEvents() {
  systemLoading.value = true
  systemError.value = null
  try {
    const { data, error: err } = await api.GET('/api/v1/admin/system-audit', {
      params: { query: buildSystemQuery() as any },
    })
    if (err) {
      systemError.value = formatError(err)
      return
    }
    const page = data as unknown as SystemAuditPage | undefined
    systemEvents.value = page?.items ?? []
    systemTotal.value = page?.total ?? 0
  } catch (e: unknown) {
    systemError.value = formatApiError(e)
  } finally {
    systemLoading.value = false
  }
}

function applySystemFilters() {
  systemPage.value = 1
  loadSystemEvents()
}

function resetSystemFilters() {
  systemFilterEventType.value = ''
  systemFilterOrgId.value = ''
  systemFilterDateFrom.value = ''
  systemFilterDateTo.value = ''
  systemPage.value = 1
  loadSystemEvents()
}

function goSystemPage(page: number) {
  if (page < 1) return
  systemPage.value = page
  loadSystemEvents()
}

function toggleSystemExpand(id: string) {
  if (expandedSystemId.value === id) {
    expandedSystemId.value = null
    expandedSystemEvent.value = null
    return
  }
  expandedSystemId.value = id
  expandedSystemEvent.value = systemEvents.value.find(e => e.id === id) ?? null
}

// Switching to the ledger fetches it (the first switch is the initial load);
// filters apply immediately on the dropdown/date controls and debounced on the
// free-text fields, mirroring the organisation trail above.
watch(auditSource, (source) => {
  if (source === 'system') loadSystemEvents()
})

let systemDebounce: ReturnType<typeof setTimeout> | null = null
const applySystemFiltersDebounced = () => {
  if (systemDebounce) clearTimeout(systemDebounce)
  systemDebounce = setTimeout(applySystemFilters, 300)
}
watch([systemFilterEventType, systemFilterOrgId], applySystemFiltersDebounced)
watch([systemFilterDateFrom, systemFilterDateTo], applySystemFiltersDebounced)

async function exportCsv() {
  exporting.value = true
  try {
    const allEvents: AuditEvent[] = []
    let page = 1
    const pageSize = 1000
    let totalPages = 1

    while (page <= totalPages) {
      const { data, error: err } = await api.GET('/api/v1/admin/audit/export', {
        params: {
          query: {
            page,
            page_size: pageSize,
            event_type: filterEventType.value || undefined,
            user_id: filterActor.value || undefined,
            entity_type: filterTargetType.value || undefined,
            from_date: filterDateFrom.value || undefined,
            to_date: filterDateTo.value || undefined,
          } as any,
        },
      })
      if (err) {
        error.value = `${t('views.AdminAuditView.export_failed')} ${formatError(err)}`
        return
      }
      if (!data) break
      const exportPage = data as unknown as AuditPage
      allEvents.push(...exportPage.items)
      totalPages = Math.ceil(exportPage.total / pageSize)
      page++
    }

    const headers = ['Timestamp', 'Event Type', 'Actor ID', 'Target Type', 'Target ID', 'Summary', 'Request ID', 'Previous Hash']
    const rows = allEvents.map(e => [
      e.created_at ?? '',
      e.event_type,
      e.actor_user_id ?? '',
      e.resource_type ?? '',
      e.resource_id ?? '',
      summarize(e).replaceAll('"', '""'),
      e.request_id ?? '',
      e.previous_hash ?? '',
    ])
    const csvContent = [
      headers.join(','),
      ...rows.map(r => r.map(v => `"${v}"`).join(',')),
    ].join('\n')

    const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `audit-log-${formatDateFilename(new Date())}.csv`
    a.click()
    URL.revokeObjectURL(url)
  } catch (e: unknown) {
    error.value = `${t('views.AdminAuditView.export_failed')} ${formatApiError(e)}`
  } finally {
    exporting.value = false
  }
}

async function verifyChain() {
  verifying.value = true
  chainResult.value = null
  error.value = null
  try {
    const res = await (api as any).GET('/api/v1/admin/audit/verify')
    const data = res.data as any
    const err = res.error
    if (err) {
      chainResult.value = { valid: false, error: formatError(err) }
    } else if (data) {
      chainResult.value = {
        valid: data.valid !== false,
        event_count: data.event_count,
        error: data.detail || data.error,
      }
    }
  } catch (e: unknown) {
    chainResult.value = { valid: false, error: formatApiError(e) }
  } finally {
    verifying.value = false
  }
}

async function exportJsonl() {
  exportingJsonl.value = true
  try {
    const allEvents: AuditEvent[] = []
    let page = 1
    const pageSize = 1000
    let totalPages = 1

    while (page <= totalPages) {
      const { data, error: err } = await api.GET('/api/v1/admin/audit/export', {
        params: {
          query: {
            page,
            page_size: pageSize,
            event_type: filterEventType.value || undefined,
            user_id: filterActor.value || undefined,
            entity_type: filterTargetType.value || undefined,
            from_date: filterDateFrom.value || undefined,
            to_date: filterDateTo.value || undefined,
          } as any,
        },
      })
      if (err) {
        error.value = `${t('views.AdminAuditView.export_failed')} ${formatError(err)}`
        return
      }
      if (!data) break
      const exportPage = data as unknown as AuditPage
      allEvents.push(...exportPage.items)
      totalPages = Math.ceil(exportPage.total / pageSize)
      page++
    }

    const jsonl = allEvents.map(e => JSON.stringify(e)).join('\n')
    const blob = new Blob([jsonl], { type: 'application/x-ndjson' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `audit-log-${formatDateFilename(new Date())}.jsonl`
    a.click()
    URL.revokeObjectURL(url)
  } catch (e: unknown) {
    error.value = `${t('views.AdminAuditView.export_failed')} ${formatApiError(e)}`
  } finally {
    exportingJsonl.value = false
  }
}

planStore.fetchPlan()
</script>
