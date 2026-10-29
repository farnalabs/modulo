import { getCurrentInstance, ref, onUnmounted } from 'vue'
import { useAssistantStore } from './useAssistantStore'
import type { PageContext, ChatSession } from '@/types/assistant'
import { getAuthHeaders } from '@/lib/api/client'
import { parseSSEStream } from '@/lib/sse'

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

type AssistantStoreInstance = ReturnType<typeof useAssistantStore>

/** What the SSE loop should do after handling one event. */
type StreamEventOutcome = 'proceed' | 'break'

interface AssistantEventContext {
  store: AssistantStoreInstance
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

async function dispatchParsedEvent(
  ctx: AssistantEventContext,
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
    store.error = 'Unsupported assistant event: permission_request'
    return 'break'
  } else if (event === 'ui_command_batch') {
    ctx.store.error = 'Unsupported assistant event: ui_command_batch'
    return 'break'
  } else if (event === 'turn_separator') {
    store.appendTurnSeparator(resolveTurnSeparatorLabel(parsed))
  } else if (event === 'abort_summary') {
    store.appendSystemMessage(resolveAbortSummaryText(parsed))
    return 'break'
  }
  // ping — keepalive, ignore
  return 'proceed'
}

async function handleAssistantSseEvent(
  ctx: AssistantEventContext,
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

async function consumeAssistantStream(
  ctx: AssistantEventContext,
  reader: ReadableStreamDefaultReader<Uint8Array>,
): Promise<void> {
  for await (const { event, data } of parseSSEStream(reader)) {
    const outcome = await handleAssistantSseEvent(ctx, event, data)
    if (outcome === 'break') break
  }
}

function rejectStreamResponse(store: AssistantStoreInstance, response: Response): void {
  const errorDetail = response.status === 403 ? 'Access denied. Contact your admin.' : (response.statusText || 'Stream connection failed')
  store.error = errorDetail
  store.removeLastUserMessage()
}

export function useAssistantStream() {
  const store = useAssistantStore()
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
      const response = await fetch(`/api/v1/assistant/sessions/${sessionId}/stream`, {
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
      await consumeAssistantStream({ store }, reader)
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
        console.warn('[AssistantStream] disconnect error while awaiting stream', e)
      }
    }
    if (streamId === pendingId) {
      connected.value = false
      store.isStreaming = false
    }
  }

  if (getCurrentInstance()) {
    onUnmounted(() => {
      void disconnectStream()
    })
  }

  return { connected, connectStream, disconnectStream }
}
