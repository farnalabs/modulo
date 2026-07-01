import type { ErrorEventInput, SessionKeyResponse } from './types'
import { getAccessToken, onAuthChange } from '../api/client'

let _sessionKey: string | null = null
let _keyPromise: Promise<string | null> | null = null
let _unsubAuth: (() => void) | null = null

interface PendingItem {
  event: ErrorEventInput
  retries: number
}

const PENDING: PendingItem[] = []
let flushTimer: ReturnType<typeof setTimeout> | null = null
const RATE_LIMIT_WINDOW_MS = 60_000
const RATE_LIMIT_MAX = 10
let requestTimestamps: number[] = []

export function initTransport(onAuthChangeFn: typeof onAuthChange): void {
  _unsubAuth = onAuthChangeFn(() => {
    _sessionKey = null
    _keyPromise = null
  })
}

export function disposeTransport(): void {
  if (flushTimer) {
    clearTimeout(flushTimer)
    flushTimer = null
  }
  if (_unsubAuth) {
    _unsubAuth()
    _unsubAuth = null
  }
  PENDING.length = 0
  requestTimestamps = []
}

function isDisabled(): boolean {
  return !!(window as unknown as Record<string, unknown>).__MODULO_ERROR_TRACKING_DISABLED__
}

async function getSessionKey(): Promise<string | null> {
  if (_sessionKey) return _sessionKey
  if (_keyPromise) return _keyPromise

  _keyPromise = fetchSessionKey()
  _sessionKey = await _keyPromise
  _keyPromise = null
  return _sessionKey
}

async function fetchSessionKey(): Promise<string | null> {
  if (isDisabled()) return null
  try {
    const token = getAccessToken()
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
    }
    if (token) headers['Authorization'] = `Bearer ${token}`

    const res = await fetch('/api/v1/errors/session-key', {
      method: 'POST',
      headers,
    })
    if (!res.ok) return null
    const data: SessionKeyResponse = await res.json()
    return data.session_key
  } catch {
    return null
  }
}

async function signPayload(payload: string, key: string): Promise<string> {
  try {
    const encoder = new TextEncoder()
    const cryptoKey = await crypto.subtle.importKey(
      'raw',
      encoder.encode(key),
      { name: 'HMAC', hash: 'SHA-256' },
      false,
      ['sign'],
    )
    const sig = await crypto.subtle.sign('HMAC', cryptoKey, encoder.encode(payload))
    return bytesToHex(new Uint8Array(sig))
  } catch {
    try {
      const encoder = new TextEncoder()
      const data = encoder.encode(payload + key)
      const hash = await crypto.subtle.digest('SHA-256', data)
      return bytesToHex(new Uint8Array(hash))
    } catch {
      return ''
    }
  }
}

function bytesToHex(bytes: Uint8Array): string {
  return Array.from(bytes).map((b) => b.toString(16).padStart(2, '0')).join('')
}

function isRateLimited(): boolean {
  const now = Date.now()
  requestTimestamps = requestTimestamps.filter((t) => now - t < RATE_LIMIT_WINDOW_MS)
  if (requestTimestamps.length >= RATE_LIMIT_MAX) return true
  requestTimestamps.push(now)
  return false
}

export function enqueueError(event: ErrorEventInput): void {
  if (isDisabled()) return
  PENDING.push({ event, retries: 0 })
  if (PENDING.length >= 10) {
    void flush()
  } else if (!flushTimer) {
    flushTimer = setTimeout(() => {
      flushTimer = null
      void flush()
    }, 5000)
  }
}

export async function flush(): Promise<void> {
  if (isDisabled() || PENDING.length === 0) return
  if (isRateLimited()) return

  const batch = PENDING.splice(0)
  const body = JSON.stringify({ events: batch.map((b) => b.event) })

  try {
    const sessionKey = await getSessionKey()
    if (!sessionKey) {
      // Session key not available, re-queue with retry
      reQueueWithBackoff(batch)
      return
    }

    const token = getAccessToken()
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
    }
    if (token) headers['Authorization'] = `Bearer ${token}`

    const signature = await signPayload(body, sessionKey)
    if (signature) {
      headers['X-Modulo-Error-Token'] = signature
    }

    const res = await fetch('/api/v1/errors/ingest', {
      method: 'POST',
      headers,
      body,
    })

    if (!res.ok && res.status >= 500) {
      reQueueWithBackoff(batch)
    } else if (!res.ok) {
      // 4xx — drop, likely a configuration issue
    }
  } catch {
    reQueueWithBackoff(batch)
  }
}

const BACKOFF_DELAYS = [1000, 5000, 30_000]

function reQueueWithBackoff(items: PendingItem[]): void {
  for (const item of items) {
    if (item.retries < 3) {
      item.retries++
      const delay = BACKOFF_DELAYS[Math.min(item.retries - 1, BACKOFF_DELAYS.length - 1)]
      setTimeout(() => {
        PENDING.push(item)
        if (!flushTimer) {
          flushTimer = setTimeout(() => {
            flushTimer = null
            void flush()
          }, delay)
        }
      }, delay)
    }
    // Dropped after max retries
  }
}

export function getPendingCount(): number {
  return PENDING.length
}
