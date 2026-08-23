import { describe, it, expect, beforeEach, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { useRemyStore } from '../composables/useRemyStore'

describe('useRemyStore', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('starts with panel closed', () => {
    const store = useRemyStore()
    expect(store.panelState).toBe('docked')
  })

  it('starts with empty sessions', () => {
    const store = useRemyStore()
    expect(store.sessions).toEqual([])
  })

  it('starts with empty messages', () => {
    const store = useRemyStore()
    expect(store.messages).toEqual([])
  })

  it('starts with isStreaming false', () => {
    const store = useRemyStore()
    expect(store.isStreaming).toBe(false)
  })

  it('setPanelState updates state', () => {
    const store = useRemyStore()
    store.setPanelState('floating')
    expect(store.panelState).toBe('floating')
    store.setPanelState('docked')
    expect(store.panelState).toBe('docked')
    store.setPanelState('maximised')
    expect(store.panelState).toBe('maximised')
    store.setPanelState('closed')
    expect(store.panelState).toBe('closed')
  })

  it('appendToken creates new message when last is not assistant', () => {
    const store = useRemyStore()
    store.activeSessionId = 'session-1'
    store.appendToken('Hello')
    expect(store.messages).toHaveLength(1)
    expect(store.messages[0].role).toBe('assistant')
    expect(store.messages[0].content).toBe('Hello')
  })

  it('appendToken appends to existing assistant message', () => {
    const store = useRemyStore()
    store.activeSessionId = 'session-1'
    store.appendToken('Hello')
    store.appendToken(' World')
    expect(store.messages).toHaveLength(1)
    expect(store.messages[0].content).toBe('Hello World')
  })

  it('removeLastUserMessage removes last user message', () => {
    const store = useRemyStore()
    store.messages.push({
      id: '1', session_id: 's1', role: 'user',
      content: 'hi', tool_calls_json: null, tool_results_json: null,
      token_count: null, parent_id: null, created_at: new Date().toISOString(),
    })
    store.messages.push({
      id: '2', session_id: 's1', role: 'assistant',
      content: 'hello', tool_calls_json: null, tool_results_json: null,
      token_count: null, parent_id: null, created_at: new Date().toISOString(),
    })
    store.removeLastUserMessage()
    expect(store.messages).toHaveLength(2) // last is assistant, not removed
    store.messages.push({
      id: '3', session_id: 's1', role: 'user',
      content: 'bye', tool_calls_json: null, tool_results_json: null,
      token_count: null, parent_id: null, created_at: new Date().toISOString(),
    })
    store.removeLastUserMessage()
    expect(store.messages).toHaveLength(2) // last user removed
  })

  it('appendToolCall adds a tool_result message', () => {
    const store = useRemyStore()
    store.activeSessionId = 'session-1'
    store.appendToolCall({
      tool_call_id: 'tc-1', tool_name: 'test', success: true,
    })
    expect(store.messages).toHaveLength(1)
    expect(store.messages[0].role).toBe('tool_result')
    expect(store.messages[0].content).toContain('completed')
  })

  it('appendToolCall shows error for failed tool', () => {
    const store = useRemyStore()
    store.activeSessionId = 'session-1'
    store.appendToolCall({
      tool_call_id: 'tc-2', tool_name: 'test', success: false, error: 'timeout',
    })
    expect(store.messages[0].content).toContain('failed')
    expect(store.messages[0].content).toContain('timeout')
  })

  it('collapses floating panel to closed on narrow viewport', () => {
    const store = useRemyStore()
    store.setPanelState('floating')
    vi.stubGlobal('innerWidth', 390)
    try {
      store.collapseIfNarrow()
      expect(store.panelState).toBe('closed')
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('collapses docked and maximised panels to closed on narrow viewport', () => {
    const store = useRemyStore()
    vi.stubGlobal('innerWidth', 390)
    try {
      store.setPanelState('docked')
      store.collapseIfNarrow()
      expect(store.panelState).toBe('closed')
      store.setPanelState('maximised')
      store.collapseIfNarrow()
      expect(store.panelState).toBe('closed')
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('keeps panel state on wide viewport', () => {
    const store = useRemyStore()
    store.setPanelState('floating')
    vi.stubGlobal('innerWidth', 1280)
    try {
      store.collapseIfNarrow()
      expect(store.panelState).toBe('floating')
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('collapseIfNarrow never force-opens a closed panel', () => {
    const store = useRemyStore()
    store.setPanelState('closed')
    vi.stubGlobal('innerWidth', 390)
    try {
      store.collapseIfNarrow()
      expect(store.panelState).toBe('closed')
    } finally {
      vi.unstubAllGlobals()
    }
  })
})
