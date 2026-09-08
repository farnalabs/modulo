<template>
  <div class="space-y-3" data-testid="hitl-gate-card">
    <!-- Header: label + full-id copy + status badge + pipeline -->
    <div class="flex items-center gap-2 text-sm">
      <span :class="statusBadgeClass(status)">{{ status }}</span>
      <span class="font-medium">{{ $t('hitl.gate.gate_label') }}</span>
      <code
        v-tooltip.top="{ value: gate.gate_id, showDelay: 300 }"
        class="cursor-help select-all rounded bg-muted px-1.5 py-0.5 font-mono text-xs"
      >{{ gate.label || shortId(gate.gate_id) }}</code>
      <button
        type="button"
        data-testid="hitl-gate-copy-id"
        :aria-label="$t('hitl.gate.copy_gate_id')"
        class="inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-xs font-medium text-primary hover:bg-primary/10"
        @click="copyText(gate.gate_id)"
      >
        <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
      </button>
      <span v-if="pipelineName" class="ml-auto truncate text-xs text-muted-foreground">{{ pipelineName }}</span>
    </div>

    <!-- Run link -->
    <RouterLink
      v-if="showRunLink"
      :to="`/runs/${gate.run_id}`"
      class="text-xs text-primary hover:underline"
      data-testid="hitl-gate-run-link"
    >
      {{ $t('hitl.gate.run_label') }} <span class="font-mono">{{ shortId(gate.run_id) }}</span>
    </RouterLink>

    <!-- Claim metadata -->
    <div class="space-y-1 text-sm">
      <div class="flex justify-between">
        <span class="text-muted-foreground">{{ $t('hitl.gate.run_id') }}</span>
        <span class="font-mono text-xs">{{ shortId(gate.run_id) }}</span>
      </div>
      <div class="flex justify-between">
        <span class="text-muted-foreground">{{ $t('hitl.gate.pipeline_label') }}</span>
        <span>{{ pipelineName || $t('hitl.gate.pipeline_fallback', { id: shortId(gate.pipeline_id) }) }}</span>
      </div>
      <div v-if="gate.created_at" class="flex justify-between">
        <span class="text-muted-foreground">{{ $t('hitl.gate.created_label') }}</span>
        <span>{{ formatDate(gate.created_at) }}</span>
      </div>
      <div v-if="gate.claimed_at" class="flex justify-between">
        <span class="text-muted-foreground">{{ $t('hitl.gate.claimed_label') }}</span>
        <span>{{ formatDate(gate.claimed_at) }}</span>
      </div>
      <div v-if="gate.expires_at" class="flex justify-between">
        <span class="text-muted-foreground">{{ $t('hitl.gate.expires_label') }}</span>
        <span>{{ formatDate(gate.expires_at) }}</span>
      </div>
      <div v-if="gate.decision_at" class="flex justify-between">
        <span class="text-muted-foreground">{{ $t('hitl.gate.decided_label') }}</span>
        <span>{{ formatDate(gate.decision_at) }}</span>
      </div>
      <div v-if="gate.decision" class="flex justify-between">
        <span class="text-muted-foreground">{{ $t('hitl.gate.decision_label') }}</span>
        <span :class="gate.decision === 'approved' ? 'text-success' : 'text-destructive'">{{ gate.decision }}</span>
      </div>
      <div v-if="gate.claimed_by" class="flex justify-between">
        <span class="text-muted-foreground">{{ $t('hitl.gate.claimed_by_label') }}</span>
        <span>{{ claimedByDisplay }}</span>
      </div>
      <div v-if="gate.team_scope" class="flex justify-between">
        <span class="text-muted-foreground">{{ $t('hitl.gate.team_label') }}</span>
        <span>{{ gate.team_scope }}</span>
      </div>
    </div>

    <!-- FAR-613: the decision briefing — WHY the gate exists and WHAT the
         reviewer is looking at, always visible before the controls. -->
    <HitlBriefing :description="gate.description" :context="gate.context" class="mb-3" />

    <!-- Actions -->
    <div
      v-if="status === 'pending'"
      class="pt-2"
    >
      <Button :disabled="claiming" class="w-full" data-testid="hitl-gate-claim" @click="claimGate">
        {{ claiming ? $t('hitl.gate.claiming') : $t('hitl.gate.claim') }}
      </Button>
    </div>
    <div v-else-if="status === 'claimed' && claimToken" class="space-y-2 pt-2">
      <textarea
        v-model="notes"
        rows="2"
        :aria-label="$t('hitl.gate.review_notes')"
        data-testid="hitl-gate-notes"
        class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        :placeholder="$t('hitl.gate.review_notes')"
      />
      <div class="flex gap-2">
        <button
          type="button"
          :disabled="Boolean(actioning)"
          data-testid="hitl-gate-approve"
          class="flex-1 rounded-lg bg-success px-4 py-2 text-sm font-medium text-white hover:bg-success/90 disabled:opacity-50"
          @click="approveGate"
        >
          {{ actioning === 'approve' ? $t('hitl.gate.approving') : $t('hitl.gate.approve') }}
        </button>
        <button
          type="button"
          :disabled="Boolean(actioning)"
          data-testid="hitl-gate-reject"
          class="flex-1 rounded-lg bg-destructive px-4 py-2 text-sm font-medium text-destructive-foreground hover:bg-destructive/90 disabled:opacity-50"
          @click="rejectGate"
        >
          {{ actioning === 'reject' ? $t('hitl.gate.rejecting') : $t('hitl.gate.reject') }}
        </button>
      </div>
    </div>
    <!-- FAR-691: the server reports the gate claimed by ANOTHER account and
         this session's token was dropped as stale. Render the claimed-by-other
         state only (the claimed metadata row above already shows the holder):
         no approve/reject and no re-claim — the backend 409s foreign claims.
         role="status" announces the swap from interactive controls to
         read-only. -->
    <div
      v-else-if="status === 'claimed' && isForeignClaim"
      data-testid="hitl-gate-foreign-claim"
      role="status"
      class="pt-2 text-sm text-muted-foreground"
    >
      {{ $t('hitl.gate.claimed_by_other_note') }}
    </div>
    <!-- Token-recovery path (FAR-686): claimed but this component holds no
         token (e.g. the reviewer reloaded the page). Backend same-account
         re-claim re-issues a fresh token; a 409 means another reviewer. -->
    <div v-else-if="status === 'claimed' && !claimToken" class="space-y-2 pt-2">
      <Button :disabled="claiming" class="w-full" data-testid="hitl-gate-reclaim" @click="claimGate">
        {{ claiming ? $t('hitl.gate.claiming') : $t('hitl.gate.re_claim') }}
      </Button>
    </div>
    <div v-if="status === 'approved'" class="rounded-lg bg-success/10 p-3 text-sm text-success">
      {{ $t('hitl.gate.approved_banner') }}
    </div>
    <div v-if="status === 'rejected'" class="rounded-lg bg-destructive/10 p-3 text-sm text-destructive">
      {{ $t('hitl.gate.rejected_banner') }}
    </div>
    <div v-if="status === 'claimed' && claimToken" class="rounded-lg bg-muted p-3 text-xs">
      <p class="font-medium text-muted-foreground mb-1">{{ $t('hitl.gate.claim_token_label') }}</p>
      <code data-testid="hitl-gate-claim-token" class="break-all">{{ claimToken }}</code>
    </div>

    <!-- Feedback banner (component-internal; parents get the same payload
         via the decided/claimed emits so they can hoist it if the card may
         unmount). -->
    <div
      v-if="message"
      data-testid="hitl-gate-message"
      class="text-sm"
      :class="message.type === 'error' ? 'text-destructive' : 'text-success'"
    >
      {{ message.text }}
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, onBeforeUnmount } from 'vue'
import { useI18n } from 'vue-i18n'
import { api } from '../../lib/api/client'
import { formatApiError } from '../../lib/api/formatError'
import { shortId } from '../../utils/format'
import { formatDateShortWithTime } from '../../lib/formatDate'
import { useHitlGateState } from '../../composables/useHitlGateState'
import Button from 'primevue/button'
import HitlBriefing from '../HitlBriefing.vue'

export interface HitlGate {
  run_id: string
  gate_id: string
  pipeline_id: string
  pipeline_name?: string | null
  label?: string | null
  description?: string | null
  context?: Record<string, unknown> | null
  claimed_by?: string | null
  /** FAR-691: claimant's human-readable display name (server-resolved). */
  claimed_by_name?: string | null
  /**
   * FAR-691: server-side stamp — is the claimant the caller? Absent in older
   * cached payloads, in which case the component keeps the FAR-686 behaviour
   * (only treat the stamp as authoritative when it is explicitly false).
   */
  claimed_by_me?: boolean
  claimed_at?: string | null
  expires_at?: string | null
  decision?: string | null
  decision_at?: string | null
  created_at?: string
  team_scope?: string
}

interface HitlMessage {
  type: 'success' | 'error'
  text: string
}

const props = withDefaults(
  defineProps<{
    gate: HitlGate
    showRunLink?: boolean
  }>(),
  { showRunLink: false },
)

const emit = defineEmits<{
  (e: 'claimed', payload: HitlMessage): void
  (e: 'decided', payload: HitlMessage): void
  (e: 'claim-failed', payload: HitlMessage): void
}>()

const { t } = useI18n()

// Per-gate session state (FAR-686): the claim token and notes live in a
// module-scoped store keyed by run_id:gate_id so they survive the card
// unmounting — the review page's auto-refresh unmounts the whole list branch
// every 30s. A gate the server reports as pending has no live claim anymore,
// so any persisted token for it is stale and is dropped at setup; the card
// then offers a fresh claim instead of approving with a dead token.
const gateState = useHitlGateState(props.gate.run_id, props.gate.gate_id)
if (!props.gate.claimed_by) gateState.clear()

const claimToken = gateState.claimToken
const notes = gateState.notes
const claimedByYou = ref(false)
const claiming = ref(false)
const actioning = ref<'approve' | 'reject' | null>(null)
const message = ref<HitlMessage | null>(null)
let messageTimer: ReturnType<typeof setTimeout> | null = null

// FAR-691: the server stamps whether the claim belongs to the caller. When it
// reports a FOREIGN claim, any claim token this session still holds for the
// gate is provably stale (e.g. the local claim expired and another reviewer
// claimed): drop the token and render the claimed-by-other state — no
// approve/reject and no re-claim (the backend 409s foreign claims by design).
// The stamp is only authoritative when explicitly false: absent
// `claimed_by_me` (older cached payloads) keeps the FAR-686 behaviour.
const isForeignClaim = computed(() => props.gate.claimed_by != null && props.gate.claimed_by_me === false)
watch(
  isForeignClaim,
  (foreign) => {
    if (foreign && gateState.claimToken.value) {
      gateState.clear()
      claimedByYou.value = false
    }
  },
  { immediate: true },
)

const status = computed(() => {
  if (props.gate.decision === 'approved') return 'approved'
  if (props.gate.decision === 'rejected') return 'rejected'
  // An in-session claim token counts as claimed (FAR-686): the parent may not
  // re-fetch immediately after the claim resolves, so without this arm the
  // card would keep rendering the Claim button instead of approve/reject.
  if (props.gate.claimed_by || claimToken.value) return 'claimed'
  return 'pending'
})

const claimedByDisplay = computed(() => {
  if (claimedByYou.value || props.gate.claimed_by_me === true) return t('hitl.gate.claimed_by_you')
  // FAR-691: prefer the server-resolved display name; fall back to the raw
  // account UUID (e.g. the account row disappeared).
  return props.gate.claimed_by_name || props.gate.claimed_by || ''
})

const pipelineName = computed(() => props.gate.pipeline_name || '')

function statusBadgeClass(state: string): string {
  const classMap: Record<string, string> = {
    pending: 'badge badge-status-pending',
    claimed: 'badge badge-context-purple',
    approved: 'badge badge-status-success',
    rejected: 'badge badge-status-destructive',
  }
  return classMap[state] ?? 'badge badge-context-slate'
}

function formatDate(dateStr: string | null | undefined): string {
  if (!dateStr) return '-'
  const d = new Date(dateStr)
  if (Number.isNaN(d.getTime())) return '-'
  return formatDateShortWithTime(d)
}

async function copyText(text: string) {
  try {
    await navigator.clipboard.writeText(text)
  } catch (e) {
    console.warn('Failed to copy gate id', e)
  }
}

function showMessage(payload: HitlMessage, transient = true) {
  // One timer at a time: a stale timer from a previous success message must
  // not clear a newer one (e.g. claim banner followed seconds later by the
  // approve banner).
  if (messageTimer !== null) {
    clearTimeout(messageTimer)
    messageTimer = null
  }
  message.value = payload
  if (transient && payload.type === 'success') {
    messageTimer = setTimeout(() => { message.value = null; messageTimer = null }, 5000)
  }
}

onBeforeUnmount(() => {
  if (messageTimer !== null) clearTimeout(messageTimer)
})

function claimFailureMessage(err: unknown): string {
  // FAR-612: map the backend's claim-failure detail to a specific message so
  // the operator knows what actually happened (conflict shapes from the claim
  // endpoint: already claimed / already decided / run not awaiting).
  const detail = formatApiError(err)
  if (detail.includes('already claimed')) {
    return t('hitl.gate.claim_failed_already_claimed')
  }
  if (detail.includes('already has a decision')) {
    return t('hitl.gate.claim_failed_already_decided')
  }
  if (detail.includes('not awaiting a human decision')) {
    return t('hitl.gate.claim_failed_run_not_awaiting', { reason: detail })
  }
  return `${t('hitl.gate.claim_failed')} ${detail}`
}

async function claimGate() {
  claiming.value = true
  message.value = null
  try {
    const { data, error: err } = await api.POST('/api/v1/runs/{run_id}/hitl/{gate_id}/claim', {
      params: { path: { run_id: props.gate.run_id, gate_id: props.gate.gate_id } },
      body: { expiry_minutes: 15 },
    })
    if (err) {
      // FAR-612: emit too — parents may drop the card on their post-failure
      // refresh (terminal-run / decided gates leave the pending list), so the
      // message must survive at view level.
      const failure: HitlMessage = { type: 'error', text: claimFailureMessage(err) }
      showMessage(failure, false)
      emit('claim-failed', failure)
    } else if (data) {
      const d = data as { claim_token: string; expires_at: string }
      gateState.setClaimToken(d.claim_token)
      claimedByYou.value = true
      const payload: HitlMessage = { type: 'success', text: t('hitl.gate.gate_claimed_you_can_now_approve_or_reject') }
      showMessage(payload)
      emit('claimed', payload)
    }
  } catch (e: unknown) {
    // Network errors also emit: the claim may have landed before the
    // connection dropped, so the parent refreshes to converge on reality.
    const failure: HitlMessage = { type: 'error', text: claimFailureMessage(e) }
    showMessage(failure, false)
    emit('claim-failed', failure)
  } finally {
    claiming.value = false
  }
}

async function decideGate(decision: 'approve' | 'reject') {
  const token = claimToken.value
  if (!token) {
    showMessage({ type: 'error', text: t('hitl.gate.no_claim_token_claim_the_gate_first') }, false)
    return
  }
  actioning.value = decision
  message.value = null
  const reason = decision === 'reject' ? notes.value || t('hitl.gate.rejected_by_reviewer') : null
  const errorKey = decision === 'approve' ? 'hitl.gate.approve_failed' : 'hitl.gate.reject_failed'
  const successKey =
    decision === 'approve'
      ? 'hitl.gate.gate_approved_pipeline_resuming'
      : 'hitl.gate.gate_rejected_pipeline_routed_to_reject_target'
  try {
    const { error: err } = await api.POST(
      decision === 'approve'
        ? '/api/v1/runs/{run_id}/hitl/{gate_id}/approve'
        : '/api/v1/runs/{run_id}/hitl/{gate_id}/reject',
      {
        params: { path: { run_id: props.gate.run_id, gate_id: props.gate.gate_id } },
        body:
          decision === 'approve'
            ? { claim_token: token, notes: notes.value || null }
            : { claim_token: token, reason },
      },
    )
    if (err) {
      showMessage({ type: 'error', text: `${t(errorKey)} ${formatApiError(err)}` }, false)
    } else {
      // Decision done: drop the persisted token/notes for this gate so no
      // stale session state is left behind (FAR-686).
      gateState.clear()
      claimedByYou.value = false
      const payload: HitlMessage = { type: 'success', text: t(successKey) }
      showMessage(payload)
      emit('decided', payload)
    }
  } catch (e: unknown) {
    showMessage({ type: 'error', text: `${t(errorKey)} ${formatApiError(e)}` }, false)
  } finally {
    actioning.value = null
  }
}

async function approveGate() {
  await decideGate('approve')
}

async function rejectGate() {
  await decideGate('reject')
}
</script>
