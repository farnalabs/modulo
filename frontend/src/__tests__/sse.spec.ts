import { describe, it, expect } from 'vitest'
import { parseSSEStream } from '../lib/sse'

const encoder = new TextEncoder()

function readerFromChunks(chunks: string[]): ReadableStreamDefaultReader<Uint8Array> {
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) {
        controller.enqueue(encoder.encode(chunk))
      }
      controller.close()
    },
  })
  return stream.getReader()
}

async function collect(chunks: string[], maxBufferSize?: number) {
  const events: Array<{ event: string; data: string }> = []
  for await (const event of parseSSEStream(readerFromChunks(chunks), maxBufferSize)) {
    events.push(event)
  }
  return events
}

describe('parseSSEStream', () => {
  it('parses a single event', async () => {
    const events = await collect(['event: resource_changed\ndata: {"a":1}\n\n'])
    expect(events).toEqual([{ event: 'resource_changed', data: '{"a":1}' }])
  })

  it('parses multiple events in a single chunk', async () => {
    const events = await collect([
      'event: token\ndata: {"token":"hello"}\n\nevent: token\ndata: {"token":" world"}\n\n',
    ])
    expect(events).toEqual([
      { event: 'token', data: '{"token":"hello"}' },
      { event: 'token', data: '{"token":" world"}' },
    ])
  })

  it('handles a single data line without a leading space', async () => {
    const events = await collect(['event: ping\ndata:keepalive\n\n'])
    expect(events).toEqual([{ event: 'ping', data: 'keepalive' }])
  })

  it('joins multi-line data fields into one event with newlines', async () => {
    const events = await collect([
      'event: message\ndata: line one\ndata: line two\n\n',
    ])
    expect(events).toEqual([{ event: 'message', data: 'line one\nline two' }])
  })

  it('ignores comment lines (heartbeats)', async () => {
    const events = await collect([': heartbeat\n: still here\nevent: done\ndata: {}\n\n'])
    expect(events).toEqual([{ event: 'done', data: '{}' }])
  })

  it('ignores unsupported fields such as id and retry', async () => {
    const events = await collect(['id: 1\nretry: 1000\nevent: done\ndata: {}\n\n'])
    expect(events).toEqual([{ event: 'done', data: '{}' }])
  })

  it('handles lines split across multiple chunks', async () => {
    const events = await collect([
      'event: token\n',
      'data: {"to',
      'ken":"x"}\n\n',
      'event: done\n',
      'data: {}\n\n',
    ])
    expect(events).toEqual([
      { event: 'token', data: '{"token":"x"}' },
      { event: 'done', data: '{}' },
    ])
  })

  it('supports CRLF line endings', async () => {
    const events = await collect(['event: done\r\ndata: {}\r\n\r\n'])
    expect(events).toEqual([{ event: 'done', data: '{}' }])
  })

  it('dispatches a pending event when the stream ends without a blank line', async () => {
    const events = await collect(['event: done\ndata: {}'])
    expect(events).toEqual([{ event: 'done', data: '{}' }])
  })

  it('dispatches a pending event when the stream ends with a partial data line', async () => {
    const events = await collect(['event: done\ndata: {"to'])
    expect(events).toEqual([{ event: 'done', data: '{"to' }])
  })

  it('parses complete lines and stops when the buffer limit is reached', async () => {
    const events = await collect(['event: done\ndata: {}\n'], 16)
    expect(events).toEqual([{ event: 'done', data: '{}' }])
  })

  it('yields no events for an empty stream', async () => {
    const events = await collect([''])
    expect(events).toEqual([])
  })
})
