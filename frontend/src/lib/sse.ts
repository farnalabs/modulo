export interface SSEEvent {
  event: string
  data: string
}

const MAX_BUFFER_SIZE = 1024 * 1024

export async function* parseSSEStream(
  reader: ReadableStreamDefaultReader<Uint8Array>,
  maxBufferSize = MAX_BUFFER_SIZE,
): AsyncGenerator<SSEEvent> {
  const decoder = new TextDecoder()
  let buffer = ''
  let currentEvent = ''
  const dataLines: string[] = []

  function dispatchPending(): SSEEvent | null {
    if (dataLines.length === 0) return null
    const event: SSEEvent = { event: currentEvent, data: dataLines.join('\n') }
    currentEvent = ''
    dataLines.length = 0
    return event
  }

  function processLine(line: string): SSEEvent | null {
    if (line === '') {
      return dispatchPending()
    }
    if (line.startsWith(':')) {
      // comment / heartbeat — ignore
      return null
    }
    if (line.startsWith('event:')) {
      currentEvent = line.slice(6).trim()
      return null
    }
    if (line.startsWith('data:')) {
      // Per the SSE spec, a single leading space after the field name is stripped.
      dataLines.push(line[5] === ' ' ? line.slice(6) : line.slice(5))
    }
    // id:, retry:, and other fields are ignored.
    return null
  }

  for (;;) {
    const { done, value } = await reader.read()
    if (!done) {
      buffer += decoder.decode(value, { stream: true })
    } else {
      // Flush any trailing bytes so a final chunk ending mid-multi-byte
      // sequence isn't silently dropped.
      buffer += decoder.decode()
    }
    const overflow = !done && buffer.length > maxBufferSize

    const lines = buffer.split('\n')
    const keepPartial = !done && !overflow
    buffer = keepPartial ? (lines.pop() ?? '') : ''

    for (const rawLine of lines) {
      const line = rawLine.endsWith('\r') ? rawLine.slice(0, -1) : rawLine
      const pending = processLine(line)
      if (pending) yield pending
    }

    if (done) {
      const pending = dispatchPending()
      if (pending) yield pending
      break
    }
    // Safety cap: if the buffer limit is hit before a terminating blank line,
    // drop the accumulated partial event rather than growing the buffer forever.
    if (overflow) break
  }
}
