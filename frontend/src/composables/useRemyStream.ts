import { getCurrentInstance, ref, onUnmounted } from 'vue'
import { useRemyStore } from './useRemyStore'
import type { PermissionRequest } from './useRemyStore'
import { usePlanStore } from '../stores/planStore'
import type { PageContext, ChatSession } from '@/types/remy'
import { getAuthHeaders } from '@/lib/api/client'
import { parseSSEStream } from '@/lib/sse'
import type { UiCommand, UiCommandResult } from './useUiCommandExecutor'
import { executeCommandBatch, isPaused as isExecutorPaused } from './useUiCommandExecutor'

export interface ToolCallEvent {
  tool_call_id: string
  tool_name: string
  success: boolean
  result?: unknown
  error?: string
}

export interface StreamOptions {
  excludeUiTools?: boolean
}

const FETCH_TIMEOUT_MS = 30000

type RemyStoreInstance = ReturnType<typeof useRemyStore>

/** What the SSE loop should do after handling one event. */
type StreamEventOutcome = 'proceed' | 'break'

interface RemyEventContext {
  store: RemyStoreInstance
  sessionId: string
  headers: Record<string, string>
  controller: AbortController
  options: StreamOptions
}

function buildPageContext(pageCtx: PageContext): string | undefined {
  if (!pageCtx.route) return undefined
  let ctx = `Page: ${pageCtx.route}`
  if (pageCtx.params.id) ctx += ` / ${pageCtx.params.id}`
  if (pageCtx.entities.length) ctx += `\nEntities: ${pageCtx.entities.join(', ')}`
  return ctx
}

function buildStreamStartBody(
  content: string,
  session: ChatSession,
  headers: Record<string, string>,
  options: StreamOptions,
  pageCtx: PageContext,
): Record<string, unknown> {
  return {
    content,
    provider: session.provider,
    model: session.model,
    context_window_tokens: session.context_window_tokens,
    api_key: '',
    mcp_api_key: headers.Authorization?.replace('Bearer ', '') || '',
    page_context: options.excludeUiTools ? undefined : buildPageContext(pageCtx),
    exclude_ui_tools: options.excludeUiTools || undefined,
  }
}

function resolveStreamErrorMessage(parsed: unknown): string {
  const payload = parsed as { detail?: unknown; message?: unknown }
  return (payload.detail ?? payload.message ?? 'Stream error') as string
}

function resolveTurnSeparatorLabel(parsed: unknown): string {
  return ((parsed as { label?: unknown }).label ?? '---') as string
}

function resolveAbortSummaryText(parsed: unknown): string {
  return ((parsed as { summary?: unknown }).summary ?? 'Action cancelled by user.') as string
}

/** Hold-off window after UI command execution so pause/abort intent is honoured
 * before the results are reported back to the server. */
async function waitForExecutorResume(streamSignal: AbortSignal | null | undefined): Promise<void> {
  const pauseDeadline = Date.now() + 60000
  while (isExecutorPaused()) {
    if (Date.now() > pauseDeadline) break
    if (streamSignal?.aborted) break
    await new Promise(r => setTimeout(r, 200))
  }
}

async function submitUiCommandResults(ctx: RemyEventContext, results: UiCommandResult[]): Promise<void> {
  const { store, sessionId, headers } = ctx
  const body = JSON.stringify({ results })
  const maxRetries = 3
  for (let retries = 0; retries < maxRetries; retries++) {
    const resp = await fetch(`/api/v1/remy/sessions/${sessionId}/ui-command-results`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...headers,
      },
      body,
    })
    if (resp.ok) break
    if (retries >= maxRetries - 1) {
      store.error = `Failed to submit UI command results (${resp.status})`
    } else {
      await new Promise(r => setTimeout(r, 500 * (retries + 1)))
    }
  }
}

async function handleUiCommandBatch(ctx: RemyEventContext, parsed: unknown): Promise<StreamEventOutcome> {
  const { store, sessionId, headers, controller, options } = ctx
  const planStore = usePlanStore()
  // Belt-and-braces: remy-only mode never executes UI command batches,
  // and never submits them even if the server (or a text-mode backend
  // path) somehow emits one.
  if (options.excludeUiTools || !planStore.featureEnabled('remy_ui_driving')) {
    console.warn('[RemyStream] UI driving disabled — skipping command batch')
    const body = JSON.stringify({ results: [] })
    await fetch(`/api/v1/remy/sessions/${sessionId}/ui-command-results`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...headers },
      body,
    })
    return 'proceed'
  }
  const commands = ((parsed as { commands?: UiCommand[] }).commands ?? parsed) as UiCommand[]
  store.isExecutingUi = true
  store.isPaused = false
  try {
    const results = await executeCommandBatch(commands)
    store.isExecutingUi = false
    await waitForExecutorResume(controller?.signal)
    await submitUiCommandResults(ctx, results)
    return 'proceed'
  } catch (e) {
    store.error = e instanceof Error ? e.message : 'UI command execution failed'
    store.isExecutingUi = false
    return 'break'
  }
}

async function dispatchParsedEvent(
  ctx: RemyEventContext,
  event: string,
  parsed: unknown,
): Promise<StreamEventOutcome> {
  const { store } = ctx
  if (event === 'token' && (parsed as { token?: unknown }).token) {
    store.appendToken((parsed as { token: string }).token)
  } else if (event === 'error') {
    store.error = resolveStreamErrorMessage(parsed)
    return 'break'
  } else if (event === 'done') {
    return 'break'
  } else if (event === 'tool_call') {
    store.appendToolCall(parsed as ToolCallEvent)
  } else if (event === 'permission_request') {
    store.setPendingPermission(parsed as PermissionRequest)
  } else if (event === 'ui_command_batch') {
    return await handleUiCommandBatch(ctx, parsed)
  } else if (event === 'turn_separator') {
    store.appendTurnSeparator(resolveTurnSeparatorLabel(parsed))
  } else if (event === 'abort_summary') {
    store.appendSystemMessage(resolveAbortSummaryText(parsed))
    return 'break'
  }
  // ping — keepalive, ignore
  return 'proceed'
}

async function handleRemySseEvent(
  ctx: RemyEventContext,
  event: string,
  data: string,
): Promise<StreamEventOutcome> {
  try {
    const parsed = JSON.parse(data)
    return await dispatchParsedEvent(ctx, event, parsed)
  } catch {
    if (event === 'token' && data.trim()) {
      ctx.store.appendToken(data)
    }
    return 'proceed'
  }
}

async function consumeRemyStream(
  ctx: RemyEventContext,
  reader: ReadableStreamDefaultReader<Uint8Array>,
): Promise<void> {
  for await (const { event, data } of parseSSEStream(reader)) {
    const outcome = await handleRemySseEvent(ctx, event, data)
    if (outcome === 'break') break
  }
}

function rejectStreamResponse(store: RemyStoreInstance, response: Response): void {
  const errorDetail = response.status === 403 ? 'Access denied. Contact your admin.' : (response.statusText || 'Stream connection failed')
  store.error = errorDetail
  store.removeLastUserMessage()
}

export function useRemyStream() {
  const store = useRemyStore()
  const connected = ref(false)
  let abortController: AbortController | null = null
  let streamId = 0
  let activeStream: Promise<void> | null = null

  function finalizeIfCurrent(seq: number): void {
    if (seq === streamId) {
      store.isStreaming = false
      connected.value = false
    }
  }

  async function connectStream(sessionId: string, options: StreamOptions = {}) {
    if (connected.value && store.activeSessionId === sessionId) return
    await disconnectStream()
    const seq = ++streamId
    const controller = new AbortController()
    abortController = controller
    const timeoutId = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS)
    connected.value = true
    store.isStreaming = true

    const run = runStream(sessionId, options, seq, controller, timeoutId)
    activeStream = run
    return run
  }

  async function runStream(
    sessionId: string,
    options: StreamOptions,
    seq: number,
    controller: AbortController,
    timeoutId: ReturnType<typeof setTimeout>,
  ): Promise<void> {
    const session = store.sessions.find(s => s.id === sessionId)
    if (!session) {
      store.error = 'Session not found'
      clearTimeout(timeoutId)
      finalizeIfCurrent(seq)
      return
    }

    const lastMsg = store.messages[store.messages.length - 1]
    if (!lastMsg?.content) {
      store.removeLastUserMessage()
      clearTimeout(timeoutId)
      finalizeIfCurrent(seq)
      return
    }

    const headers = getAuthHeaders()
    let reader: ReadableStreamDefaultReader<Uint8Array> | null = null

    try {
      const response = await fetch(`/api/v1/remy/sessions/${sessionId}/stream`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...headers,
        },
        body: JSON.stringify(
          buildStreamStartBody(lastMsg.content, session, headers, options, store.pageContext),
        ),
        signal: controller.signal,
      })
      clearTimeout(timeoutId)

      if (!response.ok || !response.body) {
        rejectStreamResponse(store, response)
        return
      }

      reader = response.body.getReader()
      await consumeRemyStream({ store, sessionId, headers, controller, options }, reader)
    } catch (e: unknown) {
      clearTimeout(timeoutId)
      if (e instanceof Error && e.name === 'AbortError') return
      store.error = e instanceof Error ? e.message : 'Stream disconnected'
      store.removeLastUserMessage()
    } finally {
      reader?.cancel().catch(() => {})
      finalizeIfCurrent(seq)
    }
  }

  async function disconnectStream() {
    const pendingId = streamId
    if (abortController) {
      abortController.abort()
      abortController = null
    }
    const pending = activeStream
    activeStream = null
    if (pending) {
      try {
        await pending
      } catch (e) {
        // runStream handles its own errors
        console.warn('[RemyStream] disconnect error while awaiting stream', e)
      }
    }
    if (streamId === pendingId) {
      connected.value = false
      store.isStreaming = false
    }
  }

  if (getCurrentInstance()) {
    onUnmounted(() => {
      disconnectStream()
    })
  }

  return { connected, connectStream, disconnectStream }
}
