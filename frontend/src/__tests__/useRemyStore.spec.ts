import { describe, it, expect, beforeEach } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { useRemyStore } from '../composables/useRemyStore'

describe('useRemyStore', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('starts with panel closed', () => {
    const store = useRemyStore()
    expect(store.panelState).toBe('closed')
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
    expect(store.messages.length).toBe(1)
    expect(store.messages[0].role).toBe('assistant')
    expect(store.messages[0].content).toBe('Hello')
  })

  it('appendToken appends to existing assistant message', () => {
    const store = useRemyStore()
    store.activeSessionId = 'session-1'
    store.appendToken('Hello')
    store.appendToken(' World')
    expect(store.messages.length).toBe(1)
    expect(store.messages[0].content).toBe('Hello World')
  })

  it('removeLastUserMessage removes last user message', () => {
    const store = useRemyStore()
    store.messages.push({
      id: '1', session_id: 's1', role: 'user',
      content: 'hi', token_count: null, created_at: new Date().toISOString(),
    })
    store.messages.push({
      id: '2', session_id: 's1', role: 'assistant',
      content: 'hello', token_count: null, created_at: new Date().toISOString(),
    })
    store.removeLastUserMessage()
    expect(store.messages.length).toBe(2) // last is assistant, not removed
    store.messages.push({
      id: '3', session_id: 's1', role: 'user',
      content: 'bye', token_count: null, created_at: new Date().toISOString(),
    })
    store.removeLastUserMessage()
    expect(store.messages.length).toBe(2) // last user removed
  })

  it('appendToolCall adds a tool_result message', () => {
    const store = useRemyStore()
    store.activeSessionId = 'session-1'
    store.appendToolCall({
      tool_call_id: 'tc-1', tool_name: 'test', success: true,
    })
    expect(store.messages.length).toBe(1)
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
})
