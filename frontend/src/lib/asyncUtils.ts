import { formatApiError } from './api/formatError'

export async function withTimeout<T>(
  promise: Promise<T>,
  ms: number,
  label: string,
): Promise<T> {
  let timer: ReturnType<typeof setTimeout>;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new Error(`${label} timed out after ${ms}ms`)), ms);
  });
  promise.catch(() => {});
  timeout.catch(() => {});
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

export function asErrorMessage(e: unknown): string {
  return formatApiError(e);
}
