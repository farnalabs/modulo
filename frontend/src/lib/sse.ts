export interface SSEEvent {
  event: string
  data: string
}

export async function* parseSSEStream(
  reader: ReadableStreamDefaultReader<Uint8Array>,
  maxBufferSize = 1024 * 1024,
): AsyncGenerator<SSEEvent> {
  const decoder = new TextDecoder()
  let buffer = ''
  let currentEvent = ''

  for (;;) {
    const { done, value } = await reader.read()
    if (done) break

    buffer += decoder.decode(value, { stream: true })
    if (buffer.length > maxBufferSize) break

    const lines = buffer.split('\n')
    buffer = lines.pop() ?? ''

    for (const line of lines) {
      if (line.startsWith('event: ')) {
        currentEvent = line.slice(7).trim()
      } else if (line.startsWith('data: ')) {
        yield { event: currentEvent, data: line.slice(6) }
        currentEvent = ''
      }
    }
  }
}
