<template>
    <div class="page-wide">
    <LoadingSpinner v-if="loading" />
    <ErrorAlert v-else-if="error" :message="error" />
    <template v-else-if="run">
      <nav aria-label="Breadcrumb" class="mb-4 flex items-center gap-1 text-sm text-muted-foreground">
        <router-link to="/runs" class="hover:text-foreground transition-colors">{{ $t('views.RunDetailView.runs') }}</router-link>
        <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="h-3.5 w-3.5"><polyline points="9 18 15 12 9 6"/></svg>
        <span class="text-foreground font-medium">{{ run.pipeline_name || (run.run_number != null ? '#' + run.run_number : shortId(run.run_id)) }}</span>
      </nav>
      <!-- Run Header -->
      <header class="flex flex-wrap items-center justify-between gap-4">
        <div>
          <div class="flex items-center gap-3">
            <PageHeader :title="$t('views.RunDetailView.run_detail')" />
            <span :class="statusBadgeClass" class="capitalize">{{ run.status }}</span>
          </div>
          <p class="mt-1 text-sm text-muted-foreground">
            Pipeline: <span class="font-medium text-foreground">{{ formatRun(run) }}</span>
          </p>
          <p class="text-xs text-muted-foreground">
            Run ID: <code class="rounded bg-muted px-1.5 py-0.5 font-mono text-xs">{{ shortId(run.run_id) }}</code>
            <button
              type="button"
              :aria-label="$t('views.RunDetailView.copy_run_id')"
              class="ml-1 inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-xs font-medium text-primary hover:bg-primary/10"
              @click="copyRunId"
            >
              {{ copied ? $t('views.RunDetailView.copied') : $t('views.RunDetailView.copy') }}
            </button>
          </p>
        </div>
        <div class="text-right text-xs text-muted-foreground">
          <div v-if="run.total_cost_usd != null" class="text-base font-semibold tabular-nums text-foreground">
            Total: {{ formatMoney(Number(formattedCost), currencyCode, 6) }}
          </div>
          <button
            type="button"
            data-testid="run-detail-share-summary"
            class="mt-2 inline-flex items-center gap-1 rounded-md px-3 py-1.5 text-sm font-medium text-primary hover:bg-primary/10"
            @click="copyShareSummary"
          >
            {{ shareCopied ? $t('views.RunDetailView.copied') : $t('views.RunDetailView.share_summary') }}
          </button>
        </div>
      </header>

      <!-- Queue position banner (pending + waiting on sandbox capacity) -->
      <output
        v-if="run.status === 'pending' && run.capacity?.waiting"
        data-testid="run-detail-queue-banner"
        aria-live="polite"
        :aria-label="$t('views.RunDetailView.queued_waiting_slot', { active: run.capacity.active_runs, limit: run.capacity.concurrency_limit ?? '∞' })"
        class="mb-4 block rounded-lg border border-warning/50 bg-warning/10 px-4 py-2 text-sm text-warning"
      >
        {{ $t('views.RunDetailView.queued_waiting_slot', { active: run.capacity.active_runs, limit: run.capacity.concurrency_limit ?? '∞' }) }}
      </output>
      <output
        v-else-if="run.status === 'pending'"
        data-testid="run-detail-queued-starting"
        :aria-label="$t('views.RunDetailView.queued_starting_soon')"
        class="mb-4 block text-xs text-muted-foreground"
      >
        {{ $t('views.RunDetailView.queued_starting_soon') }}
      </output>

      <!-- HITL Gate -->
      <section v-if="run.status === 'awaiting_human' && pendingGates.length > 0" class="rounded-lg border bg-card p-6 mb-6">
        <h2 class="text-base font-semibold tracking-tight mb-4">HITL Gate</h2>
        <div v-for="gate in pendingGates" :key="gate.gate_id" class="space-y-3">
          <div class="flex items-center gap-2 text-sm">
            <span class="font-medium">{{ $t('views.RunDetailView.gate_label') }}</span>
            <code
              v-tooltip.top="{ value: gate.gate_id, showDelay: 300 }"
              class="cursor-help select-all rounded bg-muted px-1.5 py-0.5 font-mono text-xs"
            >{{ gate.label || shortId(gate.gate_id) }}</code>
            <button
              type="button"
              data-testid="run-detail-copy-gate-id"
              :aria-label="$t('views.RunDetailView.copy_gate_id')"
              class="inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-xs font-medium text-primary hover:bg-primary/10"
              @click="copyText(gate.gate_id)"
            >
              <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
            </button>
          </div>
          <div v-if="gate.claimed_by && !claimToken" class="rounded-lg bg-muted/50 p-3 text-sm text-muted-foreground">
            Claimed by {{ gate.claimed_by }}
          </div>
          <div v-else-if="claimLoading" class="flex justify-center py-4">
            <div class="h-5 w-5 animate-spin rounded-full border-2 border-primary border-t-transparent" />
          </div>
          <template v-else-if="claimToken">
            <div class="space-y-3">
              <textarea
                v-model="hitlNotes"
                rows="2"
                data-testid="run-detail-hitl-notes"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                placeholder="Review notes (optional)"
                aria-label="Review notes"
              />
              <div class="flex gap-2">
                <button
                  type="button"
                  :disabled="Boolean(actioning)"
                  data-testid="run-detail-approve"
                  class="flex-1 rounded-lg bg-success px-4 py-2 text-sm font-medium text-white hover:bg-success/90 disabled:opacity-50"
                  @click="approveGate"
                >
                  {{ actioning === 'approve' ? 'Approving...' : 'Approve' }}
                </button>
                <button
                  type="button"
                  :disabled="Boolean(actioning)"
                  data-testid="run-detail-reject"
                  class="flex-1 rounded-lg bg-destructive px-4 py-2 text-sm font-medium text-destructive-foreground hover:bg-destructive/90 disabled:opacity-50"
                  @click="rejectGate"
                >
                  {{ actioning === 'reject' ? 'Rejecting...' : 'Reject' }}
                </button>
              </div>
            </div>
          </template>
          <button
            type="button"
            v-else
            :disabled="claimLoading"
            class="w-full rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
            data-testid="run-detail-claim-gate"
            @click="claimGate(gate)"
          >
            {{ claimLoading ? 'Claiming...' : 'Claim Gate' }}
          </button>
          <div v-if="hitlMessage" class="text-sm" :class="hitlMessage.type === 'error' ? 'text-destructive' : 'text-success'">
            {{ hitlMessage.text }}
          </div>
        </div>
      </section>

      <!-- Timestamps -->
      <div v-if="runTimestamps" class="flex flex-wrap gap-x-6 gap-y-1 text-xs text-muted-foreground">
        <div><span class="font-medium text-foreground">{{ $t('views.RunDetailView.created') }}</span> {{ runTimestamps.created }}</div>
        <div><span class="font-medium text-foreground">{{ $t('views.RunDetailView.started') }}</span> {{ runTimestamps.started }}</div>
        <div><span class="font-medium text-foreground">{{ $t('views.RunDetailView.completed') }}</span> {{ runTimestamps.completed }}</div>
        <div data-testid="run-detail-trigger-actor"><span class="font-medium text-foreground">{{ $t('views.RunDetailView.triggered_by') }}</span> {{ run.trigger_actor || triggerTypeLabel(run.trigger_type, t) }}</div>
        <div data-testid="run-detail-heartbeat">
          <span class="font-medium text-foreground">{{ $t('views.RunDetailView.last_heartbeat') }}</span>
          <span :class="isHeartbeatStale(heartbeatAge) ? 'font-medium text-warning' : ''">{{ formatHeartbeatAge(heartbeatAge, t) }}<span v-if="isHeartbeatStale(heartbeatAge)"> ({{ $t('views.RunDetailView.stale') }})</span></span>
        </div>
      </div>

      <!-- Live cost + tokens so far (non-terminal runs only) -->
      <div v-if="liveCostPresent" data-testid="run-detail-live-cost" class="mt-2 mb-4 text-xs text-muted-foreground">
        <span class="font-medium text-foreground">{{ $t('views.RunDetailView.cost_so_far') }}:</span>
        {{ formatMoney(liveCostTotal, currencyCode, 4) }}<span v-if="liveTokenTotal > 0"> · {{ formatTokenCount(liveTokenTotal) }} {{ $t('views.RunDetailView.tokens') }}</span>
      </div>

      <!-- Live node progress strip -->
      <div
        v-if="nodeProgressChips.length > 0"
        data-testid="run-detail-node-progress"
        class="mb-4 flex flex-wrap items-center gap-1.5"
        aria-live="polite"
      >
        <button
          v-for="chip in nodeProgressChips"
          :key="chip.name"
          type="button"
          :aria-label="$t('views.RunDetailView.node_progress_aria', { name: chip.name, state: nodeStateLabel(chip.state) })"
          :data-testid="`run-detail-node-progress-${chip.name}`"
          class="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium transition-colors hover:opacity-80"
          :class="[chipClass(chip.state), expandedLogs.has(chip.name) ? 'underline decoration-dotted underline-offset-2' : '']"
          @click="toggleNodeLogs(chip.name)"
        >
          <template v-if="chip.state === 'running'">
            <span class="relative flex h-2 w-2" aria-hidden="true">
              <span class="absolute inline-flex h-full w-full animate-ping rounded-full bg-warning opacity-75"></span>
              <span class="relative inline-flex h-2 w-2 rounded-full bg-warning"></span>
            </span>
            {{ $t('views.RunDetailView.running_label') }}
          </template>
          <Check v-else-if="chip.state === 'completed'" class="h-3 w-3" aria-hidden="true" />
          <X v-else-if="chip.state === 'failed'" class="h-3 w-3" aria-hidden="true" />
          <span v-else class="h-1.5 w-1.5 rounded-full bg-muted-foreground/40" aria-hidden="true"></span>
          <span>{{ nodeLabel(chip.name) }}</span>
        </button>
      </div>

      <!-- Run Input Payload — the parameters provided when the run was scheduled -->
      <div v-if="runIO?.input_payload" data-testid="run-detail-input-payload" class="rounded-lg border border-border bg-card p-4 mb-4">
        <div class="flex items-center justify-between mb-1">
          <h3 class="text-sm font-semibold">{{ $t('views.RunDetailView.run_input') }}</h3>
          <button
            type="button"
            class="text-xs text-primary hover:bg-primary/10 rounded px-2 py-1"
            data-testid="run-detail-copy-input"
            @click="copyInputPayload"
          >
            {{ inputPayloadCopied ? $t('views.RunDetailView.copied') : $t('views.RunDetailView.copy') }}
          </button>
        </div>
        <JsonViewer :data="runIO.input_payload" :show-toolbar="true" :max-height="'16rem'" />
      </div>

      <!-- Work items -->
      <section v-if="run.work_item_refs && run.work_item_refs.length > 0" data-testid="run-detail-work-items" class="rounded-lg border border-border bg-card p-4 mb-4">
        <h3 class="text-sm font-semibold mb-2">{{ $t('views.RunDetailView.work_items') }}</h3>
        <div class="space-y-1.5">
          <div v-for="(item, idx) in run.work_item_refs" :key="`${item.kind}-${item.ref}-${idx}`" class="flex flex-wrap items-center gap-2 text-xs">
            <template v-if="isGithubWorkItem(item)">
              <a v-if="getPrUrl(item)" :href="getPrUrl(item)!" target="_blank" rel="noopener noreferrer" :data-testid="`run-detail-pr-link-${idx}`" class="inline-flex items-center gap-1 text-primary hover:underline">
                <span class="badge text-xs badge-context-blue">{{ githubKindLabel(item) }}</span>
                <span class="font-medium">#{{ githubRefId(item) }}</span>
              </a>
              <span v-else class="inline-flex items-center gap-1">
                <span class="badge text-xs badge-context-blue">{{ githubKindLabel(item) }}</span>
                <span class="font-medium">#{{ githubRefId(item) }}</span>
              </span>
            </template>
            <template v-else>
              <span class="inline-flex items-center rounded-full bg-muted px-2 py-0.5 font-medium capitalize">{{ item.kind || '—' }}</span>
              <code class="rounded bg-muted px-1.5 py-0.5 font-mono">{{ item.ref || '—' }}</code>
            </template>
            <span v-if="item.source" class="text-muted-foreground">{{ item.source }}</span>
            <span v-if="item.status" class="inline-flex items-center rounded-full bg-muted px-2 py-0.5 text-muted-foreground capitalize">{{ item.status }}</span>
          </div>
        </div>
      </section>

      <!-- Child runs -->
      <section v-if="run.child_runs && run.child_runs.length > 0" data-testid="run-detail-child-runs" class="rounded-lg border border-border bg-card p-4 mb-4">
        <h3 class="text-sm font-semibold mb-2">{{ $t('views.RunDetailView.child_runs') }}</h3>
        <div class="space-y-1.5">
          <div v-for="child in run.child_runs" :key="child.run_id" class="flex flex-wrap items-center gap-2 text-xs">
            <router-link
              :to="`/runs/${child.run_id}`"
              :data-testid="`run-detail-child-link-${child.run_id}`"
              class="font-medium text-primary hover:underline"
            >
              {{ child.run_number != null ? `#${child.run_number}` : shortId(child.run_id) }}
            </router-link>
            <span :class="childRunBadgeClass(child.status)" class="capitalize">{{ child.status || '—' }}</span>
            <span v-if="child.pipeline_name" class="text-muted-foreground">{{ child.pipeline_name }}</span>
          </div>
        </div>
      </section>

      <!-- Cancel button for non-terminal runs -->
      <div v-if="canCancel" class="my-4">
        <button
          type="button"
          :disabled="cancelling"
          data-testid="run-detail-cancel"
          class="inline-flex items-center gap-2 rounded-lg border border-destructive/50 bg-destructive/10 px-4 py-2 text-sm font-medium text-destructive hover:bg-destructive/20 disabled:opacity-50"
          @click="cancelRun"
        >
          <svg v-if="cancelling" class="h-4 w-4 animate-spin" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/></svg>
          <svg v-else xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="8" y1="12" x2="16" y2="12"/></svg>
          {{ cancelling ? $t('views.RunDetailView.stopping') : $t('views.RunDetailView.stop') }}
        </button>
        <span v-if="cancelError" role="alert" class="ml-3 text-xs text-destructive">{{ cancelError }}</span>
      </div>

      <!-- Trace ID -->
      <div v-if="run.trace_id" class="flex items-center gap-2">
        <span class="text-xs text-muted-foreground">{{ $t('views.RunDetailView.otel_trace_id') }}</span>
        <code class="select-all rounded bg-muted px-1.5 py-0.5 font-mono text-xs" :title="run.trace_id">{{ shortId(run.trace_id) }}</code>
        <button
          type="button"
          data-testid="run-detail-copy-trace-id"
          :aria-label="$t('views.RunDetailView.copy_trace_id')"
          class="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium text-primary hover:bg-primary/10"
          @click="copyTraceId"
        >
          {{ copied ? $t('views.RunDetailView.copied') : $t('views.RunDetailView.copy') }}
        </button>
        <a
          v-if="run.trace_url"
          :href="run.trace_url"
          target="_blank"
          rel="noopener noreferrer"
          data-testid="run-detail-view-trace"
          class="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium text-primary hover:bg-primary/10"
        >
          <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>
          {{ $t('views.RunDetailView.view_trace') }}
        </a>
      </div>

      <div v-if="run?.status === 'complete' && lastNodeOutput" class="card p-5 mb-6">
        <div class="flex items-center justify-between mb-3">
          <h2 class="text-base font-semibold text-foreground">{{ $t('views.RunDetailView.final_output') }}</h2>
          <button
            type="button"
            class="px-3 py-1.5 text-xs font-medium rounded-lg border border-input bg-background hover:bg-accent transition-colors"
            @click="copyOutput"
            data-testid="run-detail-copy-output"
          >
            {{ outputCopied ? $t('views.RunDetailView.copied') : $t('views.RunDetailView.copy') }}
          </button>
        </div>
        <JsonViewer :data="lastNodeOutput" :show-toolbar="false" :max-height="'30rem'" />
      </div>

      <!-- Capacity-blocked pending run (queued on sandbox concurrency limit) -->
      <div v-if="run.status === 'pending' && (run.error_code === 'capacity.org' || run.error_code === 'capacity.pipeline')" data-testid="run-detail-waiting-for-capacity" class="rounded-lg border border-warning/50 bg-warning/10 p-4 mb-4">
        <h3 class="text-sm font-semibold text-warning mb-1">{{ $t('views.RunDetailView.waiting_for_capacity') }}</h3>
        <p v-if="run.error_detail" class="text-xs whitespace-pre-wrap text-warning/80">{{ run.error_detail }}</p>
      </div>

      <!-- Failed Run Diagnostics -->
      <div v-if="run.status === 'failed' && run.error_detail" class="rounded-lg border border-destructive/50 bg-destructive/10 p-4 mb-4">
        <div class="flex flex-wrap items-center gap-2 mb-1">
          <h3 class="text-sm font-semibold text-destructive">{{ $t('views.RunDetailView.run_error') }}</h3>
          <RunErrorTag
            v-if="run.error_code"
            :code="run.error_code"
            :detail="(run.error_detail as string | null | undefined)?.slice(0, 200)"
          />
        </div>
        <pre class="text-xs whitespace-pre-wrap font-mono text-destructive/80">{{ run.error_detail }}</pre>
      </div>

      <!-- Guardrail Summary -->
      <section v-if="guardrailBuckets.length > 0" class="rounded-lg border bg-card p-6 mb-6" data-testid="run-detail-guardrail-summary">
        <h2 class="mb-3 text-base font-semibold tracking-tight">{{ $t('views.RunDetailGuardrailSummary.guardrail_summary_title') }}</h2>
        <div class="flex flex-wrap gap-3">
          <div
            v-for="bucket in guardrailBuckets"
            :key="bucket.key"
            data-testid="run-detail-guardrail-bucket"
            class="inline-flex items-center gap-2 rounded-lg border bg-muted/30 px-3 py-2 text-sm"
          >
            <span :class="bucketClass(bucket.key)" class="inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium capitalize">
              {{ bucket.label }}
            </span>
            <span class="tabular-nums font-semibold">{{ bucket.value }}</span>
          </div>
        </div>
      </section>

      <!-- Guardrail-blocked override (terminal eval_failed / eval_blocked) -->
      <div
        v-if="isGuardrailBlocked"
        data-testid="run-detail-guardrail-override-panel"
        class="rounded-lg border border-warning/50 bg-warning/10 p-4 mb-4"
      >
        <h3 class="text-sm font-semibold text-warning mb-1">{{ $t('views.RunDetailGuardrailSummary.override_guardrail') }}</h3>
        <p class="text-xs text-warning/80 mb-3">{{ $t('views.RunDetailGuardrailSummary.override_disclosure') }}</p>
        <Button v-if="isOrgOperator" data-testid="run-detail-override-guardrail" @click="openOverrideDialog">
          {{ $t('views.RunDetailGuardrailSummary.override_guardrail') }}
        </Button>
        <p
          v-else
          data-testid="run-detail-override-role-note"
          class="text-xs text-muted-foreground"
        >
          {{ $t('views.RunDetailGuardrailSummary.override_requires_operator') }}
        </p>
      </div>

      <!-- Per-Node Execution Trace -->
      <section class="space-y-4 rounded-lg border bg-card p-6">
        <div class="flex items-baseline justify-between gap-4">
          <h2 class="text-base font-semibold tracking-tight">{{ $t('views.RunDetailView.execution_trace') }}</h2>
          <p class="text-xs text-muted-foreground">{{ $t('views.RunDetailView.per_node_model_cost_caveat') }}</p>
        </div>

        <div v-if="nodeEntries.length === 0 && run.status !== 'failed'" class="py-4 text-center text-sm text-muted-foreground">
          {{ $t('views.RunDetailView.no_node_data') }}
        </div>
        <div v-else-if="nodeEntries.length === 0 && run.status === 'failed'" class="py-4 text-center text-sm text-muted-foreground">
          {{ $t('views.RunDetailView.no_node_data_failed') }}
        </div>

        <div v-else class="overflow-x-auto">
          <table class="w-full text-left text-sm">
          <thead>
            <tr class="border-b text-xs uppercase text-muted-foreground">
              <th class="pb-2 pr-4 font-medium">{{ $t('views.RunDetailView.node') }}</th>
              <th class="pb-2 pr-4 font-medium capitalize">{{ $t('views.RunDetailView.status') }}</th>
              <th class="pb-2 pr-4 font-medium">{{ $t('views.RunDetailView.duration') }}</th>
              <th class="pb-2 pr-4 font-medium">{{ $t('views.RunDetailView.input_tokens') }}</th>
              <th class="pb-2 pr-4 font-medium">{{ $t('views.RunDetailView.output_tokens') }}</th>
              <th class="pb-2 pr-4 font-medium">{{ $t('views.RunDetailView.model_cost') }}</th>
              <th class="pb-2 pr-4 font-medium">{{ $t('views.RunDetailView.trace_id') }}</th>
              <th class="pb-2 pr-4 font-medium">{{ $t('views.RunDetailView.io') }}</th>
              <th class="pb-2 pr-4 font-medium">{{ $t('views.RunDetailView.logs') }}</th>
              <th class="pb-2 font-medium">{{ $t('views.RunDetailView.prompt') }}</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="node in nodeEntries"
              :key="node.name"
              class="border-b last:border-b-0 hover:bg-muted/30"
            >
              <td class="py-3 pr-4 font-medium" :title="node.name">
                <span class="select-all">{{ nodeLabel(node.name) }}</span>
                <button
                  type="button"
                  data-testid="run-detail-copy-node-id"
                  :aria-label="$t('views.RunDetailView.copy_node_id')"
                  class="ml-1 inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-xs font-medium text-primary hover:bg-primary/10"
                  @click="copyText(node.name)"
                >
                  <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
                </button>
              </td>
              <td class="py-3 pr-4">
                <span :class="[nodeStatusBadgeClass(node), 'capitalize']">{{ node.status }}</span>
                <span
                  v-if="node.stallReason"
                  data-testid="run-detail-node-stalled"
                  class="ml-2 inline-flex items-center rounded-full bg-warning/10 px-2 py-0.5 text-xs font-medium text-warning"
                  :title="node.stallReason"
                >
                  {{ $t('views.RunDetailView.agent_stalled', { reason: node.stallReason }) }}
                </span>
              </td>
              <td class="py-3 pr-4 tabular-nums text-muted-foreground">{{ node.duration }}</td>
              <td class="py-3 pr-4 tabular-nums">{{ node.inputTokens ?? '—' }}</td>
              <td class="py-3 pr-4 tabular-nums">{{ node.outputTokens ?? '—' }}</td>
              <td class="py-3 pr-4 tabular-nums">{{ node.cost != null ? formatMoney(node.cost, currencyCode, 6) : '—' }}</td>
              <td class="py-3 pr-4">
                <button
                  v-if="node.traceId"
                  type="button"
                  data-testid="run-detail-node-trace-id"
                  :aria-label="node.isNodeSpanId ? $t('views.RunDetailView.copy_node_span_id') : $t('views.RunDetailView.copy_node_trace_id')"
                  class="cursor-pointer rounded bg-muted px-1.5 py-0.5 font-mono text-xs"
                  :title="node.traceId"
                  @click="copyText(node.traceId!)"
                  @keydown.enter="copyText(node.traceId!)"
                  @keydown.space.prevent="copyText(node.traceId!)"
                >{{ shortId(node.traceId) }}…</button>
                <span v-else class="text-muted-foreground">—</span>
              </td>
              <td class="py-3">
                <button
                  v-if="node.io"
                  type="button"
                  data-testid="run-detail-toggle-io"
                  class="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium text-primary hover:bg-primary/10"
                  @click="toggleNodeIO(node.name)"
                >
                  {{ expandedNodes.has(node.name) ? $t('views.RunDetailView.hide') : $t('views.RunDetailView.show') }}
                </button>
                <span v-else class="text-muted-foreground">—</span>
              </td>
              <td class="py-3">
                <button
                  v-if="node.hasLogs || node.telemetry"
                  type="button"
                  data-testid="run-detail-toggle-logs"
                  class="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium text-primary hover:bg-primary/10"
                  @click="toggleNodeLogs(node.name)"
                >
                  {{ expandedLogs.has(node.name) ? $t('views.RunDetailView.hide') : $t('views.RunDetailView.view') }}
                </button>
                <span v-else class="text-muted-foreground">—</span>
              </td>
              <td class="py-3">
                <button
                  type="button"
                  data-testid="run-detail-show-prompt"
                  class="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium text-primary hover:bg-primary/10 disabled:opacity-50"
                  :disabled="promptLoading.has(node.name)"
                  @click="revealPrompt(node.name)"
                >
                  {{ $t('views.RunDetailView.view_prompt') }}
                </button>
              </td>
            </tr>

            <!-- Expandable IO rows -->
            <tr
              v-for="node in nodeEntries"
              :key="'io-' + node.name"
              v-show="expandedNodes.has(node.name)"
              data-testid="run-detail-io-row"
            >
              <td colspan="10" class="space-y-3 px-0 pb-4 pt-1">
                <div class="rounded-lg border bg-muted p-4">
                  <h4 class="mb-2 text-xs font-semibold text-muted-foreground">{{ $t('views.RunDetailView.input') }}</h4>
                  <JsonViewer v-if="node.io?.input != null" :data="node.io.input" :show-toolbar="false" :max-height="'16rem'" />
                  <p v-else data-testid="run-detail-no-input" class="text-sm text-muted-foreground">{{ $t('views.RunDetailView.no_input_data') }}</p>
                </div>
                <div class="rounded-lg border bg-muted p-4">
                  <h4 class="mb-2 text-xs font-semibold text-muted-foreground">{{ $t('views.RunDetailView.output') }}</h4>
                  <JsonViewer v-if="node.io?.output != null" :data="node.io.output" :show-toolbar="false" :max-height="'16rem'" />
                  <p v-else data-testid="run-detail-no-output" class="text-sm text-muted-foreground">{{ $t('views.RunDetailView.no_output_data') }}</p>
                </div>
              </td>
            </tr>

            <!-- Expandable Telemetry / Log rows -->
            <tr
              v-for="node in nodeEntries"
              :key="'log-' + node.name"
              v-show="expandedLogs.has(node.name)"
              data-testid="run-detail-log-row"
            >
              <td colspan="10" class="space-y-3 px-0 pb-4 pt-1">
                <div
                  v-if="!isTerminal && liveOutput[node.name]"
                  class="rounded-lg border border-primary/40 bg-muted p-4"
                  data-testid="run-detail-live-output"
                >
                  <h4 class="mb-2 text-xs font-semibold text-primary">{{ $t('views.RunDetailView.live_output') }}</h4>
                  <pre class="max-h-96 overflow-auto rounded bg-background p-3 text-xs leading-relaxed font-mono whitespace-pre-wrap"><code>{{ liveOutput[node.name] }}</code></pre>
                </div>
                <div v-if="node.telemetry" class="rounded-lg border bg-muted p-4" data-testid="run-detail-node-telemetry">
                  <div class="mb-2 flex flex-wrap items-center gap-2">
                    <h4 class="text-xs font-semibold text-muted-foreground">{{ $t('views.RunDetailView.telemetry') }}</h4>
                    <span v-if="node.telemetry?.status != null" class="inline-flex items-center rounded-full bg-muted px-2 py-0.5 text-xs font-medium capitalize">{{ node.telemetry?.status }}</span>
                    <span v-if="node.telemetry?.exit_code != null && Number(node.telemetry?.exit_code) !== 0" class="inline-flex items-center rounded-full bg-muted px-2 py-0.5 text-xs font-medium tabular-nums">{{ $t('views.RunDetailView.exit_code') }}: {{ node.telemetry?.exit_code }}</span>
                    <span v-if="node.telemetry?.wall_clock_time_ms != null" class="inline-flex items-center rounded-full bg-muted px-2 py-0.5 text-xs font-medium tabular-nums">{{ $t('views.RunDetailView.wall_clock_time') }}: {{ formatMs(Number(node.telemetry?.wall_clock_time_ms)) }}</span>
                    <span v-if="node.telemetry?.cost_estimate_usd != null" class="inline-flex items-center rounded-full bg-muted px-2 py-0.5 text-xs font-medium tabular-nums">{{ $t('views.RunDetailView.cost_estimate') }}: {{ formatMoney(Number(node.telemetry?.cost_estimate_usd), currencyCode, 6) }}</span>
                    <span
                      v-if="node.stallReason"
                      data-testid="run-detail-node-stall-telemetry"
                      class="inline-flex items-center rounded-full bg-warning/10 px-2 py-0.5 text-xs font-medium text-warning"
                      :title="node.stallReason"
                    >
                      {{ $t('views.RunDetailView.agent_stalled', { reason: node.stallReason }) }}
                    </span>
                  </div>
                  <p v-if="nodeSummary(node)" class="text-xs whitespace-pre-wrap text-muted-foreground" data-testid="run-detail-node-summary">
                    <span class="font-medium text-foreground">{{ $t('views.RunDetailView.summary') }}:</span> {{ nodeSummary(node) }}
                  </p>
                </div>
                <div v-if="getNodeLog(node.name, 'agent_stdout')" class="rounded-lg border bg-muted p-4">
                  <h4 class="mb-2 text-xs font-semibold text-muted-foreground">{{ $t('views.RunDetailView.agent_stdout') }}</h4>
                  <pre class="max-h-96 overflow-auto rounded bg-background p-3 text-xs leading-relaxed font-mono whitespace-pre-wrap"><code>{{ getNodeLog(node.name, 'agent_stdout') }}</code></pre>
                  <p v-if="isNodeLogTruncated(node.name, 'agent_stdout')" class="mt-1 text-xs text-muted-foreground">
                    {{ $t('views.RunDetailView.log_truncated', { count: MAX_LOG_CHARS.toLocaleString() }) }}
                  </p>
                </div>
                <div v-if="getNodeLog(node.name, 'agent_stderr')" class="rounded-lg border bg-destructive/10 p-4">
                  <h4 class="mb-2 text-xs font-semibold text-destructive">{{ $t('views.RunDetailView.agent_stderr') }}</h4>
                  <pre class="max-h-48 overflow-auto rounded bg-background p-3 text-xs leading-relaxed font-mono whitespace-pre-wrap"><code>{{ getNodeLog(node.name, 'agent_stderr') }}</code></pre>
                  <p v-if="isNodeLogTruncated(node.name, 'agent_stderr')" class="mt-1 text-xs text-muted-foreground">
                    {{ $t('views.RunDetailView.log_truncated', { count: MAX_LOG_CHARS.toLocaleString() }) }}
                  </p>
                </div>
                <div
                  v-if="!getNodeLog(node.name, 'agent_stdout') && !getNodeLog(node.name, 'agent_stderr') && !liveOutput[node.name]"
                  class="text-center text-sm text-muted-foreground py-4"
                >
                  {{ $t('views.RunDetailView.no_agent_logs') }}
                </div>
              </td>
            </tr>
          </tbody>
        </table>
        </div>
      </section>

      <!-- Workspace Lease -->
      <section v-if="workspaceLease" class="rounded-lg border bg-card p-6">
        <h2 class="mb-3 text-base font-semibold tracking-tight">{{ $t('views.RunDetailView.workspace') }}</h2>
        <div class="space-y-2 text-sm">
          <div class="flex items-center gap-2">
            <span class="font-medium capitalize">{{ $t('views.RunDetailView.status_label') }}</span>
            <span
              class="inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium"
              :class="workspaceStatusClass"
            >
              <span class="h-1.5 w-1.5 rounded-full" :class="workspaceDotClass" />
              <span class="capitalize">{{ workspaceLease.status }}</span>
            </span>
          </div>
          <div v-if="workspaceLease.sandbox_id" class="flex items-center gap-2">
            <span class="font-medium">{{ $t('views.RunDetailView.sandbox_label') }}</span>
            <code class="select-all rounded bg-muted px-1.5 py-0.5 font-mono text-xs" :title="workspaceLease.sandbox_id">{{ shortId(workspaceLease.sandbox_id) }}</code>
          </div>
          <div v-if="workspaceLease.duration_seconds != null">
            <span class="font-medium">{{ $t('views.RunDetailView.duration_label') }}</span>
            <span class="ml-1 tabular-nums">{{ formatDuration(workspaceLease.duration_seconds) }}</span>
          </div>
          <div v-if="workspaceLease.error_message" class="text-destructive">
            <span class="font-medium">{{ $t('views.RunDetailView.error_label') }}</span>
            <span class="ml-1">{{ workspaceLease.error_message }}</span>
          </div>
        </div>
      </section>

      <!-- Total Run Cost -->
      <section v-if="run.total_cost_usd != null" class="rounded-lg border bg-card p-6">
        <div class="flex items-center justify-between">
          <h2 class="text-base font-semibold tracking-tight">{{ $t('views.RunDetailView.total_run_cost') }}</h2>
          <span class="text-2xl font-semibold tabular-nums">{{ formatMoney(Number(formattedCost), currencyCode, 6) }}</span>
        </div>
        <p v-if="totalTokens != null" class="mt-1 text-xs text-muted-foreground">
          {{ $t('views.RunDetailView.total_tokens', { count: totalTokens.toLocaleString() }) }}
        </p>

        <div
          v-if="childRunCost > 0 && aggregateCost != null"
          data-testid="run-detail-aggregate-cost"
          class="mt-3 rounded-lg border border-muted bg-muted/30 px-3 py-2 text-sm"
        >
          <span class="font-medium text-foreground">{{ childRunCount > 0 ? $t('views.RunDetailView.total_including_child_runs_count', childRunCount) : $t('views.RunDetailView.total_including_child_runs') }}</span>
          <span class="ml-1 tabular-nums font-semibold">{{ formatMoney(aggregateCost, currencyCode, 6) }}</span>
          <span class="ml-1 text-xs text-muted-foreground">{{ $t('views.RunDetailView.includes_child_run_cost', { amount: formatMoney(childRunCost, currencyCode, 6) }) }}</span>
        </div>

        <template v-if="breakdownPresent">
          <p v-if="breakdownTotalClamped" class="mt-4 rounded-lg border border-warning/30 bg-warning/5 px-3 py-2 text-sm text-warning" data-testid="run-detail-cost-clamped">
            {{ $t('views.RunDetailView.total_clamped_to_column_capacity') }}
          </p>
          <div v-if="breakdownEntries.length > 0" class="mt-4 overflow-x-auto">
            <table class="w-full text-left text-sm">
              <thead>
                <tr class="border-b text-xs uppercase text-muted-foreground">
                  <th class="pb-2 pr-4 font-medium">{{ $t('views.RunDetailView.component') }}</th>
                  <th class="pb-2 pr-4 font-medium">{{ $t('views.RunDetailView.amount') }}</th>
                  <th class="pb-2 pr-4 font-medium">{{ $t('views.RunDetailView.source') }}</th>
                  <th class="pb-2 font-medium">{{ $t('views.RunDetailView.basis') }}</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="entry in breakdownEntries" :key="entry.component" class="border-b last:border-b-0">
                  <td class="py-2 pr-4 font-medium">
                    {{ entry.display_name || entry.component }}
                    <span v-if="entry.missing_self_report" class="ml-1 inline-flex items-center rounded-full bg-muted px-1.5 py-0.5 text-xs text-muted-foreground" data-testid="run-detail-not-reported">{{ $t('views.RunDetailView.not_reported') }}</span>
                    <span v-if="entry.error" class="ml-1 inline-flex items-center rounded-full bg-warning/10 px-1.5 py-0.5 text-xs text-warning">{{ $t('views.RunDetailView.eval_error_badge') }}</span>
                  </td>
                  <td class="py-2 pr-4 tabular-nums">{{ formatMoney(Number(entry.amountUsd), currencyCode, 6) }}</td>
                  <td class="py-2 pr-4">
                    <span class="inline-flex items-center rounded-full px-1.5 py-0.5 text-xs" :class="entry.source === 'self_reported' ? 'bg-primary/10 text-primary' : 'bg-muted text-muted-foreground'">
                      {{ entry.source === 'self_reported' ? $t('views.RunDetailView.reported') : $t('views.RunDetailView.estimated') }}
                    </span>
                  </td>
                  <td class="py-2 text-xs text-muted-foreground">{{ entry.basisLine }}</td>
                </tr>
              </tbody>
              <tfoot>
                <tr class="border-t font-medium">
                  <td class="py-2 pr-4">{{ $t('views.RunDetailView.sum_of_components') }}</td>
                  <td class="py-2 pr-4 tabular-nums">{{ formatMoney(Number(breakdownTotal), currencyCode, 6) }}</td>
                  <td colspan="2" class="py-2 text-xs text-muted-foreground">{{ $t('views.RunDetailView.breakdown_sum_not_total') }}</td>
                </tr>
              </tfoot>
            </table>
            <p class="mt-2 text-xs text-muted-foreground">
              {{ $t('views.RunDetailView.amounts_below_micro_note') }}
            </p>
          </div>
          <p v-else class="mt-4 text-sm text-muted-foreground" data-testid="run-detail-no-attributable-costs">
            {{ $t('views.RunDetailView.no_attributable_costs_this_run') }}
          </p>
          <p class="mt-2 text-xs text-muted-foreground" data-testid="run-detail-cost-transition-note">
            {{ $t('views.RunDetailView.cost_accounting_migrated') }}
          </p>
        </template>
      </section>

      <!-- Prompt Reveal Dialog -->
      <Dialog v-if="selectedPrompt" :visible="!!selectedPrompt" :modal="true" :dismissable-mask="true" :style="{ width: '48rem' }" @update:visible="closePromptDialog">
        <template #header>
          <div>
            <div class="text-lg font-semibold">
              Prompt — {{ selectedPrompt.nodeName }}
              <span v-if="selectedPrompt.tokenCount != null" class="ml-2 text-sm font-normal text-muted-foreground">
                ~{{ selectedPrompt.tokenCount.toLocaleString() }} tokens
              </span>
            </div>
          </div>
        </template>
        <div class="max-h-[60vh] overflow-auto rounded-lg border bg-muted p-4">
          <pre class="whitespace-pre-wrap text-xs leading-relaxed"><code>{{ selectedPrompt.prompt }}</code></pre>
        </div>
        <template #footer>
          <div class="flex justify-end">
            <Button data-testid="run-detail-copy-prompt" @click="copyPromptText">
              {{ promptCopied ? $t('views.RunDetailView.copied') : $t('views.RunDetailView.copy_prompt') }}
            </Button>
          </div>
        </template>
      </Dialog>

      <!-- Guardrail Override Dialog -->
      <Dialog :visible="overrideDialogOpen" :modal="true" :dismissable-mask="true" :style="{ width: '42rem' }" @update:visible="overrideDialogOpen = false">
        <template #header>
          <div>
            <div class="text-lg font-semibold">{{ $t('views.RunDetailGuardrailSummary.override_guardrail') }}</div>
            <div class="mt-0.5 text-sm text-muted-foreground">
              {{ $t('views.RunDetailGuardrailSummary.override_guardrail_description') }}
            </div>
          </div>
        </template>
        <div class="space-y-4">
          <label for="run-detail-override-input" class="mb-1 block text-sm font-medium">
            {{ $t('views.RunDetailGuardrailSummary.input_payload') }}
          </label>
          <textarea
            id="run-detail-override-input"
            v-model="overrideInput"
            rows="10"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 font-mono text-xs ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            data-testid="run-detail-override-input"
            :aria-label="$t('views.RunDetailGuardrailSummary.input_payload')"
          />
          <p class="text-xs text-muted-foreground">{{ $t('views.RunDetailGuardrailSummary.override_disclosure') }}</p>
          <output
            v-if="overrideMessage"
            :data-testid="overrideMessage.type === 'error' ? 'run-detail-override-error' : 'run-detail-override-success'"
            :aria-label="overrideMessage.text"
            class="block text-sm font-medium"
            :class="overrideMessage.type === 'error' ? 'text-destructive' : 'text-success'"
          >
            {{ overrideMessage.text }}
          </output>
        </div>
        <template #footer>
          <div class="flex justify-end">
            <Button data-testid="run-detail-override-submit" :disabled="overrideSubmitting" @click="submitOverride">
              {{ overrideSubmitting ? '...' : $t('views.RunDetailGuardrailSummary.override_guardrail') }}
            </Button>
          </div>
        </template>
      </Dialog>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onUnmounted, ref } from 'vue'
import type { Ref } from 'vue'
import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { api, getAccessToken } from '../lib/api/client'
import type { components } from '../lib/api/client'
import { decodeJwtPayload } from '../lib/jwt'
import { useApi } from '../composables/useApi'
import PageHeader from '../components/shared/PageHeader.vue'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import RunErrorTag from '../components/shared/RunErrorTag.vue'
import JsonViewer from '../components/shared/JsonViewer.vue'
import Dialog from 'primevue/dialog'
import Button from 'primevue/button'
import { formatApiError } from '../lib/api/formatError'
import { requestRunCancellation } from '../lib/api/runs'
import { isTerminalStatus } from '../constants/runStatuses'
import { triggerTypeLabel, heartbeatAgeSeconds, isHeartbeatStale, formatHeartbeatAge } from '../utils/runUtils'
import { shortId, formatRun } from '../utils/format'
import { formatMoney } from '../lib/money'
import { useOrgCurrency } from '../composables/useOrgCurrency'
import { Check, X } from '@lucide/vue'

type RunResponse = components['schemas']['RunResponse'] & {
  created_at?: string | null
  started_at?: string | null
  completed_at?: string | null
  child_runs_cost_usd?: string | null
  child_runs_count?: number
  aggregate_cost_usd?: string | null
  trigger_actor?: string | null
  trigger_type?: string | null
  trigger_id?: string | null
  heartbeat_at?: string | null
  work_item_refs?: WorkItemRef[] | null
  child_runs?: ChildRunRef[] | null
  capacity?: RunCapacity | null
}
type RunIOResponse = components['schemas']['RunIOResponse']

interface NodeTokenUsage {
  input_tokens?: number
  output_tokens?: number
  total_tokens?: number
  cost_usd?: number
  model_cost_display_usd?: number
}

interface WorkItemRef {
  kind?: string
  ref?: string
  source?: string
  status?: string | null
}

interface ChildRunRef {
  run_id: string
  run_number?: number | null
  status?: string
  pipeline_name?: string | null
}

interface RunCapacity {
  active_runs: number
  concurrency_limit: number | null
  waiting: boolean
}

interface CostBreakdownEntry {
  component?: string
  display_name?: string
  source?: string
  amount_usd?: string | number
  formula_applied?: string | null
  rate_usd?: string | number | null
  basis?: Record<string, unknown>
  missing_self_report?: boolean
  error?: string
  total_clamped?: boolean
}

interface NodeEntry {
  name: string
  status: string
  duration: string
  inputTokens: number | null
  outputTokens: number | null
  cost: number | null
  traceId: string | null
  isNodeSpanId: boolean
  io: { input: unknown; output: unknown } | null
  telemetry: Record<string, unknown> | null
  hasLogs: boolean
  stallReason: string | null
}

interface WorkspaceLeaseInfo {
  status: string
  sandbox_id?: string
  duration_seconds?: number
  error_message?: string
}

interface RunChunkEvent {
  seq: number
  event_type: string
  payload?: { node_id?: string; chunk?: string }
  ts?: string
}

const route = useRoute()
const { t, locale } = useI18n()
const { currencyCode, loadCurrency } = useOrgCurrency()
const run = ref<RunResponse | null>(null)
const runIO = ref<RunIOResponse | null>(null)
const expandedNodes = ref(new Set<string>())
const expandedLogs = ref(new Set<string>())
const copied = ref(false)
const shareCopied = ref(false)
const promptCopied = ref(false)
const outputCopied = ref(false)
const inputPayloadCopied = ref(false)
const pollInterval = ref<ReturnType<typeof setInterval> | null>(null)
const promptLoading = ref(new Set<string>())
const revealedPrompts = ref<Record<string, null | { prompt: string; messages: { role: string; content: string }[]; tokenCount: number; promptAlwaysVisible: boolean }>>({})
const selectedPrompt = ref<{ nodeName: string; prompt: string; tokenCount: number | null } | null>(null)
const workspaceLease = ref<WorkspaceLeaseInfo | null>(null)
const cancelling = ref(false)
const cancelError = ref<string | null>(null)
const pendingGates = ref<components['schemas']['GateResponse'][]>([])
const hitlLoading = ref(false)
const claimToken = ref<string | null>(null)
const claimLoading = ref(false)
const actioning = ref<string | null>(null)
const hitlNotes = ref('')
const hitlMessage = ref<{ type: string; text: string } | null>(null)
const liveOutput = ref<Record<string, string>>({})
const liveOutputSeq = ref(0)
const liveNodeStates = ref<Record<string, 'running' | 'completed' | 'failed'>>({})
const heartbeatNow = ref(Date.now())
const overrideDialogOpen = ref(false)
const overrideInput = ref('')
const overrideSubmitting = ref(false)
const overrideMessage = ref<{ type: string; text: string } | null>(null)

// agent_stdout strings can reach ~512KB; cap the Logs pre for display.
// (FAR-123 delivers the full truncation UX later.)
const MAX_LOG_CHARS = 20000

interface JwtPayload {
  org_role?: string
}

function readJwtPayload(): JwtPayload | null {
  return decodeJwtPayload(getAccessToken()) as JwtPayload | null
}

const isOrgOperator = computed(() => {
  const role = readJwtPayload()?.org_role
  return role === 'operator' || role === 'admin'
})

const isGuardrailBlocked = computed(() =>
  run.value?.status === 'eval_failed' && run.value?.error_code === 'eval_blocked',
)

const guardrailSummary = computed<Record<string, number>>(() => {
  const s = run.value?.guardrail_summary
  return s && typeof s === 'object' ? (s as Record<string, number>) : {}
})

const GUARDRAIL_BUCKET_KEYS = ['evaluated', 'passed', 'violated', 'observed', 'errored', 'redacted', 'skipped'] as const

const guardrailBuckets = computed(() => {
  const summary = guardrailSummary.value
  if (Object.keys(summary).length === 0) return []
  return GUARDRAIL_BUCKET_KEYS
    .filter(key => typeof summary[key] === 'number' && summary[key] > 0)
    .map(key => ({
      key,
      value: summary[key] as number,
      label: t(`views.RunDetailGuardrailSummary.${key}`),
    }))
})

function bucketClass(key: string): string {
  const classes: Record<string, string> = {
    evaluated: 'bg-muted text-muted-foreground',
    passed: 'bg-success/10 text-success',
    violated: 'bg-destructive/10 text-destructive',
    observed: 'bg-warning/10 text-warning',
    errored: 'bg-destructive/10 text-destructive',
    redacted: 'bg-purple-500/10 text-purple-600',
    skipped: 'bg-muted text-muted-foreground',
  }
  return classes[key] || 'bg-muted text-muted-foreground'
}

function openOverrideDialog() {
  overrideInput.value = ''
  overrideMessage.value = null
  overrideDialogOpen.value = true
}

async function submitOverride() {
  const runId = route.params.id as string
  if (!runId || overrideSubmitting.value) return
  let inputData: unknown
  try {
    inputData = JSON.parse(overrideInput.value || '{}')
  } catch {
    overrideMessage.value = {
      type: 'error',
      text: t('views.RunDetailGuardrailSummary.override_invalid_json'),
    }
    return
  }
  overrideSubmitting.value = true
  overrideMessage.value = null
  try {
    const { data, error: err } = await api.POST('/api/v1/runs/{run_id}/guardrail-override', {
      params: { path: { run_id: runId } },
      body: { input_data: inputData } as any,
    })
    if (err) {
      const status = (err as Record<string, unknown>)?.status
      // 422 = still-violating supplied input — re-block safe.
      overrideMessage.value = {
        type: 'error',
        text: status === 422
          ? t('views.RunDetailGuardrailSummary.override_reblocked')
          : `${t('views.RunDetailGuardrailSummary.override_failed')} ${formatApiError(err)}`,
      }
      return
    }
    if (data) {
      if (run.value) run.value.status = (data as { status?: string }).status ?? 'pending'
      overrideMessage.value = {
        type: 'success',
        text: t('views.RunDetailGuardrailSummary.override_success'),
      }
      setTimeout(() => {
        overrideDialogOpen.value = false
        overrideMessage.value = null
      }, 1500)
    }
  } catch (e: unknown) {
    overrideMessage.value = {
      type: 'error',
      text: `${t('views.RunDetailGuardrailSummary.override_failed')} ${formatApiError(e)}`,
    }
  } finally {
    overrideSubmitting.value = false
  }
}

const shareSummary = computed(() => {
  const r = run.value
  if (!r) return ''
  const completed = nodeEntries.value.filter(n => n.status === 'complete').length
  const total = nodeEntries.value.length
  const tokens = totalTokens.value?.toLocaleString() ?? '—'
  const cost = r.total_cost_usd != null ? formatMoney(Number(r.total_cost_usd), currencyCode.value, 6) : '—'
  const runNumber = r.run_number != null ? `#${r.run_number}` : shortId(r.run_id)
  return [
    `Run: ${runNumber}`,
    `Pipeline: ${r.pipeline_name || shortId(r.pipeline_id)}`,
    `Status: ${r.status}`,
    `Nodes: ${completed}/${total}`,
    `Tokens: ${tokens}`,
    `Cost: ${cost}`,
    `Duration: —`,
  ].join('\n')
})

async function copyShareSummary() {
  const text = shareSummary.value
  if (!text) return
  try {
    await navigator.clipboard.writeText(text)
    shareCopied.value = true
    setTimeout(() => { shareCopied.value = false }, 2000)
  } catch (e) {
    console.warn('Failed to copy share summary', e)
  }
}

function toggleNodeIO(name: string) {
  const s = expandedNodes.value
  if (s.has(name)) s.delete(name)
  else s.add(name)
}

function toggleNodeLogs(name: string) {
  const s = expandedLogs.value
  if (s.has(name)) s.delete(name)
  else s.add(name)
}

async function copyTraceId() {
  if (!run.value?.trace_id) return
  await copyText(run.value.trace_id)
}

async function copyRunId() {
  if (!run.value?.run_id) return
  await copyText(run.value.run_id)
}

async function copyText(text: string, flag: Ref<boolean> = copied) {
  try {
    await navigator.clipboard.writeText(text)
    flag.value = true
    setTimeout(() => { flag.value = false }, 2000)
  } catch (e) {
    console.warn('Failed to copy text', e)
  }
}

function nodeLabel(nodeId: string): string {
  const labels = runIO.value?.node_labels as Record<string, string> | undefined
  return labels?.[nodeId] || shortId(nodeId)
}

const GITHUB_KINDS = ['github', 'github_pr', 'github_issue'] as const

function isGithubWorkItem(item: WorkItemRef): boolean {
  return GITHUB_KINDS.includes((item.kind || '').toLowerCase() as (typeof GITHUB_KINDS)[number])
}

function githubKindLabel(item: WorkItemRef): string {
  const kind = (item.kind || '').toLowerCase()
  const key =
    kind === 'github' || kind === 'github_pr' || kind === 'github_issue'
      ? `views.RunDetailView.work_item_kind_${kind}`
      : 'views.RunDetailView.work_item_kind_github_default'
  return t(key)
}

function githubRefId(item: WorkItemRef): string {
  return (item.ref || '').trim().replace(/^[^/\s]+\/[^/\s]+#/, '')
}

function getPrUrl(item: WorkItemRef): string | null {
  const kind = (item.kind || '').toLowerCase()
  const ref = (item.ref || '').trim()
  const hashIndex = ref.indexOf('#')
  if (hashIndex <= 0) return null
  const slashIndex = ref.indexOf('/')
  if (slashIndex <= 0 || slashIndex >= hashIndex) return null
  const owner = ref.slice(0, slashIndex)
  const repo = ref.slice(slashIndex + 1, hashIndex)
  const id = ref.slice(hashIndex + 1)
  if (!owner || !repo || !id || /\s/.test(owner) || /\s/.test(repo) || repo.includes('/')) return null
  if (kind === 'github_pr') return `https://github.com/${owner}/${repo}/pull/${id}`
  if (kind === 'github_issue') return `https://github.com/${owner}/${repo}/issues/${id}`
  return `https://github.com/${owner}/${repo}`
}

async function revealPrompt(nodeName: string) {
  const cached = revealedPrompts.value[nodeName]
  if (cached?.prompt) {
    showPrompt(nodeName)
    return
  }
  if (promptLoading.value.has(nodeName)) return
  const runId = route.params.id as string
  if (!runId) return

  promptLoading.value = new Set([...promptLoading.value, nodeName])
  try {
    const { data, error: err } = await api.POST(
      '/api/v1/runs/{run_id}/nodes/{node_id}/prompt/reveal',
      {
        params: { path: { run_id: runId, node_id: nodeName } },
      },
    )
    if (err || !data) {
      if (typeof err === 'object' && err !== null && 'name' in err && (err as Record<string, unknown>).name === 'AbortError') throw err
      revealedPrompts.value = { ...revealedPrompts.value, [nodeName]: null }
      const detail = (err as Record<string, unknown>)?.detail
      error.value = `${t('views.RunDetailView.prompt_reveal_error')} ${detail ? String(detail) : ''}`
      return
    }
    const d = data as components['schemas']['PromptRevealResponse']
    const revealed = {
      prompt: d.prompt,
      messages: d.messages.map(message => ({
        role: message.role ?? '',
        content: message.content ?? '',
      })),
      tokenCount: d.token_count,
      promptAlwaysVisible: d.prompt_always_visible,
    }
    revealedPrompts.value = { ...revealedPrompts.value, [nodeName]: revealed }
    showPrompt(nodeName)
  } finally {
    const s = new Set(promptLoading.value)
    s.delete(nodeName)
    promptLoading.value = s
  }
}

function showPrompt(nodeName: string) {
  const entry = revealedPrompts.value[nodeName]
  if (!entry) return
  selectedPrompt.value = {
    nodeName,
    prompt: entry.prompt,
    tokenCount: entry.tokenCount,
  }
}

function closePromptDialog() {
  selectedPrompt.value = null
}

async function copyPromptText() {
  if (!selectedPrompt.value?.prompt) return
  try {
    await navigator.clipboard.writeText(selectedPrompt.value.prompt)
    promptCopied.value = true
    setTimeout(() => { promptCopied.value = false }, 2000)
  } catch (e) {
    console.warn('Failed to copy prompt text', e)
  }
}

function getNodeLog(nodeName: string, field: string): string | null {
  const nodeTelemetry = nodeTelemetryFor(nodeName)
  if (!nodeTelemetry) return null
  const val = nodeTelemetry[field]
  if (typeof val !== 'string' || val.length === 0) return null
  return val.length > MAX_LOG_CHARS ? val.slice(0, MAX_LOG_CHARS) : val
}

function isNodeLogTruncated(nodeName: string, field: string): boolean {
  const nodeTelemetry = nodeTelemetryFor(nodeName)
  const val = nodeTelemetry?.[field]
  return typeof val === 'string' && val.length > MAX_LOG_CHARS
}

function nodeTelemetryFor(nodeName: string): Record<string, unknown> | null {
  const telemetry = runIO.value?.node_telemetry as Record<string, unknown> | null ?? {}
  const entry = telemetry[nodeName]
  return entry && typeof entry === 'object' ? (entry as Record<string, unknown>) : null
}

function nodeSummary(node: NodeEntry): string | null {
  const telemetryStatus = node.telemetry?.status
  const output = node.io?.output as Record<string, unknown> | null | undefined
  if (output && typeof output === 'object' && output !== null) {
    const returnSummary = output.summary
    if (typeof returnSummary === 'string' && returnSummary.length > 0 && telemetryStatus !== 'failed') {
      return returnSummary
    }
  }
  const telemetrySummary = node.telemetry?.summary
  return typeof telemetrySummary === 'string' && telemetrySummary.length > 0 ? telemetrySummary : null
}

function statusBadgeClassFor(status: string | undefined): string {
  const map: Record<string, string> = {
    running: 'badge badge-status-primary',
    complete: 'badge badge-status-success',
    failed: 'badge badge-status-destructive',
    stalled: 'badge badge-status-destructive',
    cancelled: 'badge badge-status-warning',
    pending: 'badge badge-status-muted',
    awaiting_human: 'badge badge-status-pending',
  }
  return map[status ?? ''] ?? 'badge badge-context-slate'
}

const statusBadgeClass = computed(() => statusBadgeClassFor(run.value?.status))

const isTerminal = computed(() => run.value != null && isTerminalStatus(run.value.status))

const canCancel = computed(() => run.value != null && !isTerminalStatus(run.value.status))

function nodeStatusBadgeClass(node: NodeEntry): string {
  return statusBadgeClassFor(node.status)
}

const runTimestamps = computed(() => {
  const r = run.value
  if (!r) return null
  return {
    created: r.created_at ? formatTimestamp(r.created_at) : '—',
    started: r.started_at ? formatTimestamp(r.started_at) : '—',
    completed: r.completed_at ? formatTimestamp(r.completed_at) : '—',
  }
})

function formatTimestamp(dateStr: string): string {
  const d = new Date(dateStr)
  if (Number.isNaN(d.getTime())) return '—'
  return d.toLocaleString(locale.value, {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
  })
}

const totalTokens = computed(() => {
  if (!run.value?.node_token_usage) return null
  const ntu = run.value.node_token_usage as Record<string, NodeTokenUsage>
  return Object.values(ntu).reduce((sum, n) => sum + (n.total_tokens ?? 0), 0)
})

const formattedCost = computed(() => {
  const c = run.value?.total_cost_usd
  if (c == null) return '0.00'
  return Number(c).toFixed(6)
})

const childRunCost = computed(() => {
  const c = run.value?.child_runs_cost_usd
  if (c == null || c === '') return 0
  const n = Number(c)
  return Number.isFinite(n) ? n : 0
})

const aggregateCost = computed(() => {
  const a = run.value?.aggregate_cost_usd
  if (a == null || a === '') return null
  const n = Number(a)
  return Number.isFinite(n) ? n : null
})

const childRunCount = computed(() => {
  const c = run.value?.child_runs_count
  return Number.isInteger(c) && (c ?? 0) > 0 ? (c as number) : 0
})

const breakdownRaw = computed<CostBreakdownEntry[]>(() => {
  const raw = run.value?.cost_breakdown
  return Array.isArray(raw) ? (raw as CostBreakdownEntry[]) : []
})

const breakdownPresent = computed(() => breakdownRaw.value.length > 0)

const breakdownTotalClamped = computed(() => breakdownRaw.value.some((e) => e.total_clamped === true))

function parseBreakdownAmount(value: string | number | undefined): number {
  if (value == null || value === '') return 0
  const n = Number(value)
  return Number.isFinite(n) ? n : 0
}

function formatBreakdownAmount(value: string | number | undefined): string {
  return parseBreakdownAmount(value).toFixed(6)
}

function breakdownBasisLine(entry: CostBreakdownEntry): string {
  const basis = entry.basis
  if (!basis || typeof basis !== 'object') return '—'
  const parts = Object.entries(basis)
    .filter(([k]) => k !== 'raw_reported' && k !== 'per_node_raw')
    .slice(0, 6)
    .map(([k, v]) => `${k}=${typeof v === 'object' ? JSON.stringify(v) : String(v)}`)
  return parts.length > 0 ? parts.join(', ') : '—'
}

const breakdownEntries = computed(() =>
  breakdownRaw.value
    .filter((e) => e.total_clamped !== true)
    .filter((e) => {
      const amount = parseBreakdownAmount(e.amount_usd)
      // Zero-amount rows are omitted — except self-report / eval-error rows
      // that carry a chip or badge (a dead report_key must stay visible).
      if (amount !== 0) return true
      return Boolean(e.error) || e.missing_self_report === true
    })
    .map((e) => ({
      ...e,
      amountUsd: formatBreakdownAmount(e.amount_usd),
      basisLine: breakdownBasisLine(e),
    })),
)

const breakdownTotal = computed(() =>
  breakdownEntries.value.reduce((sum, e) => sum + parseBreakdownAmount(e.amount_usd), 0).toFixed(6),
)

const lastNodeOutput = computed(() => {
  const outputs = runIO.value?.outputs_json as Record<string, unknown> | null | undefined
  if (!outputs) return null
  const keys = Object.keys(outputs)
  for (let i = keys.length - 1; i >= 0; i--) {
    const value = outputs[keys[i]]
    if (value != null) return value
  }
  return null
})

const formattedOutput = computed(() => {
  const output = lastNodeOutput.value
  if (output == null) return ''
  if (typeof output === 'string') return output
  return JSON.stringify(output, null, 2)
})

const workspaceStatusClass = computed(() => {
  const s = workspaceLease.value?.status ?? ''
  const map: Record<string, string> = {
    running: 'bg-primary/10 text-primary',
    pending: 'bg-warning/10 text-warning',
    completed: 'bg-success/10 text-success',
    failed: 'bg-destructive/10 text-destructive',
    expired: 'bg-muted text-muted-foreground',
  }
  return map[s] ?? 'bg-muted text-muted-foreground'
})

const workspaceDotClass = computed(() => {
  const s = workspaceLease.value?.status ?? ''
  const map: Record<string, string> = {
    running: 'bg-primary',
    pending: 'bg-warning',
    completed: 'bg-success',
    failed: 'bg-destructive',
    expired: 'bg-muted-foreground',
  }
  return map[s] ?? 'bg-muted-foreground'
})

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${seconds}s`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  return `${h}h ${m}m`
}

function formatMs(ms: number): string {
  if (!Number.isFinite(ms)) return '—'
  return formatDuration(ms / 1000)
}

async function copyOutput() {
  const text = formattedOutput.value
  if (!text) return
  try {
    await navigator.clipboard.writeText(text)
    outputCopied.value = true
    setTimeout(() => { outputCopied.value = false }, 2000)
  } catch (e) {
    console.warn('Failed to copy output', e)
  }
}

async function copyInputPayload() {
  const payload = runIO.value?.input_payload
  if (!payload) return
  await copyText(JSON.stringify(payload, null, 2), inputPayloadCopied)
}

async function claimGate(gate: components['schemas']['GateResponse']) {
  claimLoading.value = true
  hitlMessage.value = null
  try {
    const { data, error: err } = await api.POST('/api/v1/runs/{run_id}/hitl/{gate_id}/claim', {
      params: { path: { run_id: gate.run_id, gate_id: gate.gate_id } },
      body: { expiry_minutes: 15 },
    })
    if (err) {
      hitlMessage.value = { type: 'error', text: `Claim failed: ${formatApiError(err)}` }
    } else if (data) {
      const d = data as components['schemas']['ClaimResponse']
      claimToken.value = d.claim_token
      hitlMessage.value = { type: 'success', text: 'Gate claimed. You can now approve or reject.' }
      setTimeout(() => { hitlMessage.value = null }, 5000)
    }
  } catch (e: unknown) {
    hitlMessage.value = { type: 'error', text: `Claim failed: ${formatApiError(e)}` }
  } finally {
    claimLoading.value = false
  }
}

async function cancelRun() {
  const runId = route.params.id as string
  if (!runId) return
  cancelling.value = true
  cancelError.value = null
  try {
    const { error } = await requestRunCancellation(runId, t('views.RunDetailView.cancel_failed'))
    if (error) {
      cancelError.value = error
    } else {
      if (run.value) run.value.status = 'cancelled'
      if (pollInterval.value) {
        clearInterval(pollInterval.value)
        pollInterval.value = null
      }
    }
  } finally {
    cancelling.value = false
  }
}

async function approveGate() {
  if (!claimToken.value || pendingGates.value.length === 0) return
  const gate = pendingGates.value[0]
  actioning.value = 'approve'
  hitlMessage.value = null
  try {
    const { error: err } = await api.POST('/api/v1/runs/{run_id}/hitl/{gate_id}/approve', {
      params: { path: { run_id: gate.run_id, gate_id: gate.gate_id } },
      body: { claim_token: claimToken.value, notes: hitlNotes.value || null },
    })
    if (err) {
      hitlMessage.value = {
        type: 'error',
        text: `Approve failed: ${formatApiError(err)}`,
      }
    } else {
      pendingGates.value = []
      claimToken.value = null
      hitlNotes.value = ''
      if (run.value) run.value.status = 'running'
      hitlMessage.value = { type: 'success', text: 'Gate approved. Pipeline resuming.' }
      setTimeout(() => { hitlMessage.value = null }, 5000)
    }
  } catch (e: unknown) {
    hitlMessage.value = { type: 'error', text: `Approve failed: ${formatApiError(e)}` }
  } finally {
    actioning.value = null
  }
}

async function rejectGate() {
  if (!claimToken.value || pendingGates.value.length === 0) return
  const gate = pendingGates.value[0]
  actioning.value = 'reject'
  hitlMessage.value = null
  try {
    const { error: err } = await api.POST('/api/v1/runs/{run_id}/hitl/{gate_id}/reject', {
      params: { path: { run_id: gate.run_id, gate_id: gate.gate_id } },
      body: { claim_token: claimToken.value, reason: hitlNotes.value || 'Rejected by reviewer' },
    })
    if (err) {
      hitlMessage.value = {
        type: 'error',
        text: `Reject failed: ${formatApiError(err)}`,
      }
    } else {
      pendingGates.value = []
      claimToken.value = null
      hitlNotes.value = ''
      if (run.value) run.value.status = 'running'
      hitlMessage.value = { type: 'success', text: 'Gate rejected. Pipeline routed to reject target.' }
      setTimeout(() => { hitlMessage.value = null }, 5000)
    }
  } catch (e: unknown) {
    hitlMessage.value = { type: 'error', text: `Reject failed: ${formatApiError(e)}` }
  } finally {
    actioning.value = null
  }
}

function resolveNodeIO(nodeOutput: Record<string, unknown> | undefined): { input: unknown; output: unknown } | null {
  if (nodeOutput == null) return null
  if (!Array.isArray(nodeOutput) && typeof nodeOutput === 'object') {
    if ('artifacts' in nodeOutput) {
      // Legacy mixed envelope — the meaningful output is under `output`.
      return {
        input: nodeOutput.input ?? runIO.value?.input_payload ?? null,
        output: nodeOutput.output ?? null,
      }
    }
    // P1+ pure return — honour an explicit `input` key, otherwise fall back to
    // the run-level input payload; the value itself is the output when there is
    // no `output` wrapper. An empty object carries no data — treat as absent.
    if (Object.keys(nodeOutput).length === 0) {
      return {
        input: runIO.value?.input_payload ?? null,
        output: null,
      }
    }
    return {
      input: nodeOutput.input ?? runIO.value?.input_payload ?? null,
      output: nodeOutput.output !== undefined ? nodeOutput.output : nodeOutput,
    }
  }
  // Pure scalar/array return — the value itself is the output.
  return {
    input: runIO.value?.input_payload ?? null,
    output: nodeOutput,
  }
}

const nodeEntries = computed<NodeEntry[]>(() => {
  const r = run.value
  if (!r) return []

  const ntu = r.node_token_usage as Record<string, NodeTokenUsage> | null ?? {}
  const outputs = runIO.value?.outputs_json as Record<string, unknown> | null ?? {}
  const telemetry = runIO.value?.node_telemetry as Record<string, unknown> | null ?? {}

  const names = new Set([...Object.keys(ntu), ...Object.keys(outputs), ...Object.keys(telemetry)])
  if (names.size === 0) return []

  return Array.from(names).map(name => {
    const usage = ntu[name] as NodeTokenUsage | undefined
    const nodeOutput = outputs[name] as Record<string, unknown> | undefined
    const nodeTelemetry = (telemetry[name] as Record<string, unknown> | undefined) ?? null
    const stallReason = nodeTelemetry && typeof nodeTelemetry.stall_reason === 'string' && nodeTelemetry.stall_reason.length > 0
      ? nodeTelemetry.stall_reason
      : null
    const hasLogs = !!(
      nodeTelemetry &&
      ((typeof nodeTelemetry.agent_stdout === 'string' && nodeTelemetry.agent_stdout.length > 0)
        || (typeof nodeTelemetry.agent_stderr === 'string' && nodeTelemetry.agent_stderr.length > 0))
    )
    // FAR-198: prefer the node's REAL span id (stamped into node telemetry at
    // execution time); fall back to the run trace id so the column never
    // shows a duplicate run value when no per-node span is available.
    const nodeSpanId = nodeTelemetry && typeof nodeTelemetry.otel_span_id === 'string' && nodeTelemetry.otel_span_id.length > 0
      ? nodeTelemetry.otel_span_id
      : null
    const nodeTraceId = nodeTelemetry && typeof nodeTelemetry.otel_trace_id === 'string' && nodeTelemetry.otel_trace_id.length > 0
      ? nodeTelemetry.otel_trace_id
      : null

    return {
      name,
      status: run.value?.status ?? 'unknown',
      duration: '—',
      inputTokens: usage?.input_tokens ?? null,
      outputTokens: usage?.output_tokens ?? null,
      cost: usage?.model_cost_display_usd ?? usage?.cost_usd ?? null,
      traceId: nodeSpanId ?? nodeTraceId ?? run.value?.trace_id ?? null,
      isNodeSpanId: nodeSpanId !== null,
      io: resolveNodeIO(nodeOutput),
      telemetry: nodeTelemetry,
      hasLogs,
      stallReason,
    }
  })
})

type NodeProgressState = 'completed' | 'running' | 'failed' | 'pending'

function nodeHasUsageOrOutput(name: string): boolean {
  const ntu = run.value?.node_token_usage as Record<string, NodeTokenUsage> | null ?? {}
  const outputs = runIO.value?.outputs_json as Record<string, unknown> | null ?? {}
  return Boolean(ntu[name]) || Boolean(outputs[name])
}

function nodeProgressState(name: string): NodeProgressState {
  const live = liveNodeStates.value[name]
  if (live === 'running') return 'running'
  if (live === 'failed') return 'failed'
  if (live === 'completed') return 'completed'
  if (nodeHasUsageOrOutput(name)) return 'completed'
  return 'pending'
}

const nodeProgressChips = computed(() => {
  const names: string[] = []
  const seen = new Set<string>()
  for (const entry of nodeEntries.value) {
    if (!seen.has(entry.name)) {
      seen.add(entry.name)
      names.push(entry.name)
    }
  }
  for (const name of Object.keys(liveNodeStates.value)) {
    if (!seen.has(name)) {
      seen.add(name)
      names.push(name)
    }
  }
  return names.map(name => ({ name, state: nodeProgressState(name) }))
})

function nodeStateLabel(state: NodeProgressState): string {
  return t(`views.RunDetailView.node_state_${state}`)
}

function chipClass(state: NodeProgressState): string {
  const map: Record<NodeProgressState, string> = {
    completed: 'bg-success/10 text-success border border-success/30',
    running: 'bg-warning/10 text-warning border border-warning/30',
    failed: 'bg-destructive/10 text-destructive border border-destructive/30',
    pending: 'bg-muted text-muted-foreground border border-border',
  }
  return map[state]
}

const liveTokenTotal = computed(() => {
  const ntu = run.value?.node_token_usage as Record<string, NodeTokenUsage> | null ?? {}
  return Object.values(ntu).reduce((sum, n) => {
    if (typeof n?.total_tokens === 'number') return sum + n.total_tokens
    return sum + (n?.input_tokens ?? 0) + (n?.output_tokens ?? 0)
  }, 0)
})

const liveCostTotal = computed(() => {
  const ntu = run.value?.node_token_usage as Record<string, NodeTokenUsage> | null ?? {}
  return Object.values(ntu).reduce((sum, n) => sum + (n?.cost_usd ?? n?.model_cost_display_usd ?? 0), 0)
})

const liveCostPresent = computed(() => {
  const r = run.value
  if (!r) return false
  if (isTerminalStatus(r.status)) return false
  return liveTokenTotal.value > 0 || liveCostTotal.value > 0
})

function formatTokenCount(count: number): string {
  if (count >= 1_000_000) return `${(Math.round((count / 1_000_000) * 10) / 10)}M`
  if (count >= 1_000) return `${(Math.round((count / 1_000) * 10) / 10)}k`
  return String(count)
}

const heartbeatAge = computed<number | null>(() => {
  const r = run.value
  if (!r) return null
  return heartbeatAgeSeconds(r.heartbeat_at, r.status, heartbeatNow.value)
})

function childRunBadgeClass(status: string | undefined): string {
  return statusBadgeClassFor(status)
}

async function fetchHitlGates(runId: string) {
  if (hitlLoading.value) return
  hitlLoading.value = true
  try {
    const { data } = await api.GET('/api/v1/runs/{run_id}/hitl/pending', {
      params: { path: { run_id: runId } },
    })
    if (data) {
      pendingGates.value = ((data as any).gates || []) as components['schemas']['GateResponse'][]
    }
  } catch (e: unknown) {
    console.warn('Failed to load pending HITL gates', e)
  } finally {
    hitlLoading.value = false
  }
}

async function fetchRunData(runId: string) {
  try {
    const { data: runData } = await api.GET('/api/v1/runs/{run_id}', {
      params: { path: { run_id: runId } },
    })
    if (runData) {
      run.value = runData as unknown as RunResponse
      if (run.value.status === 'awaiting_human') {
        fetchHitlGates(runId)
      }
    }
    const { data: ioData } = await api.GET('/api/v1/runs/{run_id}/io', {
      params: { path: { run_id: runId } },
    })
    if (ioData) runIO.value = ioData as unknown as RunIOResponse
  } catch (e) {
    console.warn('Failed to fetch run data', e)
  }
}

async function fetchLiveOutput(runId: string) {
  if (run.value && isTerminalStatus(run.value.status)) return
  try {
    const data = await useApi().get<{ events?: RunChunkEvent[] }>(
      `/api/v1/runs/${runId}/events?since_seq=${liveOutputSeq.value}`,
    )
    const events = data?.events
    if (!events || events.length === 0) return
    const next = { ...liveOutput.value }
    const nextStates = { ...liveNodeStates.value }
    let maxSeq = liveOutputSeq.value
    for (const evt of events) {
      if (!applyLiveEvent(evt, next, nextStates)) continue
      if (evt.seq > maxSeq) maxSeq = evt.seq
    }
    liveOutput.value = next
    liveNodeStates.value = nextStates
    liveOutputSeq.value = maxSeq
  } catch (e) {
    // Live output is best-effort — polling must never break the page.
    console.warn('Failed to fetch live run output', e)
  }
}

function applyLiveEvent(
  evt: RunChunkEvent,
  liveOutputRef: Record<string, string>,
  liveNodeStatesRef: Record<string, 'running' | 'completed' | 'failed'>,
): boolean {
  if (!evt || typeof evt.seq !== 'number') return false
  const nodeId = evt.payload?.node_id
  if (!nodeId) return true
  if (evt.event_type === 'node.stdout_chunk' || evt.event_type === 'node.stderr_chunk') {
    liveOutputRef[nodeId] = (liveOutputRef[nodeId] ?? '') + (evt.payload?.chunk ?? '')
    return true
  }
  if (evt.event_type === 'node_started') {
    liveNodeStatesRef[nodeId] = 'running'
  } else if (evt.event_type === 'node_completed') {
    liveNodeStatesRef[nodeId] = 'completed'
  } else if (evt.event_type === 'node_failed') {
    liveNodeStatesRef[nodeId] = 'failed'
  }
  return true
}

function startPolling(runId: string) {
  heartbeatNow.value = Date.now()
  pollInterval.value = setInterval(async () => {
    heartbeatNow.value = Date.now()
    if (run.value && isTerminalStatus(run.value.status)) {
      clearInterval(pollInterval.value!)
      pollInterval.value = null
      return
    }
    await fetchRunData(runId)
    await fetchLiveOutput(runId)
  }, 3000)
}

import { useDataFetch } from '../composables/useDataFetch'

interface RunFetchResult {
  run: RunResponse | null
  io: RunIOResponse | null
  workspace: WorkspaceLeaseInfo | null
}

const { loading, error } = useDataFetch<RunFetchResult>(
  async () => {
    const runId = route.params.id as string
    if (!runId) {
      return { data: { run: null, io: null, workspace: null }, error: { detail: t('views.RunDetailView.no_run_id_provided') } }
    }

    try {
      const [runResp, ioResp, wsResp] = await Promise.all([
        api.GET('/api/v1/runs/{run_id}', { params: { path: { run_id: runId } } }).catch(() => ({ data: null })),
        api.GET('/api/v1/runs/{run_id}/io', { params: { path: { run_id: runId } } }).catch(() => ({ data: null })),
        api.GET('/api/v1/runs/{run_id}/workspace-lease', { params: { path: { run_id: runId } } }).catch(() => ({ data: null })),
      ])
      const runData = runResp?.data
      const ioData = ioResp?.data
      const wsData = wsResp?.data

      if (runData) {
        run.value = runData as unknown as RunResponse
        if (run.value.status === 'awaiting_human') {
          fetchHitlGates(runId)
        }
      }
      if (ioData) runIO.value = ioData as unknown as RunIOResponse
      if (wsData) workspaceLease.value = wsData as unknown as WorkspaceLeaseInfo

      if (run.value?.status === 'complete' && nodeEntries.value.length > 0) {
        const last = nodeEntries.value[nodeEntries.value.length - 1]
        expandedNodes.value.add(last.name)
      }
      startPolling(runId)

      return { data: { run: run.value, io: runIO.value, workspace: workspaceLease.value }, error: undefined }
    } catch (e: unknown) {
      return { data: undefined, error: { detail: `${t('views.RunDetailView.failed_to_load_run')} ${formatApiError(e)}` } }
    }
  },
  { immediate: true },
)

onUnmounted(() => {
  if (pollInterval.value) {
    clearInterval(pollInterval.value)
  }
})

loadCurrency()
</script>
