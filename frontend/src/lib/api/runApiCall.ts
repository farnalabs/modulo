import { formatApiError } from "./formatError";

/**
 * Runs an API call with a consistent loading/error lifecycle so the
 * try/catch + `formatApiError` boilerplate is not duplicated across views.
 *
 * `setLoading`/`setError` receive a value rather than a ref so callers can
 * route the loading flag to a plain boolean ref, a record entry, or any
 * other store without aliasing.
 */
export async function runApiCall<T>(opts: {
  setLoading: (v: boolean) => void;
  setError: (msg: string) => void;
  call: () => Promise<{ data?: T; error?: unknown }>;
  onSuccess: (data: T) => void | Promise<void>;
}): Promise<void> {
  opts.setLoading(true);
  try {
    const { data, error } = await opts.call();
    if (error) {
      opts.setError(formatApiError(error));
      return;
    }
    if (data !== undefined) await opts.onSuccess(data as T);
  } catch (e) {
    opts.setError(formatApiError(e));
  } finally {
    opts.setLoading(false);
  }
}
