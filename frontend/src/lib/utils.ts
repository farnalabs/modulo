import type { ClassValue } from "clsx"
import { clsx } from "clsx"
import { twMerge } from "tailwind-merge"

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

export function formatError(err: unknown): string {
  if (typeof err === 'string') return err
  if (err && typeof err === 'object') {
    const obj = err as Record<string, unknown>
    const msg = obj.message ?? obj.detail ?? obj.error ?? obj.title
    if (typeof msg === 'string') return msg
    try { return JSON.stringify(err) } catch { return String(err) }
  }
  return String(err)
}
