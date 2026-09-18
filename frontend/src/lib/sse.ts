export interface SSEEvent {
  event: string
  data: string
}

const MAX_BUFFER_SIZE = 1024 * 1024

/**
 * Incremental server-sent-events frame parser.
 *
 * Accumulates decoded bytes, splits them into lines, and emits complete
 * events. The trailing partial line is kept buffered between chunks; the
 * decoder is flushed once the stream is done so a final chunk ending
 * mid-multi-byte sequence isn't silently dropped.
 */
class SseFrameParser {
  private readonly decoder = new TextDecoder()
  private readonly maxBufferSize: number
  private buffer = ''
  private currentEvent = ''
  private readonly dataLines: string[] = []
  /** Set when the buffer cap is hit before a terminating blank line. */
  overflowed = false

  constructor(maxBufferSize: number) {
    this.maxBufferSize = maxBufferSize
  }

  private dispatchPending(): SSEEvent | null {
    if (this.dataLines.length === 0) return null
    const event: SSEEvent = { event: this.currentEvent, data: this.dataLines.join('\n') }
    this.currentEvent = ''
    this.dataLines.length = 0
    return event
  }

  private processLine(line: string): SSEEvent | null {
    if (line === '') {
      return this.dispatchPending()
    }
    if (line.startsWith(':')) {
      // comment / heartbeat — ignore
      return null
    }
    if (line.startsWith('event:')) {
      this.currentEvent = line.slice(6).trim()
      return null
    }
    if (line.startsWith('data:')) {
      // Per the SSE spec, a single leading space after the field name is stripped.
      this.dataLines.push(line[5] === ' ' ? line.slice(6) : line.slice(5))
    }
    // id:, retry:, and other fields are ignored.
    return null
  }

  private takeLines(done: boolean): string[] {
    const overflow = !done && this.buffer.length > this.maxBufferSize
    const lines = this.buffer.split('\n')
    const keepPartial = !done && !overflow
    this.buffer = keepPartial ? (lines.pop() ?? '') : ''
    this.overflowed = overflow
    return lines
  }

  /** Consume one read() result and return every event completed by it. */
  feed(value: Uint8Array | undefined, done: boolean): SSEEvent[] {
    if (done) {
      this.buffer += this.decoder.decode()
    } else {
      this.buffer += this.decoder.decode(value, { stream: true })
    }
    const lines = this.takeLines(done)
    const events: SSEEvent[] = []
    for (const rawLine of lines) {
      const line = rawLine.endsWith('\r') ? rawLine.slice(0, -1) : rawLine
      const pending = this.processLine(line)
      if (pending) events.push(pending)
    }
    if (done) {
      const pending = this.dispatchPending()
      if (pending) events.push(pending)
    }
    return events
  }
}

export async function* parseSSEStream(
  reader: ReadableStreamDefaultReader<Uint8Array>,
  maxBufferSize = MAX_BUFFER_SIZE,
): AsyncGenerator<SSEEvent> {
  const parser = new SseFrameParser(maxBufferSize)
  for (;;) {
    const { done, value } = await reader.read()
    for (const event of parser.feed(value, done)) {
      yield event
    }
    if (done) break
    // Safety cap: if the buffer limit is hit before a terminating blank line,
    // drop the accumulated partial event rather than growing the buffer forever.
    if (parser.overflowed) break
  }
}
